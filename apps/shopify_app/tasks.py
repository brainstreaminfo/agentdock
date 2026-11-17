"""Celery tasks for the Shopify integration."""

from __future__ import annotations

import traceback
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.core.logging import bind_context, get_logger
from apps.shops.models import Shop

from . import oauth, services, tokens, webhooks
from .models import WebhookEvent

log = get_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_shop(self, shop_id: int) -> None:  # type: ignore[no-untyped-def]
    """Refresh shop details and granted scopes from Shopify (spec §5.3 "catalog sync")."""
    bind_context(shop_id=shop_id)
    shop = Shop.objects.installed().filter(pk=shop_id).first()
    if shop is None:
        log.info("sync_shop.skipped_not_installed")
        return
    try:
        services.apply_shop_info(shop, services.fetch_shop_info(shop))
    except (tokens.ShopNotInstalled, oauth.ReauthRequired):
        log.warning("sync_shop.needs_reauth")
        return
    shop.save()
    log.info("sync_shop.done", scopes=shop.granted_scopes)


@shared_task(bind=True, max_retries=5)
def process_webhook_event(self, event_id: int) -> None:  # type: ignore[no-untyped-def]
    event = WebhookEvent.objects.select_related("shop").filter(pk=event_id).first()
    if event is None:  # e.g. deleted by shop/redact in the meantime
        return
    if event.status == WebhookEvent.Status.PROCESSED:
        return
    bind_context(shop_id=event.shop_id, webhook_topic=event.topic, webhook_event_id=event.pk)
    event.attempts += 1
    try:
        handled = webhooks.dispatch(event)
    except Exception as exc:
        event.status = WebhookEvent.Status.FAILED
        event.error = traceback.format_exc()
        event.save(update_fields=["status", "error", "attempts"])
        log.exception("webhook.failed")
        raise self.retry(exc=exc, countdown=min(60 * 2**self.request.retries, 3600)) from exc

    # shop/redact deletes the shop, and the event with it (CASCADE).
    if not WebhookEvent.objects.filter(pk=event.pk).exists():
        return
    event.status = WebhookEvent.Status.PROCESSED if handled else WebhookEvent.Status.IGNORED
    event.processed_at = timezone.now()
    event.error = ""
    event.save(update_fields=["status", "processed_at", "error", "attempts"])
    log.info("webhook.processed", status=event.status)


@shared_task
def purge_uninstalled_shops() -> int:
    """Daily (beat): delete shops uninstalled more than 30 days ago."""
    count = services.purge_due_shops()
    log.info("purge_uninstalled_shops.done", purged=count)
    return count


@shared_task
def refresh_dormant_tokens() -> int:
    """
    Daily (beat): refresh tokens whose refresh token expires within 14 days, so
    stores where no agent has run for a while don't lose access.
    """
    soon = timezone.now() + timedelta(days=14)
    refreshed = 0
    for shop in Shop.objects.installed().filter(refresh_token_expires_at__lt=soon):
        bind_context(shop_id=shop.pk)
        try:
            tokens.get_access_token(shop, force_refresh=True)
            refreshed += 1
        except (tokens.ShopNotInstalled, oauth.OAuthError) as exc:
            log.warning("refresh_dormant_tokens.failed", error=str(exc))
    return refreshed


@shared_task
def delete_old_webhook_events(days: int = 30) -> int:
    """Daily (beat): webhook payloads may contain customer data — don't keep them forever."""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = WebhookEvent.objects.filter(received_at__lt=cutoff).delete()
    return deleted
