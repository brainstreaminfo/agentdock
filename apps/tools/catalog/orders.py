"""Order tools: search, read, tag, note, and sales statistics."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from typing import Any, Literal

from pydantic import Field, field_validator

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from . import _stats
from ._common import PageInput, build_query, gid, load_document, money, nodes, page

DOC = load_document("orders")
LIMIT_NOTE = (
    "Only the last 60 days of orders are available unless the app has the "
    "read_all_orders permission."
)


def _summary(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": o["id"],
        "name": o["name"],
        "created_at": o["createdAt"],
        "cancelled": bool(o.get("cancelledAt")),
        "financial_status": o.get("displayFinancialStatus"),
        "fulfillment_status": o.get("displayFulfillmentStatus"),
        "items": o.get("currentSubtotalLineItemsQuantity"),
        "total": money(o.get("currentTotalPriceSet")),
        "tags": o.get("tags", []),
        "customer_id": (o.get("customer") or {}).get("id"),
    }


# --------------------------------------------------------------------------- search
class SearchOrdersInput(PageInput):
    query: str = Field("", description="Free text or Shopify order search syntax.")
    financial_status: (
        Literal["paid", "pending", "refunded", "partially_refunded", "authorized", "voided"] | None
    ) = None
    fulfillment_status: Literal["unfulfilled", "fulfilled", "partial"] | None = None
    tag: str | None = None
    created_from: date | None = Field(None, description="YYYY-MM-DD (inclusive).")
    created_to: date | None = Field(None, description="YYYY-MM-DD (inclusive).")


@tool(
    key="orders.search",
    name="Search orders",
    description=f"""Find orders, newest first, by text, payment/fulfillment status,
    tag or creation date. {LIMIT_NOTE}""",
    category="Orders",
    scopes=["read_orders"],
)
def search_orders(ctx: ToolContext, args: SearchOrdersInput) -> dict[str, Any]:
    q = build_query(
        args.query,
        financial_status=args.financial_status,
        fulfillment_status=args.fulfillment_status,
        tag=args.tag,
    )
    dates = []
    if args.created_from:
        dates.append(f"created_at:>={args.created_from.isoformat()}")
    if args.created_to:
        dates.append(f"created_at:<={args.created_to.isoformat()}")
    query = " AND ".join([p for p in [q, *dates] if p])
    data = ctx.gql(
        DOC, "SearchOrders", {"first": args.limit, "after": args.cursor, "query": query or None}
    )
    result = page(data["orders"], "orders")
    result["orders"] = [_summary(o) for o in result["orders"]]
    return result


# --------------------------------------------------------------------------- get
class OrderRefInput(ToolInput):
    order_id: str = Field(description="Order id (gid or number) or order name like '#1001'.")


def _resolve_order_id(ctx: ToolContext, ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("#") or (not ref.isdigit() and not ref.startswith("gid://")):
        found = nodes(
            ctx.gql(DOC, "SearchOrders", {"first": 1, "query": f"name:{_quote(ref)}"})["orders"]
        )
        if not found:
            raise ToolError(f"No order named {ref}.")
        return str(found[0]["id"])
    return gid("Order", ref)


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


@tool(
    key="orders.get",
    name="Get order details",
    description=f"""One order with line items, totals (subtotal, discounts, shipping,
    tax, refunds), discount codes, note, tags and the customer's name. {LIMIT_NOTE}""",
    category="Orders",
    scopes=["read_orders"],
)
def get_order(ctx: ToolContext, args: OrderRefInput) -> dict[str, Any]:
    o = ctx.gql(DOC, "GetOrder", {"id": _resolve_order_id(ctx, args.order_id)})["order"]
    if o is None:
        raise ToolError(f"No order found for {args.order_id}.")
    out = _summary(o)
    out.update(
        {
            "note": o.get("note"),
            "processed_at": o.get("processedAt"),
            "test": o.get("test"),
            "source": o.get("sourceName"),
            "customer": o.get("customer"),
            "subtotal": money(o.get("subtotalPriceSet")),
            "discounts": money(o.get("totalDiscountsSet")),
            "shipping": money(o.get("totalShippingPriceSet")),
            "tax": money(o.get("totalTaxSet")),
            "refunded": money(o.get("totalRefundedSet")),
            "discount_codes": o.get("discountCodes", []),
            "line_items": [
                {
                    "title": li["title"],
                    "variant": li.get("variantTitle"),
                    "quantity": li["quantity"],
                    "sku": li.get("sku"),
                    "product_id": (li.get("product") or {}).get("id"),
                    "variant_id": (li.get("variant") or {}).get("id"),
                    "total": money(li.get("discountedTotalSet")),
                }
                for li in nodes(o.get("lineItems"))
            ],
        }
    )
    return out


