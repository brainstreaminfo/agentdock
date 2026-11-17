"""Sales statistics and analytics computed from orders."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.tools.catalog import _stats
from apps.tools.executor import execute_tool

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(_stats.timezone, "now", lambda: NOW)


def order(
    i, created, total, *, cancelled=False, test=False, discounts="0", customer=None, lines=()
):
    return {
        "id": f"gid://shopify/Order/{i}",
        "createdAt": created,
        "cancelledAt": "x" if cancelled else None,
        "test": test,
        "currentTotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "GBP"}},
        "totalDiscountsSet": {"shopMoney": {"amount": discounts}},
        "customer": customer,
        "lineItems": {
            "nodes": [
                {
                    "title": t,
                    "quantity": q,
                    "product": {"id": f"gid://shopify/Product/{t}", "title": t},
                    "discountedTotalSet": {"shopMoney": {"amount": a, "currencyCode": "GBP"}},
                }
                for t, q, a in lines
            ]
        },
    }


def pages(*batches):
    out = []
    for n, batch in enumerate(batches):
        last = n == len(batches) - 1
        out.append(
            {
                "orders": {
                    "nodes": batch,
                    "pageInfo": {"hasNextPage": not last, "endCursor": f"c{n}"},
                }
            }
        )
    return out


def test_sales_summary_excludes_cancelled_and_test(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "OrdersForStats",
        pages(
            [
                order(1, "2026-11-16T10:00:00Z", "100.00", discounts="10"),
                order(2, "2026-11-16T11:00:00Z", "50.00"),
                order(3, "2026-11-16T12:00:00Z", "999.00", cancelled=True),
                order(4, "2026-11-16T13:00:00Z", "999.00", test=True),
            ]
        ),
    )
    data = execute_tool(ctx, "orders.sales_summary", {"days": 7}).data
    assert data["orders"] == 2
    assert data["total_sales"] == "150.00" and data["average_order_value"] == "75.00"
    assert data["total_discounts"] == "10.00" and data["currency"] == "GBP"
    assert data["period"] == {"from": "2026-11-11", "to": "2026-11-17", "timezone": "Europe/London"}
    query = fake_shopify.calls_to("OrdersForStats")[0]["query"]
    assert query == "created_at:>='2026-11-11T00:00:00Z' AND created_at:<'2026-11-17T12:00:00Z'"


def test_timeseries_buckets_in_shop_timezone_and_pages(ctx, fake_shopify) -> None:
    ctx.shop.timezone = "America/New_York"  # UTC-5 in November
    fake_shopify.on(
        "OrdersForStats",
        pages(
            [order(1, "2026-11-16T03:00:00Z", "10.00")],  # 15 Nov 22:00 in New York
            [order(2, "2026-11-16T15:00:00Z", "20.00"), order(3, "2026-11-16T16:00:00Z", "30.00")],
        ),
    )
    data = execute_tool(ctx, "orders.timeseries", {"days": 7, "interval": "day"}).data
    assert [(s["period"], s["orders"], s["total_sales"]) for s in data["series"]] == [
        ("2026-11-15", 1, "10.00"),
        ("2026-11-16", 2, "50.00"),
    ]
    assert len(fake_shopify.calls_to("OrdersForStats")) == 2
    assert fake_shopify.calls_to("OrdersForStats")[1]["after"] == "c0"


def test_weekly_buckets(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "OrdersForStats",
        pages(
            [order(1, "2026-11-12T10:00:00Z", "10.00"), order(2, "2026-11-16T10:00:00Z", "10.00")]
        ),
    )
    data = execute_tool(ctx, "orders.timeseries", {"days": 14, "interval": "week"}).data
    assert [s["period"] for s in data["series"]] == ["week of 2026-11-09", "week of 2026-11-16"]


def test_stops_at_safety_cap(ctx, fake_shopify, monkeypatch) -> None:
    monkeypatch.setattr(_stats, "MAX_ORDERS", 2)
    fake_shopify.on(
        "OrdersForStats",
        pages(
            [order(1, "2026-11-16T10:00:00Z", "1"), order(2, "2026-11-16T10:00:00Z", "1")],
            [order(3, "2026-11-16T10:00:00Z", "1")],
        ),
    )
    data = execute_tool(ctx, "orders.sales_summary", {"days": 3}).data
    assert data["truncated"] is True and data["orders"] == 2


def test_top_products(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "OrderLinesForStats",
        pages(
            [
                order(1, "", "0", lines=[("Tea", 2, "20.00"), ("Mug", 1, "30.00")]),
                order(2, "", "0", lines=[("Tea", 5, "50.00")]),
                order(3, "", "0", cancelled=True, lines=[("Mug", 100, "999.00")]),
            ]
        ),
    )
    by_revenue = execute_tool(ctx, "analytics.top_products", {"days": 30}).data["products"]
    assert [(p["title"], p["quantity"], p["revenue"]) for p in by_revenue] == [
        ("Tea", 7, "70.00"),
        ("Mug", 1, "30.00"),
    ]


def test_top_customers_skips_guests(ctx, fake_shopify) -> None:
    jo = {"id": "gid://shopify/Customer/1", "displayName": "Jo"}
    fake_shopify.on(
        "OrderCustomersForStats",
        pages(
            [
                order(1, "", "10.00", customer=jo),
                order(2, "", "15.00", customer=jo),
                order(3, "", "500.00"),
            ]
        ),
    )
    data = execute_tool(ctx, "analytics.top_customers", {"days": 30}).data
    assert data["customers"] == [
        {"customer_id": jo["id"], "name": "Jo", "orders": 2, "spent": "25.00"}
    ]


def test_aov_compares_with_previous_period(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "OrdersForStats",
        [
            pages([order(1, "2026-11-16T10:00:00Z", "60.00")])[0],  # current period
            pages(
                [
                    order(2, "2026-11-05T10:00:00Z", "40.00"),
                    order(3, "2026-11-06T10:00:00Z", "40.00"),
                ]
            )[0],
        ],
    )
    data = execute_tool(ctx, "analytics.aov", {"days": 7}).data
    assert data["current"]["average_order_value"] == "60.00"
    assert data["previous"]["average_order_value"] == "40.00"
    assert data["aov_change"] == "50.0%"
    current_q, previous_q = [c["query"] for c in fake_shopify.calls_to("OrdersForStats")]
    assert "created_at:<'2026-11-11T00:00:00Z'" in previous_q  # previous ends where current starts
    assert "created_at:>='2026-11-11T00:00:00Z'" in current_q
