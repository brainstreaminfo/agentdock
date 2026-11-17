"""
Example agent templates (spec §7.5), created by `manage.py seed_agent_templates`.

They are marked `is_template` and are never published: staff clone them in the
Studio (Phase 4) to learn how an agent is put together. Running the command
again updates the templates' drafts to these definitions.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.tools.models import ToolDefinition
from apps.tools.sync import sync_tools

from .models import AgentDefinition, Autonomy, Effort

TEMPLATES: list[dict[str, Any]] = [
    {
        "slug": "product-description-writer",
        "name": "Product Description Writer",
        "icon": "pen-line",
        "category": "Content",
        "tagline": "Rewrites product descriptions in your brand voice.",
        "description_md": (
            "Writes clear, persuasive product descriptions that match your brand voice. "
            "Every change is shown to you as a before/after for approval, and can be undone."
        ),
        "capabilities": [
            "Rewrite descriptions of selected or newest products",
            "Follow your brand voice and banned words from Brand Memory",
            "Suggest changes for your approval, never publishes on its own",
        ],
        "example_prompts": [
            "Rewrite the descriptions of my 5 newest products",
            "Make the description of my best-selling product more persuasive",
        ],
        "instructions_md": """You write product descriptions for {{shop.name}}.

Tone: {{settings.tone}}. Length: {{settings.length}}.
{{memory}}

How to work:
1. Find the products the merchant means with products.search (newest first if they
   say "newest"), then read each one with products.get.
2. Write a new description in HTML (<p>, <ul><li>, <strong>). Keep facts that are in
   the current description (materials, sizes, care); never invent specs.
3. Save each one with products.update (description_html only). Each change waits for
   the merchant's approval, so tell them how many you proposed.
4. Respect banned words from brand memory.""",
        "tools": ["products.search", "products.get", "products.update", "memory.read"],
        "settings_schema": {
            "type": "object",
            "properties": {
                "tone": {
                    "type": "string",
                    "title": "Tone",
                    "enum": ["friendly", "professional", "playful", "luxurious"],
                    "default": "friendly",
                },
                "length": {
                    "type": "string",
                    "title": "Length",
                    "enum": [
                        "short (40-70 words)",
                        "medium (80-150 words)",
                        "long (150-250 words)",
                    ],
                    "default": "medium (80-150 words)",
                },
            },
        },
        "settings_defaults": {"tone": "friendly", "length": "medium (80-150 words)"},
        "triggers": {"chat": True, "schedule_cron": "", "events": []},
        "default_autonomy": Autonomy.APPROVE,
        "allowed_autonomy": [Autonomy.SUGGEST, Autonomy.APPROVE],
        "effort": Effort.MEDIUM,
    },
    {
        "slug": "low-stock-alert",
        "name": "Low Stock Alert",
        "icon": "package-search",
        "category": "Inventory",
        "tagline": "Warns you every morning about products running out.",
        "description_md": (
            "Checks your inventory every morning and creates one alert listing the "
            "products that are at or below your threshold. It never changes stock."
        ),
        "capabilities": ["Daily low-stock check", "One clear alert, no noise", "Read-only"],
        "example_prompts": ["What's running low right now?", "Which products are out of stock?"],
        "instructions_md": """You watch inventory for {{shop.name}}.

1. Call inventory.low_stock with threshold {{settings.threshold}} (page through results
   if there are more).
2. If nothing is low, say so briefly and do NOT create an alert.
3. Otherwise create ONE alert with alerts.create: title like "7 products are running
   low", body listing each product/variant with its available quantity, lowest first.
   Severity "critical" if anything is at 0, else "warning".
4. In chat, answer with a short table (ui.render_table) instead of an alert.""",
        "tools": ["inventory.low_stock", "alerts.create", "ui.render_table"],
        "settings_schema": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "integer",
                    "title": "Alert when available stock is at or below",
                    "minimum": 0,
                    "maximum": 1000,
                    "default": 5,
                }
            },
        },
        "settings_defaults": {"threshold": 5},
        "triggers": {"chat": True, "schedule_cron": "0 8 * * *", "events": []},
        "default_autonomy": Autonomy.AUTO,
        "allowed_autonomy": [Autonomy.AUTO],
        "effort": Effort.LOW,
    },
    {
        "slug": "sales-qa-analyst",
        "name": "Sales Q&A Analyst",
        "icon": "chart-line",
        "category": "Analytics",
        "tagline": "Answers questions about your sales with charts and tables.",
        "description_md": (
            "Ask about revenue, orders, average order value, best sellers or top customers "
            "and get a clear answer with a chart. Read-only."
        ),
        "capabilities": ["Sales trends with charts", "Best sellers and top customers", "Read-only"],
        "example_prompts": [
            "How did sales go in the last 30 days?",
            "What are my top 10 products this month?",
            "Is my average order value going up?",
        ],
        "instructions_md": """You are the sales analyst of {{shop.name}}.
Currency: {{shop.currency}}.

- Use the orders and analytics tools to get real numbers; never guess.
- Show trends with ui.render_chart (line for time series, bar for rankings) and
  detailed numbers with ui.render_table.
- Lead with the answer in one or two sentences, then the chart, then 2-3 insights.
- Mention when data is limited (e.g. "only the last 60 days are available").""",
        "tools": [
            "shop.info",
            "orders.sales_summary",
            "orders.timeseries",
            "orders.search",
            "analytics.top_products",
            "analytics.top_customers",
            "analytics.aov",
            "ui.render_chart",
            "ui.render_table",
        ],
        "settings_schema": {"type": "object", "properties": {}},
        "settings_defaults": {},
        "triggers": {"chat": True, "schedule_cron": "", "events": []},
        "default_autonomy": Autonomy.SUGGEST,
        "allowed_autonomy": [Autonomy.SUGGEST],
        "effort": Effort.MEDIUM,
    },
]

LISTING_FIELDS = (
    "name",
    "icon",
    "category",
    "tagline",
    "description_md",
    "capabilities",
    "example_prompts",
)
DRAFT_FIELDS = (
    "instructions_md",
    "settings_schema",
    "settings_defaults",
    "triggers",
    "default_autonomy",
    "allowed_autonomy",
    "effort",
)


@transaction.atomic
def seed_templates() -> list[AgentDefinition]:
    sync_tools()  # templates reference built-in tools
    agents = []
    for spec in TEMPLATES:
        agent, _ = AgentDefinition.objects.update_or_create(
            slug=spec["slug"],
            defaults={**{f: spec[f] for f in LISTING_FIELDS}, "is_template": True},
        )
        draft = agent.draft
        for f in DRAFT_FIELDS:
            setattr(draft, f, spec[f])
        draft.save()
        tools = list(ToolDefinition.objects.filter(key__in=spec["tools"]))
        missing = set(spec["tools"]) - {t.key for t in tools}
        if missing:
            raise ValueError(f"Template {spec['slug']} uses unknown tools: {sorted(missing)}")
        draft.tools.set(tools)
        draft.required_scopes = draft.compute_required_scopes()
        draft.save(update_fields=["required_scopes"])
        agents.append(agent)
    return agents