# --------------------------------------------------------------------------- tags
class TagListInput(ToolInput):
    """Shared by orders.add_tags and customers.add_tags."""

    tags: list[str] = Field(min_length=1, max_length=20)

    @field_validator("tags")
    @classmethod
    def _clean(cls, tags: list[str]) -> list[str]:
        cleaned = [t.strip() for t in tags if t.strip()]
        if any(len(t) > 255 or "," in t for t in cleaned):
            raise ValueError("Tags can't contain commas or exceed 255 characters.")
        return cleaned


class TagsInput(OrderRefInput, TagListInput):
    pass


def _order_state(ctx: ToolContext, ref: str) -> dict[str, Any]:
    o = ctx.gql(DOC, "OrderTagsAndNote", {"id": _resolve_order_id(ctx, ref)})["order"]
    if o is None:
        raise ToolError(f"No order found for {ref}.")
    return dict(o)


def add_tags_tool(resource: str, key: str, doc: str, state_fn: Any, input_model: type) -> Any:
    """Build a tags tool (orders.add_tags / customers.add_tags share the same logic)."""

    def run(ctx: ToolContext, args: Any) -> dict[str, Any]:
        state = state_fn(ctx, args)
        ctx.gql(doc, "AddTags", {"id": state["id"], "tags": args.tags}, user_errors_at="tagsAdd")
        return {"id": state["id"], "added": [t for t in args.tags if t not in state["tags"]]}

    run.__annotations__ = {"ctx": ToolContext, "args": input_model, "return": dict}
    spec = tool(
        key=key,
        name=f"Add tags to {resource}",
        description=f"""Add tags to one {resource} (existing tags are kept).""",
        category=f"{resource.capitalize()}s",
        scopes=[f"write_{resource}s"],
        mutating=True,
    )(run)

    @spec.preview
    def _preview(ctx: ToolContext, args: Any) -> dict[str, Any]:
        state = state_fn(ctx, args)
        new = [t for t in args.tags if t not in state["tags"]]
        label = state.get("name") or state.get("displayName") or state["id"]
        return {
            "summary": f"Add tag(s) {', '.join(new) or '(none new)'} to {resource} {label}",
            "changes": [{"field": "tags", "before": state["tags"], "after": state["tags"] + new}],
        }

    @spec.snapshot
    def _snapshot(ctx: ToolContext, args: Any) -> dict[str, Any]:
        state = state_fn(ctx, args)
        return {"id": state["id"], "tags": state["tags"]}

    @spec.undo
    def _undo(ctx: ToolContext, args: Any, before: dict, after: dict) -> Any:
        added = after.get("added") or []
        if added:
            ctx.gql(
                doc, "RemoveTags", {"id": before["id"], "tags": added}, user_errors_at="tagsRemove"
            )
        return {"removed": added}

    return spec


add_order_tags = add_tags_tool(
    "order", "orders.add_tags", DOC, lambda ctx, a: _order_state(ctx, a.order_id), TagsInput
)


