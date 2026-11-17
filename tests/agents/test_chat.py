"""Merchant chat: send → run (Celery eager in tests) → SSE stream → final answer."""

from __future__ import annotations

import json

import pytest
from django.test import AsyncClient

from apps.accounts.models import Membership
from apps.chat.models import Conversation, Message
from apps.chat.rendering import render_markdown
from apps.runs.models import ActionProposal, Run
from tests.agents.conftest import text_turn, tool_turn
from tests.factories import ShopFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
async def close_thread_connections():
    """Async views query the DB from a worker thread; close its connection afterwards
    so the test database can be dropped at the end of the run."""
    from asgiref.sync import sync_to_async
    from django.db import connections

    yield
    await sync_to_async(connections.close_all)()


@pytest.fixture
def merchant(installation):
    user = UserFactory()
    Membership.objects.create(user=user, shop=installation.shop, role="OWNER")
    return user


@pytest.fixture
def conversation(installation, merchant):
    return Conversation.objects.create(
        shop=installation.shop, user=merchant, installation=installation, title="Chat with Helper"
    )


def test_chat_page_lists_installed_agents(client, merchant, installation) -> None:
    client.force_login(merchant)
    content = client.get(f"/s/{installation.shop_id}/chat/").content.decode()
    assert "Helper" in content and "Start a chat" in content


def test_start_conversation(client, merchant, installation) -> None:
    client.force_login(merchant)
    resp = client.post(f"/s/{installation.shop_id}/chat/start/", {"installation": installation.pk})
    conversation = Conversation.objects.get()
    assert resp["Location"] == f"/s/{installation.shop_id}/chat/{conversation.pk}/"
    assert conversation.user == merchant


def test_send_message_runs_agent(client, merchant, conversation, fake_llm) -> None:
    fake_llm.script(text_turn("**Hi!** I can help."))
    client.force_login(merchant)
    resp = client.post(
        f"/s/{conversation.shop_id}/chat/{conversation.pk}/send/", {"message": "Hello there"}
    )

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Hello there" in html and "data-stream" in html  # user bubble + live assistant bubble
    run = Run.objects.get()
    assert run.status == "SUCCEEDED" and run.trigger == "CHAT"
    assert [m.role for m in Message.objects.filter(conversation=conversation)] == [
        "user",
        "assistant",
    ]
    conversation.refresh_from_db()
    assert conversation.title == "Hello there"
    # Reloading the page shows the finished answer, rendered from Markdown.
    page = client.get(f"/s/{conversation.shop_id}/chat/{conversation.pk}/").content.decode()
    assert "<strong>Hi!</strong> I can help." in page


@pytest.mark.django_db(transaction=True)
async def test_stream_replays_events_and_ends_with_done(
    merchant, conversation, fake_llm, close_thread_connections
) -> None:
    from asgiref.sync import sync_to_async

    from apps.agents.runtime import create_run, execute_run

    fake_llm.script(text_turn("Streaming works"))

    @sync_to_async
    def make_run():
        run = create_run(
            shop=conversation.shop,
            version=conversation.installation.version,
            installation=conversation.installation,
            conversation=conversation,
            trigger=Run.Trigger.CHAT,
            input={"message": "hi"},
        )
        execute_run(run)
        return run

    run = await make_run()
    client = AsyncClient()
    await sync_to_async(client.force_login)(merchant)
    resp = await client.get(f"/s/{conversation.shop_id}/runs/{run.pk}/stream/")
    assert resp["Content-Type"] == "text/event-stream"
    body = "".join(
        [
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in resp.streaming_content
        ]
    )
    assert "event: delta" in body and body.rstrip().splitlines()[-2] == "event: done"
    done = json.loads(body.rstrip().splitlines()[-1].removeprefix("data: "))
    assert "Streaming works" in done["html"]


@pytest.mark.django_db(transaction=True)
async def test_stream_access_control(
    merchant, conversation, fake_llm, close_thread_connections
) -> None:
    from asgiref.sync import sync_to_async

    from apps.agents.runtime import create_run

    run = await sync_to_async(create_run)(
        shop=conversation.shop,
        version=conversation.installation.version,
        trigger=Run.Trigger.CHAT,
        input={"message": "x"},
    )
    anonymous = AsyncClient()
    assert (
        await anonymous.get(f"/s/{conversation.shop_id}/runs/{run.pk}/stream/")
    ).status_code == 302

    stranger = await sync_to_async(UserFactory)()
    client = AsyncClient()
    await sync_to_async(client.force_login)(stranger)
    assert (await client.get(f"/s/{conversation.shop_id}/runs/{run.pk}/stream/")).status_code == 404


