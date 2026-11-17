"""Custom GraphQL tools (spec §6.2) and the Studio tool pages."""

from __future__ import annotations

import json

import pytest

from apps.accounts.models import Membership
from apps.runs.models import ActionProposal, Run
from apps.tools.custom import apply_output_path, check_custom_tool
from apps.tools.executor import execute_tool
from apps.tools.models import ToolDefinition
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db

QUERY = """query RecentProducts($first: Int!) {
  products(first: $first) { nodes { id title } }
}"""
SCHEMA = {
    "type": "object",
    "properties": {"first": {"type": "integer", "minimum": 1, "maximum": 50}},
    "required": ["first"],
}
MUTATION = """mutation TagIt($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { userErrors { message } }
}"""
MUTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "tags"],
}


def check(**overrides):
    args = {
        "key": "products.recent",
        "document": QUERY,
        "input_schema": SCHEMA,
        "scopes": ["read_products"],
    }
    return check_custom_tool(**{**args, **overrides})


# ------------------------------------------------------------------ validation
def test_valid_tool() -> None:
    problems, info = check()
    assert problems == [] and info.operation_type == "query" and info.variables == {"first": True}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"key": "Bad Key"}, "Key must look like"),
        ({"document": "query { shop { name "}, "Syntax error"),
        ({"document": "query A { shop { name } } query B { shop { id } }"}, "exactly one"),
        (
            {"document": "query { shop { nope } }", "input_schema": {"type": "object"}},
            "Cannot query field 'nope'",
        ),
        ({"input_schema": {"type": "object", "properties": {}}}, "Variable $first is missing"),
        ({"input_schema": {**SCHEMA, "required": []}}, "Variable $first is required"),
        (
            {
                "input_schema": {
                    **SCHEMA,
                    "properties": {**SCHEMA["properties"], "x": {"type": "string"}},
                }
            },
            "Input 'x' is not a variable",
        ),
        ({"input_schema": {"type": "nonsense"}}, "not valid JSON Schema"),
        ({"scopes": []}, "at least one Shopify scope"),
        ({"scopes": ["read_everything"]}, "Unknown Shopify scope"),
        (
            {"document": MUTATION, "input_schema": MUTATION_SCHEMA, "scopes": ["read_orders"]},
            "needs at least one write_* scope",
        ),
    ],
)
def test_invalid_tools_are_explained(overrides, expected) -> None:
    problems, _ = check(**overrides)
    assert any(expected in p for p in problems), problems


def test_output_path() -> None:
    data = {"products": {"edges": [{"node": {"id": 1}}, {"node": {"id": 2}}]}}
    assert apply_output_path(data, "data.products.edges[*].node") == [{"id": 1}, {"id": 2}]
    assert apply_output_path(data, "products.edges[1].node.id") == 2
    assert apply_output_path(data, "products.missing") is None
    assert apply_output_path(data, "") == data


# ------------------------------------------------------------------ running
@pytest.fixture
def custom_query(db):
    return ToolDefinition.objects.create(
        key="products.recent",
        name="Recent products",
        category="Products",
        kind="GRAPHQL",
        description="Newest products",
        graphql_document=QUERY,
        input_schema=SCHEMA,
        scopes=["read_products"],
        output_path="products.nodes",
    )


def test_custom_tool_runs_through_executor(ctx, fake_shopify, custom_query) -> None:
    fake_shopify.on("RecentProducts", {"products": {"nodes": [{"id": "p1", "title": "Tea"}]}})
    result = execute_tool(ctx, "products.recent", {"first": 3})
    assert result.ok and result.data == [{"id": "p1", "title": "Tea"}]
    assert fake_shopify.calls_to("RecentProducts") == [{"first": 3}]


def test_custom_tool_input_validated_by_json_schema(ctx, custom_query) -> None:
    result = execute_tool(ctx, "products.recent", {"first": 500})
    assert result.status == "error" and "first" in result.error


def test_custom_tool_scopes_enforced(ctx, custom_query) -> None:
    ctx.shop.granted_scopes = ["read_orders"]
    assert "read_products" in execute_tool(ctx, "products.recent", {"first": 1}).error


def test_custom_mutation_goes_to_approval_and_has_no_undo(tool_shop, fake_shopify) -> None:
    from apps.tools.context import ToolContext

    ToolDefinition.objects.create(
        key="orders.tag_it",
        name="Tag",
        category="Orders",
        kind="GRAPHQL",
        graphql_document=MUTATION,
        input_schema=MUTATION_SCHEMA,
        scopes=["write_orders"],
        mutating=True,
    )
    result = execute_tool(
        ToolContext(shop=tool_shop), "orders.tag_it", {"id": "gid://shopify/Order/1", "tags": ["x"]}
    )
    assert result.status == "pending_approval"
    assert ActionProposal.objects.get(uuid=result.proposal_id).preview["inputs"] == {
        "id": "gid://shopify/Order/1",
        "tags": ["x"],
    }


