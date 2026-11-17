"""
Webhook processing.

The view (`views.webhook`) verifies the HMAC, stores a `WebhookEvent` and
enqueues `tasks.process_webhook_event`. That task calls `dispatch(event)`, which
looks up the handler for the topic in `HANDLERS`.

Add a handler:

    @handler("shop/update")
    def on_shop_update(event: WebhookEvent) -> None: ...

Topics with no handler are marked IGNORED. The trigger router
(`apps/triggers/router.py`) hooks in with `register_fallback` to start runs of
event-triggered agents; it returns True when it started something.
"""

from __future__ import annotations

from collections.abc import Callable

from apps.core.audit import audit
from apps.core.logging import get_logger
from apps.shops.models import Shop

from . import scopes as scope_utils
from . import services, signals
from .models import WebhookEvent

log = get_logger(__name__)

Handler = Callable[[WebhookEvent], None]
Fallback = Callable[[WebhookEvent], bool]  # returns True if it handled the event
HANDLERS: dict[str, Handler] = {}
_fallbacks: list[Fallback] = []

# Compliance topics Shopify requires every App Store app to handle.
COMPLIANCE_TOPICS = ("customers/data_request", "customers/redact", "shop/redact")


def handler(topic: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        HANDLERS[topic] = func
        return func

    return decorator


def register_fallback(func: Fallback) -> Fallback:
    """Called for every event (after the topic handler). Used by the trigger router."""
    if func not in _fallbacks:
        _fallbacks.append(func)
    return func


def dispatch(event: WebhookEvent) -> bool:
    """Run handlers for one event. Returns True if anything handled it."""
    handled = False
    if event.topic in HANDLERS:
        HANDLERS[event.topic](event)
        handled = True
    for fallback in _fallbacks:
        handled = fallback(event) or handled
    return handled


# ---------------------------------------------------------------- app lifecycle
@handler("app/uninstalled")
def on_app_uninstalled(event: WebhookEvent) -> None:
    if event.shop is not None:
        services.mark_uninstalled(event.shop)


@handler("app/scopes_update")
def on_scopes_update(event: WebhookEvent) -> None:
    shop = event.shop
    if shop is None:
        return
    current = event.payload.get("current")
    if current is None:
        return
    shop.granted_scopes = scope_utils.parse(current)
    shop.save(update_fields=["granted_scopes", "updated_at"])
    audit("shop.scopes_updated", shop=shop, scopes=shop.granted_scopes)
    signals.shop_scopes_updated.send(sender=Shop, shop=shop, scopes=shop.granted_scopes)


@handler("shop/update")
def on_shop_update(event: WebhookEvent) -> None:
    """REST-style payload: keep name/email/currency/timezone in sync."""
    shop, p = event.shop, event.payload
    if shop is None:
        return
    old_timezone = shop.timezone
    shop.name = p.get("name") or shop.name
    shop.email = p.get("email") or shop.email
    shop.currency = p.get("currency") or shop.currency
    shop.timezone = p.get("iana_timezone") or shop.timezone
    shop.plan_name = p.get("plan_display_name") or shop.plan_name
    shop.save()
    if shop.timezone != old_timezone:
        # Agent schedules run in the shop's timezone: let the triggers app re-sync them.
        signals.shop_timezone_changed.send(sender=Shop, shop=shop)


# ---------------------------------------------------------------- compliance (GDPR)
@handler("customers/data_request")
def on_customers_data_request(event: WebhookEvent) -> None:
    """
    A customer asked the merchant for their data. We store very little customer
    data (only what appears in agent run logs). Record it and notify staff, who
    must reply to the merchant within 30 days.
    """
    audit(
        "compliance.customers_data_request",
        shop=event.shop,
        customer_id=(event.payload.get("customer") or {}).get("id"),
        data_request_id=(event.payload.get("data_request") or {}).get("id"),
    )
    signals.customers_data_requested.send(sender=Shop, shop=event.shop, payload=event.payload)


@handler("customers/redact")
def on_customers_redact(event: WebhookEvent) -> None:
    """Delete data about one customer. Later phases delete matching run logs/messages."""
    audit(
        "compliance.customers_redact",
        shop=event.shop,
        customer_id=(event.payload.get("customer") or {}).get("id"),
    )
    signals.customers_redact_requested.send(sender=Shop, shop=event.shop, payload=event.payload)


@handler("shop/redact")
def on_shop_redact(event: WebhookEvent) -> None:
    """Sent 48 h after uninstall: delete everything we hold about the shop."""
    shop = event.shop
    if shop is None:
        log.info("compliance.shop_redact_unknown_shop", domain=event.shop_domain)
        return
    if shop.is_installed:
        # Merchant re-installed in the meantime; their data is in use again.
        log.warning("compliance.shop_redact_ignored_reinstalled", shop_id=shop.pk)
        return
    services.purge_shop(shop, reason="shop/redact webhook")
