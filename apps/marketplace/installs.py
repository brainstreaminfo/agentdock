"""
Install, update, configure and uninstall agents (spec §8.2, §8.3).

The views stay thin; the rules live here:

    check = check_install(shop, agent)      plan entitlement + missing scopes
    inst = install_agent(shop, agent, settings=..., autonomy=..., user=...)
    update_settings(inst, settings=..., autonomy=..., schedule_enabled=...)
    update_to_latest(inst, user)            move to the newest published version
    uninstall(inst, user)
    run = run_now(inst, user, task="")

Missing scopes: the view stores `pending_agent_install` in the session and sends
the merchant through OAuth with the UNION of granted and needed scopes
(`grant_url`). The OAuth callback redirects back to the install page, which
then shows the settings form.

Saving or deleting an installation re-syncs its schedule and the shop's
Shopify event subscriptions automatically (apps/triggers/receivers.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.agents.models import AgentDefinition, AgentInstallation, AgentVersion, Autonomy
from apps.agents.settings_schema import validate_settings
from apps.billing import entitlements
from apps.billing.entitlements import Decision
from apps.core.audit import audit

if TYPE_CHECKING:
    from apps.runs.models import Run
    from apps.shops.models import Shop

SESSION_PENDING = "pending_agent_install"


class InstallError(Exception):
    """A merchant-safe reason why the action can't be done."""


@dataclass
class InstallCheck:
    version: AgentVersion
    entitlement: Decision
    missing_scopes: list[str] = field(default_factory=list)
    installation: AgentInstallation | None = None

    @property
    def ready(self) -> bool:
        return bool(self.entitlement) and not self.missing_scopes and self.installation is None


def check_install(shop: Shop, agent: AgentDefinition) -> InstallCheck:
    version = agent.latest_version
    if version is None:
        raise InstallError("This agent hasn't been published yet.")
    return InstallCheck(
        version=version,
        entitlement=entitlements.can_install(shop, agent),
        missing_scopes=shop.missing_scopes(version.required_scopes),
        installation=AgentInstallation.objects.for_shop(shop).filter(agent=agent).first(),
    )


def grant_url(shop: Shop, scopes: list[str], next_url: str) -> str:
    """OAuth URL that asks for `scopes` (the view adds already-granted ones) then returns."""
    return (
        reverse("shopify:auth")
        + "?"
        + urlencode({"shop": shop.domain, "scopes": ",".join(scopes), "next": next_url})
    )


def allowed_autonomy(version: AgentVersion) -> list[tuple[str, str]]:
    allowed = version.allowed_autonomy or [version.default_autonomy]
    return [(value, label) for value, label in Autonomy.choices if value in allowed]


def _validate(
    shop: Shop, version: AgentVersion, settings: dict[str, Any], autonomy: str
) -> dict[str, Any]:
    values = {**(version.settings_defaults or {}), **settings}
    errors = validate_settings(version.settings_schema or {}, values)
    if errors:
        raise InstallError("Invalid settings: " + "; ".join(errors))
    if autonomy not in dict(allowed_autonomy(version)):
        raise InstallError("This agent can't run in that mode.")
    decision = entitlements.can_use_autonomy(shop, autonomy)
    if not decision:
        raise InstallError(decision.reason)
    return settings


def install_agent(
    shop: Shop,
    agent: AgentDefinition,
    *,
    settings: dict[str, Any],
    autonomy: str,
    schedule_enabled: bool = False,
    user: Any = None,
) -> AgentInstallation:
    check = check_install(shop, agent)
    if check.installation is not None:
        raise InstallError("This agent is already installed.")
    if not check.entitlement:
        raise InstallError(check.entitlement.reason)
    if check.missing_scopes:
        raise InstallError("Grant the permissions this agent needs first.")
    version = check.version
    _validate(shop, version, settings, autonomy)
    try:
        with transaction.atomic():
            inst = AgentInstallation.objects.create(
                shop=shop,
                agent=agent,
                version=version,
                settings=settings,
                autonomy=autonomy,
                schedule_enabled=bool(schedule_enabled and version.schedule_cron),
                installed_by=user if getattr(user, "is_authenticated", False) else None,
            )
    except IntegrityError as exc:  # double submit
        raise InstallError("This agent is already installed.") from exc
    audit("agent.installed", shop=shop, user=user, agent=agent.slug, version=version.number)
    return inst


def update_settings(
    inst: AgentInstallation,
    *,
    settings: dict[str, Any],
    autonomy: str,
    schedule_enabled: bool,
    user: Any = None,
) -> AgentInstallation:
    _validate(inst.shop, inst.version, settings, autonomy)
    if schedule_enabled and not entitlements.can_use_schedules(inst.shop):
        raise InstallError(entitlements.can_use_schedules(inst.shop).reason)
    inst.settings = settings
    inst.autonomy = autonomy
    inst.schedule_enabled = bool(schedule_enabled and inst.version.schedule_cron)
    inst.save()
    audit("agent.configured", shop=inst.shop, user=user, agent=inst.agent.slug, autonomy=autonomy)
    return inst


def set_enabled(inst: AgentInstallation, enabled: bool, *, user: Any = None) -> AgentInstallation:
    inst.enabled = enabled
    inst.save(update_fields=["enabled", "updated_at"])
    audit(
        "agent.resumed" if enabled else "agent.paused",
        shop=inst.shop,
        user=user,
        agent=inst.agent.slug,
    )
    return inst


def update_to_latest(inst: AgentInstallation, *, user: Any = None) -> AgentInstallation:
    """Move to the newest published version (after the merchant approved new scopes)."""
    latest = inst.agent.latest_version
    if latest is None or latest.pk == inst.version_id:
        raise InstallError("This agent is already up to date.")
    missing = inst.shop.missing_scopes(latest.required_scopes)
    if missing:
        raise InstallError("Grant the new permissions first.")
    old = inst.version
    inst.version = latest
    # Keep settings that still exist in the new form; new fields use their defaults.
    keys = set((latest.settings_schema or {}).get("properties") or {})
    inst.settings = {k: v for k, v in (inst.settings or {}).items() if k in keys}
    if inst.autonomy not in (latest.allowed_autonomy or [latest.default_autonomy]):
        inst.autonomy = latest.default_autonomy
    if validate_settings(latest.settings_schema or {}, inst.effective_settings()):
        inst.settings = {}  # incompatible values: fall back to the defaults
    inst.save()
    audit(
        "agent.updated",
        shop=inst.shop,
        user=user,
        agent=inst.agent.slug,
        from_version=old.number,
        to_version=latest.number,
    )
    return inst


def uninstall(inst: AgentInstallation, *, user: Any = None) -> None:
    shop, slug = inst.shop, inst.agent.slug
    inst.delete()  # cascades to its chats; runs and approvals stay in the history
    audit("agent.uninstalled", shop=shop, user=user, agent=slug)


def run_now(inst: AgentInstallation, *, user: Any = None, task: str = "") -> Run:
    """The "Run now" button: a MANUAL run in the background."""
    from apps.agents.runtime import create_run
    from apps.agents.tasks import execute_run_task
    from apps.runs.models import Run

    if not inst.enabled:
        raise InstallError("Resume the agent first.")
    run = create_run(
        shop=inst.shop,
        version=inst.version,
        installation=inst,
        trigger=Run.Trigger.MANUAL,
        input={"task": task.strip()} if task.strip() else {},
    )
    audit("agent.run_now", shop=inst.shop, user=user, agent=inst.agent.slug, run=run.pk)
    execute_run_task.delay(run.pk)
    return run
