"""
Platform tools (no Shopify API): charts and tables for the chat UI, alerts,
brand memory, reply drafts, and (off by default) fetching a web page.
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import Field, HttpUrl, model_validator

from apps.agents.models import ShopMemory
from apps.dashboard.models import Alert, create_alert
from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

# =========================================================================== UI
Number = int | float


class Dataset(ToolInput):
    label: str = Field(max_length=100)
    data: list[Number] = Field(max_length=500)


class ChartInput(ToolInput):
    type: Literal["bar", "line", "pie", "doughnut"] = "bar"
    title: str = Field(max_length=200)
    labels: list[str] = Field(min_length=1, max_length=500)
    datasets: list[Dataset] = Field(min_length=1, max_length=10)
    y_label: str | None = Field(None, max_length=50)

    @model_validator(mode="after")
    def _lengths_match(self) -> ChartInput:
        for ds in self.datasets:
            if len(ds.data) != len(self.labels):
                raise ValueError(f"Dataset '{ds.label}' must have one value per label.")
        return self


@tool(
    key="ui.render_chart",
    name="Show a chart",
    description="""Show a chart in the chat (bar, line, pie, doughnut). Give one
    label per data point and one value per label in each dataset. Use it after
    getting numbers from analytics/orders tools.""",
    category="Platform",
)
def render_chart(ctx: ToolContext, args: ChartInput) -> dict[str, Any]:
    return {"render": "chart", **args.model_dump()}


class TableInput(ToolInput):
    title: str = Field("", max_length=200)
    columns: list[str] = Field(min_length=1, max_length=20)
    rows: list[list[str | Number | None]] = Field(max_length=500)

    @model_validator(mode="after")
    def _row_width(self) -> TableInput:
        for i, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise ValueError(
                    f"Row {i} has {len(row)} cells but there are {len(self.columns)} columns."
                )
        return self


@tool(
    key="ui.render_table",
    name="Show a table",
    description="Show a table in the chat: column headers plus rows of cells.",
    category="Platform",
)
def render_table(ctx: ToolContext, args: TableInput) -> dict[str, Any]:
    return {"render": "table", **args.model_dump()}


# =========================================================================== alerts
class AlertInput(ToolInput):
    title: str = Field(max_length=200)
    body: str = Field("", max_length=5000)
    severity: Literal["info", "warning", "critical"] = "info"


@tool(
    key="alerts.create",
    name="Create alert",
    description="""Notify the merchant on their dashboard (e.g. "5 products are out
    of stock"). Use for things that need attention, not for normal answers.""",
    category="Platform",
)
def create_alert_tool(ctx: ToolContext, args: AlertInput) -> dict[str, Any]:
    alert = create_alert(
        ctx.shop,
        args.title,
        args.body,
        severity=Alert.Severity[args.severity.upper()],
        agent_slug=ctx.agent_slug,
        # Same title from the same agent while unread -> one alert, not twenty.
        dedupe_key=f"agent:{ctx.agent_slug}:{args.title}"[:200],
    )
    return {"alert_id": alert.pk, "created": True}


# =========================================================================== memory
class MemoryReadInput(ToolInput):
    keys: list[str] | None = Field(None, description="Only these keys; default all.")


@tool(
    key="memory.read",
    name="Read brand memory",
    description="""The store's brand memory: brand voice, audience, policies,
    banned words, and other notes the merchant saved. Shared by all agents.""",
    category="Platform",
)
def memory_read(ctx: ToolContext, args: MemoryReadInput) -> dict[str, Any]:
    qs = ShopMemory.objects.for_shop(ctx.shop)
    if args.keys:
        qs = qs.filter(key__in=args.keys)
    return {"memory": {m.key: m.value for m in qs}}


class MemoryUpdateInput(ToolInput):
    key: str = Field(pattern=r"^[a-z0-9_-]{2,100}$", description="e.g. brand_voice")
    value: str = Field(max_length=5000)
    reason: str = Field(max_length=500, description="Why this should be remembered.")


