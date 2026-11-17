"""Platform tools: charts, tables, alerts, brand memory, reply drafts, web fetch."""

from __future__ import annotations

import socket

import httpx
import pytest

from apps.agents.models import ShopMemory
from apps.dashboard.models import Alert
from apps.runs.models import ActionProposal
from apps.tools.catalog import platform
from apps.tools.context import ToolContext
from apps.tools.executor import apply_proposal, execute_tool, undo_proposal
from apps.tools.models import ToolDefinition

pytestmark = pytest.mark.django_db


def test_chart_validates_shape(ctx) -> None:
    good = {
        "type": "line",
        "title": "Sales",
        "labels": ["Mon", "Tue"],
        "datasets": [{"label": "GBP", "data": [10, 12.5]}],
    }
    assert execute_tool(ctx, "ui.render_chart", good).data["render"] == "chart"
    bad = {**good, "datasets": [{"label": "GBP", "data": [1]}]}
    assert "one value per label" in execute_tool(ctx, "ui.render_chart", bad).error


def test_table_validates_rows(ctx) -> None:
    assert execute_tool(ctx, "ui.render_table", {"columns": ["A", "B"], "rows": [[1, "x"]]}).ok
    assert (
        "Row 0" in execute_tool(ctx, "ui.render_table", {"columns": ["A"], "rows": [[1, 2]]}).error
    )


def test_alerts_are_deduplicated_per_agent_and_title(ctx) -> None:
    ctx.agent_slug = "stock-watch"
    for _ in range(3):
        execute_tool(
            ctx, "alerts.create", {"title": "3 products out of stock", "severity": "warning"}
        )
    alert = Alert.objects.get(shop=ctx.shop)
    assert alert.severity == "WARNING" and alert.agent_slug == "stock-watch"


def test_memory_read_is_shop_scoped(ctx) -> None:
    from tests.factories import ShopFactory

    ShopMemory.objects.create(shop=ctx.shop, key="brand_voice", value="Warm and calm")
    ShopMemory.objects.create(shop=ShopFactory(), key="brand_voice", value="OTHER SHOP")
    assert execute_tool(ctx, "memory.read", {}).data == {"memory": {"brand_voice": "Warm and calm"}}


def test_memory_update_needs_approval_and_can_be_undone(tool_shop) -> None:
    ShopMemory.objects.create(shop=tool_shop, key="banned_words", value="cheap")
    ctx = ToolContext(shop=tool_shop, autonomy="APPROVE")
    result = execute_tool(
        ctx,
        "memory.propose_update",
        {"key": "banned_words", "value": "cheap, bargain", "reason": "Merchant said so"},
    )
    assert result.status == "pending_approval"
    assert ShopMemory.objects.get(key="banned_words").value == "cheap"

    proposal = apply_proposal(ActionProposal.objects.get(uuid=result.proposal_id))
    assert ShopMemory.objects.get(key="banned_words").value == "cheap, bargain"
    undo_proposal(proposal)
    memory = ShopMemory.objects.get(key="banned_words")
    assert memory.value == "cheap" and memory.source == "MERCHANT"


def test_memory_undo_deletes_new_key(ctx) -> None:
    result = execute_tool(
        ctx, "memory.propose_update", {"key": "audience", "value": "Runners", "reason": "x"}
    )
    undo_proposal(ActionProposal.objects.get(uuid=result.proposal_id))
    assert not ShopMemory.objects.filter(key="audience").exists()


def test_reply_draft_never_sends(ctx) -> None:
    data = execute_tool(ctx, "reply.draft", {"body": "Thanks for your order!"}).data
    assert data["sent"] is False and data["render"] == "reply_draft"


# ------------------------------------------------------------------ web.fetch_url
@pytest.fixture
def web_enabled(db):
    ToolDefinition.objects.create(
        key="web.fetch_url", name="Fetch", category="Platform", enabled=True
    )


def _resolve_to(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 443))])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"])
def test_web_fetch_blocks_private_addresses(ctx, web_enabled, monkeypatch, ip) -> None:
    _resolve_to(monkeypatch, ip)
    result = execute_tool(ctx, "web.fetch_url", {"url": "https://internal.example.com/"})
    assert "public internet" in result.error


def test_web_fetch_extracts_text(ctx, web_enabled, monkeypatch, respx_mock) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    respx_mock.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>Example</title><script>evil()</script></head>"
            "<body><h1>Hello</h1><p>World</p></body></html>",
        )
    )
    data = execute_tool(ctx, "web.fetch_url", {"url": "https://example.com/"}).data
    assert data["title"] == "Example"
    assert "Hello" in data["text"] and "World" in data["text"] and "evil" not in data["text"]


def test_web_fetch_checks_redirect_targets(ctx, web_enabled, monkeypatch, respx_mock) -> None:
    ips = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (next(ips), 443))])
    respx_mock.get("https://example.com/").mock(
        return_value=httpx.Response(302, headers={"location": "http://localhost/admin"})
    )
    result = execute_tool(ctx, "web.fetch_url", {"url": "https://example.com/"})
    assert "public internet" in result.error


def test_web_fetch_rejects_binary(ctx, web_enabled, monkeypatch, respx_mock) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    respx_mock.get("https://example.com/x.zip").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK")
    )
    result = execute_tool(ctx, "web.fetch_url", {"url": "https://example.com/x.zip"})
    assert "Not a text page" in result.error
    assert platform.MAX_BYTES == 1_000_000
