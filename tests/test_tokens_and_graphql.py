"""Expiring-token refresh and the GraphQL client (throttling, 401, errors)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from apps.shopify_app import oauth
from apps.shopify_app.graphql import ShopifyGraphQL, ShopifyGraphQLError
from apps.shopify_app.tokens import ShopNotInstalled, get_access_token
from tests.factories import ShopFactory

pytestmark = pytest.mark.django_db
QUERY = "query ShopName { shop { name } }"


def expire(shop) -> None:
    shop.access_token_expires_at = timezone.now() + timedelta(seconds=30)
    shop.save()


def test_fresh_token_not_refreshed(shop, mock_token_endpoint) -> None:
    route = mock_token_endpoint()
    assert get_access_token(shop) == "shpat_existing"
    assert not route.called


def test_expiring_token_is_refreshed_and_rotated(shop, mock_token_endpoint) -> None:
    expire(shop)
    route = mock_token_endpoint(access="shpat_refreshed", refresh="shprt_rotated")

    assert get_access_token(shop) == "shpat_refreshed"

    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body and "refresh_token=shprt_existing" in body
    shop.refresh_from_db()
    assert shop.decrypted_refresh_token() == "shprt_rotated"
    assert shop.access_token_expires_at > timezone.now() + timedelta(minutes=50)


def test_revoked_refresh_token_clears_tokens(shop, mock_token_endpoint) -> None:
    expire(shop)
    mock_token_endpoint(status=401)
    with pytest.raises(oauth.ReauthRequired):
        get_access_token(shop)
    shop.refresh_from_db()
    assert shop.access_token_enc == "" and not shop.is_installed


def test_refresh_retries_server_errors(shop, respx_mock) -> None:
    expire(shop)
    route = respx_mock.post(f"https://{shop.domain}/admin/oauth/access_token").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(
                200, json={"access_token": "shpat_ok", "expires_in": 3600, "refresh_token": "r2"}
            ),
        ]
    )
    assert get_access_token(shop) == "shpat_ok"
    assert route.call_count == 2


def test_no_token_raises(db) -> None:
    shop = ShopFactory(tokens=False)
    with pytest.raises(ShopNotInstalled):
        get_access_token(shop)


# ------------------------------------------------------------------ GraphQL client
def test_graphql_success_sends_token(shop, mock_graphql) -> None:
    route = mock_graphql({"data": {"shop": {"name": "X"}}})
    result = ShopifyGraphQL(shop, sleep=lambda s: None).execute(QUERY)
    assert result.data == {"shop": {"name": "X"}}
    assert route.calls.last.request.headers["X-Shopify-Access-Token"] == "shpat_existing"


def test_graphql_throttled_waits_for_bucket_then_retries(shop, mock_graphql) -> None:
    throttled = {
        "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
        "extensions": {
            "cost": {
                "requestedQueryCost": 300,
                "throttleStatus": {"currentlyAvailable": 100, "restoreRate": 100},
            }
        },
    }
    route = mock_graphql(throttled, {"data": {"ok": True}})
    waits: list[float] = []
    result = ShopifyGraphQL(shop, sleep=waits.append).execute(QUERY)
    assert result.data == {"ok": True}
    assert route.call_count == 2
    assert waits == [2.0]  # (300 - 100) / 100


def test_graphql_401_forces_token_refresh(shop, mock_graphql, mock_token_endpoint) -> None:
    mock_token_endpoint(access="shpat_after_401")
    route = mock_graphql(httpx.Response(401), {"data": {"ok": True}})
    ShopifyGraphQL(shop, sleep=lambda s: None).execute(QUERY)
    assert route.calls.last.request.headers["X-Shopify-Access-Token"] == "shpat_after_401"


def test_graphql_errors_raise_with_messages(shop, mock_graphql) -> None:
    mock_graphql({"errors": [{"message": "Field 'nope' doesn't exist"}]})
    with pytest.raises(ShopifyGraphQLError, match="Field 'nope'"):
        ShopifyGraphQL(shop, sleep=lambda s: None).execute(QUERY)


def test_graphql_gives_up_after_repeated_server_errors(shop, mock_graphql) -> None:
    mock_graphql(*[httpx.Response(502)] * 5)
    with pytest.raises(ShopifyGraphQLError, match="failed after 5 attempts"):
        ShopifyGraphQL(shop, sleep=lambda s: None).execute(QUERY)
