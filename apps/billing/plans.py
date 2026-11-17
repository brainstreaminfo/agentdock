"""
Subscription plans (spec §11). Placeholder prices; billing itself is Phase 6.
Agents list which plans include them (AgentDefinition.plans).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_usd: int
    max_agents: int | None  # None = unlimited
    runs_per_month: int
    auto_mode: bool
    schedules: bool
    team: bool


PLANS: dict[str, Plan] = {
    "starter": Plan("starter", "Starter", 19, 3, 300, auto_mode=False, schedules=False, team=False),
    "growth": Plan("growth", "Growth", 49, 10, 1500, auto_mode=True, schedules=True, team=False),
    "pro": Plan("pro", "Pro", 149, None, 6000, auto_mode=True, schedules=True, team=True),
}

PLAN_CHOICES = [(p.key, f"{p.name} (${p.price_usd}/mo)") for p in PLANS.values()]
