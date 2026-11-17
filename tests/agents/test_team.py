"""My Team orchestrator (spec §8.4, acceptance §15.7)."""

from __future__ import annotations

import pytest

from apps.accounts.models import Membership
from apps.agents.models import AgentDefinition, AgentInstallation
from apps.agents.orchestrator import delegate_tools, team_members, team_version
from apps.chat.models import Conversation
from apps.runs.models import ActionProposal, Run
from tests.agents.conftest import text_turn, tool_turn
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def delegate_turn(slug: str, task: str, call_id: str = "toolu_d1"):
    turn = tool_turn("x", {"task": task}, call_id=call_id)
    turn.content[-1]["name"] = f"delegate_to_{slug}"
    return turn


@pytest.fixture
def second_agent(agent_shop):
    agent = AgentDefinition.objects.create(slug="writer", name="Writer", tagline="Writes copy")
    agent.draft.publish()
    return AgentInstallation.objects.create(
        shop=agent_shop, agent=agent, version=agent.latest_version
    )


@pytest.fixture
def team_client(client, installation):
    user = UserFactory()
    Membership.objects.create(user=user, shop=installation.shop, role="OWNER")
    client.force_login(user)
    return client


def test_team_version_is_hidden_system_agent(db) -> None:
    v = team_version()
    assert v.agent.is_system and v.agent.slug == "my-team"
    assert team_version().pk == v.pk  # created once


def test_team_includes_every_chat_enabled_agent(installation, second_agent) -> None:
    """Acceptance §15.7: no code changes when agents are added."""
    tools, names = delegate_tools(installation.shop)
    assert sorted(names) == ["delegate_to_helper", "delegate_to_writer"]
    writer = next(t for t in tools if t["name"] == "delegate_to_writer")
    assert "Writes copy" in writer["description"] and writer["input_schema"]["required"] == ["task"]

    second_agent.version.triggers = {"chat": False}
    second_agent.version.save()
    second_agent.enabled = True
    assert [i.agent.slug for i in team_members(installation.shop)] == ["helper"]


def test_team_chat_delegates_and_shows_child_approvals(
    team_client, installation, fake_llm, fake_shopify
) -> None:
    shop = installation.shop
    fake_shopify.on(
        "ProductEditableFields",
        {"product": {"id": "gid://shopify/Product/1", "title": "Old", "seo": {}}},
    )
    fake_llm.script(
        delegate_turn("helper", "Rename product 1 to New"),  # My Team, turn 1
        tool_turn("products.update", {"product_id": "1", "title": "New"}),  # Helper, turn 1
        text_turn("I proposed the new title."),  # Helper, turn 2
        text_turn("Helper proposed a new title; approve it below."),  # My Team, turn 2
    )
    team_client.post(f"/s/{shop.pk}/chat/start/", {"installation": "team"})
    conversation = Conversation.objects.get()
    assert conversation.installation is None and conversation.version is None

    team_client.post(f"/s/{shop.pk}/chat/{conversation.pk}/send/", {"message": "Rename it"})

    parent = Run.objects.get(parent_run__isnull=True)
    child = Run.objects.get(parent_run=parent)
    assert parent.agent_slug == "my-team" and parent.status == "SUCCEEDED"
    assert child.agent_slug == "helper" and child.installation == installation
    assert fake_llm.requests[1]["messages"][0]["content"] == "Rename product 1 to New"
    assert parent.steps.filter(name="delegate:helper").exists()

    proposal = ActionProposal.objects.get()
    assert proposal.run == child
    page = team_client.get(f"/s/{shop.pk}/chat/{conversation.pk}/").content.decode()
    assert "My Team" in page and "Asked Helper" in page
    assert str(proposal.uuid) in page  # the approval card from the child run


def test_team_chat_needs_installed_agents(client, agent_shop) -> None:
    user = UserFactory()
    Membership.objects.create(user=user, shop=agent_shop, role="OWNER")
    client.force_login(user)
    assert (
        client.post(f"/s/{agent_shop.pk}/chat/start/", {"installation": "team"}).status_code == 404
    )


def test_playground_chats_stay_hidden(team_client, installation, agent) -> None:
    Conversation.objects.create(
        shop=installation.shop, user=None, version=agent.draft, title="Playground"
    )
    page = team_client.get(f"/s/{installation.shop_id}/chat/").content.decode()
    assert "Playground" not in page
