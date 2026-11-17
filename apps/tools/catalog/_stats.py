"""
Order statistics computed from the Orders API.

Why not ShopifyQL? It exists in 2026-10 but needs the `read_reports` scope AND
Level 2 protected-customer-data approval from Shopify, which a new app won't
have. Computing from orders works for every store with `read_orders`. See
docs/DECISIONS.md.

Totals use each order's *current* total in the shop's currency: after refunds
and edits, including tax and shipping. Cancelled and test orders are skipped.
Dates are bucketed in the shop's timezone.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.tools.context import ToolContext

from ._common import load_document, nodes

DOC = load_document("orders")

# Safety caps so one tool call can't page through a huge store for minutes.
MAX_ORDERS = 2500
PAGE_SIZE = {"OrdersForStats": 100, "OrderLinesForStats": 25, "OrderCustomersForStats": 100}


@dataclass
class Period:
    start: datetime  # aware, shop timezone
    end: datetime
    tz: ZoneInfo

    @property
    def query(self) -> str:
        start = self.start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = self.end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"created_at:>='{start}' AND created_at:<'{end}'"

    def describe(self) -> dict[str, str]:
        return {
            "from": self.start.date().isoformat(),
            "to": (self.end - timedelta(seconds=1)).date().isoformat(),
            "timezone": str(self.tz),
        }


def shop_tz(ctx: ToolContext) -> ZoneInfo:
    try:
        return ZoneInfo(ctx.shop.timezone or "UTC")
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def last_days(ctx: ToolContext, days: int, *, offset_days: int = 0) -> Period:
    """The last `days` full days up to now (in the shop timezone), optionally shifted back."""
    tz = shop_tz(ctx)
    now = timezone.now().astimezone(tz)
    end = now - timedelta(days=offset_days)
    start = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    start += timedelta(days=1) if days > 0 else timedelta(0)
    return Period(start=start, end=end, tz=tz)


def iter_orders(ctx: ToolContext, operation: str, period: Period) -> Iterator[dict[str, Any]]:
    """Yield non-cancelled, non-test orders in the period; sets ctx.extra['truncated']."""
    after = None
    seen = 0
    ctx.extra["truncated"] = False
    while True:
        data = ctx.gql(
            DOC, operation, {"first": PAGE_SIZE[operation], "after": after, "query": period.query}
        )
        conn = data["orders"]
        for order in nodes(conn):
            seen += 1
            if order.get("cancelledAt") or order.get("test"):
                continue
            yield order
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return
        if seen >= MAX_ORDERS:
            ctx.extra["truncated"] = True
            return
        after = info["endCursor"]


def amount(money_set: dict[str, Any] | None) -> Decimal:
    if not money_set:
        return Decimal(0)
    return Decimal(str((money_set.get("shopMoney") or money_set).get("amount") or 0))


def currency_of(orders: list[dict[str, Any]], ctx: ToolContext) -> str:
    for o in orders:
        cur = ((o.get("currentTotalPriceSet") or {}).get("shopMoney") or {}).get("currencyCode")
        if cur:
            return str(cur)
    return ctx.shop.currency or ""


def bucket(dt_iso: str, tz: ZoneInfo, interval: str) -> str:
    local = datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).astimezone(tz)
    if interval == "month":
        return local.strftime("%Y-%m")
    if interval == "week":
        monday = local.date() - timedelta(days=local.weekday())
        return f"week of {monday.isoformat()}"
    return local.date().isoformat()


def fmt(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def summarize(orders: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum((amount(o.get("currentTotalPriceSet")) for o in orders), Decimal(0))
    discounts = sum((amount(o.get("totalDiscountsSet")) for o in orders), Decimal(0))
    count = len(orders)
    return {
        "orders": count,
        "total_sales": fmt(total),
        "average_order_value": fmt(total / count) if count else "0.00",
        "total_discounts": fmt(discounts),
    }
