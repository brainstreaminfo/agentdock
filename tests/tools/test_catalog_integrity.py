"""
Checks that cover the whole catalog at once, so a new tool can't slip in broken:
GraphQL valid against Shopify's schema, operation names exist, metadata complete.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from graphql import OperationDefinitionNode, OperationType, parse

from apps.shopify_app.schema import load_schema, validate_document
from apps.shopify_app.scopes import KNOWN_SCOPES
from apps.tools.models import ToolDefinition
from apps.tools.registry import registry
from apps.tools.sync import sync_tools

CATALOG = Path(__file__).resolve().parents[2] / "apps" / "tools" / "catalog"
GRAPHQL_FILES = sorted(CATALOG.glob("*.graphql"))

# The spec's v1 catalog (§6.1). If you add a tool, add it here too.
SPEC_TOOLS = {
    "shop.info",
    "shop.policies",
    "products.search",
    "products.get",
    "products.update",
    "products.update_seo",
    "products.update_images_alt",
    "variants.update_price",
    "collections.list",
    "collections.get",
    "collections.add_products",
    "collections.create",
    "inventory.levels",
    "inventory.adjust",
    "inventory.low_stock",
    "orders.search",
    "orders.get",
    "orders.add_tags",
    "orders.add_note",
    "orders.timeseries",
    "orders.sales_summary",
    "customers.search",
    "customers.get",
    "customers.add_tags",
    "customers.segments_list",
    "discounts.list",
    "discounts.create_code",
    "discounts.deactivate",
    "pages.list",
    "pages.get",
    "pages.create",
    "pages.update",
    "blogs.list",
    "articles.list",
    "articles.get",
    "articles.create_draft",
    "articles.update",
    "metafields.get",
    "metafields.set",
    "files.list",
    "analytics.top_products",
    "analytics.top_customers",
    "analytics.aov",
    "ui.render_chart",
    "ui.render_table",
    "alerts.create",
    "memory.read",
    "memory.propose_update",
    "reply.draft",
    "web.fetch_url",
}


def test_schema_file_is_present() -> None:
    assert load_schema() is not None, "Run scripts/update_shopify_schema.py"


@pytest.mark.parametrize("path", GRAPHQL_FILES, ids=lambda p: p.name)
def test_graphql_documents_are_valid_against_shopify_schema(path: Path) -> None:
    assert validate_document(path.read_text()) == []


@pytest.mark.parametrize("path", GRAPHQL_FILES, ids=lambda p: p.name)
def test_every_mutation_asks_for_user_errors(path: Path) -> None:
    for op in parse(path.read_text()).definitions:
        if isinstance(op, OperationDefinitionNode) and op.operation == OperationType.MUTATION:
            assert "userErrors" in path.read_text()[op.loc.start : op.loc.end], op.name.value


def test_every_operation_used_in_python_exists() -> None:
    """Catches typos like ctx.gql(DOC, "GetProdcut")."""
    pattern = re.compile(r'gql\(\s*(?:DOC|doc),\s*"(\w+)"')
    for py in CATALOG.glob("*.py"):
        used = set(pattern.findall(py.read_text()))
        if not used:
            continue
        domain = "orders" if py.stem in ("_stats", "analytics") else py.stem
        graphql = CATALOG / f"{domain}.graphql"
        if py.stem == "customers":
            graphql = CATALOG / "customers.graphql"
        defined = {
            d.name.value
            for d in parse(graphql.read_text()).definitions
            if isinstance(d, OperationDefinitionNode) and d.name
        }
        # orders.py's shared tags builder is also used with the customers document.
        assert used <= defined | {"AddTags", "RemoveTags"}, f"{py.name}: {used - defined}"


def test_catalog_matches_spec() -> None:
    assert set(registry.keys()) == SPEC_TOOLS


@pytest.mark.parametrize("spec", registry.all(), ids=lambda s: s.key)
def test_tool_metadata(spec) -> None:
    assert re.match(r"^[a-z]+\.[a-z_]+$", spec.key)
    assert spec.name and spec.category
    assert len(spec.description) >= 30, "Describe the tool well: the AI reads this"
    assert set(spec.scopes) <= KNOWN_SCOPES
    json.dumps(spec.input_schema())  # must be JSON-serialisable for the LLM
    writes = [s for s in spec.scopes if s.startswith("write_")]
    if spec.mutating and spec.category not in ("Platform", "Metafields"):
        assert writes, f"{spec.key} changes data but declares no write_ scope"
    if not spec.mutating:
        assert not writes, f"{spec.key} only reads but asks for {writes}"


# Mutations that can't be reversed automatically (documented exceptions).
NO_UNDO: set[str] = set()


@pytest.mark.parametrize("spec", [s for s in registry.all() if s.mutating], ids=lambda s: s.key)
def test_mutating_tools_support_undo(spec) -> None:
    assert spec.can_undo or spec.key in NO_UNDO


@pytest.mark.django_db
def test_sync_tools_is_idempotent_and_keeps_enabled_flag() -> None:
    report = sync_tools()
    assert len(report.created) == len(registry)
    assert not ToolDefinition.objects.get(key="web.fetch_url").enabled  # off by default

    ToolDefinition.objects.filter(key="products.update").update(enabled=False)
    report = sync_tools()
    assert report.created == [] and report.updated == []
    assert not ToolDefinition.objects.get(key="products.update").enabled

    ToolDefinition.objects.create(key="old.tool", name="Old", category="X", kind="CODE")
    assert sync_tools().removed == ["old.tool"]
    assert not ToolDefinition.objects.get(key="old.tool").enabled


@pytest.mark.django_db
def test_sync_refuses_custom_tool_with_builtin_key() -> None:
    ToolDefinition.objects.create(key="products.get", name="X", category="X", kind="GRAPHQL")
    with pytest.raises(ValueError, match="same key"):
        sync_tools()