def test_executor_refuses_unmarked_mutation(ctx, fake_shopify) -> None:
    """Even if someone edits the database by hand, an unmarked mutation never runs."""
    ToolDefinition.objects.create(
        key="orders.sneaky",
        name="Sneaky",
        category="Orders",
        kind="GRAPHQL",
        graphql_document=MUTATION,
        input_schema=MUTATION_SCHEMA,
        scopes=["write_orders"],
        mutating=False,
    )
    result = execute_tool(ctx, "orders.sneaky", {"id": "gid://shopify/Order/1", "tags": ["x"]})
    assert "Refused" in result.error
    assert fake_shopify.calls == []


# ------------------------------------------------------------------ studio pages
@pytest.fixture
def staff(tool_shop):
    user = UserFactory(is_staff=True)
    Membership.objects.create(user=user, shop=tool_shop, role="OWNER")
    return user


@pytest.mark.parametrize(
    "path", ["/studio/tools/", "/studio/tools/new/", "/studio/tools/shop.info/"]
)
def test_studio_is_staff_only(client, path, tool_shop) -> None:
    assert client.get(path).status_code == 302  # anonymous -> login
    client.force_login(UserFactory())
    assert client.get(path).status_code == 404  # merchants don't even see it exists


def test_catalog_page_lists_all_tools(client, staff) -> None:
    client.force_login(staff)
    content = client.get("/studio/tools/").content.decode()
    assert "products.update" in content and "web.fetch_url" in content
    assert "50 tools" in content


def test_create_custom_tool_via_studio(client, staff) -> None:
    client.force_login(staff)
    resp = client.post(
        "/studio/tools/new/",
        {
            "key": "orders.tag_it",
            "name": "Tag order",
            "category": "Orders",
            "description": "Adds tags to an order. Use for follow-ups.",
            "graphql_document": MUTATION,
            "input_schema": json.dumps(MUTATION_SCHEMA),
            "scopes_text": "write_orders",
            "enabled": "on",
        },
    )
    assert resp.status_code == 302, resp.content.decode()[:2000]
    tool = ToolDefinition.objects.get(key="orders.tag_it")
    assert tool.kind == "GRAPHQL" and tool.mutating is True  # auto-detected
    assert tool.created_by == staff


def test_studio_shows_schema_errors(client, staff) -> None:
    client.force_login(staff)
    resp = client.post(
        "/studio/tools/new/",
        {
            "key": "shop.bad",
            "name": "Bad",
            "category": "Shop",
            "description": "x",
            "graphql_document": "query { shop { nope } }",
            "input_schema": '{"type": "object"}',
            "scopes_text": "read_products",
        },
    )
    assert resp.status_code == 200
    assert "Cannot query field" in resp.content.decode()
    assert not ToolDefinition.objects.filter(key="shop.bad").exists()


def test_builtin_key_cannot_be_reused(client, staff) -> None:
    client.force_login(staff)
    resp = client.post(
        "/studio/tools/new/",
        {
            "key": "products.get",
            "name": "x",
            "category": "x",
            "description": "x",
            "graphql_document": QUERY,
            "input_schema": json.dumps(SCHEMA),
            "scopes_text": "read_products",
        },
    )
    assert "built-in tool already uses this key" in resp.content.decode()


def test_toggle_tool(client, staff) -> None:
    client.force_login(staff)
    client.get("/studio/tools/")  # first visit syncs tools
    client.post("/studio/tools/shop.info/toggle/")
    assert ToolDefinition.objects.get(key="shop.info").enabled is False


def test_test_button_runs_against_own_store(client, staff, tool_shop, fake_shopify) -> None:
    fake_shopify.on(
        "ShopInfo",
        {
            "shop": {
                "name": "Tea",
                "myshopifyDomain": "x",
                "currencyCode": "GBP",
                "ianaTimezone": "UTC",
            }
        },
    )
    client.force_login(staff)
    resp = client.post("/studio/tools/shop.info/test/", {"shop": tool_shop.pk, "arguments": "{}"})
    assert resp.status_code == 200 and "OK" in resp.content.decode()
    run = Run.objects.get(trigger=Run.Trigger.TOOL_TEST)
    assert run.status == "SUCCEEDED" and run.steps.count() == 1


def test_test_button_cannot_target_someone_elses_store(client, staff) -> None:
    from tests.factories import ShopFactory

    other = ShopFactory()
    client.force_login(staff)
    resp = client.post("/studio/tools/shop.info/test/", {"shop": other.pk, "arguments": "{}"})
    assert "Select a valid choice" in resp.content.decode()
    assert not Run.objects.exists()


def test_test_button_requires_confirmation_for_writes(client, staff, tool_shop) -> None:
    client.force_login(staff)
    resp = client.post(
        "/studio/tools/products.update/test/",
        {"shop": tool_shop.pk, "arguments": json.dumps({"product_id": "1", "title": "X"})},
    )
    assert "Tick this box" in resp.content.decode()
    assert not Run.objects.exists()


def test_tool_detail_page_renders_each_section_once(client, staff) -> None:
    client.force_login(staff)
    content = client.get("/studio/tools/products.update/").content.decode()
    for heading in ("What the AI is told", "Shopify permissions", "Inputs", "Run tool"):
        assert content.count(heading) == 1, heading
    assert "product_id" in content and "one of: ACTIVE, DRAFT, ARCHIVED" in content
