"""Install / re-auth / scope-upgrade via authorization code grant (spec §5)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from apps.accounts.models import Membership, User
from apps.core.models import AuditLog
from apps.shops.models import Shop
from tests.conftest import SHOP_DOMAIN, shop_info_response, signed_query

pytestmark = pytest.mark.django_db


def start_oauth(client, **params):
    resp = client.get("/shopify/auth/", {"shop": SHOP_DOMAIN, **params})
    assert resp.status_code == 302
    url = urlparse(resp["Location"])
    return url, {k: v[0] for k, v in parse_qs(url.query).items()}


def finish_oauth(client, state: str, **extra):
    return client.get(
        "/shopify/callback/", signed_query(shop=SHOP_DOMAIN, code="the-code", state=state, **extra)
    )


def test_auth_redirects_to_shopify_with_base_scopes(client) -> None:
    url, q = start_oauth(client)
    assert url.netloc == SHOP_DOMAIN and url.path == "/admin/oauth/authorize"
    assert q["client_id"] == "test-api-key"
    assert q["scope"] == "read_products"
    assert q["redirect_uri"] == "https://app.test/shopify/callback/"
    assert "grant_options[]" not in q  # offline token, never per-user
    assert client.session["shopify_oauth_state"] == q["state"]


def test_auth_rejects_bad_shop_and_scopes(client) -> None:
    assert client.get("/shopify/auth/", {"shop": "evil.com"}).status_code == 400
    resp = client.get("/shopify/auth/", {"shop": SHOP_DOMAIN, "scopes": "admin_everything"})
    assert resp.status_code == 400


def test_fresh_install(client, mock_token_endpoint, mock_graphql) -> None:
    token_route = mock_token_endpoint()
    mock_graphql(shop_info_response())
    _, q = start_oauth(client)

    resp = finish_oauth(client, q["state"])

    shop = Shop.objects.get(domain=SHOP_DOMAIN)
    assert resp.status_code == 302
    assert resp["Location"] == f"/s/{shop.pk}/welcome/"
    # Expiring offline token requested and stored encrypted.
    assert b"expiring=1" in token_route.calls.last.request.content
    assert shop.decrypted_access_token() == "shpat_new"
    assert shop.decrypted_refresh_token() == "shprt_new"
    assert "shpat_new" not in shop.access_token_enc
    assert shop.access_token_expires_at is not None
    # Shop info synced.
    assert shop.name == "Test Store" and shop.timezone == "Europe/London"
    assert shop.is_dev_store and shop.granted_scopes == ["read_products"]
    # Owner user created and logged in.
    user = User.objects.get(email="owner@test-store.com")
    assert Membership.objects.get(user=user, shop=shop).role == "OWNER"
    assert int(client.session["_auth_user_id"]) == user.pk
    assert AuditLog.objects.filter(action="shop.installed", shop=shop).exists()


def test_callback_rejects_bad_hmac(client) -> None:
    _, q = start_oauth(client)
    params = signed_query(shop=SHOP_DOMAIN, code="c", state=q["state"])
    params["code"] = "tampered"
    assert client.get("/shopify/callback/", params).status_code == 403
    assert not Shop.objects.exists()


def test_callback_rejects_wrong_state(client) -> None:
    start_oauth(client)
    resp = finish_oauth(client, "not-the-state")
    assert resp.status_code == 400
    assert b"Try again" in resp.content
    assert not Shop.objects.exists()


def test_callback_without_started_session(client) -> None:
    # State can't be verified -> no install (CSRF protection).
    assert finish_oauth(client, "anything").status_code == 400


def test_scope_upgrade_requests_union(client, shop, owner) -> None:
    client.force_login(owner)
    shop.granted_scopes = ["read_products", "read_orders"]
    shop.save()
    _, q = start_oauth(client, scopes="write_products", next=f"/s/{shop.pk}/settings/")
    assert q["scope"] == "read_orders,write_products"
    assert client.session["shopify_post_auth_redirect"] == f"/s/{shop.pk}/settings/"


def test_scope_upgrade_callback_resumes_next(
    client, shop, owner, mock_token_endpoint, mock_graphql
) -> None:
    client.force_login(owner)
    mock_token_endpoint(scope="write_products", access="shpat_upgraded")
    mock_graphql(shop_info_response(scopes=["write_products"], email=shop.email))
    _, q = start_oauth(client, scopes="write_products", next=f"/s/{shop.pk}/settings/")

    resp = finish_oauth(client, q["state"])

    assert resp["Location"] == f"/s/{shop.pk}/settings/"
    shop.refresh_from_db()
    assert shop.granted_scopes == ["write_products"]
    assert shop.has_scopes(["read_products", "write_products"])
    assert shop.decrypted_access_token() == "shpat_upgraded"


def test_open_redirect_blocked(client) -> None:
    start_oauth(client, next="https://evil.com/")
    assert "shopify_post_auth_redirect" not in client.session


def test_connect_another_store_adds_membership_for_logged_in_user(
    client, user, mock_token_endpoint, mock_graphql
) -> None:
    client.force_login(user)
    mock_token_endpoint()
    mock_graphql(shop_info_response(email="someone-else@shop.com"))
    _, q = start_oauth(client)
    finish_oauth(client, q["state"])

    shop = Shop.objects.get(domain=SHOP_DOMAIN)
    assert Membership.objects.get(user=user, shop=shop).role == "OWNER"
    assert not User.objects.filter(email="someone-else@shop.com").exists()


def test_reinstall_after_uninstall(client, shop, owner, mock_token_endpoint, mock_graphql) -> None:
    from django.utils import timezone

    shop.clear_tokens()
    shop.uninstalled_at = timezone.now()
    shop.save()
    mock_token_endpoint()
    mock_graphql(shop_info_response(email=owner.email))
    _, q = start_oauth(client)

    resp = finish_oauth(client, q["state"])

    shop.refresh_from_db()
    assert shop.uninstalled_at is None and shop.is_installed
    assert resp["Location"] == f"/s/{shop.pk}/welcome/"


def test_install_failure_shows_error(client, mock_token_endpoint) -> None:
    mock_token_endpoint(status=400)
    _, q = start_oauth(client)
    resp = finish_oauth(client, q["state"])
    assert resp.status_code == 502
    assert b"couldn" in resp.content
