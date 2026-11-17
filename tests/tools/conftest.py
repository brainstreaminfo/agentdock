"""
Fixtures for tool tests.

`fake_shopify` replaces the GraphQL client. Register answers per operation:

    fake_shopify.on("GetProduct", {"product": {...}})            # same answer every time
    fake_shopify.on("UpdateProduct", lambda v: {...})            # computed from variables
    fake_shopify.on("Op", [{...}, {...}])                        # one per call, in order

Every call is CHECKED against Shopify's real 2026-10 schema:
* the operation exists in the document that was sent;
* the variables match the operation's variable definitions: required ones are
  present and every value fits its GraphQL input type (fields, enums, scalars).
So a test fails if a tool sends input Shopify would reject.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any

import pytest
from graphql import OperationDefinitionNode, parse
from graphql.execution.values import get_variable_values

from apps.shopify_app.graphql import GraphQLResult, ShopifyGraphQL
from apps.shopify_app.schema import load_schema
from apps.tools.context import ToolContext
from tests.factories import ShopFactory

Answer = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | list[dict[str, Any]]


@cache
def _operations(document: str) -> dict[str, OperationDefinitionNode]:
    ast = parse(document)
    return {
        d.name.value: d
        for d in ast.definitions
        if isinstance(d, OperationDefinitionNode) and d.name is not None
    }


class FakeShopify:
    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def on(self, operation: str, answer: Answer) -> FakeShopify:
        self.answers[operation] = list(answer) if isinstance(answer, list) else answer
        return self

    def calls_to(self, operation: str) -> list[dict[str, Any]]:
        return [v for op, v in self.calls if op == operation]

    def execute(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str | None = None,
    ) -> GraphQLResult:
        variables = variables or {}
        ops = _operations(document)
        assert operation_name in ops, f"Operation {operation_name} is not in the document"
        coerced = get_variable_values(
            load_schema(), ops[operation_name].variable_definitions or [], variables
        )
        assert not isinstance(coerced, list), (
            f"{operation_name}: variables rejected by Shopify's schema: "
            + "; ".join(e.message for e in coerced)
        )
        self.calls.append((operation_name, variables))
        if operation_name not in self.answers:
            raise AssertionError(f"No fake answer registered for {operation_name}")
        answer = self.answers[operation_name]
        if isinstance(answer, list):
            assert answer, f"No more fake answers left for {operation_name}"
            data = answer.pop(0)
        elif callable(answer):
            data = answer(variables)
        else:
            data = answer
        return GraphQLResult(data=data, extensions={})


@pytest.fixture
def fake_shopify(monkeypatch: pytest.MonkeyPatch) -> FakeShopify:
    fake = FakeShopify()

    def execute(
        self: ShopifyGraphQL, document: str, variables: Any = None, *, operation_name: Any = None
    ):
        return fake.execute(document, variables, operation_name=operation_name)

    monkeypatch.setattr(ShopifyGraphQL, "execute", execute)
    return fake


ALL_SCOPES = [
    "write_products",
    "write_inventory",
    "write_orders",
    "write_customers",
    "write_discounts",
    "write_content",
    "write_files",
    "read_legal_policies",
    "read_all_orders",
]


@pytest.fixture
def tool_shop(db: Any) -> Any:
    """An installed shop that granted every scope the catalog uses."""
    return ShopFactory(
        domain="tools-store.myshopify.com",
        granted_scopes=ALL_SCOPES,
        timezone="Europe/London",
        currency="GBP",
    )


@pytest.fixture
def ctx(tool_shop: Any) -> ToolContext:
    return ToolContext(shop=tool_shop, autonomy="AUTO")
