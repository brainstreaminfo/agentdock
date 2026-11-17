"""
Plan entitlements: the ONE place that answers "is this shop allowed to…?" (spec §11).

Phase 5 calls these functions from the install flow and the agent settings page.
Billing (Shopify subscriptions) arrives in Phase 6. Until then every shop is
treated as having every plan feature, so nothing is blocked. Phase 6 only has to
change the bodies of these functions; callers already handle a refusal
(they show `reason` and link to `upgrade_url`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.agents.models import AgentDefinition
    from apps.shops.models import Shop


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    upgrade_url: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Decision(True)


def can_install(shop: Shop, agent: AgentDefinition) -> Decision:
    """Plan includes this agent and the shop is below its installed-agents limit."""
    return ALLOWED


def can_use_autonomy(shop: Shop, autonomy: str) -> Decision:
    """AUTO mode is a Growth/Pro feature (enforced from Phase 6)."""
    return ALLOWED


def can_use_schedules(shop: Shop) -> Decision:
    """Scheduled runs are a Growth/Pro feature (enforced from Phase 6)."""
    return ALLOWED