def test_other_users_conversation_is_hidden(client, conversation, installation) -> None:
    colleague = UserFactory()
    Membership.objects.create(user=colleague, shop=installation.shop, role="ADMIN")
    client.force_login(colleague)
    assert client.get(f"/s/{installation.shop_id}/chat/{conversation.pk}/").status_code == 404


def test_cannot_send_while_answer_is_running(client, merchant, conversation) -> None:
    Run.objects.create(
        shop=conversation.shop, conversation=conversation, trigger="CHAT", status=Run.Status.RUNNING
    )
    client.force_login(merchant)
    resp = client.post(f"/s/{conversation.shop_id}/chat/{conversation.pk}/send/", {"message": "hi"})
    assert resp.status_code == 409


def test_approve_and_undo_from_chat_card(
    client, merchant, conversation, fake_llm, fake_shopify
) -> None:
    state = {"id": "gid://shopify/Product/1", "title": "Old", "seo": {}}

    def update(v):
        state.update({k: val for k, val in v["product"].items() if k != "id"})
        return {"productUpdate": {"product": dict(state), "userErrors": []}}

    fake_shopify.on("ProductEditableFields", lambda v: {"product": dict(state)})
    fake_shopify.on("UpdateProduct", update)
    fake_llm.script(
        tool_turn("products.update", {"product_id": "1", "title": "New"}), text_turn("Proposed.")
    )
    client.force_login(merchant)
    client.post(f"/s/{conversation.shop_id}/chat/{conversation.pk}/send/", {"message": "Retitle"})
    proposal = ActionProposal.objects.get()

    page = client.get(f"/s/{conversation.shop_id}/chat/{conversation.pk}/").content.decode()
    assert "Approve" in page and "Update product" in page

    resp = client.post(f"/s/{conversation.shop_id}/actions/{proposal.uuid}/approve/")
    assert "Applied" in resp.content.decode() and state["title"] == "New"
    resp = client.post(f"/s/{conversation.shop_id}/actions/{proposal.uuid}/undo/")
    assert "Undone" in resp.content.decode() and state["title"] == "Old"


def test_members_cannot_approve(client, conversation, installation, fake_shopify) -> None:
    proposal = ActionProposal.objects.create(
        shop=installation.shop, tool="products.update", payload={"product_id": "1", "title": "X"}
    )
    member = UserFactory()
    Membership.objects.create(user=member, shop=installation.shop, role="MEMBER")
    client.force_login(member)
    assert (
        client.post(f"/s/{installation.shop_id}/actions/{proposal.uuid}/approve/").status_code
        == 403
    )


def test_cannot_approve_other_shops_proposal(client, merchant, installation) -> None:
    other = ActionProposal.objects.create(shop=ShopFactory(), tool="products.update", payload={})
    client.force_login(merchant)
    assert (
        client.post(f"/s/{installation.shop_id}/actions/{other.uuid}/approve/").status_code == 404
    )


def test_trace_page(client, merchant, installation, fake_llm) -> None:
    from apps.agents.runtime import create_run, execute_run

    fake_llm.script(text_turn("ok"))
    run = execute_run(
        create_run(
            shop=installation.shop,
            version=installation.version,
            trigger=Run.Trigger.MANUAL,
            input={"task": "t"},
        )
    )
    client.force_login(merchant)
    content = client.get(f"/s/{installation.shop_id}/runs/{run.pk}/").content.decode()
    assert f"Run #{run.pk}" in content and "claude-opus-5-5" in content
    assert "1,000 / 200" in content  # tokens in / out
    other_run = Run.objects.create(shop=ShopFactory(), trigger="MANUAL")
    assert client.get(f"/s/{installation.shop_id}/runs/{other_run.pk}/").status_code == 404


def test_chart_tool_renders_canvas(client, merchant, conversation, fake_llm) -> None:
    chart = {
        "type": "bar",
        "title": "Sales",
        "labels": ["Mon"],
        "datasets": [{"label": "GBP", "data": [5]}],
    }
    fake_llm.script(tool_turn("ui.render_chart", chart), text_turn("Here you go."))
    client.force_login(merchant)
    client.post(f"/s/{conversation.shop_id}/chat/{conversation.pk}/send/", {"message": "chart"})
    page = client.get(f"/s/{conversation.shop_id}/chat/{conversation.pk}/").content.decode()
    assert "<canvas data-chart=" in page and "Show a chart" in page


def test_markdown_is_sanitized() -> None:
    html = render_markdown("Hi <script>alert(1)</script> [x](javascript:alert(1)) **bold**")
    assert "<script>" not in html and "javascript:" not in html
    assert "<strong>bold</strong>" in html
