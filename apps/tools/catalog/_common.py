"""Small helpers shared by the catalog tools."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import Field

from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput

_DIR = Path(__file__).parent
_GID_RE = re.compile(r"^gid://shopify/([A-Za-z]+)/(\d+)(\?.*)?$")


@cache
def load_document(domain: str) -> str:
    """The .graphql file next to the tool module, e.g. catalog/products.graphql."""
    return (_DIR / f"{domain}.graphql").read_text()


def gid(kind: str, value: str | int) -> str:
    """
    Normalise an id to a Shopify GID. Accepts `123`, `"123"` or
    `"gid://shopify/Product/123"`. Raises ToolError for the wrong kind of id,
    which usually means the AI mixed up e.g. a variant id and a product id.
    """
    text = str(value).strip()
    if text.isdigit():
        return f"gid://shopify/{kind}/{text}"
    match = _GID_RE.match(text)
    if not match:
        raise ToolError(f"'{text}' is not a valid {kind} id (expected gid://shopify/{kind}/123).")
    if match.group(1) != kind:
        raise ToolError(f"Expected a {kind} id but got a {match.group(1)} id: {text}")
    return text


def nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not connection:
        return []
    if "nodes" in connection:
        return list(connection["nodes"])
    return [edge["node"] for edge in connection.get("edges", [])]


def next_cursor(connection: dict[str, Any] | None) -> str | None:
    info = (connection or {}).get("pageInfo") or {}
    return info.get("endCursor") if info.get("hasNextPage") else None


def page(connection: dict[str, Any] | None, key: str = "items", **extra: Any) -> dict[str, Any]:
    """Standard list result: {"items": [...], "next_cursor": "..."|None}."""
    return {key: nodes(connection), "next_cursor": next_cursor(connection), **extra}


def money(value: dict[str, Any] | None) -> dict[str, str] | None:
    """Flatten MoneyBag/MoneyV2 into {"amount": "12.50", "currency": "GBP"}."""
    if not value:
        return None
    m = value.get("shopMoney", value)
    return {"amount": m.get("amount"), "currency": m.get("currencyCode")}


def search_value(value: str) -> str:
    """Quote a value for Shopify's search syntax (title:"blue shirt")."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_query(text: str = "", **filters: str | None) -> str:
    """Shopify search string from free text + field filters (None/empty are skipped)."""
    parts = [text.strip()] if text and text.strip() else []
    for field, value in filters.items():
        if value not in (None, ""):
            parts.append(f"{field}:{search_value(str(value))}")
    return " AND ".join(parts)


class PageInput(ToolInput):
    limit: int = Field(20, ge=1, le=50, description="How many items to return (max 50).")
    cursor: str | None = Field(None, description="next_cursor from the previous page.")


def changed_fields(args: ToolInput, mapping: dict[str, str]) -> dict[str, Any]:
    """{shopify_field: value} for every input field that was provided (not None)."""
    data = args.model_dump(exclude_none=True)
    return {shopify: data[ours] for ours, shopify in mapping.items() if ours in data}
