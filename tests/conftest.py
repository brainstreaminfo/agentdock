"""
Shared pytest fixtures.

Shopify is never called for real: `respx` intercepts httpx requests. Helpers:
* `signed_query(**params)` -> query dict with a valid Shopify `hmac`
* `mock_token_endpoint(...)` / `mock_graphql(...)` -> respx routes
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
import respx
from django.core.cache import cache

from apps.shopify_app import oauth
from apps.shopify_app.hmac import compute_query_hmac, compute_webhook_hmac
from tests.factories import MembershipFactory, ShopFactory, UserFactory

SHOP_DOMAIN = "test-store.myshopify.com"


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "RETRY_SLEEP_SECONDS", 0)
    cache.clear()


@pytest.fixture
def respx_mock() -> Any:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


# ------------------------------------------------------------------ signing helpers
def signed_query(**params: str) -> dict[str, str]:
    params.setdefault("timestamp", str(int(time.time())))
    params["hmac"] = compute_query_hmac({k: [v] for k, v in params.items()})
    return params


def webhook_headers(body: bytes, topic: str, shop: str = SHOP_DOMAIN, event_id: str = "evt-1"):
    return {
        "HTTP_X_SHOPIFY_HMAC_SHA256": compute_webhook_hmac(body),
        "HTTP_X_SHOPIFY_TOPIC": topic,
        "HTTP_X_SHOPIFY_SHOP_DOMAIN": shop,
        "HTTP_X_SHOPIFY_EVENT_ID": event_id,
        "HTTP_X_SHOPIFY_API_VERSION": "2026-10",
    }


@pytest.fixture
def post_webhook(client: Any):
    def _post(
        topic: str, payload: dict[str, Any], shop: str = SHOP_DOMAIN, event_id: str = "evt-1"
    ):
        body = json.dumps(payload).encode()
        return client.post(
            "/shopify/webhooks/",
            data=body,
            content_type="application/json",
            **webhook_headers(body, topic, shop, event_id),
        )

    return _post


# ------------------------------------------------------------------ Shopify mocks
def token_response(
    scope: str = "read_products", access: str = "shpat_new", refresh: str = "shprt_new"
) -> dict[str, Any]:
    return {
        "access_token": access,
        "scope": scope,
        "expires_in": 3600,
        "refresh_token": refresh,
        "refresh_token_expires_in": 7776000,
    }


def shop_info_response(
    scopes: list[str] | None = None, email: str = "owner@test-store.com"
) -> dict[str, Any]:
    return {
        "data": {
            "shop": {
                "id": "gid://shopify/Shop/1",
                "name": "Test Store",
                "email": email,
                "myshopifyDomain": SHOP_DOMAIN,
                "currencyCode": "GBP",
                "ianaTimezone": "Europe/London",
                "plan": {
                    "publicDisplayName": "Development",
                    "partnerDevelopment": True,
                    "shopifyPlus": False,
                },
            },
            "currentAppInstallation": {
                "accessScopes": [{"handle": s} for s in (scopes or ["read_products"])]
            },
        },
        "extensions": {"cost": {"requestedQueryCost": 2, "actualQueryCost": 2}},
    }


@pytest.fixture
def mock_token_endpoint(respx_mock: Any):
    def _mock(domain: str = SHOP_DOMAIN, status: int = 200, **kwargs: Any) -> Any:
        return respx_mock.post(f"https://{domain}/admin/oauth/access_token").mock(
            return_value=httpx.Response(status, json=token_response(**kwargs))
        )

    return _mock


@pytest.fixture
def mock_graphql(respx_mock: Any):
    def _mock(*responses: dict[str, Any] | httpx.Response, domain: str = SHOP_DOMAIN) -> Any:
        """One response = returned for every call; several = returned in order."""
        side = [
            r if isinstance(r, httpx.Response) else httpx.Response(200, json=r) for r in responses
        ]
        route = respx_mock.post(f"https://{domain}/admin/api/2026-10/graphql.json")
        if len(side) == 1:
            return route.mock(return_value=side[0])
        return route.mock(side_effect=side)

    return _mock


# ------------------------------------------------------------------ model fixtures
@pytest.fixture
def shop(db: Any):
    return ShopFactory(domain=SHOP_DOMAIN)


@pytest.fixture
def owner(shop: Any):
    return MembershipFactory(shop=shop, role="OWNER").user


@pytest.fixture
def user(db: Any):
    return UserFactory()
