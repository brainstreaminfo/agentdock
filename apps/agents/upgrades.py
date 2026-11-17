"""
What happens to installed shops when a new version is published (spec §7.2):

* same scopes (the shop already granted everything) and the agent allows
  auto-upgrade -> the installation moves to the new version right away;
* new scopes -> it stays on its current version; the merchant sees "Update
  available — needs access to X" and approves (re-OAuth) in Phase 5;
* auto-upgrade switched off -> it stays; the merchant updates manually (Phase 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.core.audit import audit

from .models import AgentInstallation, AgentVersion


@dataclass
class UpgradeReport:
    upgraded: list[int] = field(default_factory=list)
    needs_scopes: list[int] = field(default_factory=list)
    manual: list[int] = field(default_factory=list)


def upgrade_installations(version: AgentVersion) -> UpgradeReport:
    report = UpgradeReport()
    installs = AgentInstallation.objects.filter(agent=version.agent).exclude(version=version)
    for inst in installs.select_related("shop"):
        if not version.agent.auto_upgrade:
            report.manual.append(inst.pk)
        elif inst.shop.missing_scopes(version.required_scopes):
            report.needs_scopes.append(inst.pk)
        else:
            old = inst.version_id
            inst.version = version
            inst.save(update_fields=["version", "updated_at"])
            audit(
                "agent.auto_upgraded",
                shop=inst.shop,
                agent=version.agent.slug,
                from_version=old,
                to_version=version.number,
            )
            report.upgraded.append(inst.pk)
    return report


def pending_update(installation: AgentInstallation) -> tuple[AgentVersion | None, list[str]]:
    """(newer version, scopes it still needs) for the merchant's "Update available"."""
    latest = installation.agent.latest_version
    if latest is None or latest.pk == installation.version_id:
        return None, []
    if (latest.number or 0) <= (installation.version.number or 0):
        return None, []
    return latest, installation.shop.missing_scopes(latest.required_scopes)
