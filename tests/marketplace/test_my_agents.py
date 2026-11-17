"""My agents: settings, pause, run now, update to a new version, uninstall, picker (spec §8.2)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from django_celery_beat.models import PeriodicTask

from apps.accounts.models import Membership
from apps.agents.models import AgentInstallation
from apps.runs.models import Run
from apps.tools.models import ToolDefinition
from tests.agents.conftest import text_turn
from tests.factories import ShopFactory, UserFactory
from tests.marketplace.conftest import publish

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(client, installation):
    user = UserFactory()
    Membership.objects.create(user=user, shop=installation.shop, role="OWNER")
    client.force_login(user)
    return client


def url(inst, suffix=""):
    return f"/s/{inst.shop_id}/agents/{inst.pk}/{suffix}"


def test_my_agents_lists_installations(owner_client, installation) -> None:
    page = owner_client.get(f"/s/{installation.shop_id}/agents/").content.decode()
    assert "Helper" in page and "Active" in page


def test_save_settings_and_autonomy(owner_client, installation) -> None:
    resp = owner_client.post(url(installation), {"agent-autonomy": "SUGGEST"})
    assert resp.status_code == 302
    installation.refresh_from_db()
    assert installation.autonomy == "SUGGEST"


def test_pause_and_resume(owner_client, installation) -> None:
    owner_client.post(url(installation, "toggle/"))
    installation.refresh_from_db()
    assert not installation.enabled
    owner_client.post(url(installation, "toggle/"), {"next": "https://evil.com/"})
    installation.refresh_from_db()
    assert installation.enabled


def test_run_now_starts_manual_run(owner_client, installation, fake_llm) -> None:
    fake_llm.script(text_turn("Done my job."))
    resp = owner_client.post(url(installation, "run/"), {"task": "Check titles"})
    run = Run.objects.get()
    assert resp["Location"] == f"/s/{installation.shop_id}/runs/{run.pk}/"
    assert run.trigger == "MANUAL" and run.status == "SUCCEEDED"
    assert fake_llm.requests[0]["messages"][0]["content"] == "Check titles"
    page = owner_client.get(resp["Location"]).content.decode()
    assert "Done my job." in page


def test_update_needing_new_scopes_goes_through_oauth(owner_client, installation, agent) -> None:
    shop = installation.shop
    shop.granted_scopes = ["write_products"]
    shop.save()
    publish(agent, triggers={"chat": True, "schedule_cron": "", "events": ["orders/create"]})
    page = owner_client.get(url(installation)).content.decode()
    assert "Update available: v2" in page

    resp = owner_client.post(url(installation, "update/"))
    q = {k: v[0] for k, v in parse_qs(urlparse(resp["Location"]).query).items()}
    assert q["scopes"] == "read_orders" and q["next"] == url(installation)
    installation.refresh_from_db()
    assert installation.version.number == 1

    shop.granted_scopes = ["write_products", "read_orders"]
    shop.save()
    owner_client.post(url(installation, "update/"))
    installation.refresh_from_db()
    assert installation.version.number == 2


def test_uninstall_removes_installation_and_schedule(owner_client, installation, agent) -> None:
    publish(agent, triggers={"chat": True, "schedule_cron": "0 9 * * *", "events": []})
    installation.version = agent.latest_version
    installation.schedule_enabled = True
    installation.save()
    assert PeriodicTask.objects.filter(name=f"agent-installation-{installation.pk}").exists()

    owner_client.post(url(installation, "uninstall/"))
    assert not AgentInstallation.objects.exists()
    assert not PeriodicTask.objects.filter(name=f"agent-installation-{installation.pk}").exists()


def test_other_shops_installation_is_404(client, installation) -> None:
    other = ShopFactory()
    user = UserFactory()
    Membership.objects.create(user=user, shop=other, role="OWNER")
    client.force_login(user)
    assert client.get(f"/s/{other.pk}/agents/{installation.pk}/").status_code == 404
    assert client.post(f"/s/{other.pk}/agents/{installation.pk}/uninstall/").status_code == 404
    assert AgentInstallation.objects.exists()


def test_members_see_but_cannot_change(client, installation) -> None:
    user = UserFactory()
    Membership.objects.create(user=user, shop=installation.shop, role="MEMBER")
    client.force_login(user)
    assert client.get(url(installation)).status_code == 200
    assert client.post(url(installation, "toggle/")).status_code == 403
    assert client.post(url(installation, "run/")).status_code == 403


def test_product_picker_search(owner_client, installation, fake_shopify) -> None:
    ToolDefinition.objects.filter(key="products.search").update(enabled=True)
    fake_shopify.on(
        "SearchProducts",
        {
            "products": {
                "nodes": [{"id": "gid://shopify/Product/7", "title": "Green tea", "tags": []}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
    )
    page = owner_client.get(
        f"/s/{installation.shop_id}/pickers/product/", {"q": "tea"}
    ).content.decode()
    assert 'data-id="gid://shopify/Product/7"' in page and "Green tea" in page


def test_picker_widget_on_settings_form(owner_client, installation, agent) -> None:
    publish(
        agent,
        settings_schema={
            "type": "object",
            "properties": {
                "featured": {
                    "title": "Featured products",
                    "type": "array",
                    "items": {"type": "string"},
                    "x-widget": "product-picker",
                }
            },
        },
    )
    installation.version = agent.latest_version
    installation.save()
    page = owner_client.get(url(installation)).content.decode()
    assert f"/s/{installation.shop_id}/pickers/product/" in page
    owner_client.post(
        url(installation), {"agent-autonomy": "APPROVE", "featured": "gid://shopify/Product/7"}
    )
    installation.refresh_from_db()
    assert installation.settings["featured"] == ["gid://shopify/Product/7"]
