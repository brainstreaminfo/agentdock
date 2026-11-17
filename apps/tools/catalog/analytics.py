"""
Analytics tools: best-selling products, best customers, average order value.
Computed from orders (see _stats.py for why not ShopifyQL).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from apps.tools.context import ToolContext
from apps.tools.registry import tool

from . import _stats
from ._common import nodes
from .orders import LIMIT_NOTE, PeriodInput


class TopInput(PeriodInput):
    limit: int = Field(10, ge=1, le=50)


class TopProductsInput(TopInput):
    by: Literal["revenue", "quantity"] = "revenue"


@tool(
    key="analytics.top_products",
    name="Best-selling products",
    description=f"""Products ranked by revenue or units sold over the last `days`
    days (cancelled/test orders excluded; line-item revenue after discounts).
    {LIMIT_NOTE}""",
    category="Analytics",
    scopes=["read_orders"],
)
def top_products(ctx: ToolContext, args: TopProductsInput) -> dict[str, Any]:
    period = _stats.last_days(ctx, args.days)
    totals: dict[str, dict[str, Any]] = {}
    currency = ""
    for order in _stats.iter_orders(ctx, "OrderLinesForStats", period):
        for line in nodes(order.get("lineItems")):
            product = line.get("product") or {}
            key = product.get("id") or f"(deleted) {line['title']}"
            row = totals.setdefault(
                key,
                {
                    "product_id": product.get("id"),
                    "title": product.get("title") or line["title"],
                    "quantity": 0,
                    "revenue": Decimal(0),
                },
            )
            row["quantity"] += line["quantity"]
            row["revenue"] += _stats.amount(line.get("discountedTotalSet"))
            currency = currency or (line.get("discountedTotalSet") or {}).get("shopMoney", {}).get(
                "currencyCode", ""
            )
    ranked = sorted(totals.values(), key=lambda r: r[args.by], reverse=True)[: args.limit]
    return {
        "period": period.describe(),
        "currency": currency or ctx.shop.currency,
        "ranked_by": args.by,
        "products": [{**r, "revenue": _stats.fmt(r["revenue"])} for r in ranked],
        "truncated": ctx.extra.get("truncated", False),
    }


@tool(
    key="analytics.top_customers",
    name="Top customers",
    description=f"""Customers ranked by money spent over the last `days` days, with
    their number of orders. Guest checkouts are skipped. {LIMIT_NOTE}""",
    category="Analytics",
    scopes=["read_orders", "read_customers"],
)
def top_customers(ctx: ToolContext, args: TopInput) -> dict[str, Any]:
    period = _stats.last_days(ctx, args.days)
    totals: dict[str, dict[str, Any]] = {}
    orders = []
    for order in _stats.iter_orders(ctx, "OrderCustomersForStats", period):
        orders.append(order)
        customer = order.get("customer")
        if not customer:
            continue
        row = totals.setdefault(
            customer["id"],
            {
                "customer_id": customer["id"],
                "name": customer.get("displayName"),
                "orders": 0,
                "spent": Decimal(0),
            },
        )
        row["orders"] += 1
        row["spent"] += _stats.amount(order.get("currentTotalPriceSet"))
    ranked = sorted(totals.values(), key=lambda r: r["spent"], reverse=True)[: args.limit]
    return {
        "period": period.describe(),
        "currency": _stats.currency_of(orders, ctx),
        "customers": [{**r, "spent": _stats.fmt(r["spent"])} for r in ranked],
        "truncated": ctx.extra.get("truncated", False),
    }


@tool(
    key="analytics.aov",
    name="Average order value",
    description=f"""Average order value for the last `days` days compared with the
    same number of days before that, with the % change. {LIMIT_NOTE}""",
    category="Analytics",
    scopes=["read_orders"],
)
def average_order_value(ctx: ToolContext, args: PeriodInput) -> dict[str, Any]:
    current_period = _stats.last_days(ctx, args.days)
    previous_period = _stats.Period(
        start=current_period.start - timedelta(days=args.days),
        end=current_period.start,
        tz=current_period.tz,
    )
    current = list(_stats.iter_orders(ctx, "OrdersForStats", current_period))
    truncated = ctx.extra.get("truncated", False)
    previous = list(_stats.iter_orders(ctx, "OrdersForStats", previous_period))
    truncated = truncated or ctx.extra.get("truncated", False)

    now, before = _stats.summarize(current), _stats.summarize(previous)
    aov_now = Decimal(now["average_order_value"])
    aov_before = Decimal(before["average_order_value"])
    change = (
        f"{((aov_now - aov_before) / aov_before * 100).quantize(Decimal('0.1'))}%"
        if aov_before
        else None
    )
    return {
        "currency": _stats.currency_of(current or previous, ctx),
        "current": {"period": current_period.describe(), **now},
        "previous": {"period": previous_period.describe(), **before},
        "aov_change": change,
        "truncated": truncated,
    }
