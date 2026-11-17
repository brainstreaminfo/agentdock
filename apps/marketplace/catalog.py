"""
Which agents a shop can see in the Marketplace (spec §8.1).

Visible = published, not a template, not a system agent, and either
PUBLIC, BETA (shown with a "Beta" badge) or PRIVATE with this shop in
`allowed_shops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Q, QuerySet
from django.http import Http404

from apps.agents.models import AgentDefinition
from apps.shopify_app import scopes as scope_utils

if TYPE_CHECKING:
    from apps.shops.models import Shop


def visible_agents(shop: Shop) -> QuerySet[AgentDefinition]:
    V = AgentDefinition.Visibility
    return (
        AgentDefinition.objects.filter(
            status=AgentDefinition.Status.PUBLISHED, is_template=False, is_system=False
        )
        .filter(Q(visibility__in=[V.PUBLIC, V.BETA]) | Q(visibility=V.PRIVATE, allowed_shops=shop))
        .distinct()
        .order_by("-featured", "name")
    )


def get_visible_agent(shop: Shop, slug: str) -> AgentDefinition:
    agent = visible_agents(shop).filter(slug=slug).first()
    if agent is None or agent.latest_version is None:
        raise Http404("Agent not found")
    return agent


def search(shop: Shop, query: str = "", category: str = "") -> QuerySet[AgentDefinition]:
    agents = visible_agents(shop)
    if query:
        agents = agents.filter(
            Q(name__icontains=query)
            | Q(tagline__icontains=query)
            | Q(description_md__icontains=query)
            | Q(category__icontains=query)
        )
    if category:
        agents = agents.filter(category=category)
    return agents


def categories(shop: Shop) -> list[str]:
    return sorted({c for c in visible_agents(shop).values_list("category", flat=True) if c})


def permission_lines(scopes: list[str], granted: list[str] | None = None) -> list[dict[str, Any]]:
    """Plain-language permission list, e.g. "Edit your products", with "already granted"."""
    lines = []
    for scope in scope_utils.union(scopes):
        lines.append(
            {
                "scope": scope,
                "label": scope_utils.describe(scope),
                "writes": scope.startswith("write_"),
                "granted": granted is not None and scope_utils.satisfies([scope], granted),
            }
        )
    return lines
