"""
The tool executor: the ONE place where every tool call goes through (spec §6.3).

    result = execute_tool(ctx, "products.update", {"product_id": "...", "title": "..."})
    result.status       "ok" | "error" | "pending_approval"
    result.for_llm()    text to send back to the AI as the tool_result

Rules, in order:
1. Tool exists and is enabled.
2. The shop granted every scope the tool needs. If not: clear error for the AI
   plus an alert asking the merchant to grant access.
3. Input is validated (Pydantic for code tools, JSON Schema for custom tools).
4. A mutating tool creates an ActionProposal and returns "pending approval",
   unless the agent's autonomy is AUTO (then it's applied right away, with a
   snapshot so it can still be undone).
5. GraphQL cost / THROTTLED back-off happens inside the GraphQL client.
6. Results are sanitized and framed as untrusted data. Emails/phones are redacted
   in logs and in the stored trace.
7. Every call is recorded as a RunStep (input, output, duration, error).

Nothing in here raises to the caller: every problem becomes a ToolResult with
status "error", so the AI can read it and the run continues.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from pydantic import ValidationError

from apps.core.logging import get_logger, redact_value
from apps.dashboard.models import Alert, create_alert
from apps.runs.models import ActionProposal, ActionSnapshot, RunStep
from apps.shopify_app.graphql import ShopifyGraphQLError
from apps.shopify_app.oauth import OAuthError
from apps.shopify_app.tokens import ShopNotInstalled

from .context import ToolContext
from .errors import MissingScopesError, ToolError
from .resolver import ResolvedTool, resolve_tool
from .untrusted import frame_for_llm, sanitize

log = get_logger(__name__)


@dataclass
class ToolResult:
    status: str  # "ok" | "error" | "pending_approval"
    data: Any = None
    error: str = ""
    proposal_id: str | None = None
    duration_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.data is not None:
            out["data"] = self.data
        if self.error:
            out["error"] = self.error
        if self.proposal_id:
            out["proposal_id"] = self.proposal_id
        return out

    def for_llm(self) -> str:
        return frame_for_llm(self.to_dict())


# --------------------------------------------------------------------------- entry point
def execute_tool(ctx: ToolContext, key: str, raw_args: dict[str, Any] | None) -> ToolResult:
    started = time.monotonic()
    raw_args = raw_args or {}
    ctx.tool_key = key
    tb = ""
    try:
        result = _execute(ctx, key, raw_args)
    except Exception as exc:  # last-resort safety net: never crash the agent loop
        tb = traceback.format_exc()
        log.exception("tool.crashed", tool=key, shop_id=ctx.shop.pk)
        result = ToolResult("error", error=f"Internal error in tool {key}: {type(exc).__name__}")
    result.duration_ms = int((time.monotonic() - started) * 1000)
    _record_step(ctx, key, raw_args, result, tb)
    log.info(
        "tool.executed",
        tool=key,
        status=result.status,
        duration_ms=result.duration_ms,
        shop_id=ctx.shop.pk,
        run_id=getattr(ctx.run, "pk", None),
    )
    return result


def _execute(ctx: ToolContext, key: str, raw_args: dict[str, Any]) -> ToolResult:
    # 1. exists + enabled
    tool = resolve_tool(key)
    if tool is None:
        return ToolResult("error", error=f"Unknown or disabled tool: {key}")

    # 2. scopes
    missing = ctx.shop.missing_scopes(tool.scopes)
    if missing:
        _alert_missing_scopes(ctx, tool, missing)
        return ToolResult("error", error=MissingScopesError(missing).message)

    # 3. validate input
    try:
        args = tool.validate(raw_args)
    except ValidationError as exc:
        return ToolResult("error", error="Invalid input: " + _pydantic_errors(exc))
    except ToolError as exc:
        return ToolResult("error", error=exc.message)

    # 4. mutating -> approval or auto-apply
    if tool.mutating:
        return _handle_mutation(ctx, tool, args)

    return _run_guarded(ctx, lambda: tool.run(ctx, args))


# --------------------------------------------------------------------------- mutations
def _needs_approval(ctx: ToolContext) -> bool:
    return ctx.force_approval or ctx.autonomy != "AUTO"


def _handle_mutation(ctx: ToolContext, tool: ResolvedTool, args: Any) -> ToolResult:
    preview_result = _run_guarded(ctx, lambda: tool.preview(ctx, args))
    if not preview_result.ok:
        return preview_result

    proposal = ActionProposal.objects.create(
        shop=ctx.shop,
        run=ctx.run,
        tool=tool.key,
        agent_slug=ctx.agent_slug,
        payload=tool.payload(args),
        preview=sanitize(preview_result.data or {}),
    )

    if _needs_approval(ctx):
        return ToolResult(
            "pending_approval",
            data={
                "message": "This change is waiting for the merchant's approval. "
                "It has NOT been applied yet. Tell the merchant what you proposed.",
                "preview": proposal.preview,
            },
            proposal_id=str(proposal.uuid),
        )

    applied = apply_proposal(proposal, ctx=ctx, auto=True)
    if applied.status == ActionProposal.Status.APPLIED:
        return ToolResult("ok", data=applied.result, proposal_id=str(proposal.uuid))
    return ToolResult("error", error=applied.error, proposal_id=str(proposal.uuid))


def apply_proposal(
    proposal: ActionProposal,
    *,
    ctx: ToolContext | None = None,
    user: Any = None,
    auto: bool = False,
) -> ActionProposal:
    """
    Apply a PENDING proposal: snapshot the current data, run the tool, save the
    snapshot. Used for merchant approvals (Phase 5 UI) and for AUTO mode.
    Safe against double-clicks: the row is locked and must still be PENDING.
    """
    from django.utils import timezone

    with transaction.atomic():
        locked = ActionProposal.objects.select_for_update().get(pk=proposal.pk)
        if locked.status != ActionProposal.Status.PENDING:
            return locked
        tool = resolve_tool(locked.tool)
        ctx = ctx or ToolContext(shop=locked.shop, run=locked.run, autonomy="AUTO")
        ctx.tool_key = locked.tool
        ctx.idempotency_key = str(locked.uuid)
        locked.decided_by = user if getattr(user, "is_authenticated", False) else None
        locked.decided_at = timezone.now()
        locked.auto_applied = auto

        if tool is None:
            locked.status = ActionProposal.Status.FAILED
            locked.error = f"Tool {locked.tool} no longer exists or is disabled."
            locked.save()
            return locked

        def perform() -> dict[str, Any]:
            args = tool.validate(locked.payload)
            before = tool.snapshot(ctx, args) or {}
            after = tool.run(ctx, args) or {}
            ActionSnapshot.objects.update_or_create(
                proposal=locked, defaults={"before": before, "after": after}
            )
            return after

        outcome = _run_guarded(ctx, perform)
        if outcome.ok:
            locked.status = ActionProposal.Status.APPLIED
            locked.result = outcome.data or {}
            locked.error = ""
        else:
            locked.status = ActionProposal.Status.FAILED
            locked.error = outcome.error
        locked.save()
    return locked


def undo_proposal(proposal: ActionProposal, *, user: Any = None) -> ActionProposal:
    """Revert an APPLIED proposal using its snapshot (if the tool supports undo)."""
    from django.utils import timezone

    with transaction.atomic():
        locked = ActionProposal.objects.select_for_update().get(pk=proposal.pk)
        if locked.status != ActionProposal.Status.APPLIED:
            raise ToolError(f"Only applied actions can be undone (this one is {locked.status}).")
        tool = resolve_tool(locked.tool)
        if tool is None or not tool.can_undo:
            raise ToolError("This kind of change can't be undone automatically.")
        snapshot = ActionSnapshot.objects.filter(proposal=locked).first()
        if snapshot is None:
            raise ToolError("No snapshot was saved for this change, so it can't be undone.")
        ctx = ToolContext(shop=locked.shop, run=locked.run, autonomy="AUTO", tool_key=locked.tool)
        ctx.idempotency_key = f"{locked.uuid}-undo"
        outcome = _run_guarded(
            ctx,
            lambda: tool.undo(ctx, tool.validate(locked.payload), snapshot.before, snapshot.after),
        )
        if not outcome.ok:
            raise ToolError(f"Undo failed: {outcome.error}")
        locked.status = ActionProposal.Status.UNDONE
        locked.undone_by = user if getattr(user, "is_authenticated", False) else None
        locked.undone_at = timezone.now()
        locked.save()
    return locked


def reject_proposal(proposal: ActionProposal, *, user: Any = None) -> ActionProposal:
    from django.utils import timezone

    updated = ActionProposal.objects.filter(
        pk=proposal.pk, status=ActionProposal.Status.PENDING
    ).update(
        status=ActionProposal.Status.REJECTED,
        decided_by=user if getattr(user, "is_authenticated", False) else None,
        decided_at=timezone.now(),
    )
    proposal.refresh_from_db()
    if not updated and proposal.status != ActionProposal.Status.REJECTED:
        raise ToolError(f"Only pending actions can be rejected (this one is {proposal.status}).")
    return proposal


# --------------------------------------------------------------------------- helpers
def _run_guarded(ctx: ToolContext, fn: Any) -> ToolResult:
    """Run tool code and convert known failures into an error ToolResult."""
    try:
        return ToolResult("ok", data=sanitize(fn()))
    except MissingScopesError as exc:
        _alert_missing_scopes(ctx, None, exc.scopes)
        return ToolResult("error", error=exc.message)
    except ToolError as exc:
        return ToolResult("error", error=exc.message)
    except ValidationError as exc:
        return ToolResult("error", error="Invalid input: " + _pydantic_errors(exc))
    except ShopifyGraphQLError as exc:
        return ToolResult("error", error=f"Shopify API error: {exc}")
    except (ShopNotInstalled, OAuthError):
        return ToolResult(
            "error",
            error="The store's connection to the app has expired. The merchant must "
            "open the app from Shopify Admin to reconnect.",
        )


def _pydantic_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)


def _alert_missing_scopes(ctx: ToolContext, tool: ResolvedTool | None, missing: list[str]) -> None:
    from apps.shopify_app.scopes import describe

    url = (
        reverse("shopify:auth")
        + "?"
        + urlencode({"shop": ctx.shop.domain, "scopes": ",".join(missing)})
    )
    what = f"the {tool.name} tool" if tool else "an agent"
    create_alert(
        ctx.shop,
        "Permission needed: " + ", ".join(describe(s) for s in missing),
        f"{what.capitalize()} needs access that this store hasn't granted yet: "
        + ", ".join(missing)
        + ". Grant access to let it continue.",
        severity=Alert.Severity.WARNING,
        agent_slug=ctx.agent_slug,
        dedupe_key="missing-scopes:" + ",".join(sorted(missing)),
        action_url=url,
    )


def _record_step(
    ctx: ToolContext, key: str, raw_args: dict[str, Any], result: ToolResult, tb: str
) -> None:
    if ctx.run is None:
        return
    last_idx = RunStep.objects.filter(run=ctx.run).aggregate(m=Max("idx"))["m"]
    next_idx = 0 if last_idx is None else last_idx + 1
    RunStep.objects.create(
        run=ctx.run,
        idx=next_idx,
        type=RunStep.Type.TOOL,
        name=key,
        input=redact_value("input", raw_args),
        output=redact_value("output", result.to_dict()),
        duration_ms=result.duration_ms,
        error=(result.error + ("\n\n" + tb if tb else "")) if result.status == "error" else "",
    )
