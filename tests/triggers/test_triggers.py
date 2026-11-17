"""Schedules and Shopify-event triggers (spec §9, acceptance §15.6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django_celery_beat.models import PeriodicTask

from apps.runs.models import Run
from apps.shopify_app.models import WebhookEvent
from apps.shopify_app.services import mark_uninstalled
from apps.shopify_app.topics import AGENT_EVENT_TOPICS
from apps.triggers import schedules, subscriptions
from apps.triggers.tasks import run_scheduled_agent, start_debounced_event_run
from tests.agents.conftest import text_turn
from tests.marketplace.conftest import publish

pytestmark = pytest.mark.django_db


def use_version(installation, version, **fields):
    installation.version = version
    for k, v in fields.items():
        setattr(installation, k, v)
    installation.save()
    return installation


# --------------------------------------------------------------------------- schedules
def test_schedule_is_created_in_shop_timezone(installation, agent) -> None:
    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "30 9 * * 1", "events": []})
    use_version(installation, v2, schedule_enabled=True)

    task = PeriodicTask.objects.get(name=f"agent-installation-{installation.pk}")
    assert task.task == "apps.triggers.tasks.run_scheduled_agent"
    assert json.loads(task.args) == [installation.pk]
    assert (task.crontab.minute, task.crontab.hour, task.crontab.day_of_week) == ("30", "9", "1")
    assert str(task.crontab.timezone) == "Europe/London"


def test_schedule_removed_when_paused_or_switched_off(installation, agent) -> None:
    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "0 9 * * *", "events": []})
    use_version(installation, v2, schedule_enabled=True)
    use_version(installation, v2, enabled=False)
    assert not PeriodicTask.objects.exists()
    use_version(installation, v2, enabled=True)
    assert PeriodicTask.objects.count() == 1
    use_version(installation, v2, schedule_enabled=False)
    assert not PeriodicTask.objects.exists()


def test_next_run_respects_timezone() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)  # 09:00 in New York
    nxt = schedules.next_cron_time("30 9 * * *", tz, now)
    assert nxt == datetime(2026, 11, 17, 9, 30, tzinfo=tz)
    # Mondays only (cron 1 = Monday); 17 Nov 2026 is a Tuesday.
    assert schedules.next_cron_time("0 8 * * 1", tz, now) == datetime(2026, 11, 23, 8, 0, tzinfo=tz)


def test_scheduled_task_runs_the_agent(installation, agent, fake_llm) -> None:
    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "0 9 * * *", "events": []})
    use_version(installation, v2, schedule_enabled=True)
    fake_llm.script(text_turn("Daily report done."))

    run_scheduled_agent(installation.pk)

    run = Run.objects.get()
    assert run.trigger == "SCHEDULE" and run.status == "SUCCEEDED"
    assert "schedule" in fake_llm.requests[0]["messages"][0]["content"]


def test_scheduled_task_skips_paused_and_cleans_up_deleted(installation, agent) -> None:
    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "0 9 * * *", "events": []})
    use_version(installation, v2, schedule_enabled=True, enabled=False)
    assert run_scheduled_agent(installation.pk) == "skipped"
    assert run_scheduled_agent(999999) == "gone"
    assert not Run.objects.exists()


# --------------------------------------------------------------------------- events
@pytest.fixture
def order_agent(installation, agent):
    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "", "events": ["orders/create"]})
    return use_version(installation, v2)


def test_orders_create_webhook_starts_event_run(post_webhook, order_agent, fake_llm) -> None:
    """Acceptance §15.6: an event-triggered agent runs on orders/create."""
    fake_llm.script(text_turn("New order noted."))
    payload = {"id": 42, "email": "buyer@example.com", "total_price": "10.00"}

    post_webhook("orders/create", payload, shop=order_agent.shop.domain, event_id="evt-order-1")

    run = Run.objects.get()
    assert run.trigger == "EVENT" and run.status == "SUCCEEDED"
    assert run.input["event"]["topic"] == "orders/create"
    assert "buyer@example.com" not in json.dumps(run.input)  # customer data masked
    assert "orders/create" in fake_llm.requests[0]["messages"][0]["content"]
    assert WebhookEvent.objects.get().status == WebhookEvent.Status.PROCESSED


def test_event_is_not_routed_twice(order_agent, fake_llm) -> None:
    from apps.triggers.router import route_event

    fake_llm.script(text_turn("ok"))
    event = WebhookEvent.objects.create(
        shop=order_agent.shop,
        shop_domain=order_agent.shop.domain,
        topic="orders/create",
        shopify_event_id="evt-x",
        payload={"id": 1},
    )
    assert route_event(event) is True
    assert route_event(event) is True  # a Celery retry of the same webhook
    assert Run.objects.count() == 1


def test_installations_not_listening_are_ignored(post_webhook, installation) -> None:
    post_webhook("orders/create", {"id": 1}, shop=installation.shop.domain)
    assert not Run.objects.exists()
    assert WebhookEvent.objects.get().status == WebhookEvent.Status.IGNORED


def test_paused_installation_gets_no_event_runs(post_webhook, order_agent) -> None:
    use_version(order_agent, order_agent.version, enabled=False)
    post_webhook("orders/create", {"id": 1}, shop=order_agent.shop.domain)
    assert not Run.objects.exists()


def test_noisy_topics_are_debounced(installation, agent, fake_llm) -> None:
    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "", "events": ["products/update"]})
    use_version(installation, v2)
    events = [
        WebhookEvent.objects.create(
            shop=installation.shop,
            shop_domain=installation.shop.domain,
            topic="products/update",
            shopify_event_id=f"evt-{i}",
            payload={"admin_graphql_api_id": "gid://shopify/Product/1"},
        )
        for i in range(2)
    ]
    key = f"trigger-debounce:{installation.pk}:products/update:gid://shopify/Product/1"
    cache.set(key, events[1].pk)  # the second update arrived before the first timer fired

    assert start_debounced_event_run(installation.pk, events[0].pk, key) == "superseded"
    fake_llm.script(text_turn("ok"))
    assert start_debounced_event_run(installation.pk, events[1].pk, key).startswith("run ")
    assert Run.objects.count() == 1


# --------------------------------------------------------------------------- subscriptions
def test_every_agent_topic_is_a_shopify_enum() -> None:
    from apps.shopify_app.schema import load_schema

    enum = load_schema().get_type("WebhookSubscriptionTopic")
    assert {subscriptions.topic_enum(t) for t in AGENT_EVENT_TOPICS} <= set(enum.values)


def test_sync_creates_needed_and_deletes_unused_subscriptions(order_agent, fake_shopify) -> None:
    shop = order_agent.shop
    uri = "https://app.test/shopify/webhooks/"
    fake_shopify.on(
        "AgentWebhookSubscriptions",
        {
            "webhookSubscriptions": {
                "nodes": [
                    {
                        "id": "gid://shopify/WebhookSubscription/1",
                        "topic": "PRODUCTS_UPDATE",
                        "uri": uri,
                    },
                    {
                        "id": "gid://shopify/WebhookSubscription/2",
                        "topic": "APP_UNINSTALLED",
                        "uri": uri,
                    },
                ]
            }
        },
    )
    fake_shopify.on(
        "CreateAgentWebhookSubscription",
        {
            "webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "x", "topic": "ORDERS_CREATE"},
                "userErrors": [],
            }
        },
    )
    fake_shopify.on(
        "DeleteAgentWebhookSubscription",
        {"webhookSubscriptionDelete": {"deletedWebhookSubscriptionId": "1", "userErrors": []}},
    )

    report = subscriptions.sync_webhook_subscriptions(shop)

    assert report.created == ["orders/create"] and report.deleted == ["products/update"]
    created = fake_shopify.calls_to("CreateAgentWebhookSubscription")[0]
    assert created == {"topic": "ORDERS_CREATE", "subscription": {"uri": uri, "format": "JSON"}}
    # Lifecycle topics from shopify.app.toml are never touched.
    assert fake_shopify.calls_to("DeleteAgentWebhookSubscription") == [
        {"id": "gid://shopify/WebhookSubscription/1"}
    ]


def test_topics_without_granted_scope_are_skipped(order_agent) -> None:
    shop = order_agent.shop
    shop.granted_scopes = ["write_products"]
    shop.save()
    assert subscriptions.desired_topics(shop) == set()


# --------------------------------------------------------------------------- uninstall
def test_app_uninstall_stops_schedules_and_queued_runs(installation, agent) -> None:
    from apps.agents.runtime import create_run

    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "0 9 * * *", "events": []})
    use_version(installation, v2, schedule_enabled=True)
    queued = create_run(
        shop=installation.shop, version=v2, installation=installation, trigger="MANUAL", input={}
    )

    mark_uninstalled(installation.shop)

    assert not PeriodicTask.objects.exists()
    queued.refresh_from_db()
    assert queued.status == "CANCELLED"
    installation.refresh_from_db()
    assert installation.enabled  # resumes after a re-install


def test_reinstall_restores_schedules(installation, agent) -> None:
    from apps.shopify_app import signals

    v2 = publish(agent, triggers={"chat": True, "schedule_cron": "0 9 * * *", "events": []})
    use_version(installation, v2, schedule_enabled=True)
    PeriodicTask.objects.all().delete()
    signals.shop_installed.send(sender=None, shop=installation.shop, user=None, is_new_install=True)
    assert PeriodicTask.objects.count() == 1
