"""Marketplace visibility, detail page and the install flow with scope upgrade (spec §8.1, §8.3)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from apps.agents.models import AgentDefinition, AgentInstallation
from apps.marketplace import catalog
from apps.marketplace.installs import SESSION_PENDING, InstallError, install_agent
from tests.conftest import SHOP_DOMAIN, shop_info_response, signed_query
from tests.factories import ShopFactory
from tests.marketplace.conftest import publish

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- visibility
def test_visibility_rules(shop, public_agent) -> None:
    other = ShopFactory()
    beta = AgentDefinition.objects.create(slug="beta", name="Beta", visibility="BETA")
    publish(beta)
    private = AgentDefinition.objects.create(slug="private", name="Private")
    publish(private)
    private.allowed_shops.add(other)
    AgentDefinition.objects.create(slug="draft-only", name="Draft", visibility="PUBLIC")
    template = AgentDefinition.objects.create(
        slug="tmpl", name="T", visibility="PUBLIC", is_template=True
    )
    publish(template)

    assert set(catalog.visible_agents(shop).values_list("slug", flat=True)) == {"helper", "beta"}
    assert "private" in set(catalog.visible_agents(other).values_list("slug", flat=True))


def test_system_agents_are_hidden(shop) -> None:
    from apps.agents.orchestrator import team_version

    team_version()
    assert not catalog.visible_agents(shop).filter(slug="my-team").exists()


def test_marketplace_page_search_and_category(merchant_client, shop, public_agent) -> None:
    page = merchant_client.get(f"/s/{shop.pk}/marketplace/").content.decode()
    assert "Helper" in page and "Products" in page
    page = merchant_client.get(f"/s/{shop.pk}/marketplace/?q=nothing-like-this").content.decode()
    assert "No agents match" in page
    page = merchant_client.get(f"/s/{shop.pk}/marketplace/?category=Products").content.decode()
    assert "Helper" in page


def test_detail_lists_permissions_in_plain_language(merchant_client, shop, public_agent) -> None:
    page = merchant_client.get(f"/s/{shop.pk}/marketplace/helper/").content.decode()
    assert "Permissions it needs" in page
    assert "products" in page.lower() and "Install" in page


def test_hidden_agent_detail_is_404(merchant_client, shop, agent) -> None:
    # `agent` is PRIVATE and this shop isn't allowed.
    assert merchant_client.get(f"/s/{shop.pk}/marketplace/helper/").status_code == 404


# --------------------------------------------------------------------------- install flow
def test_install_asks_for_missing_scopes(merchant_client, shop, public_agent) -> None:
    resp = merchant_client.get(f"/s/{shop.pk}/marketplace/helper/install/")
    page = resp.content.decode()
    assert "needs access to" in page and "write_products" in page
    assert not AgentInstallation.objects.exists()


def test_grant_saves_pending_install_and_redirects_to_oauth(
    merchant_client, shop, public_agent
) -> None:
    resp = merchant_client.post(f"/s/{shop.pk}/marketplace/helper/grant/")
    url = urlparse(resp["Location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.path == "/shopify/auth/"
    assert q["scopes"] == "write_products" and q["shop"] == SHOP_DOMAIN
    assert q["next"] == f"/s/{shop.pk}/marketplace/helper/install/"
    assert merchant_client.session[SESSION_PENDING]["agent"] == "helper"


def test_full_install_with_scope_upgrade(
    merchant_client, shop, public_agent, mock_token_endpoint, mock_graphql
) -> None:
    """Acceptance §15.3: install asks for write_products, re-OAuth, then completes."""
    client = merchant_client
    # 1. Continue → our auth view → Shopify with the union of scopes.
    auth = client.post(f"/s/{shop.pk}/marketplace/helper/grant/")["Location"]
    shopify = urlparse(client.get(auth)["Location"])
    q = {k: v[0] for k, v in parse_qs(shopify.query).items()}
    assert q["scope"] == "write_products"  # write_products includes read_products

    # 2. Shopify redirects back: the callback resumes the install page.
    mock_token_endpoint(scope="write_products")
    mock_graphql(shop_info_response(scopes=["write_products"], email=shop.email))
    resp = client.get(
        "/shopify/callback/",
        signed_query(shop=SHOP_DOMAIN, code="code", state=q["state"]),
    )
    assert resp["Location"] == f"/s/{shop.pk}/marketplace/helper/install/"

    # 3. Settings form (pre-filled), then save.
    page = client.get(resp["Location"]).content.decode()
    assert "Access granted" in page and "Set up Helper" in page
    resp = client.post(f"/s/{shop.pk}/marketplace/helper/install/", {"agent-autonomy": "APPROVE"})
    inst = AgentInstallation.objects.get(shop=shop)
    assert resp["Location"] == f"/s/{shop.pk}/agents/{inst.pk}/"
    assert inst.version == public_agent.latest_version and inst.autonomy == "APPROVE"
    assert SESSION_PENDING not in client.session


def test_install_validates_settings_and_autonomy(shop, public_agent) -> None:
    shop.granted_scopes = ["write_products"]
    shop.save()
    with pytest.raises(InstallError, match="can't run in that mode"):
        install_agent(shop, public_agent, settings={}, autonomy="AUTO")
    version = publish(
        public_agent,
        settings_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "maximum": 5}},
        },
    )
    assert version.number == 2
    with pytest.raises(InstallError, match="Invalid settings"):
        install_agent(shop, public_agent, settings={"limit": 50}, autonomy="APPROVE")
    inst = install_agent(shop, public_agent, settings={"limit": 3}, autonomy="APPROVE")
    with pytest.raises(InstallError, match="already installed"):
        install_agent(shop, public_agent, settings={}, autonomy="APPROVE")
    assert inst.settings == {"limit": 3}


def test_members_cannot_install(member_client, shop, public_agent) -> None:
    assert member_client.get(f"/s/{shop.pk}/marketplace/helper/install/").status_code == 403
    assert member_client.post(f"/s/{shop.pk}/marketplace/helper/grant/").status_code == 403


def test_installed_agent_redirects_to_manage_page(merchant_client, shop, public_agent) -> None:
    shop.granted_scopes = ["write_products"]
    shop.save()
    inst = install_agent(shop, public_agent, settings={}, autonomy="APPROVE")
    resp = merchant_client.get(f"/s/{shop.pk}/marketplace/helper/install/")
    assert resp["Location"] == f"/s/{shop.pk}/agents/{inst.pk}/"
