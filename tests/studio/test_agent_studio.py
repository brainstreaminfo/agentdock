"""Agent Studio (spec §7): builder sections, playground, tests, publish, versions."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.agents.models import AgentDefinition, AgentInstallation, AgentTestCase, AgentTestResult
from apps.agents.settings_schema import (
    build_form,
    fields_to_schema,
    schema_to_fields,
    validate_fields,
    validate_settings,
)
from apps.agents.testing import evaluate
from apps.chat.models import Conversation
from apps.runs.models import ActionProposal, Run
from apps.tools.models import ToolDefinition
from tests.agents.conftest import text_turn, tool_turn
from tests.factories import ShopFactory, UserFactory

pytestmark = pytest.mark.django_db


def section(slug: str, name: str) -> str:
    return f"/studio/agents/{slug}/{name}/"


# ------------------------------------------------------------------ access & dashboard
@pytest.mark.parametrize(
    "path",
    [
        "/studio/",
        "/studio/agents/helper/brain/",
        "/studio/agents/helper/playground/",
        "/studio/agents/helper/tests/",
        "/studio/agents/helper/publish/",
        "/studio/agents/helper/analytics/",
    ],
)
def test_studio_is_staff_only(client, agent, path) -> None:
    assert client.get(path).status_code == 302
    client.force_login(UserFactory())
    assert client.get(path).status_code == 404


def test_dashboard_lists_agents_and_templates(staff_client, agent, templates, installation) -> None:
    content = staff_client.get("/studio/").content.decode()
    assert "Helper" in content and "Product Description Writer" in content and "Clone" in content


def test_create_agent(staff_client, staff) -> None:
    resp = staff_client.post("/studio/agents/new/", {"name": "Review Responder"})
    assert resp["Location"] == section("review-responder", "brain")
    agent = AgentDefinition.objects.get(slug="review-responder")
    assert agent.created_by == staff and agent.draft.is_draft


def test_clone_template(staff_client, templates) -> None:
    resp = staff_client.post("/studio/agents/sales-qa-analyst/clone/")
    copy = AgentDefinition.objects.get(slug="sales-qa-analyst-copy")
    assert resp["Location"] == section(copy.slug, "listing")
    assert not copy.is_template
    source = AgentDefinition.objects.get(slug="sales-qa-analyst").draft
    assert copy.draft.instructions_md == source.instructions_md
    assert set(copy.draft.tools.values_list("key", flat=True)) == set(
        source.tools.values_list("key", flat=True)
    )
    staff_client.post("/studio/agents/sales-qa-analyst/clone/")
    assert AgentDefinition.objects.filter(slug="sales-qa-analyst-copy-2").exists()


def test_templates_are_read_only(staff_client, templates) -> None:
    staff_client.post(section("sales-qa-analyst", "brain"), {"instructions_md": "hacked"})
    assert (
        "hacked" not in AgentDefinition.objects.get(slug="sales-qa-analyst").draft.instructions_md
    )
    resp = staff_client.get("/studio/agents/sales-qa-analyst/publish/")
    assert resp.status_code == 302


# ------------------------------------------------------------------ builder sections
def test_brain_section(staff_client, agent) -> None:
    resp = staff_client.get(section("helper", "brain"))
    content = resp.content.decode()
    assert "Full system prompt preview" in content and "Help Tea Co in a friendly tone." in content
    resp = staff_client.post(
        section("helper", "brain"),
        {
            "instructions_md": "Be brief.",
            "model": "claude-sonnet-5-5",
            "effort": "high",
            "max_steps": 8,
            "max_tokens": 20000,
        },
    )
    assert resp.status_code == 302
    draft = agent.draft
    assert (draft.instructions_md, draft.model, draft.effort) == (
        "Be brief.",
        "claude-sonnet-5-5",
        "high",
    )
    # Unknown models are rejected.
    resp = staff_client.post(
        section("helper", "brain"),
        {
            "instructions_md": "x",
            "model": "gpt-4",
            "effort": "low",
            "max_steps": 8,
            "max_tokens": 20000,
        },
    )
    assert resp.status_code == 200 and "Select a valid choice" in resp.content.decode()


def test_tools_section_computes_scopes(staff_client, agent) -> None:
    ids = list(
        ToolDefinition.objects.filter(key__in=["orders.search", "products.update"]).values_list(
            "pk", flat=True
        )
    )
    staff_client.post(section("helper", "tools"), {"tools": ids})
    draft = agent.draft
    assert set(draft.tools.values_list("key", flat=True)) == {"orders.search", "products.update"}
    assert draft.required_scopes == ["read_orders", "write_products"]


def test_triggers_section_and_event_scopes(staff_client, agent) -> None:
    resp = staff_client.post(
        section("helper", "triggers"),
        {
            "chat": "on",
            "schedule": "custom",
            "custom_cron": "30 7 * * 1-5",
            "events": ["inventory_levels/update"],
        },
    )
    assert resp.status_code == 302
    draft = agent.draft
    assert draft.triggers == {
        "chat": True,
        "schedule_cron": "30 7 * * 1-5",
        "events": ["inventory_levels/update"],
    }
    assert "read_inventory" in draft.required_scopes
    resp = staff_client.post(
        section("helper", "triggers"), {"schedule": "custom", "custom_cron": "99 * * *"}
    )
    assert resp.status_code == 200 and "5 parts" in resp.content.decode()


def test_behaviour_default_must_be_allowed(staff_client, agent) -> None:
    resp = staff_client.post(
        section("helper", "behaviour"),
        {"default_autonomy": "AUTO", "allowed_autonomy": ["SUGGEST", "APPROVE"]},
    )
    assert "must be one of the allowed" in resp.content.decode()
    staff_client.post(
        section("helper", "behaviour"),
        {
            "default_autonomy": "AUTO",
            "allowed_autonomy": ["APPROVE", "AUTO"],
            "output_types": ["actions"],
        },
    )
    draft = agent.draft
    assert draft.default_autonomy == "AUTO" and draft.output_types == ["actions"]


def test_availability_section(staff_client, agent, agent_shop) -> None:
    staff_client.post(
        section("helper", "availability"),
        {
            "visibility": "PUBLIC",
            "plans": ["growth", "pro"],
            "extra_price": "9.00",
            "featured": "on",
            "auto_upgrade": "on",
            "allowed_shops": [agent_shop.pk],
        },
    )
    agent.refresh_from_db()
    assert agent.visibility == "PUBLIC" and agent.plans == ["growth", "pro"]
    assert agent.extra_price == Decimal("9.00") and agent.featured
    assert list(agent.allowed_shops.all()) == [agent_shop]


def test_listing_section_with_screenshot(staff_client, agent) -> None:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 3), "purple").save(buffer, format="PNG")
    png = SimpleUploadedFile("shot.png", buffer.getvalue(), content_type="image/png")
    staff_client.post(
        section("helper", "listing"),
        {
            "name": "Helper",
            "icon": "sparkles",
            "category": "General",
            "tagline": "Helps",
            "description_md": "Long",
            "capabilities_text": "One\nTwo\n",
            "example_prompts_text": "Hi",
            "screenshot": png,
        },
    )
    agent.refresh_from_db()
    assert agent.capabilities == ["One", "Two"] and len(agent.screenshots) == 1
    staff_client.post(
        section("helper", "listing"),
        {"name": "Helper", "icon": "sparkles", "remove_screenshot": agent.screenshots[0]},
    )
    agent.refresh_from_db()
    assert agent.screenshots == []


# ------------------------------------------------------------------ merchant settings builder
FIELDS = [
    {
        "key": "tone",
        "label": "Tone",
        "type": "select",
        "options": ["friendly", "formal"],
        "default": "formal",
        "required": True,
    },
    {
        "key": "threshold",
        "label": "Threshold",
        "type": "number",
        "integer": True,
        "min": 0,
        "max": 100,
        "default": "5",
    },
    {"key": "notify", "label": "Notify me", "type": "toggle", "default": True},
    {"key": "featured", "label": "Featured products", "type": "product"},
]


def test_settings_schema_round_trip() -> None:
    schema = fields_to_schema(FIELDS)
    assert schema["x-order"] == ["tone", "threshold", "notify", "featured"]
    assert schema["required"] == ["tone"]
    assert schema["properties"]["threshold"] == {
        "title": "Threshold",
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
        "default": 5,
    }
    back = schema_to_fields(schema)
    assert [f["type"] for f in back] == ["select", "number", "toggle", "product"]
    assert validate_settings(schema, {"tone": "formal", "threshold": 5}) == []
    assert validate_settings(schema, {"tone": "rude"})


def test_settings_form_renders_and_cleans() -> None:
    schema = fields_to_schema(FIELDS)
    form = build_form(
        schema,
        data={
            "tone": "friendly",
            "threshold": "7",
            "notify": "on",
            "featured": "gid://shopify/Product/1, 2",
        },
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data == {
        "tone": "friendly",
        "threshold": 7,
        "notify": True,
        "featured": ["gid://shopify/Product/1", "2"],
    }
    assert build_form(schema).initial["tone"] == "formal"  # defaults pre-filled


def test_settings_field_validation() -> None:
    errors = validate_fields(
        [
            {"key": "Bad Key", "label": "", "type": "select", "options": []},
            {"key": "a", "label": "A", "type": "number", "default": "x"},
        ]
    )
    joined = " ".join(errors)
    assert "key must be" in joined and "needs a label" in joined
    assert "at least one option" in joined and "must be a number" in joined


def test_settings_section_saves_schema(staff_client, agent) -> None:
    resp = staff_client.post(section("helper", "settings"), {"fields_json": json.dumps(FIELDS)})
    assert resp.status_code == 302
    draft = agent.draft
    assert draft.settings_defaults == {"tone": "formal", "threshold": 5, "notify": True}
    resp = staff_client.post(
        section("helper", "settings"),
        {"fields_json": json.dumps([{"key": "", "label": "", "type": "text"}])},
    )
    assert resp.status_code == 200 and "needs a label" in resp.content.decode()


# ------------------------------------------------------------------ playground
def test_playground_chat_uses_draft_and_forces_approval(
    staff_client, agent, agent_shop, fake_llm, fake_shopify
) -> None:
    draft = agent.draft
    fake_shopify.on(
        "ProductEditableFields",
        {"product": {"id": "gid://shopify/Product/1", "title": "Old", "seo": {}}},
    )
    fake_llm.script(
        tool_turn("products.update", {"product_id": "1", "title": "New"}), text_turn("Proposed.")
    )
    resp = staff_client.post(
        "/studio/agents/helper/playground/start/", {"shop": agent_shop.pk, "version": draft.pk}
    )
    conversation = Conversation.objects.get()
    assert conversation.version == draft and conversation.installation is None
    assert resp["Location"].endswith(f"?c={conversation.pk}")

    staff_client.post(
        f"/studio/agents/helper/playground/{conversation.pk}/send/", {"message": "Rename product 1"}
    )
    run = Run.objects.get(trigger="PLAYGROUND")
    assert run.status == "SUCCEEDED" and run.version == draft
    assert ActionProposal.objects.get(run=run).status == "PENDING"  # never auto-applied
    page = staff_client.get(resp["Location"]).content.decode()
    assert "Rename product 1" in page and "Approve" in page
    # Playground chats don't show up in the merchant chat list.
    assert "Playground" not in staff_client.get(f"/s/{agent_shop.pk}/chat/").content.decode()


def test_playground_only_on_own_stores(staff_client, agent) -> None:
    other = ShopFactory()
    staff_client.post(
        "/studio/agents/helper/playground/start/", {"shop": other.pk, "version": agent.draft.pk}
    )
    assert not Conversation.objects.exists()


def test_playground_trigger_runs_draft(staff_client, agent, agent_shop, fake_llm) -> None:
    fake_llm.script(text_turn("Scheduled job done."))
    resp = staff_client.post("/studio/agents/helper/playground/trigger/", {"shop": agent_shop.pk})
    run = Run.objects.get()
    assert resp["Location"] == f"/studio/runs/{run.pk}/"
    assert "Scheduled job done." in staff_client.get(resp["Location"]).content.decode()


# ------------------------------------------------------------------ test cases
def test_test_case_lifecycle(staff_client, agent, agent_shop, fake_llm, fake_shopify) -> None:
    staff_client.post(
        "/studio/agents/helper/tests/new/",
        {
            "name": "Finds tea",
            "prompt": "Find tea",
            "must_call": ["products.search"],
            "must_not_call": ["products.update"],
            "reply_contains": "Green tea",
            "max_cost_usd": "0.50",
        },
    )
    staff_client.post(
        "/studio/agents/helper/tests/new/",
        {"name": "Says hello", "prompt": "Hi", "reply_contains": "hello"},
    )
    assert AgentTestCase.objects.count() == 2

    fake_shopify.on(
        "SearchProducts", {"products": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
    )
    fake_llm.script(
        tool_turn("products.search", {"query": "tea"}),
        text_turn("Green tea is in stock."),
        text_turn("Good morning!"),
    )
    staff_client.post("/studio/agents/helper/tests/run/", {"shop": agent_shop.pk})

    results = {r.case.name: r for r in AgentTestResult.objects.all()}
    assert results["Finds tea"].status == "PASSED"
    assert results["Says hello"].status == "FAILED"
    assert any(not d["ok"] and "hello" in d["check"] for d in results["Says hello"].details)
    content = staff_client.get("/studio/agents/helper/tests/").content.decode()
    assert "1 passed" in content and "1 failed" in content

    # Editing the draft makes old results stale.
    draft = agent.draft
    draft.instructions_md += " More."
    draft.save()
    assert (
        "2 not run on this draft"
        in staff_client.get("/studio/agents/helper/tests/").content.decode()
    )


def test_evaluate_checks(installation, fake_llm) -> None:
    from apps.agents.runtime import create_run, execute_run

    fake_llm.script(text_turn("Your sales were great"))
    run = execute_run(
        create_run(
            shop=installation.shop,
            version=installation.version,
            trigger=Run.Trigger.TEST,
            input={"task": "x"},
        )
    )
    ok, details = evaluate(
        run,
        {
            "reply_contains": ["SALES"],
            "reply_not_contains": ["sorry"],
            "must_not_call": ["products.update"],
            "max_cost_usd": "0.001",
        },
    )
    assert not ok
    assert [d["ok"] for d in details] == [True, True, True, False]  # cost $0.008 > $0.001


# ------------------------------------------------------------------ publish & versions
def test_publish_and_auto_upgrade(staff_client, agent, agent_shop) -> None:
    v1 = agent.latest_version
    same_scopes = AgentInstallation.objects.create(shop=agent_shop, agent=agent, version=v1)
    lacking = ShopFactory(granted_scopes=["read_products"])
    needs_scopes = AgentInstallation.objects.create(shop=lacking, agent=agent, version=v1)

    draft = agent.draft
    draft.instructions_md = "New instructions"
    draft.save()
    page = staff_client.get("/studio/agents/helper/publish/").content.decode()
    assert "Instructions" in page and "Publish v2" in page

    resp = staff_client.post("/studio/agents/helper/publish/", {"changelog": "Better prompt"})
    assert resp["Location"] == "/studio/agents/helper/versions/"
    v2 = agent.latest_version
    assert v2.number == 2 and v2.changelog == "Better prompt"
    same_scopes.refresh_from_db()
    needs_scopes.refresh_from_db()
    assert same_scopes.version == v2  # auto-upgraded
    assert needs_scopes.version == v1  # lacks write_products: merchant must approve


def test_publish_warns_about_new_scopes(staff_client, agent) -> None:
    draft = agent.draft
    draft.tools.add(ToolDefinition.objects.get(key="orders.search"))
    content = staff_client.get("/studio/agents/helper/publish/").content.decode()
    assert "New permissions" in content and "View your orders" in content


def test_nothing_to_publish(staff_client, agent) -> None:
    assert (
        "Nothing to publish" in staff_client.get("/studio/agents/helper/publish/").content.decode()
    )


def test_restore_old_version(staff_client, agent) -> None:
    draft = agent.draft
    draft.instructions_md = "v2 text"
    draft.save()
    draft.publish(changelog="v2")
    staff_client.post("/studio/agents/helper/versions/1/restore/")
    assert agent.draft.instructions_md == agent.versions.get(number=1).instructions_md
    staff_client.post("/studio/agents/helper/publish/", {"changelog": "Roll back to v1"})
    assert agent.latest_version.number == 3
    assert agent.latest_version.instructions_md == agent.versions.get(number=1).instructions_md


def test_fingerprint_tracks_config(agent) -> None:
    draft = agent.draft
    before = draft.config_fingerprint()
    draft.effort = "high"
    assert draft.config_fingerprint() != before


# ------------------------------------------------------------------ analytics & runs
def test_analytics_and_run_pages(staff_client, installation, fake_llm) -> None:
    from apps.agents.runtime import create_run, execute_run

    fake_llm.script(text_turn("ok"))
    run = execute_run(
        create_run(
            shop=installation.shop,
            version=installation.version,
            installation=installation,
            trigger=Run.Trigger.MANUAL,
            input={"task": "x"},
        )
    )
    content = staff_client.get("/studio/agents/helper/analytics/").content.decode()
    assert "Runs per day" in content and f"#{run.pk}" in content
    dashboard = staff_client.get("/studio/").content.decode()
    assert "$0.0080" in dashboard  # avg cost per run
    assert f"Run #{run.pk}" in staff_client.get(f"/studio/runs/{run.pk}/").content.decode()
