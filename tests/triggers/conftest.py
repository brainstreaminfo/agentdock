"""Fixtures for merchant-side agent tests (Marketplace, installs, triggers, approvals)."""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import Membership
from apps.agents.models import AgentDefinition
from tests.agents.conftest import (  # noqa: F401
    agent,
    agent_shop,
    fake_llm,
    fake_shopify,
    installation,
)
from tests.factories import UserFactory


@pytest.fixture
def public_agent(agent: AgentDefinition) -> AgentDefinition:  # noqa: F811
    """The 'helper' agent (products.search/update…), published and PUBLIC."""
    agent.refresh_from_db()  # publish() updated `status` in the database only
    agent.visibility = AgentDefinition.Visibility.PUBLIC
    agent.category = "Products"
    agent.tagline = "Helps with products"
    agent.save()
    return agent


@pytest.fixture
def merchant_client(client: Any, shop: Any, owner: Any) -> Any:
    """Logged-in OWNER of `shop` (test-store, only read_products granted)."""
    client.force_login(owner)
    return client


@pytest.fixture
def member_client(client: Any, shop: Any) -> Any:
    user = UserFactory()
    Membership.objects.create(user=user, shop=shop, role="MEMBER")
    client.force_login(user)
    return client


def publish(target: AgentDefinition, **changes: Any) -> Any:
    """Change the draft of `target` and publish a new version."""
    draft = target.draft
    tools = changes.pop("tools", None)
    for field, value in changes.items():
        setattr(draft, field, value)
    draft.save()
    if tools is not None:
        draft.tools.set(tools)
    return draft.publish(changelog="test")
