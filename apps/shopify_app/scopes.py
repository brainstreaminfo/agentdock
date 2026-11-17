"""
Helpers for Shopify access scopes.

Rule from Shopify: a `write_X` scope includes `read_X`, and Shopify may return
only `write_X` in the granted list. So "does the shop have read_products?" must
also be true when it has `write_products`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

SCOPE_RE = re.compile(r"^(unauthenticated_)?(read|write)_[a-z][a-z0-9_]*$")

# Authenticated Admin API scopes, from shopify.dev/docs/api/usage/access-scopes
# (checked 2026-11). Custom tools may only declare scopes from this list.
KNOWN_SCOPES: frozenset[str] = frozenset(
    {
        "read_all_orders",
        "read_analytics_annotations",
        "read_apps",
        "read_assigned_fulfillment_orders",
        "read_cart_transforms",
        "read_cash_tracking",
        "read_channels",
        "read_checkout_and_accounts_configurations",
        "read_checkout_branding_settings",
        "read_checkout_settings",
        "read_companies",
        "read_content",
        "read_customer_data_erasure",
        "read_customer_events",
        "read_customer_merge",
        "read_customer_payment_methods",
        "read_customers",
        "read_delivery_customizations",
        "read_delivery_promises",
        "read_discounts",
        "read_draft_orders",
        "read_files",
        "read_financial_kyc_information",
        "read_fulfillment_constraint_rules",
        "read_fulfillments",
        "read_gift_card_transactions",
        "read_gift_cards",
        "read_inventory",
        "read_inventory_shipments",
        "read_inventory_shipments_received_items",
        "read_inventory_transfers",
        "read_legal_policies",
        "read_locales",
        "read_locations",
        "read_marketing_events",
        "read_marketplace_fulfillment_orders",
        "read_marketplace_orders",
        "read_marketplace_returns",
        "read_markets",
        "read_merchant_approval_signals",
        "read_merchant_managed_fulfillment_orders",
        "read_metaobject_definitions",
        "read_metaobjects",
        "read_mobile_platform_applications",
        "read_online_store_navigation",
        "read_online_store_pages",
        "read_order_edits",
        "read_orders",
        "read_own_subscription_contracts",
        "read_payment_customizations",
        "read_payment_gateways",
        "read_payment_mandate",
        "read_payment_sessions",
        "read_payment_terms",
        "read_pixels",
        "read_price_rules",
        "read_privacy_settings",
        "read_product_listings",
        "read_products",
        "read_publications",
        "read_reports",
        "read_resource_feedbacks",
        "read_returns",
        "read_rollouts",
        "read_script_tags",
        "read_server_pixels",
        "read_shipping",
        "read_shopify_payments",
        "read_shopify_payments_accounts",
        "read_shopify_payments_accounts_sensitive",
        "read_shopify_payments_bank_accounts",
        "read_shopify_payments_dispute_evidences",
        "read_shopify_payments_dispute_file_uploads",
        "read_shopify_payments_disputes",
        "read_shopify_payments_payouts",
        "read_store_credit_account_transactions",
        "read_store_credit_accounts",
        "read_taxes",
        "read_themes",
        "read_third_party_fulfillment_orders",
        "read_translations",
        "read_users",
        "read_validations",
        "write_analytics_annotations",
        "write_app_proxy",
        "write_assigned_fulfillment_orders",
        "write_cart_transforms",
        "write_cash_tracking",
        "write_channels",
        "write_checkout_and_accounts_configurations",
        "write_checkout_branding_settings",
        "write_checkout_settings",
        "write_companies",
        "write_content",
        "write_customer_data_erasure",
        "write_customer_merge",
        "write_customers",
        "write_delivery_customizations",
        "write_delivery_promises",
        "write_discounts",
        "write_draft_orders",
        "write_files",
        "write_fulfillment_constraint_rules",
        "write_fulfillments",
        "write_gift_card_transactions",
        "write_gift_cards",
        "write_inventory",
        "write_inventory_shipments",
        "write_inventory_shipments_received_items",
        "write_inventory_transfers",
        "write_legal_policies",
        "write_locales",
        "write_locations",
        "write_marketing_events",
        "write_marketplace_orders",
        "write_marketplace_returns",
        "write_markets",
        "write_merchant_managed_fulfillment_orders",
        "write_metaobject_definitions",
        "write_metaobjects",
        "write_mobile_platform_applications",
        "write_online_store_navigation",
        "write_online_store_pages",
        "write_order_edits",
        "write_orders",
        "write_own_subscription_contracts",
        "write_payment_customizations",
        "write_payment_gateways",
        "write_payment_mandate",
        "write_payment_sessions",
        "write_payment_terms",
        "write_pixels",
        "write_price_rules",
        "write_privacy_settings",
        "write_products",
        "write_publications",
        "write_reports",
        "write_resource_feedbacks",
        "write_returns",
        "write_script_tags",
        "write_server_pixels",
        "write_shipping",
        "write_shopify_payments_dispute_evidences",
        "write_shopify_payments_dispute_file_uploads",
        "write_store_credit_account_transactions",
        "write_taxes",
        "write_themes",
        "write_third_party_fulfillment_orders",
        "write_translations",
        "write_validations",
    }
)

# Plain-language descriptions shown to merchants ("This agent needs access to …").
# Unknown scopes fall back to an auto-generated sentence.
SCOPE_LABELS: dict[str, str] = {
    "read_products": "View your products, collections and inventory",
    "write_products": "Edit your products and collections",
    "read_inventory": "View your inventory levels",
    "write_inventory": "Adjust your inventory levels",
    "read_orders": "View your orders",
    "write_orders": "Edit your orders (tags and notes)",
    "read_customers": "View your customers",
    "write_customers": "Edit your customers (tags)",
    "read_discounts": "View your discounts",
    "write_discounts": "Create and deactivate discounts",
    "read_content": "View your pages and blog posts",
    "write_content": "Create and edit pages and blog posts",
    "read_files": "View your uploaded files",
    "write_files": "Upload and edit files",
    "read_reports": "View your store reports and analytics",
    "read_locations": "View your store locations",
    "read_online_store_pages": "View your online store pages",
    "read_legal_policies": "View your store policies",
}


def is_valid(scope: str) -> bool:
    return bool(SCOPE_RE.match(scope))


def parse(value: str | Iterable[str] | None) -> list[str]:
    """Accept "a,b" or ["a", "b"]; return a sorted, de-duplicated, stripped list."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    return sorted({s.strip() for s in items if s and s.strip()})


