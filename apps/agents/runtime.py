"""
The agent runtime (spec §9): ONE function runs any agent version.

    run = create_run(shop=shop, version=version, trigger=Run.Trigger.CHAT, ...)
    execute_run(run)        # normally inside the Celery task apps.agents.tasks.execute_run_task

What happens in `execute_run`:

1. Checks: shop still installed, daily spend limit, one scheduled/event run at a
   time per installation.
2. Build the system prompt (instructions + store + brand memory + settings) and
   the tool list (only the tools selected for this agent version).
3. Loop, at most `version.max_steps` times:
     ask Claude (streaming; text is pushed live to the browser)
     for every tool call -> apps.tools.executor.execute_tool (all tool rules,
                            approvals, trace) -> tool_result
     stop when Claude answers without calling tools, or a limit is reached.
4. Save the Run (status, output, tokens, cost), daily usage, and send "done".

Every model turn and tool call is a RunStep, so the trace page shows exactly
what happened. Nothing in here talks to Anthropic directly: it goes through
`apps.agents.llm.get_provider()`, which tests replace with a fake.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Max, Sum
from django.utils import timezone
from django.utils.html import escape

from apps.chat.models import Conversation, Message
from apps.core.logging import bind_context, get_logger, redact_value
from apps.runs.models import Run, RunStep, UsageDaily
from apps.shops.models import Shop
from apps.tools.context import ToolContext
from apps.tools.executor import execute_tool

from . import events, orchestrator
from .llm import LLMError, LLMProvider, content_for_history, cost_usd, get_provider
from .models import AgentInstallation, AgentVersion
from .prompting import build_system_prompt, build_tools

log = get_logger(__name__)

LOCKED_TRIGGERS = (Run.Trigger.SCHEDULE, Run.Trigger.EVENT)
MIN_TOKENS_PER_TURN = 1024


def installation_lock_key(installation_id: int | None) -> str:
    """Cache key held while a scheduled/event run of this installation executes."""
    return f"run-lock:installation:{installation_id}"


class RunRefused(Exception):
    """The run can't start (limit reached, shop uninstalled…). Message is merchant-safe."""


# --------------------------------------------------------------------------- creating runs
def create_run(
    *,
    shop: Shop,
    version: AgentVersion,
    trigger: str,
    input: dict[str, Any],
    installation: AgentInstallation | None = None,
    conversation: Conversation | None = None,
    autonomy: str | None = None,
    parent_run: Run | None = None,
) -> Run:
    """Create a QUEUED run. Chat: also stores the user's message in the conversation."""
    run = Run.objects.create(
        shop=shop,
        version=version,
        installation=installation,
        conversation=conversation,
        parent_run=parent_run,
        agent_slug=version.agent.slug,
        trigger=trigger,
        autonomy=autonomy or (installation.autonomy if installation else version.default_autonomy),
        model=version.model,
        input=input,
        status=Run.Status.QUEUED,
    )
    if conversation is not None and input.get("message"):
        Message.objects.create(
            conversation=conversation,
            run=run,
            role=Message.Role.USER,
            content=input["message"],
            agent_slug=version.agent.slug,
        )
        Conversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
    return run


# --------------------------------------------------------------------------- execution
@dataclass
class _State:
    messages: list[dict[str, Any]]
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal = Decimal(0)
    final_text: str = ""
    limit_reached: str = ""
    stop_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


