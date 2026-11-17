"""
Request verification for everything Shopify sends us.

* Query-string HMAC  -> app launch from Shopify Admin and the OAuth callback.
* Body HMAC (base64) -> webhooks (header `X-Shopify-Hmac-Sha256`).
* Shop domain check  -> every `shop` parameter before we use it in a URL.

All comparisons are constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import re
import time
from collections.abc import Mapping, Sequence

from django.conf import settings

# Spec §5.1. We lower-case first, so upper-case input is accepted but normalised.
SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


def normalize_shop_domain(value: str | None) -> str | None:
    """Return the lower-cased domain if it is a valid `*.myshopify.com` host, else None."""
    if not value:
        return None
    value = value.strip().lower()
    return value if SHOP_DOMAIN_RE.match(value) else None


def _secret() -> bytes:
    return settings.SHOPIFY_API_SECRET.encode()


def _escape(value: str, *, is_key: bool) -> str:
    value = value.replace("%", "%25").replace("&", "%26")
    return value.replace("=", "%3D") if is_key else value


def build_query_message(params: Mapping[str, Sequence[str]]) -> str:
    """
    Shopify's canonical string: drop `hmac`/`signature`, sort by key, join `k=v`
    with `&`. Repeated keys (e.g. `ids[]=1&ids[]=2`) become `ids=["1", "2"]`.
    """
    parts = []
    for key in sorted(params):
        if key in ("hmac", "signature"):
            continue
        values = list(params[key])
        clean_key = _escape(key[:-2] if key.endswith("[]") else key, is_key=True)
        if key.endswith("[]") or len(values) > 1:
            value = "[" + ", ".join(f'"{_escape(v, is_key=False)}"' for v in values) + "]"
        else:
            value = _escape(values[0] if values else "", is_key=False)
        parts.append(f"{clean_key}={value}")
    return "&".join(parts)


def compute_query_hmac(params: Mapping[str, Sequence[str]]) -> str:
    message = build_query_message(params).encode()
    return _hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def verify_query_hmac(params: Mapping[str, Sequence[str]]) -> bool:
    received = (params.get("hmac") or [""])[0]
    if not received:
        return False
    return _hmac.compare_digest(compute_query_hmac(params), received)


def is_fresh_timestamp(params: Mapping[str, Sequence[str]], max_age_seconds: int) -> bool:
    """True if the `timestamp` param is within `max_age_seconds` of now (both directions)."""
    raw = (params.get("timestamp") or [""])[0]
    try:
        ts = int(raw)
    except ValueError:
        return False
    return abs(time.time() - ts) <= max_age_seconds


def compute_webhook_hmac(body: bytes) -> str:
    digest = _hmac.new(_secret(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_webhook_hmac(body: bytes, header_value: str | None) -> bool:
    if not header_value:
        return False
    return _hmac.compare_digest(compute_webhook_hmac(body), header_value)


def querydict_to_lists(query: Mapping[str, object]) -> dict[str, list[str]]:
    """Convert Django's QueryDict to {key: [values]} for the functions above."""
    getlist = getattr(query, "getlist", None)
    if getlist is None:
        return {k: [str(v)] for k, v in query.items()}
    return {k: list(getlist(k)) for k in query}
