"""Celery tasks that start agent runs from schedules and Shopify events."""

from __future__ import annotations

from celery import shared_task
from django.core.cache import cache

from apps.core.logging import bind_context, get_logger

log = get_logger(__name__)


@shared_task(acks_late=True)
def run_scheduled_agent(installation_id: int) -> str:
    """Fired by django-celery-beat (one PeriodicTask per scheduled installation)."""
    from apps.agents.models import AgentInstallation
    from apps.agents.runtime import create_run
    from apps.agents.tasks import execute_run_task
    from apps.billing.entitlements import can_use_schedules
    from apps.runs.models import Run

    from .schedules import is_scheduled, remove_schedule

    bind_context(installation_id=installation_id)
    inst = (
        AgentInstallation.objects.select_related("shop", "version__agent")
        .filter(pk=installation_id)
        .first()
    )
    if inst is None:
        remove_schedule(installation_id)  # left over from a deleted installation
        return "gone"
    if not is_scheduled(inst):
        log.info("trigger.schedule_skipped", reason="not scheduled")
        return "skipped"
    if not can_use_schedules(inst.shop):
        log.info("trigger.schedule_skipped", reason="plan")
        return "skipped"
    run = create_run(
        shop=inst.shop,
        version=inst.version,
        installation=inst,
        trigger=Run.Trigger.SCHEDULE,
        input={},
    )
    log.info("trigger.schedule_run", run_id=run.pk)
    execute_run_task.delay(run.pk)
    return f"run {run.pk}"


@shared_task(acks_late=True)
def start_debounced_event_run(installation_id: int, event_id: int, key: str) -> str:
    """Start the run only if no newer event for the same resource arrived meanwhile."""
    from apps.agents.models import AgentInstallation
    from apps.shopify_app.models import WebhookEvent

    from .router import start_event_run

    if cache.get(key) != event_id:
        return "superseded"
    cache.delete(key)
    inst = (
        AgentInstallation.objects.select_related("shop", "version__agent")
        .filter(pk=installation_id, enabled=True)
        .first()
    )
    event = WebhookEvent.objects.filter(pk=event_id).first()
    if inst is None or event is None:
        return "gone"
    run_id = start_event_run(inst, event)
    return f"run {run_id}" if run_id else "duplicate"


@shared_task(bind=True, max_retries=5, acks_late=True)
def sync_webhooks(self, shop_id: int) -> str:  # type: ignore[no-untyped-def]
    """Create/delete the shop's Shopify event subscriptions to match its agents."""
    from apps.shopify_app.graphql import ShopifyGraphQLError
    from apps.shopify_app.oauth import OAuthError
    from apps.shopify_app.tokens import ShopNotInstalled
    from apps.shops.models import Shop

    from .subscriptions import sync_webhook_subscriptions

    bind_context(shop_id=shop_id)
    shop = Shop.objects.installed().filter(pk=shop_id).first()
    if shop is None:
        return "not installed"
    try:
        report = sync_webhook_subscriptions(shop)
    except (ShopNotInstalled, OAuthError):
        log.warning("webhooks.sync_needs_reauth")
        return "needs reauth"
    except ShopifyGraphQLError as exc:
        log.warning("webhooks.sync_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=min(60 * 2**self.request.retries, 3600)) from exc
    return f"created={report.created} deleted={report.deleted} errors={report.errors}"