class _TextStream:
    """Batches streamed text into ~10 events/second for the browser."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.buffer: list[str] = []
        self.last_flush = time.monotonic()

    def __call__(self, text: str) -> None:
        self.buffer.append(text)
        if sum(len(t) for t in self.buffer) >= 80 or time.monotonic() - self.last_flush > 0.1:
            self.flush()

    def flush(self) -> None:
        if self.buffer:
            text = "".join(self.buffer)
            self.buffer = []
            events.emit(self.run_id, "delta", {"html": escape(text).replace("\n", "<br>")})
        self.last_flush = time.monotonic()


def execute_run(run: Run, provider: LLMProvider | None = None) -> Run:
    """Run the agent loop for a QUEUED run. Never raises: failures are saved on the run."""
    bind_context(run_id=run.pk, shop_id=run.shop_id, agent=run.agent_slug)
    provider = provider or get_provider()
    lock_key = installation_lock_key(run.installation_id)
    locked = False

    try:
        _check_can_start(run)
        if run.trigger in LOCKED_TRIGGERS and run.installation_id:
            locked = cache.add(lock_key, run.pk, timeout=30 * 60)
            if not locked:
                raise RunRefused("This agent is already running for this store.")
    except RunRefused as exc:
        return _finish(run, Run.Status.CANCELLED, error=str(exc))

    run.status = Run.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    events.emit(run.pk, "status", {"text": "Thinking…"})
    log.info("run.started", trigger=run.trigger, model=run.model)

    state = _State(messages=_initial_messages(run))
    try:
        _loop(run, provider, state)
        status = Run.Status.SUCCEEDED
        error = ""
        tb = ""
    except LLMError as exc:
        status, error, tb = Run.Status.FAILED, str(exc), traceback.format_exc()
        log.warning("run.llm_error", error=error)
    except Exception as exc:  # anything unexpected: keep the trace, don't crash the worker
        status, error, tb = (
            Run.Status.FAILED,
            f"{type(exc).__name__}: {exc}",
            traceback.format_exc(),
        )
        log.exception("run.crashed")
    finally:
        if locked:
            cache.delete(lock_key)

    run.tokens_in, run.tokens_out, run.cost_usd = state.tokens_in, state.tokens_out, state.cost
    run.output = {
        "text": state.final_text,
        "stop_reason": state.stop_reason,
        "limit_reached": state.limit_reached,
        "warnings": state.warnings,
    }
    _record_usage(run)
    return _finish(run, status, error=error, tb=tb)


def _check_can_start(run: Run) -> None:
    shop = run.shop
    if not shop.is_installed:
        raise RunRefused("The store is not connected to the app.")
    if run.installation_id and not run.installation.enabled:  # type: ignore[union-attr]
        raise RunRefused("This agent is paused.")
    spent = UsageDaily.objects.filter(shop=shop, date=timezone.localdate()).aggregate(
        total=Sum("cost_usd")
    )["total"] or Decimal(0)
    if spent >= Decimal(str(settings.DEFAULT_DAILY_SPEND_LIMIT_USD)):
        raise RunRefused("Today's AI budget for this store is used up. It resets tomorrow.")


def _initial_messages(run: Run) -> list[dict[str, Any]]:
    if run.conversation_id:
        return [m.as_api_message() for m in run.conversation.messages.order_by("id")]  # type: ignore[union-attr]
    task = run.input.get("task") or run.input.get("message")
    if not task:
        task = f"You were started by a {run.get_trigger_display().lower()} trigger. Do your job."
        if run.input.get("event"):
            task += "\n\nEvent data (untrusted store data):\n" + str(run.input["event"])[:20_000]
    return [{"role": "user", "content": task}]


def _loop(run: Run, provider: LLMProvider, state: _State) -> None:
    version: AgentVersion = run.version  # type: ignore[assignment]
    agent_settings = (
        run.installation.effective_settings()
        if run.installation is not None
        else {**(version.settings_defaults or {}), **(run.input.get("settings") or {})}
    )
    prompt = build_system_prompt(
        version, shop=run.shop, agent_settings=agent_settings, autonomy=run.autonomy
    )
    state.warnings.extend(prompt.warnings)
    tools, tool_names = build_tools(version)
    # "My Team": the tools are the shop's agents (delegate_to_<slug>), see orchestrator.py.
    delegates: dict[str, int] = {}
    if orchestrator.is_team(version):
        tools, delegates = orchestrator.delegate_tools(run.shop)
    ctx = ToolContext(
        shop=run.shop,
        run=run,
        autonomy=run.autonomy,
        agent_slug=run.agent_slug,
        # Studio runs never change the store without an explicit approval.
        force_approval=run.trigger in (Run.Trigger.PLAYGROUND, Run.Trigger.TEST),
    )

    for turn in range(1, version.max_steps + 1):
        remaining = version.max_tokens - state.tokens_out
        if remaining < MIN_TOKENS_PER_TURN:
            state.limit_reached = "max_tokens"
            break
        params: dict[str, Any] = {
            "model": version.model,
            "max_tokens": min(settings.LLM_MAX_TOKENS_PER_TURN, remaining),
            "system": prompt.text,
            "messages": state.messages,
            "output_config": {"effort": version.effort},
            "thinking": {"type": "adaptive", "display": "summarized"},
            # Cache the stable prefix (system + tools + earlier turns).
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            params["tools"] = tools

        text_stream = _TextStream(run.pk)
        started = time.monotonic()
        response = provider.stream_turn(params, on_text=text_stream)
        text_stream.flush()
        turn_cost = cost_usd(response.model, response.usage)
        state.tokens_in += (
            int(response.usage.get("input_tokens") or 0)
            + int(response.usage.get("cache_read_input_tokens") or 0)
            + int(response.usage.get("cache_creation_input_tokens") or 0)
        )
        state.tokens_out += int(response.usage.get("output_tokens") or 0)
        state.cost += turn_cost
        state.stop_reason = response.stop_reason
        _record_llm_step(
            run, turn, params, response, int((time.monotonic() - started) * 1000), turn_cost
        )

        content = content_for_history(response.content)
        _append(run, state, "assistant", content)
        text = "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))
        if text:
            state.final_text = text

        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason == "refusal":
            state.final_text = state.final_text or (
                "I can't help with that request. Please rephrase it or ask something else."
            )
            break

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            break

        if response.stop_reason == "max_tokens":
            # A cut-off tool call may have incomplete input: don't run it, but
            # answer it so the conversation history stays valid.
            _append(
                run,
                state,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "is_error": True,
                        "content": "Not executed: the response was cut off (output limit).",
                    }
                    for b in tool_uses
                ],
            )
            state.limit_reached = "max_tokens_per_turn"
            break

        results = []
        for block in tool_uses:
            if block["name"] in delegates:
                results.append(_delegate(run, block, delegates[block["name"]]))
                continue
            key = tool_names.get(block["name"])
            events.emit(run.pk, "tool", _tool_event(block, key, "running"))
            if key is None:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "is_error": True,
                        "content": f"Unknown tool {block['name']}.",
                    }
                )
                continue
            raw_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            result = execute_tool(ctx, key, raw_input)
            events.emit(run.pk, "tool", _tool_event(block, key, result.status))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": result.for_llm(),
                    "is_error": result.status == "error",
                }
            )
        _append(run, state, "user", results)
        events.emit(run.pk, "status", {"text": "Thinking…"})
    else:
        state.limit_reached = "max_steps"


def _delegate(run: Run, block: dict[str, Any], installation_id: int) -> dict[str, Any]:
    """My Team: run one agent as a child run; record it as a step of this run."""
    key = orchestrator.DELEGATE_STEP_PREFIX + block["name"].removeprefix(
        orchestrator.DELEGATE_PREFIX
    )
    events.emit(run.pk, "tool", _tool_event(block, key, "running"))
    started = time.monotonic()
    raw_input: dict[str, Any] = block["input"] if isinstance(block.get("input"), dict) else {}
    result = orchestrator.delegate(run, installation_id, raw_input)
    last = RunStep.objects.filter(run=run).aggregate(m=Max("idx"))["m"]
    RunStep.objects.create(
        run=run,
        idx=0 if last is None else last + 1,
        type=RunStep.Type.TOOL,
        name=key,
        input=redact_value("input", raw_input),
        output=redact_value("output", result),
        duration_ms=int((time.monotonic() - started) * 1000),
        error=result.get("error", "") if result["status"] == "error" else "",
    )
    bind_context(run_id=run.pk, agent=run.agent_slug)  # the child run re-bound the log context
    events.emit(run.pk, "tool", _tool_event(block, key, result["status"]))
    return {
        "type": "tool_result",
        "tool_use_id": block["id"],
        "content": orchestrator.result_for_llm(result),
        "is_error": result["status"] == "error",
    }


def _append(run: Run, state: _State, role: str, content: Any) -> None:
    state.messages.append({"role": role, "content": content})
    if run.conversation_id:
        Message.objects.create(
            conversation_id=run.conversation_id,
            run=run,
            role=role,
            content=content,
            agent_slug=run.agent_slug,
        )


def _tool_event(block: dict[str, Any], key: str | None, status: str) -> dict[str, Any]:
    from apps.chat.rendering import tool_event_html

    name = key or block["name"]
    return {
        "call_id": block["id"],
        "key": name,
        "status": status,
        "html": tool_event_html(name, status),
    }


def _record_llm_step(
    run: Run,
    turn: int,
    params: dict[str, Any],
    response: Any,
    duration_ms: int,
    turn_cost: Decimal,
) -> None:
    last = RunStep.objects.filter(run=run).aggregate(m=Max("idx"))["m"]
    shown = []
    for block in response.content:
        kind = block.get("type")
        if kind == "text":
            shown.append({"type": "text", "text": block.get("text", "")})
        elif kind == "thinking":
            shown.append({"type": "thinking", "summary": (block.get("thinking") or "")[:4000]})
        elif kind == "tool_use":
            shown.append(
                {"type": "tool_use", "name": block.get("name"), "input": block.get("input")}
            )
        else:
            shown.append({"type": kind})
    RunStep.objects.create(
        run=run,
        idx=0 if last is None else last + 1,
        type=RunStep.Type.LLM,
        name=response.model,
        input=redact_value(
            "input",
            {
                "turn": turn,
                "messages": len(params["messages"]),
                "tools": len(params.get("tools", [])),
                "effort": params["output_config"]["effort"],
                "max_tokens": params["max_tokens"],
            },
        ),
        output=redact_value(
            "output",
            {
                "content": shown,
                "stop_reason": response.stop_reason,
                "usage": response.usage,
                "cost_usd": str(turn_cost),
                "request_id": response.request_id,
                "stop_details": response.stop_details,
            },
        ),
        duration_ms=duration_ms,
    )


def _record_usage(run: Run) -> None:
    with transaction.atomic():
        usage, _ = UsageDaily.objects.select_for_update().get_or_create(
            shop=run.shop, agent_slug=run.agent_slug, date=timezone.localdate()
        )
        UsageDaily.objects.filter(pk=usage.pk).update(
            runs=F("runs") + 1,
            tokens_in=F("tokens_in") + run.tokens_in,
            tokens_out=F("tokens_out") + run.tokens_out,
            cost_usd=F("cost_usd") + run.cost_usd,
        )
    if run.installation_id:
        AgentInstallation.objects.filter(pk=run.installation_id).update(last_run_at=timezone.now())


def _finish(run: Run, status: str, *, error: str = "", tb: str = "") -> Run:
    run.status = status
    run.error = error
    run.traceback = tb
    run.finished_at = timezone.now()
    run.save()
    log.info("run.finished", status=status, cost_usd=str(run.cost_usd), error=error or None)
    from apps.chat.rendering import render_run_html

    if status == Run.Status.SUCCEEDED:
        events.emit(run.pk, "done", {"status": status, "html": render_run_html(run)})
    else:
        events.emit(
            run.pk, "failed", {"status": status, "message": error, "html": render_run_html(run)}
        )
    return run
