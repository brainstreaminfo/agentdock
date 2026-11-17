"""
Merchant chat (spec §8.4) + approvals from chat cards + run trace page.

Flow of one message:
  POST send  -> saves the user message, creates a QUEUED Run, starts the Celery
               task, returns the user bubble + an assistant bubble connected to
  GET stream -> Server-Sent Events: streamed text, tool chips, then "done" with
               the final rendered answer (charts, tables, approval cards).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from asgiref.sync import sync_to_async
from django import forms
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import Membership
from apps.agents import events
from apps.agents.models import AgentInstallation
from apps.agents.orchestrator import TEAM_NAME, team_members, team_version
from apps.agents.runtime import create_run
from apps.agents.tasks import execute_run_task
from apps.core.audit import audit
from apps.core.decorators import require_shop_access
from apps.runs.models import ActionProposal, Run
from apps.tools.errors import ToolError
from apps.tools.executor import apply_proposal, reject_proposal, undo_proposal

from .models import Conversation
from .rendering import build_items

STREAM_MAX_SECONDS = 10 * 60


class MessageForm(forms.Form):
    message = forms.CharField(max_length=8000, strip=True)


def _chat_installations(shop) -> list[AgentInstallation]:  # type: ignore[no-untyped-def]
    return team_members(shop)


def _merchant_conversations(shop, user):  # type: ignore[no-untyped-def]
    """The user's chats in this shop. Studio playground chats (linked to a version) are hidden.
    `installation=None` means a My Team chat."""
    return Conversation.objects.for_shop(shop).filter(user=user, version__isnull=True)


@require_shop_access()
def chat_home(
    request: HttpRequest, shop_id: int, conversation_id: int | None = None
) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    conversations = _merchant_conversations(shop, request.user).select_related(
        "installation__agent"
    )[:30]
    conversation = None
    turns = []
    if conversation_id is not None:
        conversation = get_object_or_404(
            _merchant_conversations(shop, request.user), pk=conversation_id
        )
        for run in conversation.runs.order_by("id"):
            turns.append(
                {
                    "run": run,
                    "items": build_items(run),
                    "live": run.status in (Run.Status.QUEUED, Run.Status.RUNNING),
                }
            )
    return render(
        request,
        "chat/chat.html",
        {
            "installations": _chat_installations(shop),
            "conversations": conversations,
            "conversation": conversation,
            "turns": turns,
            "form": MessageForm(),
        },
    )


@require_shop_access()
@require_POST
def start_conversation(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    if request.POST.get("installation") == "team":
        if not team_members(shop):
            raise Http404("No chat agents installed")
        installation, name = None, TEAM_NAME
    else:
        installation = get_object_or_404(
            AgentInstallation, pk=request.POST.get("installation"), shop=shop, enabled=True
        )
        name = installation.agent.name
    conversation = Conversation.objects.create(
        shop=shop, user_id=request.user.pk, installation=installation, title=f"Chat with {name}"
    )
    return redirect("chat:conversation", shop_id=shop.pk, conversation_id=conversation.pk)


@require_shop_access()
@require_POST
def send_message(request: HttpRequest, shop_id: int, conversation_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    conversation = get_object_or_404(
        _merchant_conversations(shop, request.user), pk=conversation_id
    )
    installation = conversation.installation
    if installation is not None and not installation.enabled:
        return HttpResponse(
            '<p class="text-sm text-amber-600">This agent is paused. Resume it in My agents.</p>',
            status=409,
        )
    if conversation.runs.filter(status__in=[Run.Status.QUEUED, Run.Status.RUNNING]).exists():
        return HttpResponse(
            '<p class="text-sm text-amber-600">Please wait for the current answer to finish.</p>',
            status=409,
        )
    form = MessageForm(request.POST)
    if not form.is_valid():
        return HttpResponse('<p class="text-sm text-red-600">Type a message first.</p>', status=400)

    text = form.cleaned_data["message"]
    if conversation.title.startswith("Chat with ") and not conversation.runs.exists():
        conversation.title = text[:80]
        conversation.save(update_fields=["title", "updated_at"])
    run = create_run(
        shop=shop,
        # My Team (no installation) runs the orchestrator; see apps/agents/orchestrator.py.
        version=installation.version if installation else team_version(),
        installation=installation,
        conversation=conversation,
        trigger=Run.Trigger.CHAT,
        input={"message": text},
    )
    execute_run_task.delay(run.pk)
    run.refresh_from_db()
    return render(
        request, "chat/_turn.html", {"turn": {"run": run, "items": build_items(run), "live": True}}
    )


# --------------------------------------------------------------------------- SSE
async def stream_run(
    request: HttpRequest, shop_id: int, run_id: int
) -> HttpResponse | StreamingHttpResponse:
    """Server-Sent Events for one run. Replays from the start (or Last-Event-ID)."""
    user = await request.auser()
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    has_access = await Membership.objects.filter(
        user=user, shop_id=shop_id, shop__uninstalled_at__isnull=True
    ).aexists()
    run_exists = await Run.objects.filter(pk=run_id, shop_id=shop_id).aexists()
    if not (has_access and run_exists):
        raise Http404()

    last_id = request.headers.get("Last-Event-ID") or "0"

    async def event_stream() -> AsyncIterator[str]:
        nonlocal last_id
        started = time.monotonic()
        yield "retry: 3000\n\n"
        while time.monotonic() - started < STREAM_MAX_SECONDS:
            batch = await events.backend().aread(run_id, last_id, block_ms=15000)
            if not batch:
                # Nothing new: check the run isn't already finished (events expired), then ping.
                status = await sync_to_async(
                    lambda: Run.objects.filter(pk=run_id).values_list("status", flat=True).first()
                )()
                if status not in (Run.Status.QUEUED, Run.Status.RUNNING):
                    yield 'event: done\ndata: {"status": "finished"}\n\n'
                    return
                yield ": keep-alive\n\n"
                if events.backend().__class__.__name__ == "MemoryEvents":
                    await asyncio.sleep(0.05)
                continue
            for event in batch:
                last_id = event.id
                yield event.to_sse()
                if event.name in events.FINAL_EVENTS:
                    return

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # don't let proxies buffer the stream
    return response


# --------------------------------------------------------------------------- approvals
def _proposal_card(request: HttpRequest, proposal: ActionProposal, error: str = "") -> HttpResponse:
    proposal.refresh_from_db()
    return render(request, "chat/_proposal_card.html", {"proposal": proposal, "error": error})


@require_shop_access(role="ADMIN")
@require_POST
def decide_proposal(
    request: HttpRequest, shop_id: int, proposal_uuid: str, action: str
) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    proposal = get_object_or_404(ActionProposal, uuid=proposal_uuid, shop=shop)
    error = ""
    try:
        if action == "approve":
            proposal = apply_proposal(proposal, user=request.user)
            if proposal.status == ActionProposal.Status.FAILED:
                error = proposal.error
        elif action == "reject":
            proposal = reject_proposal(proposal, user=request.user)
        elif action == "undo":
            proposal = undo_proposal(proposal, user=request.user)
        else:
            raise Http404()
    except ToolError as exc:
        error = exc.message
    audit(
        f"proposal.{action}",
        shop=shop,
        user=request.user,
        tool=proposal.tool,
        proposal=str(proposal.uuid),
        status=proposal.status,
    )
    return _proposal_card(request, proposal, error)


# --------------------------------------------------------------------------- trace
@require_shop_access()
def run_trace(request: HttpRequest, shop_id: int, run_id: int) -> HttpResponse:
    run = get_object_or_404(Run, pk=run_id, shop=request.shop)  # type: ignore[attr-defined]
    duration = (
        f"{(run.finished_at - run.started_at).total_seconds():.1f} s"
        if run.started_at and run.finished_at
        else "—"
    )
    return render(
        request,
        "runs/trace.html",
        {
            "run": run,
            "live": run.status in (Run.Status.QUEUED, Run.Status.RUNNING),
            "children": run.child_runs.order_by("id"),
            # Changes are shown here for runs without a chat (schedule, event, Run now).
            "proposals": (
                ActionProposal.objects.filter(run=run).select_related("snapshot")
                if not run.conversation_id
                else []
            ),
            "steps": run.steps.order_by("idx"),
            "show_traceback": request.user.is_staff,
            "tokens": f"{run.tokens_in:,} / {run.tokens_out:,}",
            "duration": duration,
        },
    )
