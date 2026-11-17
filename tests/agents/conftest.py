"""
Fixtures for agent runtime tests.

`fake_llm` replaces Claude with a script:

    fake_llm.script(
        tool_turn("products.search", {"query": "tea"}),   # turn 1: calls a tool
        text_turn("You have 3 teas."),                     # turn 2: answers
    )

Every request the runtime sends is kept in `fake_llm.requests` for assertions.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.agents import events, llm
from apps.agents.llm import LLMResponse
from apps.agents.models import AgentDefinition, AgentInstallation
from apps.agents.seed import seed_templates
from apps.runs.models import Run
from apps.tools.models import ToolDefinition
from apps.tools.sync import sync_tools
from tests.tools.conftest import ALL_SCOPES, FakeShopify, fake_shopify  # noqa: F401

USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


def text_turn(text: str, *, stop: str = "end_turn", usage: dict | None = None) -> LLMResponse:
    return LLMResponse(
        content=[
            {"type": "thinking", "thinking": "Let me think.", "signature": "sig-abc"},
            {"type": "text", "text": text},
        ],
        stop_reason=stop,
        model="claude-opus-5-5",
        usage=dict(usage or USAGE),
        request_id="req_1",
    )


def tool_turn(
    key: str,
    tool_input: dict[str, Any],
    *,
    call_id: str = "toolu_1",
    text: str = "",
    stop: str = "tool_use",
) -> LLMResponse:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.append(
        {"type": "tool_use", "id": call_id, "name": key.replace(".", "__"), "input": tool_input}
    )
    return LLMResponse(
        content=content,
        stop_reason=stop,
        model="claude-opus-5-5",
        usage=dict(USAGE),
        request_id="req_t",
    )


class FakeLLM:
    def __init__(self) -> None:
        self.turns: list[Any] = []
        self.requests: list[dict[str, Any]] = []

    def script(self, *turns: Any) -> FakeLLM:
        self.turns.extend(turns)
        return self

    def stream_turn(self, params: dict[str, Any], on_text: Any) -> LLMResponse:
        # Snapshot the request (messages list is mutated by the runtime later).
        import copy

        self.requests.append(copy.deepcopy(params))
        assert self.turns, "The agent asked for another model turn but the script is empty"
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if callable(turn):
            turn = turn(params)
        for block in turn.content:
            if block.get("type") == "text":
                text = block["text"]
                for i in range(0, len(text), 5):  # stream in small pieces
                    on_text(text[i : i + 5])
        return turn


@pytest.fixture
def fake_llm() -> Any:
    fake = FakeLLM()
    llm.set_provider(fake)
    events._memory().streams.clear()
    yield fake
    llm.set_provider(None)


@pytest.fixture
def agent_shop(db: Any) -> Any:
    from tests.factories import ShopFactory

    return ShopFactory(
        domain="agent-store.myshopify.com",
        name="Tea Co",
        granted_scopes=ALL_SCOPES,
        timezone="Europe/London",
        currency="GBP",
    )


@pytest.fixture
def agent(db: Any) -> AgentDefinition:
    """A small published agent with a read tool, a write tool and the chart tool."""
    sync_tools()
    agent = AgentDefinition.objects.create(slug="helper", name="Helper", example_prompts=["Hi"])
    draft = agent.draft
    draft.instructions_md = "Help {{shop.name}} in a {{settings.tone}} tone.\n{{memory}}"
    draft.settings_defaults = {"tone": "friendly"}
    draft.max_steps = 5
    draft.save()
    draft.tools.set(
        ToolDefinition.objects.filter(
            key__in=["products.search", "products.update", "ui.render_chart", "memory.read"]
        )
    )
    draft.publish(changelog="v1")
    return agent


@pytest.fixture
def installation(agent_shop: Any, agent: AgentDefinition) -> AgentInstallation:
    return AgentInstallation.objects.create(
        shop=agent_shop,
        agent=agent,
        version=agent.latest_version,
        autonomy="APPROVE",
        settings={"tone": "playful"},
    )


@pytest.fixture
def templates(db: Any) -> list[AgentDefinition]:
    return seed_templates()


def run_names(run: Run) -> list[str]:
    return events._memory().names(run.pk)