def expand(scopes: Iterable[str]) -> set[str]:
    """Add the implied read_X for every write_X."""
    result = set(scopes)
    for scope in list(result):
        prefix, _, rest = scope.partition("write_")
        if _ and prefix in ("", "unauthenticated_"):
            result.add(f"{prefix}read_{rest}")
    return result


def missing(required: Iterable[str], granted: Iterable[str]) -> list[str]:
    have = expand(granted)
    return sorted(s for s in set(required) if s not in have)


def satisfies(required: Iterable[str], granted: Iterable[str]) -> bool:
    return not missing(required, granted)


def union(*groups: Iterable[str]) -> list[str]:
    """Combine scope lists, dropping read_X when write_X is present (Shopify-style)."""
    combined: set[str] = set()
    for group in groups:
        combined.update(parse(group))

    def implied_by_write(scope: str) -> bool:
        prefix, sep, rest = scope.partition("read_")
        return (
            bool(sep)
            and prefix in ("", "unauthenticated_")
            and (f"{prefix}write_{rest}" in combined)
        )

    return sorted(s for s in combined if not implied_by_write(s))


def describe(scope: str) -> str:
    if scope in SCOPE_LABELS:
        return SCOPE_LABELS[scope]
    match = SCOPE_RE.match(scope)
    if not match:
        return scope
    verb = "View" if match.group(2) == "read" else "Edit"
    noun = scope.split("_", 1 if not match.group(1) else 2)[-1].replace("_", " ")
    return f"{verb} your {noun}"
