"""
Merchant pages for runs and approvals (spec §8.5).

    /s/<shop>/runs/                 every agent run (filter by agent, status, trigger)
    /s/<shop>/approvals/            changes waiting for approval (+ bulk approve)
    /s/<shop>/approvals/history/    applied / rejected / undone changes, with Undo

One approval card is `templates/chat/_proposal_card.html`; its buttons post to
`chat:decide` (apps/chat/views.decide_proposal), the same as in chat. The run
trace page is `chat:trace`.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.decorators import require_shop_access
from apps.tools.errors import ToolError
from apps.tools.executor import apply_proposal, reject_proposal

from .models import ActionProposal, Run

# Staff-only runs (Studio playground, tests, tool tests) are not merchant history.
STUDIO_TRIGGERS = [Run.Trigger.PLAYGROUND, Run.Trigger.TEST, Run.Trigger.TOOL_TEST]
MAX_BULK = 50


def merchant_runs(shop):  # type: ignore[no-untyped-def]
    return Run.objects.for_shop(shop).exclude(trigger__in=STUDIO_TRIGGERS)


@require_shop_access()
def run_list(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    runs = merchant_runs(shop).select_related("version")
    filters = {
        "agent": request.GET.get("agent", ""),
        "status": request.GET.get("status", ""),
        "trigger": request.GET.get("trigger", ""),
    }
    if filters["agent"]:
        runs = runs.filter(agent_slug=filters["agent"])
    if filters["status"] in Run.Status.values:
        runs = runs.filter(status=filters["status"])
    if filters["trigger"] in Run.Trigger.values:
        runs = runs.filter(trigger=filters["trigger"])
    page = Paginator(runs.order_by("-created_at"), 30).get_page(request.GET.get("page"))
    agents = sorted(set(merchant_runs(shop).values_list("agent_slug", flat=True)) - {""})
    triggers = [(v, label) for v, label in Run.Trigger.choices if v not in STUDIO_TRIGGERS]
    return render(
        request,
        "runs/list.html",
        {
            "page": page,
            "filters": filters,
            "agents": agents,
            "statuses": Run.Status.choices,
            "triggers": triggers,
        },
    )


def _proposals(shop):  # type: ignore[no-untyped-def]
    return ActionProposal.objects.for_shop(shop).select_related("snapshot", "run", "decided_by")


@require_shop_access()
def approvals(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    pending = _proposals(shop).filter(status=ActionProposal.Status.PENDING).order_by("created_at")
    return render(
        request,
        "runs/approvals.html",
        {
            "proposals": pending[:100],
            "count": pending.count(),
            "tab": "pending",
            "can_approve": request.membership.has_role("ADMIN"),  # type: ignore[attr-defined]
        },
    )


@require_shop_access()
def approval_history(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    done = _proposals(shop).exclude(status=ActionProposal.Status.PENDING).order_by("-updated_at")
    page = Paginator(done, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "runs/approvals.html",
        {"page": page, "proposals": page.object_list, "tab": "history"},
    )


@require_shop_access(role="ADMIN")
@require_POST
def bulk_decide(request: HttpRequest, shop_id: int) -> HttpResponse:
    """Approve or reject the ticked proposals. Each one is applied separately."""
    shop = request.shop  # type: ignore[attr-defined]
    action = request.POST.get("action")
    uuids = request.POST.getlist("proposal")[:MAX_BULK]
    if action not in ("approve", "reject") or not uuids:
        messages.error(request, "Tick at least one change first.")
        return redirect("runs:approvals", shop_id=shop_id)
    done, failed = 0, []
    for proposal in ActionProposal.objects.for_shop(shop).filter(
        uuid__in=uuids, status=ActionProposal.Status.PENDING
    ):
        try:
            if action == "approve":
                proposal = apply_proposal(proposal, user=request.user)
                if proposal.status == ActionProposal.Status.FAILED:
                    failed.append(
                        f"{proposal.preview.get('summary') or proposal.tool}: {proposal.error}"
                    )
                    continue
            else:
                reject_proposal(proposal, user=request.user)
        except ToolError as exc:
            failed.append(exc.message)
            continue
        audit(
            f"proposal.{action}",
            shop=shop,
            user=request.user,
            tool=proposal.tool,
            proposal=str(proposal.uuid),
            bulk=True,
        )
        done += 1
    verb = "Applied" if action == "approve" else "Rejected"
    if done:
        messages.success(request, f"{verb} {done} change{'s' if done != 1 else ''}.")
    for error in failed[:5]:
        messages.error(request, error)
    return redirect("runs:approvals", shop_id=shop_id)
