"""Opening the app from Shopify Admin (spec §5.1, §5.4)."""

from __future__ import annotations

import time

import pytest

from apps.accounts.models import Membership
from tests.conftest import SHOP_DOMAIN, signed_query
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def launch(client, **params):
    return client.get("/shopify/launch/", signed_query(shop=SHOP_DOMAIN, host="aG9zdA", **params))


def test_unknown_shop_goes_to_oauth(client) -> None:
    resp = launch(client)
    assert resp.status_code == 302
    assert resp["Location"].startswith("/shopify/auth/?shop=test-store.myshopify.com")


def test_bad_hmac_rejected(client, shop) -> None:
    params = signed_query(shop=SHOP_DOMAIN)
    params["hmac"] = "0" * 64
    assert client.get("/shopify/launch/", params).status_code == 403


def test_invalid_shop_rejected(client) -> None:
    assert client.get("/shopify/launch/", signed_query(shop="evil.com")).status_code == 400


def test_installed_shop_logs_in_owner(client, shop, owner) -> None:
    resp = launch(client)
    assert resp["Location"] == f"/s/{shop.pk}/"
    assert int(client.session["_auth_user_id"]) == owner.pk
    # And our own UI renders (non-embedded).
    page = client.get(resp["Location"])
    assert page.status_code == 200
    assert b"Dashboard" in page.content


def test_existing_member_session_is_kept(client, shop, owner) -> None:
    member = UserFactory()
    Membership.objects.create(user=member, shop=shop, role="MEMBER")
    client.force_login(member)
    launch(client)
    assert int(client.session["_auth_user_id"]) == member.pk


def test_replayed_launch_url_does_not_log_in(client, shop, owner) -> None:
    params = signed_query(shop=SHOP_DOMAIN)
    client.get("/shopify/launch/", params)
    client.logout()

    resp = client.get("/shopify/launch/", params)

    assert resp["Location"].startswith("/shopify/auth/")
    assert "_auth_user_id" not in client.session


def test_stale_launch_url_goes_to_oauth(client, shop, owner) -> None:
    resp = launch(client, timestamp=str(int(time.time()) - 3600))
    assert resp["Location"].startswith("/shopify/auth/")
    assert "_auth_user_id" not in client.session


def test_missing_base_scope_triggers_oauth(client, shop, owner) -> None:
    shop.granted_scopes = []
    shop.save()
    assert launch(client)["Location"].startswith("/shopify/auth/")
