"""Inventory tools: stock levels per location, adjust stock, find low-stock variants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import PageInput, gid, load_document, next_cursor, nodes

DOC = load_document("inventory")


def _levels(item: dict[str, Any] | None) -> dict[str, Any]:
    item = item or {}
    return {
        "inventory_item_id": item.get("id"),
        "sku": item.get("sku"),
        "tracked": item.get("tracked"),
        "locations": [
            {
                "location_id": lvl["location"]["id"],
                "location": lvl["location"]["name"],
                **{q["name"]: q["quantity"] for q in lvl.get("quantities", [])},
            }
            for lvl in nodes(item.get("inventoryLevels"))
        ],
    }


# --------------------------------------------------------------------------- levels
class LevelsInput(ToolInput):
    product_id: str | None = Field(None, description="All variants of this product…")
    variant_id: str | None = Field(None, description="…or just this variant.")

    @model_validator(mode="after")
    def _one_of(self) -> LevelsInput:
        if bool(self.product_id) == bool(self.variant_id):
            raise ValueError("Give exactly one of product_id or variant_id.")
        return self


@tool(
    key="inventory.levels",
    name="Inventory levels",
    description="""Stock per location (available, on hand, committed, incoming) for
    every variant of a product, or for one variant. Returns the inventory_item_id
    and location_id needed by inventory.adjust.""",
    category="Inventory",
    scopes=["read_inventory", "read_products"],
)
def inventory_levels(ctx: ToolContext, args: LevelsInput) -> dict[str, Any]:
    if args.variant_id:
        v = ctx.gql(DOC, "VariantInventory", {"id": gid("ProductVariant", args.variant_id)})[
            "productVariant"
        ]
        if v is None:
            raise ToolError(f"No variant found with id {args.variant_id}.")
        return {
            "product": v["product"],
            "variants": [{"id": v["id"], "title": v["title"], **_levels(v["inventoryItem"])}],
        }
    p = ctx.gql(DOC, "ProductInventory", {"id": gid("Product", args.product_id or "")})["product"]
    if p is None:
        raise ToolError(f"No product found with id {args.product_id}.")
    return {
        "product": {"id": p["id"], "title": p["title"]},
        "variants": [
            {"id": v["id"], "title": v["title"], **_levels(v["inventoryItem"])}
            for v in nodes(p["variants"])
        ],
    }


# --------------------------------------------------------------------------- adjust
Reason = Literal[
    "correction",
    "cycle_count_available",
    "damaged",
    "other",
    "promotion",
    "quality_control",
    "received",
    "restock",
    "safety_stock",
    "shrinkage",
]


class AdjustInput(ToolInput):
    inventory_item_id: str
    location_id: str
    delta: int = Field(description="+10 adds ten units, -3 removes three.")
    reason: Reason = "correction"

    @model_validator(mode="after")
    def _non_zero(self) -> AdjustInput:
        if self.delta == 0:
            raise ValueError("delta can't be 0.")
        if abs(self.delta) > 100_000:
            raise ValueError("delta is unrealistically large.")
        return self


def _available(ctx: ToolContext, args: AdjustInput) -> dict[str, Any]:
    item = ctx.gql(
        DOC,
        "ItemAvailable",
        {
            "itemId": gid("InventoryItem", args.inventory_item_id),
            "locationId": gid("Location", args.location_id),
        },
    )["inventoryItem"]
    if item is None:
        raise ToolError(f"No inventory item {args.inventory_item_id}.")
    level = item.get("inventoryLevel")
    if level is None:
        raise ToolError("This item isn't stocked at that location.")
    if not item.get("tracked"):
        raise ToolError("Inventory isn't tracked for this item, so it can't be adjusted.")
    quantity = next(q["quantity"] for q in level["quantities"] if q["name"] == "available")
    variant = item.get("variant") or {}
    product_title = (variant.get("product") or {}).get("title", "")
    return {
        "available": quantity,
        "location": level["location"]["name"],
        "label": f"{product_title} - {variant.get('title', '')}",
    }


def _adjust(ctx: ToolContext, args: AdjustInput, delta: int, expected: int, key: str) -> dict:
    data = ctx.gql(
        DOC,
        "AdjustInventory",
        {
            "idempotencyKey": key,
            "input": {
                "name": "available",
                "reason": args.reason,
                "changes": [
                    {
                        "delta": delta,
                        "inventoryItemId": gid("InventoryItem", args.inventory_item_id),
                        "locationId": gid("Location", args.location_id),
                        # Compare-and-swap: fails if stock changed since we looked.
                        "changeFromQuantity": expected,
                    }
                ],
            },
        },
        user_errors_at="inventoryAdjustQuantities",
    )
    return dict(data["inventoryAdjustQuantities"]["inventoryAdjustmentGroup"] or {})


@tool(
    key="inventory.adjust",
    name="Adjust inventory",
    description="""Add or remove available stock for one inventory item at one
    location (get ids from inventory.levels). Fails safely if the stock changed
    in the meantime.""",
    category="Inventory",
    scopes=["write_inventory"],
    mutating=True,
)
def adjust_inventory(ctx: ToolContext, args: AdjustInput) -> dict[str, Any]:
    if not ctx.idempotency_key:
        raise ToolError("Internal: inventory changes must go through an approval record.")
    current = _available(ctx, args)
    group = _adjust(ctx, args, args.delta, current["available"], ctx.idempotency_key)
    return {
        "before": current["available"],
        "after": current["available"] + args.delta,
        "group": group,
    }


@adjust_inventory.preview
def _preview_adjust(ctx: ToolContext, args: AdjustInput) -> dict[str, Any]:
    current = _available(ctx, args)
    return {
        "summary": f"{'Add' if args.delta > 0 else 'Remove'} {abs(args.delta)} unit(s) of "
        f"{current['label']} at {current['location']} ({args.reason})",
        "changes": [
            {
                "field": "available",
                "target": current["label"],
                "before": current["available"],
                "after": current["available"] + args.delta,
            }
        ],
    }


@adjust_inventory.snapshot
def _snapshot_adjust(ctx: ToolContext, args: AdjustInput) -> dict[str, Any]:
    return _available(ctx, args)


@adjust_inventory.undo
def _undo_adjust(ctx: ToolContext, args: AdjustInput, before: dict, after: dict) -> Any:
    current = _available(ctx, args)
    return _adjust(ctx, args, -args.delta, current["available"], ctx.idempotency_key or "")


# --------------------------------------------------------------------------- low stock
class LowStockInput(PageInput):
    threshold: int = Field(5, ge=0, le=10_000, description="Available ≤ this counts as low.")
    include_drafts: bool = False


@tool(
    key="inventory.low_stock",
    name="Low-stock variants",
    description="""Variants whose total available stock is at or below `threshold`,
    lowest first. Only inventory-tracked variants of active products by default.""",
    category="Inventory",
    scopes=["read_products", "read_inventory"],
)
def low_stock(ctx: ToolContext, args: LowStockInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "LowStockVariants",
        {
            "first": args.limit,
            "after": args.cursor,
            "query": f"inventory_quantity:<={args.threshold}",
        },
    )
    variants = []
    for v in nodes(data["productVariants"]):
        if not (v.get("inventoryItem") or {}).get("tracked"):
            continue
        if not args.include_drafts and v["product"]["status"] != "ACTIVE":
            continue
        variants.append(
            {
                "variant_id": v["id"],
                "variant": v["title"],
                "sku": v.get("sku"),
                "available": v.get("inventoryQuantity"),
                "product_id": v["product"]["id"],
                "product": v["product"]["title"],
                "inventory_item_id": v["inventoryItem"]["id"],
            }
        )
    return {
        "threshold": args.threshold,
        "variants": variants,
        "next_cursor": next_cursor(data["productVariants"]),
    }
