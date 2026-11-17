"""Reuse the agent fixtures (fake Claude, fake Shopify, a published agent)."""

import pytest

from apps.accounts.models import Membership
from tests.agents.conftest import (  # noqa: F401
    agent,
    agent_shop,
    fake_llm,
    fake_shopify,
    installation,
    templates,
)
from tests.factories import UserFactory


@pytest.fixture
def staff(agent_shop):  # noqa: F811
    user = UserFactory(is_staff=True)
    Membership.objects.create(user=user, shop=agent_shop, role="OWNER")
    return user


@pytest.fixture
def staff_client(client, staff):
    client.force_login(staff)
    return client
