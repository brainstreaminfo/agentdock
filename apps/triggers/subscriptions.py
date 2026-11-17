"""
Shopify event subscriptions per shop (spec §9, DECISIONS #7).

A shop is subscribed to exactly the event topics used by its ENABLED installed
agents. `sync_webhook_subscriptions(shop)` compares that with what Shopify has
and creates / deletes subscriptions. It only touches topics agents can use
(`AGENT_EVENT_TOPICS`) at our webhook URL, never the lifecycle/compliance
topics from shopify.app.toml.

It runs in Celery (`tasks.sync_webhooks`) after an installation changes, after
a re-install and after scopes change. `manage.py check_shop <domain>` lists
the subscriptions Shopify has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.urls import reverse

from apps.core.logging import get_logger
from apps.shopify_app.graphql import ShopifyGraphQL
from apps.shopify_app.topics import AGENT_EVENT_TOPICS

if TYPE_CHECKING:
    from apps.shops.models import Shop

log = get_logger(__name__)


@cache
def _document() -> str:
    return (Path(__file__).parent / "webhook_subscriptions.graphql").read_text()


def topic_enum(topic: str) -> str:
    """'inventory_levels/update' -> 'INVENTORY_LEVELS_UPDATE' (GraphQL enum)."""
    return topic.upper().replace("/", "_")


ENUM_TO_TOPIC = {topic_enum(t): t for t in AGENT_EVENT_TOPICS}


def webhook_uri() -> str:
    return settings.APP_URL + reverse("shopify:webhooks")


def desired_topics(shop: Shop) -> set[str]:
    """Topics used by the shop's enabled installations whose scope the shop granted."""
    from apps.agents.models import AgentInstallation

    topics: set[str] = set()
    for inst in (
        AgentInstallation.objects.for_shop(shop).filter(enabled=True).select_related("version")
    ):
        topics.update(t for t in inst.version.event_topics if t in AGENT_EVENT_TOPICS)
    # A topic whose read scope is missing would be refused by Shopify.
    return {t for t in topics if shop.has_scopes([AGENT_EVENT_TOPICS[t]["scope"]])}


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sync_webhook_subscriptions(shop: Shop, client: ShopifyGraphQL | None = None) -> SyncReport:
    report = SyncReport()
    if not shop.is_installed or shop.is_demo:
        return report
    client = client or ShopifyGraphQL(shop)
    uri = webhook_uri()
    want = desired_topics(shop)

    data = client.execute(
        _document(), {"uri": uri}, operation_name="AgentWebhookSubscriptions"
    ).data
    existing: dict[str, str] = {}  # topic -> subscription id
    for node in (data.get("webhookSubscriptions") or {}).get("nodes") or []:
        topic = ENUM_TO_TOPIC.get(node.get("topic", ""))
        if topic is None or node.get("uri") not in (uri, None):
            continue  # not an agent topic, or not ours
        if topic in existing or topic not in want:
            _delete(client, node["id"], topic, report)  # duplicate or no longer needed
        else:
            existing[topic] = node["id"]

    for topic in sorted(want - existing.keys()):
        result = client.execute(
            _document(),
            {"topic": topic_enum(topic), "subscription": {"uri": uri, "format": "JSON"}},
            operation_name="CreateAgentWebhookSubscription",
        ).data["webhookSubscriptionCreate"]
        if result.get("userErrors"):
            report.errors.append(
                f"{topic}: " + "; ".join(e["message"] for e in result["userErrors"])
            )
        else:
            report.created.append(topic)

    log.info(
        "webhooks.synced",
        shop_id=shop.pk,
        created=report.created,
        deleted=report.deleted,
        errors=report.errors or None,
    )
    return report


def _delete(client: ShopifyGraphQL, subscription_id: str, topic: str, report: SyncReport) -> None:
    result = client.execute(
        _document(), {"id": subscription_id}, operation_name="DeleteAgentWebhookSubscription"
    ).data["webhookSubscriptionDelete"]
    if result.get("userErrors"):
        report.errors.append(f"{topic}: " + "; ".join(e["message"] for e in result["userErrors"]))
    else:
        report.deleted.append(topic)
