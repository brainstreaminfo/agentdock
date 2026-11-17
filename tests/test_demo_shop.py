"""The create_demo_shop command (standalone testing without a Shopify app)."""

from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.models import Membership
from apps.shops.models import Shop
from apps.tools.sync import sync_tools
from tests.factories import ShopFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_creates_installed_shop_with_all_scopes_and_owner(client) -> None:
    sync_tools()
    user = UserFactory(email="me@example.com")
    call_command("create_demo_shop", email="me@example.com", timezone="Asia/Kolkata")

    shop = Shop.objects.get(domain="demo-store.myshopify.com")
    assert shop.is_installed and shop.timezone == "Asia/Kolkata"
    assert shop.has_scopes(["write_products", "read_orders", "write_discounts"])
    assert Membership.objects.get(user=user, shop=shop).role == "OWNER"

    client.force_login(user)
    assert client.get(f"/s/{shop.pk}/marketplace/").status_code == 200
    call_command("create_demo_shop", email="me@example.com")  # safe to run again
    assert Shop.objects.count() == 1


def test_refuses_to_touch_a_real_store() -> None:
    ShopFactory(domain="demo-store.myshopify.com")
    with pytest.raises(CommandError, match="real store"):
        call_command("create_demo_shop", email="me@example.com")


def test_demo_store_never_calls_shopify(respx_mock) -> None:
    from apps.shopify_app.graphql import ShopifyGraphQL, ShopifyGraphQLError
    from apps.triggers.subscriptions import sync_webhook_subscriptions

    call_command("create_demo_shop", email="me@example.com")
    shop = Shop.objects.get()
    assert shop.is_demo
    with pytest.raises(ShopifyGraphQLError, match="demo store"):
        ShopifyGraphQL(shop).execute("query ShopInfo { shop { name } }")
    assert sync_webhook_subscriptions(shop).created == []
    assert not respx_mock.calls
