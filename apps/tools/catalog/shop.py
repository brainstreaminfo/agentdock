"""Shop tools: basic store information and legal policies."""

from __future__ import annotations

from typing import Any

from apps.tools.context import ToolContext
from apps.tools.registry import NoInput, tool

from ._common import load_document

DOC = load_document("shop")


@tool(
    key="shop.info",
    name="Store information",
    description="""Basic facts about the store: name, domain, currency, timezone,
    Shopify plan and country. Use it to adapt answers (currency, tone, locale).""",
    category="Shop",
    scopes=[],
)
def shop_info(ctx: ToolContext, args: NoInput) -> dict[str, Any]:
    shop = ctx.gql(DOC, "ShopInfo")["shop"]
    return {
        "name": shop["name"],
        "domain": (shop.get("primaryDomain") or {}).get("url"),
        "myshopify_domain": shop["myshopifyDomain"],
        "contact_email": shop.get("contactEmail") or shop.get("email"),
        "currency": shop["currencyCode"],
        "presentment_currencies": shop.get("enabledPresentmentCurrencies", []),
        "timezone": shop["ianaTimezone"],
        "weight_unit": shop.get("weightUnit"),
        "plan": (shop.get("plan") or {}).get("publicDisplayName"),
        "is_development_store": (shop.get("plan") or {}).get("partnerDevelopment", False),
        "country": (shop.get("billingAddress") or {}).get("countryCodeV2"),
    }


@tool(
    key="shop.policies",
    name="Store policies",
    description="""The store's legal policies (refund, privacy, terms of service,
    shipping, contact…) with their full text. Use when answering customer questions
    about returns, shipping or privacy.""",
    category="Shop",
    scopes=["read_legal_policies"],
)
def shop_policies(ctx: ToolContext, args: NoInput) -> dict[str, Any]:
    policies = ctx.gql(DOC, "ShopPolicies")["shop"]["shopPolicies"]
    return {"policies": policies}
