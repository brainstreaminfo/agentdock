"""HMAC, shop-domain validation, scopes, encryption and log redaction."""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from apps.core import crypto
from apps.core.logging import redact_text, redact_value
from apps.shopify_app import hmac, scopes
from tests.conftest import signed_query


# ------------------------------------------------------------------ query HMAC
def test_query_hmac_round_trip() -> None:
    q = signed_query(shop="a.myshopify.com", host="abc")
    assert hmac.verify_query_hmac({k: [v] for k, v in q.items()})


def test_query_hmac_rejects_tampering() -> None:
    q = signed_query(shop="a.myshopify.com")
    q["shop"] = "evil.myshopify.com"
    assert not hmac.verify_query_hmac({k: [v] for k, v in q.items()})


def test_query_hmac_missing() -> None:
    assert not hmac.verify_query_hmac({"shop": ["a.myshopify.com"]})


def test_query_message_matches_shopify_format() -> None:
    # Example structure from Shopify's docs: sorted, hmac removed, arrays as JSON-ish lists.
    msg = hmac.build_query_message(
        {"shop": ["s.myshopify.com"], "hmac": ["x"], "ids[]": ["1", "2"], "code": ["c"]}
    )
    assert msg == 'code=c&ids=["1", "2"]&shop=s.myshopify.com'


def test_timestamp_freshness() -> None:
    now = int(time.time())
    assert hmac.is_fresh_timestamp({"timestamp": [str(now)]}, 300)
    assert not hmac.is_fresh_timestamp({"timestamp": [str(now - 3600)]}, 300)
    assert not hmac.is_fresh_timestamp({"timestamp": ["nope"]}, 300)


def test_webhook_hmac() -> None:
    body = b'{"id": 1}'
    good = hmac.compute_webhook_hmac(body)
    assert hmac.verify_webhook_hmac(body, good)
    assert not hmac.verify_webhook_hmac(body + b" ", good)
    assert not hmac.verify_webhook_hmac(body, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("my-store.myshopify.com", "my-store.myshopify.com"),
        ("My-Store.MyShopify.com", "my-store.myshopify.com"),
        ("my-store.myshopify.com.evil.com", None),
        ("evil.com/my-store.myshopify.com", None),
        ("-bad.myshopify.com", None),
        ("my_store.myshopify.com", None),
        ("", None),
        (None, None),
    ],
)
def test_shop_domain_validation(value: str | None, expected: str | None) -> None:
    assert hmac.normalize_shop_domain(value) == expected


# ------------------------------------------------------------------ scopes
def test_write_implies_read() -> None:
    assert scopes.satisfies(["read_products"], ["write_products"])
    assert not scopes.satisfies(["write_products"], ["read_products"])
    assert scopes.missing(["read_orders", "read_products"], ["write_products"]) == ["read_orders"]


def test_union_drops_redundant_reads() -> None:
    assert scopes.union(["read_products"], "write_products,read_orders", []) == [
        "read_orders",
        "write_products",
    ]


def test_scope_validation_and_labels() -> None:
    assert scopes.is_valid("write_products")
    assert scopes.is_valid("unauthenticated_read_product_listings")
    assert not scopes.is_valid("admin")
    assert scopes.describe("write_products") == "Edit your products and collections"
    assert scopes.describe("read_gift_cards") == "View your gift cards"


# ------------------------------------------------------------------ encryption
def test_encrypt_round_trip() -> None:
    token = crypto.encrypt("shpat_secret")
    assert "shpat_secret" not in token
    assert crypto.decrypt(token) == "shpat_secret"


def test_key_rotation(settings) -> None:
    old = settings.ENCRYPTION_KEY
    ciphertext = crypto.encrypt("value")
    new = Fernet.generate_key().decode()
    settings.ENCRYPTION_KEY = f"{new},{old}"
    assert crypto.decrypt(ciphertext) == "value"  # old key still decrypts
    rotated = crypto.rotate(ciphertext)
    settings.ENCRYPTION_KEY = new
    assert crypto.decrypt(rotated) == "value"  # works with only the new key
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext)


# ------------------------------------------------------------------ log redaction
def test_redaction() -> None:
    assert redact_text("contact jo@example.com or +44 20 7946 0958") == (
        "contact [email] or [phone]"
    )
    assert redact_text("gid://shopify/Product/1234567890123") == (
        "gid://shopify/Product/1234567890123"
    )
    assert redact_value("access_token", "shpat_x") == "[redacted]"
    assert redact_value("payload", {"refresh_token": "x", "note": "a@b.co"}) == {
        "refresh_token": "[redacted]",
        "note": "[email]",
    }
