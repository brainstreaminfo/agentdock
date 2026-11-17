"""
Turn a run's transcript into what the merchant sees in chat.

One chat turn = one Run. Its assistant side is built from the run's Message
rows: text blocks become Markdown, tool calls become small chips, and some tool
results become widgets: charts and tables (ui.*), reply drafts, and approval
cards for changes waiting for the merchant.

All model text is rendered as Markdown and then SANITIZED (nh3), because it can
contain text copied from store data.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import markdown as md
import nh3
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

from apps.runs.models import ActionProposal, Run
from apps.tools.untrusted import UNTRUSTED_NOTICE

ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "hr",
}


def render_markdown(text: str) -> SafeString:
    html = md.markdown(text or "", extensions=["fenced_code", "tables", "sane_lists", "nl2br"])
    clean = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )
    return mark_safe(clean)  # noqa: S308 (sanitized by nh3 above)


@lru_cache(maxsize=256)
def tool_label(key: str) -> str:
    from apps.tools.models import ToolDefinition
    from apps.tools.registry import registry

    if key.startswith("delegate:"):  # My Team delegation (apps/agents/orchestrator.py)
        from apps.agents.models import AgentDefinition

        agent = AgentDefinition.objects.filter(slug=key.removeprefix("delegate:")).first()
        return f"Asked {agent.name if agent else key.removeprefix('delegate:')}"
    spec = registry.get(key)
    if spec is not None:
        return spec.name
    definition = ToolDefinition.objects.filter(key=key).only("name").first()
    return definition.name if definition else key


def tool_event_html(key: str, status: str) -> str:
    return render_to_string(
        "chat/_tool_chip.html", {"label": tool_label(key), "key": key, "status": status}
    )


def parse_tool_result(content: Any) -> dict[str, Any]:
    """Our tool_result content is the executor's framed JSON; return it as a dict."""
    if isinstance(content, list):  # list of text blocks
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    text = str(content or "")
    if text.startswith(UNTRUSTED_NOTICE):
        text = text[len(UNTRUSTED_NOTICE) :].strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"status": "ok", "data": parsed}
    except json.JSONDecodeError:
        return {"status": "error", "error": text[:500]}


def build_items(run: Run) -> list[dict[str, Any]]:
    """Display items for the assistant side of one chat turn."""
    items: list[dict[str, Any]] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    # Also the changes proposed by agents My Team delegated to (child runs).
    proposals = {
        str(p.uuid): p for p in ActionProposal.objects.filter(Q(run=run) | Q(run__parent_run=run))
    }

    for message in run.messages.order_by("id"):
        if message.role == "assistant" and isinstance(message.content, list):
            for block in message.content:
                kind = block.get("type")
                if kind == "text" and block.get("text", "").strip():
                    items.append({"kind": "text", "html": render_markdown(block["text"])})
                elif kind == "tool_use":
                    key = _key_from_api_name(block.get("name", ""))
                    tool_calls[block.get("id", "")] = {"key": key}
        elif message.role == "user" and isinstance(message.content, list):
            for block in message.content:
                if block.get("type") != "tool_result":
                    continue
                call = tool_calls.get(block.get("tool_use_id", ""), {"key": "tool"})
                result = parse_tool_result(block.get("content"))
                items.extend(_result_items(call["key"], result, proposals))
    if run.status == Run.Status.FAILED and run.error:
        items.append({"kind": "error", "message": run.error})
    elif run.status == Run.Status.CANCELLED and run.error:
        items.append({"kind": "notice", "message": run.error})
    if (run.output or {}).get("limit_reached"):
        items.append({"kind": "notice", "message": _limit_message(run.output["limit_reached"])})
    return items


def _result_items(
    key: str, result: dict[str, Any], proposals: dict[str, ActionProposal]
) -> list[dict[str, Any]]:
    status = result.get("status", "ok")
    items: list[dict[str, Any]] = [
        {
            "kind": "tool",
            "label": tool_label(key),
            "key": key,
            "status": status,
            "error": result.get("error", ""),
        }
    ]
    data = result.get("data") or {}
    if key.startswith("delegate:") and isinstance(data, dict):
        for uuid in data.get("proposals") or []:
            if str(uuid) in proposals:
                items.append({"kind": "proposal", "proposal": proposals[str(uuid)]})
        return items
    if status == "pending_approval":
        proposal = proposals.get(str(result.get("proposal_id")))
        if proposal is not None:
            items.append({"kind": "proposal", "proposal": proposal})
    elif status == "ok" and isinstance(data, dict):
        render = data.get("render")
        if render == "chart":
            items.append(
                {
                    "kind": "chart",
                    "title": data.get("title", ""),
                    "config": json.dumps(_chart_config(data)),
                }
            )
        elif render == "table":
            items.append(
                {
                    "kind": "table",
                    "title": data.get("title", ""),
                    "columns": data.get("columns", []),
                    "rows": data.get("rows", []),
                }
            )
        elif render == "reply_draft":
            items.append({"kind": "draft", "draft": data})
    return items


def _key_from_api_name(name: str) -> str:
    """API tool name -> our key: products__search -> products.search,
    delegate_to_sales-analyst -> delegate:sales-analyst."""
    from apps.agents.orchestrator import DELEGATE_PREFIX, DELEGATE_STEP_PREFIX

    if name.startswith(DELEGATE_PREFIX):
        return DELEGATE_STEP_PREFIX + name.removeprefix(DELEGATE_PREFIX)
    return name.replace("__", ".")


def _chart_config(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": data.get("type", "bar"),
        "data": {
            "labels": data.get("labels", []),
            "datasets": [
                {"label": d.get("label"), "data": d.get("data", [])}
                for d in data.get("datasets", [])
            ],
        },
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "plugins": {"legend": {"display": len(data.get("datasets", [])) > 1}},
        },
    }


def _limit_message(limit: str) -> str:
    return {
        "max_steps": "I stopped because I reached my step limit for one request. "
        "Ask me to continue if needed.",
        "max_tokens": "I stopped because I reached my length limit for one request.",
        "max_tokens_per_turn": "My answer was cut off because it was too long.",
    }.get(limit, f"Stopped: {limit}")


def render_run_html(run: Run) -> str:
    """The final assistant bubble for a chat turn (sent in the SSE 'done' event)."""
    if not run.conversation_id:
        return ""
    return render_to_string("chat/_assistant_turn.html", {"run": run, "items": build_items(run)})
