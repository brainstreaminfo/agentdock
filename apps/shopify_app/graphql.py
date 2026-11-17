"""
Shopify Admin GraphQL client.

    client = ShopifyGraphQL(shop)
    result = client.execute(load_query("shop_info"))
    result.data["shop"]["name"]

What it handles for you:
* gets a fresh access token (refreshing expiring tokens) — see `tokens.py`
* retries once with a forced token refresh on HTTP 401
* rate limits: Shopify's GraphQL API uses a "cost" bucket. When a query is
  THROTTLED we wait exactly long enough for the bucket to refill, then retry.
* retries 5xx / network errors with backoff
* raises `ShopifyGraphQLError` with Shopify's error messages otherwise

Every call logs `shopify.graphql` with the operation name, duration and cost.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings

from apps.core.logging import get_logger
from apps.shops.models import Shop

from .tokens import get_access_token

log = get_logger(__name__)

MAX_ATTEMPTS = 5
TIMEOUT = httpx.Timeout(30.0)
_OPERATION_RE = re.compile(r"\b(query|mutation)\s+(\w+)")


class ShopifyGraphQLError(Exception):
    def __init__(self, message: str, *, errors: Any = None, status_code: int | None = None):
        super().__init__(message)
        self.errors = errors
        self.status_code = status_code


class ShopifyThrottledError(ShopifyGraphQLError):
    pass


@dataclass
class GraphQLResult:
    data: dict[str, Any]
    extensions: dict[str, Any] = field(default_factory=dict)

    @property
    def cost(self) -> dict[str, Any]:
        return dict(self.extensions.get("cost") or {})


def _operation_name(document: str) -> str:
    match = _OPERATION_RE.search(document)
    return match.group(2) if match else "anonymous"


def _throttle_wait_seconds(cost: dict[str, Any]) -> float:
    """How long until the bucket has enough points for this query."""
    status = cost.get("throttleStatus") or {}
    requested = float(cost.get("requestedQueryCost") or 0)
    available = float(status.get("currentlyAvailable") or 0)
    restore_rate = float(status.get("restoreRate") or 50) or 50
    return max(1.0, (requested - available) / restore_rate)


class ShopifyGraphQL:
    def __init__(
        self,
        shop: Shop,
        *,
        api_version: str | None = None,
        http: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.shop = shop
        self.api_version = api_version or settings.SHOPIFY_API_VERSION
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._sleep = sleep  # injectable so tests don't actually wait

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop.domain}/admin/api/{self.api_version}/graphql.json"

    def execute(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> GraphQLResult:
        """
        Run a query/mutation. If `document` contains several operations (our tool
        catalog keeps one .graphql file per domain), pass `operation_name`.
        """
        op = operation_name or _operation_name(document)
        body: dict[str, Any] = {"query": document, "variables": variables or {}}
        if operation_name:
            body["operationName"] = operation_name
        force_refresh = False
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = get_access_token(self.shop, force_refresh=force_refresh)
            started = time.monotonic()
            try:
                resp = self._http.post(
                    self.endpoint,
                    json=body,
                    headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("shopify.graphql.network_error", op=op, attempt=attempt, error=str(exc))
                self._sleep(min(2**attempt, 30))
                continue
            duration_ms = int((time.monotonic() - started) * 1000)

            if resp.status_code == 401 and not force_refresh:
                log.warning("shopify.graphql.unauthorized_retry", op=op)
                force_refresh = True
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2))
                log.warning("shopify.graphql.http_429", op=op, wait=wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 500:
                last_error = ShopifyGraphQLError(
                    f"Shopify returned HTTP {resp.status_code}", status_code=resp.status_code
                )
                log.warning("shopify.graphql.server_error", op=op, status=resp.status_code)
                self._sleep(min(2**attempt, 30))
                continue
            if resp.status_code != 200:
                raise ShopifyGraphQLError(
                    f"Shopify returned HTTP {resp.status_code}: {resp.text[:300]}",
                    status_code=resp.status_code,
                )

            payload = resp.json()
            extensions = payload.get("extensions") or {}
            errors = payload.get("errors")
            if errors and _is_throttled(errors):
                wait = _throttle_wait_seconds(extensions.get("cost") or {})
                log.warning("shopify.graphql.throttled", op=op, wait=round(wait, 2))
                last_error = ShopifyThrottledError("Throttled by Shopify", errors=errors)
                self._sleep(wait)
                continue

            log.info(
                "shopify.graphql",
                op=op,
                duration_ms=duration_ms,
                cost=(extensions.get("cost") or {}).get("actualQueryCost"),
            )
            if errors:
                messages = "; ".join(str(e.get("message", e)) for e in errors)
                raise ShopifyGraphQLError(f"GraphQL error in {op}: {messages}", errors=errors)
            return GraphQLResult(data=payload.get("data") or {}, extensions=extensions)

        raise ShopifyGraphQLError(
            f"{op} failed after {MAX_ATTEMPTS} attempts: {last_error}"
        ) from last_error


def _is_throttled(errors: list[dict[str, Any]]) -> bool:
    return any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
