"""Webhook endpoint, uninstall, compliance topics, purge (spec §5.7, §12)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.models import AuditLog
from apps.shopify_app import signals
from apps.shopify_app.models import WebhookEvent
from apps.shopify_app.tasks import purge_uninstalled_shops
from apps.shops.models import Shop
from tests.factories import ShopFactory

pytestmark = pytest.mark.django_db


def test_bad_hmac_returns_401(client) -> None:
    resp = client.post(
        "/shopify/webhooks/",
        data=b"{}",
        content_type="application/json",
        HTTP_X_SHOPIFY_HMAC_SHA256="bad",
        HTTP_X_SHOPIFY_TOPIC="app/uninstalled",
    )
    assert resp.status_code == 401
    assert not WebhookEvent.objects.exists()


def test_webhooks_are_csrf_exempt(post_webhook, shop) -> None:
    # The Django test client skips CSRF by default, so check the flag directly too.
    from apps.shopify_app.views import webhook

    assert getattr(webhook, "csrf_exempt", False)
    assert post_webhook("shop/update", {"name": "New name"}).status_code == 200


def test_app_uninstalled(post_webhook, shop) -> None:
    received = []
    signals.shop_uninstalled.connect(lambda **kw: received.append(kw["shop"].pk), weak=False)

    assert post_webhook("app/uninstalled", {"id": 1}).status_code == 200

    shop.refresh_from_db()
    assert shop.uninstalled_at is not None
    assert shop.access_token_enc == "" and shop.refresh_token_enc == ""
    assert not shop.is_installed
    assert received == [shop.pk]
    event = WebhookEvent.objects.get()
    assert event.status == WebhookEvent.Status.PROCESSED and event.shop == shop


def test_uninstalled_shop_is_inaccessible(client, post_webhook, shop, owner) -> None:
    client.force_login(owner)
    post_webhook("app/uninstalled", {})
    assert client.get(f"/s/{shop.pk}/").status_code == 404


def test_duplicate_deliveries_processed_once(post_webhook, shop) -> None:
    post_webhook("shop/update", {"name": "A"}, event_id="same")
    post_webhook("shop/update", {"name": "B"}, event_id="same")
    assert WebhookEvent.objects.count() == 1
    shop.refresh_from_db()
    assert shop.name == "A"


def test_unknown_topic_ignored(post_webhook, shop) -> None:
    post_webhook("products/update", {"id": 1})
    assert WebhookEvent.objects.get().status == WebhookEvent.Status.IGNORED


def test_scopes_update(post_webhook, shop) -> None:
    post_webhook(
        "app/scopes_update", {"previous": ["read_products"], "current": ["write_products"]}
    )
    shop.refresh_from_db()
    assert shop.granted_scopes == ["write_products"]


def test_customers_data_request_and_redact_are_recorded(post_webhook, shop) -> None:
    payload = {"shop_domain": shop.domain, "customer": {"id": 42}, "data_request": {"id": 7}}
    assert post_webhook("customers/data_request", payload, event_id="a").status_code == 200
    assert post_webhook("customers/redact", payload, event_id="b").status_code == 200
    actions = set(AuditLog.objects.filter(shop=shop).values_list("action", flat=True))
    assert {"compliance.customers_data_request", "compliance.customers_redact"} <= actions


def test_shop_redact_deletes_uninstalled_shop(post_webhook, shop, owner) -> None:
    post_webhook("app/uninstalled", {}, event_id="u")
    post_webhook("shop/redact", {"shop_domain": shop.domain}, event_id="r")
    assert not Shop.objects.filter(pk=shop.pk).exists()
    assert not WebhookEvent.objects.filter(shop_domain=shop.domain).exists()


def test_shop_redact_ignored_if_reinstalled(post_webhook, shop) -> None:
    post_webhook("shop/redact", {"shop_domain": shop.domain})
    assert Shop.objects.filter(pk=shop.pk).exists()


def test_shop_redact_for_unknown_shop_is_ok(post_webhook) -> None:
    resp = post_webhook("shop/redact", {}, shop="never-installed.myshopify.com")
    assert resp.status_code == 200


def test_purge_after_30_days(db) -> None:
    old = ShopFactory(uninstalled_at=timezone.now() - timedelta(days=31), tokens=False)
    recent = ShopFactory(uninstalled_at=timezone.now() - timedelta(days=5), tokens=False)
    active = ShopFactory()
    assert purge_uninstalled_shops() == 1
    remaining = set(Shop.objects.values_list("pk", flat=True))
    assert remaining == {recent.pk, active.pk}
    assert old.pk not in remaining
