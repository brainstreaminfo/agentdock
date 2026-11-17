"""
Agent test cases (spec §7.3): "Run all tests" before publishing.

Each case runs the DRAFT once against a store the staff member chose, as a
TEST run (store changes always go to approval, and those proposals are rejected
afterwards, so tests never change the store). Then its checks are evaluated:

    must_call          every listed tool was called
    must_not_call      none of the listed tools was called
    reply_contains     the final reply contains each text (case-insensitive)
    reply_not_contains the final reply contains none of these texts
    max_cost_usd       the run cost at most this much

Results are tied to the draft's configuration fingerprint, so the publish page
knows whether they still describe the current draft.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.runs.models import ActionProposal, Run
from apps.shops.models import Shop

from .models import AgentDefinition, AgentTestCase, AgentTestResult, AgentVersion
from .runtime import create_run, execute_run

CHECK_LABELS = {
    "must_call": "Must call",
    "must_not_call": "Must not call",
    "reply_contains": "Reply contains",
    "reply_not_contains": "Reply doesn't contain",
    "max_cost_usd": "Costs at most (USD)",
}


def evaluate(run: Run, checks: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    called = set(run.steps.filter(type="TOOL").values_list("name", flat=True))
    reply = str((run.output or {}).get("text") or "").lower()
    details: list[dict[str, Any]] = []

    if run.status != Run.Status.SUCCEEDED:
        details.append({"check": "Run finished", "ok": False, "note": run.error or run.status})
    for tool in checks.get("must_call") or []:
        details.append({"check": f"Calls {tool}", "ok": tool in called})
    for tool in checks.get("must_not_call") or []:
        details.append({"check": f"Doesn't call {tool}", "ok": tool not in called})
    for text in checks.get("reply_contains") or []:
        details.append({"check": f'Reply contains "{text}"', "ok": text.lower() in reply})
    for text in checks.get("reply_not_contains") or []:
        details.append({"check": f'Reply avoids "{text}"', "ok": text.lower() not in reply})
    if checks.get("max_cost_usd") not in (None, ""):
        limit = Decimal(str(checks["max_cost_usd"]))
        details.append(
            {"check": f"Cost ≤ ${limit}", "ok": run.cost_usd <= limit, "note": f"${run.cost_usd}"}
        )
    return all(d["ok"] for d in details), details


def start_test_suite(agent: AgentDefinition, shop: Shop) -> list[AgentTestResult]:
    """Create one PENDING result (and queued run) per case and start them in Celery."""
    from .tasks import run_agent_test_task

    draft = agent.draft
    fingerprint = draft.config_fingerprint()
    results = []
    for case in agent.test_cases.all():
        run = create_run(
            shop=shop,
            version=draft,
            trigger=Run.Trigger.TEST,
            autonomy="APPROVE",
            input={
                "task": case.prompt,
                "settings": {**(draft.settings_defaults or {}), **(case.settings or {})},
            },
        )
        results.append(
            AgentTestResult.objects.create(
                version=draft, case=case, fingerprint=fingerprint, run=run
            )
        )
    for result in results:
        run_agent_test_task.delay(result.pk)
    return results


def run_test(result: AgentTestResult) -> AgentTestResult:
    run = execute_run(result.run)  # type: ignore[arg-type]
    # Tests never change the store: reject anything the agent proposed.
    ActionProposal.objects.filter(run=run, status=ActionProposal.Status.PENDING).update(
        status=ActionProposal.Status.REJECTED, decided_at=timezone.now()
    )
    passed, details = evaluate(run, result.case.checks or {})
    result.passed = passed
    result.details = details
    result.status = (
        AgentTestResult.Status.PASSED
        if passed
        else AgentTestResult.Status.ERROR
        if run.status == Run.Status.FAILED
        else AgentTestResult.Status.FAILED
    )
    result.save()
    return result


def latest_results(agent: AgentDefinition, fingerprint: str) -> dict[int, AgentTestResult]:
    """Newest result per case for this exact draft configuration."""
    latest: dict[int, AgentTestResult] = {}
    for result in (
        AgentTestResult.objects.filter(case__agent=agent, fingerprint=fingerprint)
        .select_related("run")
        .order_by("-created_at")
    ):
        latest.setdefault(result.case_id, result)
    return latest


def summary(agent: AgentDefinition, version: AgentVersion | None = None) -> dict[str, Any]:
    draft = version or agent.draft
    cases = list(agent.test_cases.all())
    results = latest_results(agent, draft.config_fingerprint())
    statuses = [results[c.pk].status if c.pk in results else None for c in cases]
    return {
        "total": len(cases),
        "passed": statuses.count(AgentTestResult.Status.PASSED),
        "failed": statuses.count(AgentTestResult.Status.FAILED)
        + statuses.count(AgentTestResult.Status.ERROR),
        "pending": statuses.count(AgentTestResult.Status.PENDING),
        "not_run": statuses.count(None),
    }


def copy_case_checks(case: AgentTestCase) -> dict[str, Any]:
    return dict(case.checks or {})
