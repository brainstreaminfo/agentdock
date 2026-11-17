"""Customer tools: search, read, tag, list segments."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import PageInput, build_query, gid, load_document, money, nodes, page
from .orders import TagListInput, add_tags_tool

DOC = load_document("customers")
PCD_NOTE = "Requires the app's protected customer data access (approved by Shopify)."


def _summary(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c["id"],
        "name": c.get("displayName"),
        "email": (c.get("defaultEmailAddress") or {}).get("emailAddress"),
        "orders_count": c.get("numberOfOrders"),
        "amount_spent": money(c.get("amountSpent")),
        "tags": c.get("tags", []),
        "state": c.get("state"),
        "created_at": c.get("createdAt"),
    }


class SearchCustomersInput(PageInput):
    query: str = Field("", description="Name, email, or Shopify customer search syntax.")
    tag: str | None = None
    sort: Literal["RELEVANCE", "NAME", "UPDATED_AT", "CREATED_AT"] = "RELEVANCE"


@tool(
    key="customers.search",
    name="Search customers",
    description=f"""Find customers by name, email or tag. Returns orders count and
    total spent. {PCD_NOTE}""",
    category="Customers",
    scopes=["read_customers"],
)
def search_customers(ctx: ToolContext, args: SearchCustomersInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "SearchCustomers",
        {
            "first": args.limit,
            "after": args.cursor,
            "query": build_query(args.query, tag=args.tag) or None,
            "sortKey": args.sort,
            "reverse": args.sort in ("UPDATED_AT", "CREATED_AT"),
        },
    )
    result = page(data["customers"], "customers")
    result["customers"] = [_summary(c) for c in result["customers"]]
    return result


class CustomerIdInput(ToolInput):
    customer_id: str


@tool(
    key="customers.get",
    name="Get customer",
    description=f"""One customer with their note, city/country, and last 10 orders.
    {PCD_NOTE}""",
    category="Customers",
    scopes=["read_customers", "read_orders"],
)
def get_customer(ctx: ToolContext, args: CustomerIdInput) -> dict[str, Any]:
    c = ctx.gql(DOC, "GetCustomer", {"id": gid("Customer", args.customer_id)})["customer"]
    if c is None:
        raise ToolError(f"No customer found with id {args.customer_id}.")
    out = _summary(c)
    out.update(
        {
            "note": c.get("note"),
            "location": c.get("defaultAddress"),
            "last_order": c.get("lastOrder"),
            "recent_orders": [
                {
                    "id": o["id"],
                    "name": o["name"],
                    "created_at": o["createdAt"],
                    "financial_status": o.get("displayFinancialStatus"),
                    "total": money(o.get("currentTotalPriceSet")),
                }
                for o in nodes(c.get("orders"))
            ],
        }
    )
    return out


class CustomerTagsInput(CustomerIdInput, TagListInput):
    pass


def _customer_state(ctx: ToolContext, args: CustomerTagsInput) -> dict[str, Any]:
    c = ctx.gql(DOC, "CustomerTags", {"id": gid("Customer", args.customer_id)})["customer"]
    if c is None:
        raise ToolError(f"No customer found with id {args.customer_id}.")
    return dict(c)


add_customer_tags = add_tags_tool(
    "customer", "customers.add_tags", DOC, _customer_state, CustomerTagsInput
)


class SegmentsInput(PageInput):
    query: str = ""


@tool(
    key="customers.segments_list",
    name="List customer segments",
    description="Customer segments defined in the store (name and segment query).",
    category="Customers",
    scopes=["read_customers"],
)
def list_segments(ctx: ToolContext, args: SegmentsInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "ListSegments",
        {"first": args.limit, "after": args.cursor, "query": args.query or None},
    )
    return page(data["segments"], "segments")
