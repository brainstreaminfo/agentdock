"""
Keep schedules and Shopify subscriptions in sync with installations and shops.

* An installation is saved (installed, paused, settings, new version…) or deleted
  → its schedule is re-synced right away (database only), and the shop's
  webhook subscriptions are re-synced in Celery after the transaction commits.
* The app is uninstalled from Shopify → schedules removed, queued runs cancelled
  (installations keep their `enabled` flag, so a re-install resumes them).
* The app is re-installed, or scopes / timezone change → everything re-synced.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.agents.models import AgentInstallation
from apps.core.logging import get_logger
from apps.shopify_app import signals as shop_signals

from . import schedules

log = get_logger(__name__)


def queue_webhook_sync(shop_id: int) -> None:
    """Re-sync the shop's Shopify subscriptions in Celery once the change is committed.
    Off in tests (`TRIGGERS_AUTO_SYNC_WEBHOOKS=False`) so they never call Shopify."""
    from .tasks import sync_webhooks

    if settings.TRIGGERS_AUTO_SYNC_WEBHOOKS:
        transaction.on_commit(lambda: sync_webhooks.delay(shop_id))


# Saves that change nothing about triggers (bookkeeping only).
_BOOKKEEPING_FIELDS = {"last_run_at", "updated_at"}


@receiver(post_save, sender=AgentInstallation)
def installation_saved(
    sender: Any, instance: AgentInstallation, update_fields: Any = None, **kwargs: Any
) -> None:
    if update_fields and set(update_fields) <= _BOOKKEEPING_FIELDS:
        return
    schedules.sync_schedule(instance)
    queue_webhook_sync(instance.shop_id)


@receiver(post_delete, sender=AgentInstallation)
def installation_deleted(sender: Any, instance: AgentInstallation, **kwargs: Any) -> None:
    schedules.remove_schedule(instance.pk)
    queue_webhook_sync(instance.shop_id)


def resync_shop(shop: Any) -> None:
    for inst in AgentInstallation.objects.for_shop(shop).select_related("version", "shop", "agent"):
        schedules.sync_schedule(inst)


@receiver(shop_signals.shop_installed)
def on_shop_installed(sender: Any, shop: Any, **kwargs: Any) -> None:
    resync_shop(shop)
    queue_webhook_sync(shop.pk)


@receiver(shop_signals.shop_scopes_updated)
def on_scopes_updated(sender: Any, shop: Any, **kwargs: Any) -> None:
    queue_webhook_sync(shop.pk)


@receiver(shop_signals.shop_timezone_changed)
def on_timezone_changed(sender: Any, shop: Any, **kwargs: Any) -> None:
    resync_shop(shop)


@receiver(shop_signals.shop_uninstalled)
def on_shop_uninstalled(sender: Any, shop: Any, **kwargs: Any) -> None:
    """Stop everything (spec §5.7). Shopify deletes webhook subscriptions itself."""
    from apps.runs.models import Run

    for inst_id in AgentInstallation.objects.for_shop(shop).values_list("pk", flat=True):
        schedules.remove_schedule(inst_id)
    # Queued runs: the Celery task only executes runs that are still QUEUED, so
    # marking them CANCELLED is enough (no need to revoke the task itself).
    cancelled = (
        Run.objects.for_shop(shop)
        .filter(status=Run.Status.QUEUED)
        .update(status=Run.Status.CANCELLED, error="The app was uninstalled.")
    )
    log.info("triggers.shop_stopped", shop_id=shop.pk, cancelled_runs=cancelled)