# --------------------------------------------------------------------------- note
class NoteInput(OrderRefInput):
    note: str = Field(min_length=1, max_length=5000)
    mode: Literal["append", "replace"] = "append"


def _new_note(current: str | None, args: NoteInput) -> str:
    if args.mode == "replace" or not current:
        return args.note
    return f"{current}\n\n{args.note}"


def _set_note(ctx: ToolContext, order_id: str, note: str) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "UpdateOrderNote",
        {"input": {"id": order_id, "note": note}},
        user_errors_at="orderUpdate",
    )
    return dict(data["orderUpdate"]["order"])


@tool(
    key="orders.add_note",
    name="Add order note",
    description="""Add text to an order's internal note (append by default, or
    replace). Notes are visible to staff in Shopify Admin, not to the customer.""",
    category="Orders",
    scopes=["write_orders"],
    mutating=True,
)
def add_order_note(ctx: ToolContext, args: NoteInput) -> dict[str, Any]:
    state = _order_state(ctx, args.order_id)
    return _set_note(ctx, state["id"], _new_note(state.get("note"), args))


@add_order_note.preview
def _preview_note(ctx: ToolContext, args: NoteInput) -> dict[str, Any]:
    state = _order_state(ctx, args.order_id)
    verb = "Replace" if args.mode == "replace" else "Add to"
    return {
        "summary": f"{verb} the note on {state['name']}",
        "changes": [
            {
                "field": "note",
                "before": state.get("note"),
                "after": _new_note(state.get("note"), args),
            }
        ],
    }


@add_order_note.snapshot
def _snapshot_note(ctx: ToolContext, args: NoteInput) -> dict[str, Any]:
    state = _order_state(ctx, args.order_id)
    return {"id": state["id"], "note": state.get("note") or ""}


@add_order_note.undo
def _undo_note(ctx: ToolContext, args: NoteInput, before: dict, after: dict) -> Any:
    return _set_note(ctx, before["id"], before["note"])


# --------------------------------------------------------------------------- statistics
class PeriodInput(ToolInput):
    days: int = Field(30, ge=1, le=365, description="Look back this many days (incl. today).")


class TimeseriesInput(PeriodInput):
    interval: Literal["day", "week", "month"] = "day"


@tool(
    key="orders.timeseries",
    name="Sales over time",
    description=f"""Orders, sales and average order value per day / week / month for
    the last `days` days, in the shop's timezone and currency. Cancelled and test
    orders excluded. {LIMIT_NOTE}""",
    category="Orders",
    scopes=["read_orders"],
)
def orders_timeseries(ctx: ToolContext, args: TimeseriesInput) -> dict[str, Any]:
    period = _stats.last_days(ctx, args.days)
    orders = list(_stats.iter_orders(ctx, "OrdersForStats", period))
    buckets: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for o in orders:
        buckets.setdefault(_stats.bucket(o["createdAt"], period.tz, args.interval), []).append(o)
    return {
        "period": period.describe(),
        "interval": args.interval,
        "currency": _stats.currency_of(orders, ctx),
        "series": [{"period": k, **_stats.summarize(v)} for k, v in buckets.items()],
        "truncated": ctx.extra.get("truncated", False),
    }


@tool(
    key="orders.sales_summary",
    name="Sales summary",
    description=f"""Totals for the last `days` days: number of orders, total sales,
    average order value and discounts given. {LIMIT_NOTE}""",
    category="Orders",
    scopes=["read_orders"],
)
def sales_summary(ctx: ToolContext, args: PeriodInput) -> dict[str, Any]:
    period = _stats.last_days(ctx, args.days)
    orders = list(_stats.iter_orders(ctx, "OrdersForStats", period))
    return {
        "period": period.describe(),
        "currency": _stats.currency_of(orders, ctx),
        **_stats.summarize(orders),
        "truncated": ctx.extra.get("truncated", False),
    }
