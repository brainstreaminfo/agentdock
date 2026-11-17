"""Approvals queue, runs list, dashboard, alerts and brand memory (spec §8.5)."""

from __future__ import annotations

import pytest

from apps.accounts.models import Membership
from apps.agents.models import ShopMemory
from apps.agents.runtime import create_run, execute_run
from apps.dashboard.models import Alert, create_alert
from apps.runs.models import ActionProposal, Run
from tests.agents.conftest import text_turn, tool_turn
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(client, installation):
    user = UserFactory()
    Membership.objects.create(user=user, shop=installation.shop, role="OWNER")
    client.force_login(user)
    return client


@pytest.fixture
def products(fake_shopify):
    """Five fake products whose titles the update tool changes."""
    state = {
        str(i): {"id": f"gid://shopify/Product/{i}", "title": f"Old {i}", "seo": {}}
        for i in range(1, 6)
    }

    def editable(v):
        return {"product": dict(state[v["id"].rsplit("/", 1)[1]])}

    def update(v):
        p = state[v["product"]["id"].rsplit("/", 1)[1]]
        p.update({k: val for k, val in v["product"].items() if k != "id"})
        return {"productUpdate": {"product": dict(p), "userErrors": []}}

    fake_shopify.on("ProductEditableFields", editable)
    fake_shopify.on("UpdateProduct", update)
    return state


def test_bulk_approve_then_undo(owner_client, installation, fake_llm, products) -> None:
    """Acceptance §15.4: rewrite 5 products → 5 approval cards → approve → undo."""
    fake_llm.script(
        *[
            tool_turn(
                "products.update", {"product_id": str(i), "title": f"New {i}"}, call_id=f"t{i}"
            )
            for i in range(1, 6)
        ],
        text_turn("Five new titles are waiting for you."),
    )
    run = create_run(
        shop=installation.shop,
        version=installation.version,
        installation=installation,
        trigger="MANUAL",
        input={"task": "Rewrite"},
    )
    execute_run(run)
    shop_id = installation.shop_id

    page = owner_client.get(f"/s/{shop_id}/approvals/").content.decode()
    assert page.count("<div data-proposal") == 5 and "Approve selected" in page

    uuids = [str(p.uuid) for p in ActionProposal.objects.all()]
    resp = owner_client.post(
        f"/s/{shop_id}/approvals/bulk/", {"action": "approve", "proposal": uuids}
    )
    assert resp.status_code == 302
    assert all(p["title"].startswith("New") for p in products.values())
    assert ActionProposal.objects.filter(status="APPLIED").count() == 5

    one = ActionProposal.objects.get(payload__product_id="3")
    owner_client.post(f"/s/{shop_id}/actions/{one.uuid}/undo/")
    assert products["3"]["title"] == "Old 3"
    history = owner_client.get(f"/s/{shop_id}/approvals/history/").content.decode()
    assert "Undone" in history and "Applied" in history


def test_bulk_reject_and_member_cannot_bulk(owner_client, installation) -> None:
    from django.test import Client

    p = ActionProposal.objects.create(shop=installation.shop, tool="products.update", payload={})
    member = UserFactory()
    Membership.objects.create(user=member, shop=installation.shop, role="MEMBER")
    client = Client()
    client.force_login(member)
    url = f"/s/{installation.shop_id}/approvals/bulk/"
    assert client.post(url, {"action": "approve", "proposal": [str(p.uuid)]}).status_code == 403

    owner_client.post(url, {"action": "reject", "proposal": [str(p.uuid)]})
    p.refresh_from_db()
    assert p.status == "REJECTED"


def test_runs_list_hides_studio_runs_and_filters(owner_client, installation) -> None:
    shop = installation.shop
    chat = Run.objects.create(shop=shop, agent_slug="helper", trigger="CHAT", status="SUCCEEDED")
    play = Run.objects.create(
        shop=shop, agent_slug="helper", trigger="PLAYGROUND", status="SUCCEEDED"
    )
    failed = Run.objects.create(
        shop=shop, agent_slug="helper", trigger="EVENT", status="FAILED", error="boom"
    )

    def link(run):
        return f'href="/s/{shop.pk}/runs/{run.pk}/"'

    page = owner_client.get(f"/s/{shop.pk}/runs/").content.decode()
    assert link(chat) in page and link(failed) in page and link(play) not in page
    page = owner_client.get(f"/s/{shop.pk}/runs/?status=FAILED").content.decode()
    assert "boom" in page and link(chat) not in page


def test_dashboard_shows_kpis_and_lists(owner_client, installation) -> None:
    shop = installation.shop
    ActionProposal.objects.create(
        shop=shop, tool="products.update", payload={}, preview={"summary": "Retitle tea"}
    )
    create_alert(shop, "Low stock on tea", severity=Alert.Severity.WARNING)
    page = owner_client.get(f"/s/{shop.pk}/").content.decode()
    assert "Retitle tea" in page and "Low stock on tea" in page and "Helper" in page


def test_alerts_mark_read(owner_client, installation) -> None:
    shop = installation.shop
    a = create_alert(shop, "One")
    create_alert(shop, "Two")
    owner_client.post(f"/s/{shop.pk}/alerts/{a.pk}/read/")
    assert Alert.objects.filter(read_at__isnull=True).count() == 1
    owner_client.post(f"/s/{shop.pk}/alerts/read/")
    assert not Alert.objects.filter(read_at__isnull=True).exists()
    assert "Two" in owner_client.get(f"/s/{shop.pk}/alerts/?show=all").content.decode()


def test_brand_memory_crud_and_used_by_agents(owner_client, installation, fake_llm) -> None:
    shop = installation.shop
    owner_client.post(f"/s/{shop.pk}/memory/", {"key": "brand-voice", "value": "Warm and short"})
    entry = ShopMemory.objects.get()
    assert entry.key == "brand_voice"

    fake_llm.script(text_turn("ok"))
    run = create_run(
        shop=shop,
        version=installation.version,
        installation=installation,
        trigger="MANUAL",
        input={},
    )
    execute_run(run)
    assert "Warm and short" in fake_llm.requests[0]["system"]

    owner_client.post(f"/s/{shop.pk}/memory/{entry.pk}/delete/")
    assert not ShopMemory.objects.exists()


def test_members_cannot_change_memory(client, installation) -> None:
    user = UserFactory()
    Membership.objects.create(user=user, shop=installation.shop, role="MEMBER")
    client.force_login(user)
    client.post(f"/s/{installation.shop_id}/memory/", {"key": "x", "value": "y"})
    assert not ShopMemory.objects.exists()


def test_every_merchant_page_renders(owner_client, installation) -> None:
    s = installation.shop_id
    for path in [
        "",
        "chat/",
        "marketplace/",
        "agents/",
        f"agents/{installation.pk}/",
        "approvals/",
        "approvals/history/",
        "runs/",
        "alerts/",
        "memory/",
        "settings/",
    ]:
        assert owner_client.get(f"/s/{s}/{path}").status_code == 200, path
