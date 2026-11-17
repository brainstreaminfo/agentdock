"""
Shopify event → agent runs (spec §9).

Every processed webhook passes through `route_event` (registered as a webhook
fallback in `TriggersConfig.ready`). For each ENABLED installation in that shop
whose version listens to the topic, it starts an EVENT run. The run's input
holds the event payload (emails/phones masked) so the agent knows what happened.

Noisy topics (`DEBOUNCE_SECONDS`, e.g. products/update) are debounced per
installation and resource: a burst of updates to one product starts ONE run,
about a minute after the LAST update.
"""

from __future__ import annotations

from typing import Any

from django.core.cache import cache

from apps.core.logging import get_logger, redact_value
from apps.shopify_app.models import WebhookEvent
from apps.shopify_app.topics import AGENT_EVENT_TOPICS, DEBOUNCE_SECONDS

log = get_logger(__name__)


def route_event(event: WebhookEvent) -> bool:
    """Start runs for this event. Returns True if at least one installation listens."""
    from apps.agents.models import AgentInstallation

    shop = event.shop
    if event.topic not in AGENT_EVENT_TOPICS or shop is None or not shop.is_installed:
        return False
    installs = [
        inst
        for inst in AgentInstallation.objects.for_shop(shop)
        .filter(enabled=True)
        .select_related("version__agent", "shop")
        if event.topic in inst.version.event_topics
    ]
    for inst in installs:
        delay = DEBOUNCE_SECONDS.get(event.topic)
        if delay:
            key = _debounce_key(inst.pk, event)
            cache.set(key, event.pk, timeout=delay * 5)
            from .tasks import start_debounced_event_run

            start_debounced_event_run.apply_async((inst.pk, event.pk, key), countdown=delay)
        else:
            start_event_run(inst, event)
    return bool(installs)


def start_event_run(installation: Any, event: WebhookEvent) -> int | None:
    """Create and enqueue the EVENT run (once per installation + webhook event)."""
    from apps.agents.runtime import create_run
    from apps.agents.tasks import execute_run_task
    from apps.runs.models import Run

    # A Celery retry of the same webhook must not start a second run.
    if Run.objects.filter(
        installation=installation, trigger=Run.Trigger.EVENT, input__webhook_event_id=event.pk
    ).exists():
        return None
    run = create_run(
        shop=installation.shop,
        version=installation.version,
        installation=installation,
        trigger=Run.Trigger.EVENT,
        input={
            "webhook_event_id": event.pk,
            "event": {"topic": event.topic, "payload": redact_value("payload", event.payload)},
        },
    )
    log.info("trigger.event_run", topic=event.topic, installation_id=installation.pk, run_id=run.pk)
    execute_run_task.delay(run.pk)
    return run.pk


def _debounce_key(installation_id: int, event: WebhookEvent) -> str:
    payload = event.payload or {}
    resource = (
        payload.get("admin_graphql_api_id")
        or payload.get("id")
        or "{}-{}".format(payload.get("inventory_item_id"), payload.get("location_id"))
    )
    return f"trigger-debounce:{installation_id}:{event.topic}:{resource}"
