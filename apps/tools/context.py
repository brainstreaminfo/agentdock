"""
ToolContext: everything a tool receives besides its input.

    ctx.shop            the Shop (read-only; tools must not query other shops)
    ctx.gql(...)        run one operation of a .graphql document for THIS shop
    ctx.log             logger already tagged with shop_id / run_id / tool
    ctx.require_scopes  raise MissingScopesError if the shop lacks a scope
    ctx.idempotency_key unique per approved action (safe retries for Shopify)

Tools never get a raw token or a client for another shop: the GraphQL client is
created here from `ctx.shop` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from apps.core.logging import get_logger
from apps.shopify_app.graphql import ShopifyGraphQL

from .errors import MissingScopesError, ShopifyUserError

if TYPE_CHECKING:
    from apps.runs.models import Run
    from apps.shops.models import Shop


@dataclass
class ToolContext:
    shop: Shop
    run: Run | None = None
    autonomy: str = "APPROVE"  # apps.agents.models.Autonomy
    agent_slug: str = ""
    # Playground / Studio tests: every mutation goes to approval, even in AUTO.
    force_approval: bool = False
    # Set by the executor when it applies an approved action.
    idempotency_key: str | None = None
    tool_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @cached_property
    def graphql(self) -> ShopifyGraphQL:
        return ShopifyGraphQL(self.shop)

    @property
    def log(self) -> Any:
        return get_logger("apps.tools").bind(
            shop_id=self.shop.pk, run_id=getattr(self.run, "pk", None), tool=self.tool_key
        )

    def gql(
        self,
        document: str,
        operation: str | None,
        variables: dict[str, Any] | None = None,
        *,
        user_errors_at: str | None = None,
    ) -> dict[str, Any]:
        """
        Run `operation` from `document` and return `data`.

        `user_errors_at="productUpdate"` raises ShopifyUserError if
        `data.productUpdate.userErrors` is not empty (Shopify's way of saying
        "your mutation input was rejected").
        """
        result = self.graphql.execute(document, variables, operation_name=operation)
        data = result.data
        if user_errors_at:
            payload = data.get(user_errors_at) or {}
            errors = payload.get("userErrors") or []
            if errors:
                text = "; ".join(_format_user_error(e) for e in errors)
                raise ShopifyUserError(f"Shopify rejected the change: {text}", details=errors)
        return data

    def require_scopes(self, scopes: list[str]) -> None:
        missing = self.shop.missing_scopes(scopes)
        if missing:
            raise MissingScopesError(missing)


def _format_user_error(error: dict[str, Any]) -> str:
    """{"field": ["product", "title"], "message": "is too long"} -> "product.title: is too long"."""
    field_path = ".".join(str(p) for p in (error.get("field") or []))
    message = str(error.get("message"))
    return f"{field_path}: {message}" if field_path else message
