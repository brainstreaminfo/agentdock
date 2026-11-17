"""AnthropicProvider against a mocked HTTP stream; versions, templates, commands."""

from __future__ import annotations

import json
from io import StringIO

import anthropic
import httpx2
import pytest
from django.core.management import call_command
from django.db import IntegrityError

from apps.agents.llm import AnthropicProvider, LLMError
from apps.agents.models import AgentDefinition, AgentInstallation, AgentVersion
from apps.runs.models import Run
from tests.agents.conftest import text_turn

pytestmark = pytest.mark.django_db


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = sse(
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_9",
            "name": "products__search",
            "input": {},
        },
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"query": "tea"}'},
    },
    {"type": "content_block_stop", "index": 1},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 30},
    },
    {"type": "message_stop"},
)


def provider_with(handler) -> AnthropicProvider:
    client = anthropic.Anthropic(
        api_key="test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider(client=client)


def test_provider_streams_and_parses(settings) -> None:
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=STREAM)

    chunks: list[str] = []
    response = provider_with(handler).stream_turn(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
        on_text=chunks.append,
    )
    assert "".join(chunks) == "Hello there"
    assert response.stop_reason == "tool_use"
    assert response.content == [
        {"type": "text", "text": "Hello there"},
        {
            "type": "tool_use",
            "id": "toolu_9",
            "name": "products__search",
            "input": {"query": "tea"},
        },
    ]
    assert response.usage["output_tokens"] == 30
    # Refusal fallbacks are on for Opus 5.5.
    assert seen["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]


def test_no_fallbacks_for_models_without_support() -> None:
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=STREAM)

    provider_with(handler).stream_turn(
        {
            "model": "claude-haiku-5-5",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
        on_text=lambda t: None,
    )
    assert "fallbacks" not in seen["body"]


def test_api_errors_become_llm_errors() -> None:
    def handler(request):
        return httpx2.Response(
            529,
            json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        )

    with pytest.raises(LLMError) as exc:
        provider_with(handler).stream_turn(
            {
                "model": "claude-opus-5-5",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            on_text=lambda t: None,
        )
    assert exc.value.retryable and "529" in str(exc.value)


# ------------------------------------------------------------------ versions
def test_publish_creates_numbered_immutable_versions(agent) -> None:
    draft = agent.draft
    draft.instructions_md = "Changed"
    draft.save()
    v2 = draft.publish(changelog="Second")
    assert v2.number == 2 and not v2.is_draft and v2.instructions_md == "Changed"
    assert set(v2.tools.values_list("key", flat=True)) == set(
        draft.tools.values_list("key", flat=True)
    )
    assert v2.required_scopes == ["write_products"]  # products.update implies read
    assert agent.versions.filter(is_draft=True).count() == 1
    with pytest.raises(ValueError):
        v2.publish()


def test_only_one_draft_per_agent(agent) -> None:
    with pytest.raises(IntegrityError):
        AgentVersion.objects.create(agent=agent, is_draft=True)


def test_templates_seed_idempotently(templates) -> None:
    from apps.agents.seed import seed_templates

    seed_templates()
    assert AgentDefinition.objects.filter(is_template=True).count() == 3
    writer = AgentDefinition.objects.get(slug="product-description-writer")
    assert writer.draft.required_scopes == ["write_products"]
    assert writer.latest_version is None  # templates are never published
    analyst = AgentDefinition.objects.get(slug="sales-qa-analyst").draft
    assert "ui.render_chart" in analyst.tools.values_list("key", flat=True)


# ------------------------------------------------------------------ commands & task
def test_run_agent_command_prints_steps(agent_shop, agent, fake_llm) -> None:
    fake_llm.script(text_turn("All good."))
    out = StringIO()
    call_command("run_agent", shop=agent_shop.domain, agent="helper", task="Check", stdout=out)
    text = out.getvalue()
    assert "All good." in text and "SUCCEEDED" in text and "LLM" in text
    assert Run.objects.get().trigger == "MANUAL"


def test_install_agent_command(agent_shop, templates) -> None:
    out = StringIO()
    call_command(
        "install_agent", shop=agent_shop.domain, agent="sales-qa-analyst", publish=True, stdout=out
    )
    installation = AgentInstallation.objects.get(shop=agent_shop)
    assert installation.version.number == 1 and installation.autonomy == "SUGGEST"


def test_celery_task_runs_queued_run_once(installation, fake_llm) -> None:
    from apps.agents.runtime import create_run
    from apps.agents.tasks import execute_run_task

    fake_llm.script(text_turn("done"))
    run = create_run(
        shop=installation.shop,
        version=installation.version,
        installation=installation,
        trigger=Run.Trigger.MANUAL,
        input={"task": "x"},
    )
    assert execute_run_task.delay(run.pk).get() == "SUCCEEDED"
    assert execute_run_task.delay(run.pk).get() == "skipped"  # redelivery is harmless
