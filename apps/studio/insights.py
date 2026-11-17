"""Numbers for the Studio dashboard and per-agent analytics, and the publish diff."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.agents.models import AgentDefinition, AgentInstallation, AgentVersion
from apps.runs.models import Run
from apps.shopify_app.scopes import describe


def agent_stats(days: int = 30) -> dict[str, dict[str, Any]]:
    """{agent slug: stats} for every agent, computed in a few grouped queries."""
    since = timezone.now() - timedelta(days=days)
    stats: dict[str, dict[str, Any]] = {}
    runs = (
        Run.objects.filter(created_at__gte=since)
        .exclude(trigger__in=[Run.Trigger.PLAYGROUND, Run.Trigger.TEST, Run.Trigger.TOOL_TEST])
        .values("agent_slug")
        .annotate(
            runs=Count("id"),
            failed=Count("id", filter=Q(status=Run.Status.FAILED)),
            avg_cost=Avg("cost_usd"),
            total_cost=Sum("cost_usd"),
        )
    )
    for row in runs:
        runs_count = row["runs"] or 0
        stats[row["agent_slug"]] = {
            "runs": runs_count,
            "error_rate": round(100 * (row["failed"] or 0) / runs_count, 1) if runs_count else 0,
            "avg_cost": row["avg_cost"] or Decimal(0),
            "total_cost": row["total_cost"] or Decimal(0),
        }
    install_rows = AgentInstallation.objects.values("agent__slug").annotate(
        installs=Count("id"),
        active=Count("id", filter=Q(enabled=True, shop__uninstalled_at__isnull=True)),
    )
    for irow in install_rows:
        entry = stats.setdefault(
            irow["agent__slug"],
            {"runs": 0, "error_rate": 0, "avg_cost": Decimal(0), "total_cost": Decimal(0)},
        )
        entry["installs"] = irow["installs"]
        entry["active_shops"] = irow["active"]
    return stats


def revenue(agent: AgentDefinition, active_shops: int) -> Decimal:
    """Monthly extra-price revenue (plan revenue is Phase 6 billing)."""
    return (agent.extra_price or Decimal(0)) * active_shops


def runs_per_day(agent: AgentDefinition, days: int = 30) -> dict[str, Any]:
    since = timezone.now() - timedelta(days=days)
    rows = (
        Run.objects.filter(agent_slug=agent.slug, created_at__gte=since)
        .exclude(trigger__in=[Run.Trigger.PLAYGROUND, Run.Trigger.TEST])
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            runs=Count("id"),
            failed=Count("id", filter=Q(status=Run.Status.FAILED)),
            cost=Sum("cost_usd"),
        )
        .order_by("day")
    )
    by_day = {r["day"]: r for r in rows}
    labels, ok, failed, cost = [], [], [], []
    for i in range(days - 1, -1, -1):
        day = (timezone.now() - timedelta(days=i)).date()
        row: dict[str, Any] = dict(by_day.get(day, {}))
        labels.append(day.strftime("%d %b"))
        failed.append(row.get("failed", 0))
        ok.append(row.get("runs", 0) - row.get("failed", 0))
        cost.append(float(row.get("cost") or 0))
    return {"labels": labels, "ok": ok, "failed": failed, "cost": cost}


# --------------------------------------------------------------------------- publish diff
LABELS = {
    "instructions_md": "Instructions",
    "model": "Model",
    "effort": "Effort",
    "max_steps": "Max steps",
    "max_tokens": "Max tokens",
    "settings_schema": "Merchant settings form",
    "settings_defaults": "Setting defaults",
    "triggers": "Triggers",
    "default_autonomy": "Default autonomy",
    "allowed_autonomy": "Allowed autonomy",
    "output_types": "Output types",
}


def version_diff(old: AgentVersion | None, new: AgentVersion) -> dict[str, Any]:
    """What publishing `new` (the draft) changes compared with `old` (latest version)."""
    new_tools = set(new.tools.values_list("key", flat=True))
    new_scopes = set(new.compute_required_scopes())
    if old is None:
        return {
            "first": True,
            "fields": [],
            "tools_added": sorted(new_tools),
            "tools_removed": [],
            "scopes_added": [{"scope": s, "label": describe(s)} for s in sorted(new_scopes)],
            "scopes_removed": [],
            "changed": True,
        }
    old_tools = set(old.tools.values_list("key", flat=True))
    old_scopes = set(old.required_scopes or [])
    fields = [LABELS[f] for f in AgentVersion.CONFIG_FIELDS if getattr(old, f) != getattr(new, f)]
    diff = {
        "first": False,
        "fields": fields,
        "tools_added": sorted(new_tools - old_tools),
        "tools_removed": sorted(old_tools - new_tools),
        "scopes_added": [
            {"scope": s, "label": describe(s)} for s in sorted(new_scopes - old_scopes)
        ],
        "scopes_removed": sorted(old_scopes - new_scopes),
    }
    diff["changed"] = bool(fields or diff["tools_added"] or diff["tools_removed"])
    return diff
