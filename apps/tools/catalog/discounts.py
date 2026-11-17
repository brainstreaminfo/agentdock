"""Discount tools: list discounts, create a discount code, deactivate one."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import PageInput, gid, load_document, nodes, page

DOC = load_document("discounts")


def _flatten(node: dict[str, Any]) -> dict[str, Any]:
    d = node.get("discount") or {}
    return {
        "id": node["id"],
        "type": d.get("__typename"),
        "title": d.get("title"),
        "status": d.get("status"),
        "summary": d.get("summary"),
        "starts_at": d.get("startsAt"),
        "ends_at": d.get("endsAt"),
        "times_used": d.get("asyncUsageCount"),
        "codes": [c["code"] for c in nodes(d.get("codes"))],
    }


class ListDiscountsInput(PageInput):
    query: str = Field("", description="e.g. 'status:active' or a title/code fragment.")


@tool(
    key="discounts.list",
    name="List discounts",
    description="""Discounts in the store (codes and automatic), newest first, with
    status, summary, dates and how often codes were used.""",
    category="Discounts",
    scopes=["read_discounts"],
)
def list_discounts(ctx: ToolContext, args: ListDiscountsInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "ListDiscounts",
        {"first": args.limit, "after": args.cursor, "query": args.query or None},
    )
    result = page(data["discountNodes"], "discounts")
    result["discounts"] = [_flatten(n) for n in result["discounts"]]
    return result


# --------------------------------------------------------------------------- create
class CreateCodeInput(ToolInput):
    code: str = Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    title: str | None = Field(None, description="Internal name; defaults to the code.")
    percentage: float | None = Field(None, gt=0, le=100, description="e.g. 15 for 15% off.")
    amount_off: Decimal | None = Field(None, gt=0, description="Fixed amount off the order.")
    minimum_subtotal: Decimal | None = Field(None, gt=0)
    usage_limit: int | None = Field(None, ge=1, description="Total number of uses allowed.")
    once_per_customer: bool = True
    starts_at: datetime | None = Field(None, description="Default: now.")
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def _one_value(self) -> CreateCodeInput:
        if (self.percentage is None) == (self.amount_off is None):
            raise ValueError("Give exactly one of percentage or amount_off.")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


def _discount_input(args: CreateCodeInput) -> dict[str, Any]:
    value: dict[str, Any] = (
        {"percentage": round(args.percentage / 100, 4)}
        if args.percentage is not None
        else {"discountAmount": {"amount": str(args.amount_off), "appliesOnEachItem": False}}
    )
    body: dict[str, Any] = {
        "title": args.title or args.code,
        "code": args.code,
        "startsAt": (args.starts_at or datetime.now().astimezone()).isoformat(),
        "context": {"all": "ALL"},
        "customerGets": {"value": value, "items": {"all": True}},
        "appliesOncePerCustomer": args.once_per_customer,
    }
    if args.ends_at:
        body["endsAt"] = args.ends_at.isoformat()
    if args.usage_limit:
        body["usageLimit"] = args.usage_limit
    if args.minimum_subtotal:
        body["minimumRequirement"] = {
            "subtotal": {"greaterThanOrEqualToSubtotal": str(args.minimum_subtotal)}
        }
    return body


@tool(
    key="discounts.create_code",
    name="Create discount code",
    description="""Create a discount code for the whole order: either a percentage
    or a fixed amount off, with optional minimum subtotal, usage limit and dates.""",
    category="Discounts",
    scopes=["write_discounts"],
    mutating=True,
)
def create_code(ctx: ToolContext, args: CreateCodeInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "CreateBasicCode",
        {"basicCodeDiscount": _discount_input(args)},
        user_errors_at="discountCodeBasicCreate",
    )
    node = data["discountCodeBasicCreate"]["codeDiscountNode"]
    return {"id": node["id"], "code": args.code, **(node.get("codeDiscount") or {})}


@create_code.preview
def _preview_create(ctx: ToolContext, args: CreateCodeInput) -> dict[str, Any]:
    what = f"{args.percentage:g}% off" if args.percentage else f"{args.amount_off} off"
    details = [what]
    if args.minimum_subtotal:
        details.append(f"orders over {args.minimum_subtotal}")
    if args.usage_limit:
        details.append(f"max {args.usage_limit} uses")
    if args.once_per_customer:
        details.append("once per customer")
    if args.ends_at:
        details.append(f"ends {args.ends_at:%Y-%m-%d %H:%M}")
    return {"summary": f"Create discount code {args.code}: {', '.join(details)}", "code": args.code}


@create_code.undo
def _undo_create(ctx: ToolContext, args: CreateCodeInput, before: dict, after: dict) -> Any:
    return ctx.gql(DOC, "DeleteCode", {"id": after["id"]}, user_errors_at="discountCodeDelete")


# --------------------------------------------------------------------------- deactivate
class DiscountIdInput(ToolInput):
    discount_id: str = Field(description="gid://shopify/DiscountCodeNode/… from discounts.list")


def _code_discount(ctx: ToolContext, discount_id: str) -> dict[str, Any]:
    node = ctx.gql(DOC, "GetCodeDiscount", {"id": gid("DiscountCodeNode", discount_id)})[
        "codeDiscountNode"
    ]
    if node is None:
        raise ToolError(
            "No discount code found with that id (automatic discounts aren't supported)."
        )
    return {"id": node["id"], **(node.get("codeDiscount") or {})}


@tool(
    key="discounts.deactivate",
    name="Deactivate discount code",
    description="Stop a discount code from working (it can be reactivated with undo).",
    category="Discounts",
    scopes=["write_discounts"],
    mutating=True,
)
def deactivate(ctx: ToolContext, args: DiscountIdInput) -> dict[str, Any]:
    d = _code_discount(ctx, args.discount_id)
    ctx.gql(DOC, "DeactivateCode", {"id": d["id"]}, user_errors_at="discountCodeDeactivate")
    return {"id": d["id"], "deactivated": True}


@deactivate.preview
def _preview_deactivate(ctx: ToolContext, args: DiscountIdInput) -> dict[str, Any]:
    d = _code_discount(ctx, args.discount_id)
    return {
        "summary": f"Deactivate discount “{d.get('title')}”",
        "changes": [{"field": "status", "before": d.get("status"), "after": "EXPIRED"}],
    }


@deactivate.snapshot
def _snapshot_deactivate(ctx: ToolContext, args: DiscountIdInput) -> dict[str, Any]:
    return _code_discount(ctx, args.discount_id)


@deactivate.undo
def _undo_deactivate(ctx: ToolContext, args: DiscountIdInput, before: dict, after: dict) -> Any:
    if before.get("status") != "ACTIVE":
        return {"message": "It wasn't active before, so nothing to restore."}
    return ctx.gql(DOC, "ActivateCode", {"id": before["id"]}, user_errors_at="discountCodeActivate")
