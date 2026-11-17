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