@tool(
    key="memory.propose_update",
    name="Propose brand memory update",
    description="""Suggest saving or changing a brand memory entry (e.g. after the
    merchant says "we never use exclamation marks"). The merchant approves it.""",
    category="Platform",
    mutating=True,
)
def memory_propose(ctx: ToolContext, args: MemoryUpdateInput) -> dict[str, Any]:
    ShopMemory.objects.update_or_create(
        shop=ctx.shop,
        key=args.key,
        defaults={"value": args.value, "source": ShopMemory.Source.AGENT},
    )
    return {"key": args.key, "saved": True}


@memory_propose.preview
def _preview_memory(ctx: ToolContext, args: MemoryUpdateInput) -> dict[str, Any]:
    current = ShopMemory.objects.for_shop(ctx.shop).filter(key=args.key).first()
    return {
        "summary": f"Remember “{args.key}”",
        "reason": args.reason,
        "changes": [
            {"field": args.key, "before": current.value if current else None, "after": args.value}
        ],
    }


@memory_propose.snapshot
def _snapshot_memory(ctx: ToolContext, args: MemoryUpdateInput) -> dict[str, Any]:
    current = ShopMemory.objects.for_shop(ctx.shop).filter(key=args.key).first()
    return {
        "value": current.value if current else None,
        "source": current.source if current else None,
    }


@memory_propose.undo
def _undo_memory(ctx: ToolContext, args: MemoryUpdateInput, before: dict, after: dict) -> Any:
    if before.get("value") is None:
        ShopMemory.objects.for_shop(ctx.shop).filter(key=args.key).delete()
    else:
        ShopMemory.objects.update_or_create(
            shop=ctx.shop,
            key=args.key,
            defaults={"value": before["value"], "source": before["source"]},
        )
    return {"restored": args.key}


# =========================================================================== reply drafts
class ReplyDraftInput(ToolInput):
    channel: Literal["email", "chat", "review_reply", "social"] = "email"
    recipient_name: str | None = Field(None, max_length=200)
    subject: str | None = Field(None, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)


@tool(
    key="reply.draft",
    name="Draft a reply",
    description="""Present a ready-to-send reply (customer email, review answer…)
    for the merchant to copy. Nothing is sent.""",
    category="Platform",
)
def reply_draft(ctx: ToolContext, args: ReplyDraftInput) -> dict[str, Any]:
    return {"render": "reply_draft", "sent": False, **args.model_dump()}


# =========================================================================== web fetch
MAX_BYTES = 1_000_000
MAX_TEXT = 20_000
MAX_REDIRECTS = 3


def _assert_public(url: str) -> None:
    """Block requests to our own network (SSRF): only public internet addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("Only http(s) URLs are allowed.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f"Can't resolve {parsed.hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError("That address is not on the public internet.")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


class FetchInput(ToolInput):
    url: HttpUrl


@tool(
    key="web.fetch_url",
    name="Fetch a web page",
    description="""Download a public web page and return its title and text (max
    20,000 characters). Off by default; staff enable it per platform.""",
    category="Platform",
    enabled_by_default=False,
)
def fetch_url(ctx: ToolContext, args: FetchInput) -> dict[str, Any]:
    url = str(args.url)
    with httpx.Client(
        timeout=10, follow_redirects=False, headers={"User-Agent": "AgentHub/1.0"}
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _assert_public(url)
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code >= 400:
                    raise ToolError(f"The page returned HTTP {resp.status_code}.")
                content_type = resp.headers.get("content-type", "")
                if not any(t in content_type for t in ("text/", "html", "json", "xml")):
                    raise ToolError(f"Not a text page ({content_type}).")
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                break
        else:
            raise ToolError("Too many redirects.")

    text = body.decode(resp.encoding or "utf-8", errors="replace")
    title = ""
    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(text)
        title = parser.title.strip()
        text = "".join(parser.parts)
    lines = [line.strip() for line in text.splitlines()]
    clean = "\n".join(line for line in lines if line)
    return {"url": url, "title": title, "text": clean[:MAX_TEXT], "cut": len(clean) > MAX_TEXT}
