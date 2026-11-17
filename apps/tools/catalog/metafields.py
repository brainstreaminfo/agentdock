"""
Metafield tools: read and set custom fields on products, collections, customers,
orders, pages, articles…

The Shopify permission needed depends on what OWNS the metafield (a product
metafield needs write_products, a customer metafield needs write_customers), so
these tools check scopes at run time with `ctx.require_scopes(...)`.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field, field_validator

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import load_document, nodes

DOC = load_document("metafields")

# Owner type (from the gid) -> resource scope suffix.
OWNER_SCOPES = {
    "Product": "products",
    "ProductVariant": "products",
    "Collection": "products",
    "Customer": "customers",
    "Order": "orders",
    "DraftOrder": "draft_orders",
    "Page": "content",
    "Article": "content",
    "Blog": "content",
    "Location": "locations",
    "Shop": None,  # shop metafields need no extra scope
}
_GID_RE = re.compile(r"^gid://shopify/([A-Za-z]+)/\d+$")
SCOPES_NOTE = "Also needs the read/write permission of the owner (e.g. write_products)."


def owner_type(owner_id: str) -> str:
    match = _GID_RE.match(owner_id.strip())
    if not match:
        raise ToolError("owner_id must be a full id like gid://shopify/Product/123.")
    kind = match.group(1)
    if kind not in OWNER_SCOPES:
        raise ToolError(f"Metafields on {kind} are not supported by this tool.")
    return kind


def _require(ctx: ToolContext, owner_id: str, access: str) -> None:
    resource = OWNER_SCOPES[owner_type(owner_id)]
    if resource:
        ctx.require_scopes([f"{access}_{resource}"])


class GetMetafieldsInput(ToolInput):
    owner_id: str = Field(description="e.g. gid://shopify/Product/123")
    namespace: str | None = None
    limit: int = Field(50, ge=1, le=100)


@tool(
    key="metafields.get",
    name="Get metafields",
    description=f"Custom fields (metafields) of a product, customer, order, page… {SCOPES_NOTE}",
    category="Metafields",
    scopes=[],
)
def get_metafields(ctx: ToolContext, args: GetMetafieldsInput) -> dict[str, Any]:
    _require(ctx, args.owner_id, "read")
    node = ctx.gql(
        DOC,
        "OwnerMetafields",
        {"ownerId": args.owner_id, "first": args.limit, "namespace": args.namespace},
    )["node"]
    if node is None:
        raise ToolError(f"Nothing found with id {args.owner_id}.")
    return {"owner_id": node["id"], "metafields": nodes(node.get("metafields"))}


class MetafieldValue(ToolInput):
    namespace: str = Field(min_length=2, max_length=255)
    key: str = Field(min_length=2, max_length=64)
    type: str = Field(
        description="Shopify metafield type, e.g. single_line_text_field, "
        "multi_line_text_field, number_integer, boolean, json, url."
    )
    value: str = Field(description="Always a string; JSON for list/json types.")

    @field_validator("namespace")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.startswith("shopify") or value == "app":
            raise ValueError("That namespace is reserved by Shopify.")
        return value


class SetMetafieldsInput(ToolInput):
    owner_id: str
    metafields: list[MetafieldValue] = Field(min_length=1, max_length=10)


def _current(ctx: ToolContext, args: SetMetafieldsInput) -> list[dict[str, Any]]:
    """Existing value (or None) for every metafield we're about to set."""
    out = []
    for m in args.metafields:
        node = ctx.gql(
            DOC,
            "OwnerMetafield",
            {"ownerId": args.owner_id, "namespace": m.namespace, "key": m.key},
        )["node"]
        if node is None:
            raise ToolError(f"Nothing found with id {args.owner_id}.")
        out.append({"namespace": m.namespace, "key": m.key, "existing": node.get("metafield")})
    return out


def _set(ctx: ToolContext, owner_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "SetMetafields",
        {"metafields": [{"ownerId": owner_id, **i} for i in items]},
        user_errors_at="metafieldsSet",
    )
    return {"metafields": data["metafieldsSet"]["metafields"]}


@tool(
    key="metafields.set",
    name="Set metafields",
    description=f"Create or update up to 10 metafields on one owner. {SCOPES_NOTE}",
    category="Metafields",
    scopes=[],
    mutating=True,
)
def set_metafields(ctx: ToolContext, args: SetMetafieldsInput) -> dict[str, Any]:
    _require(ctx, args.owner_id, "write")
    return _set(ctx, args.owner_id, [m.model_dump() for m in args.metafields])


@set_metafields.preview
def _preview_set(ctx: ToolContext, args: SetMetafieldsInput) -> dict[str, Any]:
    _require(ctx, args.owner_id, "write")
    current = _current(ctx, args)
    return {
        "summary": f"Set {len(args.metafields)} metafield(s) on {args.owner_id}",
        "changes": [
            {
                "field": f"{m.namespace}.{m.key}",
                "before": (c["existing"] or {}).get("value"),
                "after": m.value,
            }
            for m, c in zip(args.metafields, current, strict=True)
        ],
    }


@set_metafields.snapshot
def _snapshot_set(ctx: ToolContext, args: SetMetafieldsInput) -> dict[str, Any]:
    return {"metafields": _current(ctx, args)}


@set_metafields.undo
def _undo_set(ctx: ToolContext, args: SetMetafieldsInput, before: dict, after: dict) -> Any:
    restore = [
        {
            "namespace": c["namespace"],
            "key": c["key"],
            "type": c["existing"]["type"],
            "value": c["existing"]["value"],
        }
        for c in before["metafields"]
        if c["existing"]
    ]
    delete = [
        {"ownerId": args.owner_id, "namespace": c["namespace"], "key": c["key"]}
        for c in before["metafields"]
        if not c["existing"]
    ]
    if restore:
        _set(ctx, args.owner_id, restore)
    if delete:
        ctx.gql(DOC, "DeleteMetafields", {"metafields": delete}, user_errors_at="metafieldsDelete")
    return {"restored": len(restore), "deleted": len(delete)}
