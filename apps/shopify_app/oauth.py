"""
Authorization code grant OAuth with *expiring* offline tokens.

Shopify requires public apps to use expiring offline tokens (new apps from
2026-04-01, all apps from 2027-01-01). Flow:

1. `build_authorize_url()` -> merchant approves scopes on Shopify.
2. Shopify redirects to /shopify/callback/ with `code`.
3. `exchange_code()` -> access token (1 h) + refresh token (90 days).
4. Before the access token expires, `refresh_access_token()` returns a NEW
   access token AND a NEW refresh token (the old refresh token is retired).

No offline token is ever "online": we never send `grant_options[]=per-user`.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx
from django.conf import settings
from pydantic import BaseModel

from apps.core.logging import get_logger

log = get_logger(__name__)
TIMEOUT = httpx.Timeout(15.0)
MAX_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 1.0  # tests set this to 0


class OAuthError(Exception):
    """Shopify rejected a token request."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ReauthRequired(OAuthError):
    """The refresh token is invalid/expired: the merchant must go through OAuth again."""


class TokenResponse(BaseModel):
    access_token: str
    scope: str = ""
    expires_in: int | None = None
    refresh_token: str | None = None
    refresh_token_expires_in: int | None = None


def redirect_uri() -> str:
    return f"{settings.APP_URL}/shopify/callback/"


def build_authorize_url(shop_domain: str, scopes: list[str], state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.SHOPIFY_API_KEY,
            "scope": ",".join(scopes),
            "redirect_uri": redirect_uri(),
            "state": state,
        }
    )
    return f"https://{shop_domain}/admin/oauth/authorize?{query}"


def _token_request(shop_domain: str, data: dict[str, str]) -> TokenResponse:
    url = f"https://{shop_domain}/admin/oauth/access_token"
    body = {
        "client_id": settings.SHOPIFY_API_KEY,
        "client_secret": settings.SHOPIFY_API_SECRET,
        **data,
    }
    # Shopify says: retry timeouts, network errors and 5xx with the SAME refresh token.
    resp: httpx.Response | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(
                url, data=body, headers={"Accept": "application/json"}, timeout=TIMEOUT
            )
        except httpx.HTTPError as exc:
            if attempt == MAX_ATTEMPTS:
                raise OAuthError(f"Network error talking to {shop_domain}: {exc}") from exc
            log.warning("oauth.retry", shop=shop_domain, attempt=attempt, error=str(exc))
            time.sleep(RETRY_SLEEP_SECONDS * attempt)
            continue
        if resp.status_code < 500 or attempt == MAX_ATTEMPTS:
            break
        log.warning("oauth.retry", shop=shop_domain, attempt=attempt, status=resp.status_code)
        time.sleep(RETRY_SLEEP_SECONDS * attempt)
    assert resp is not None

    if resp.status_code == 401 or (
        resp.status_code == 400 and data.get("grant_type") == "refresh_token"
    ):
        raise ReauthRequired(
            "Shopify rejected the refresh token; the merchant must re-authorize.",
            status_code=resp.status_code,
            body=resp.text[:500],
        )
    if resp.status_code != 200:
        raise OAuthError(
            f"Token request to {shop_domain} failed with HTTP {resp.status_code}",
            status_code=resp.status_code,
            body=resp.text[:500],
        )
    return TokenResponse.model_validate(resp.json())


def exchange_code(shop_domain: str, code: str) -> TokenResponse:
    """Swap the one-time `code` from the callback for an expiring offline token."""
    log.info("oauth.exchange_code", shop=shop_domain)
    return _token_request(shop_domain, {"code": code, "expiring": "1"})


def refresh_access_token(shop_domain: str, refresh_token: str) -> TokenResponse:
    log.info("oauth.refresh_token", shop=shop_domain)
    return _token_request(
        shop_domain, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
