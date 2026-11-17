"""
Scheduled agents (spec §7.1 "Triggers", §9).

Each installation whose version has a cron schedule gets ONE django-celery-beat
`PeriodicTask` named `agent-installation-<id>`. Its crontab carries the shop's
timezone, so "0 9 * * *" means 9:00 in the store's local time.

    sync_schedule(installation)   create / update / remove the PeriodicTask
    next_run_at(installation)     when it will run next (for the merchant UI)

You can inspect schedules in Django Admin → Periodic tasks. The task itself is
`apps.triggers.tasks.run_scheduled_agent`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from celery.schedules import crontab
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from apps.core.logging import get_logger

if TYPE_CHECKING:
    from apps.agents.models import AgentInstallation

log = get_logger(__name__)

TASK = "apps.triggers.tasks.run_scheduled_agent"


def task_name(installation_id: int) -> str:
    return f"agent-installation-{installation_id}"


def shop_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def is_scheduled(installation: AgentInstallation) -> bool:
    """Should this installation run on its schedule right now?"""
    return bool(
        installation.enabled
        and installation.schedule_enabled
        and installation.version.schedule_cron
        and installation.shop.is_installed
    )


def sync_schedule(installation: AgentInstallation) -> PeriodicTask | None:
    """Make the PeriodicTask match the installation. Returns it, or None if removed."""
    name = task_name(installation.pk)
    if not is_scheduled(installation):
        deleted, _ = PeriodicTask.objects.filter(name=name).delete()
        if deleted:
            log.info("schedule.removed", installation_id=installation.pk)
        return None

    minute, hour, day_of_month, month_of_year, day_of_week = (
        installation.version.schedule_cron.split()
    )
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
        timezone=shop_zone(installation.shop.timezone),
    )
    task, created = PeriodicTask.objects.update_or_create(
        name=name,
        defaults={
            "task": TASK,
            "crontab": schedule,
            "interval": None,
            "args": json.dumps([installation.pk]),
            "enabled": True,
            "description": f"{installation.agent.name} @ {installation.shop.domain}",
        },
    )
    log.info(
        "schedule.synced",
        installation_id=installation.pk,
        cron=installation.version.schedule_cron,
        tz=installation.shop.timezone,
        created=created,
    )
    return task


def remove_schedule(installation_id: int) -> None:
    PeriodicTask.objects.filter(name=task_name(installation_id)).delete()


def next_run_at(installation: AgentInstallation, now: datetime | None = None) -> datetime | None:
    """Next time the schedule fires, in the shop's timezone (None if not scheduled)."""
    if not is_scheduled(installation):
        return None
    return next_cron_time(
        installation.version.schedule_cron, shop_zone(installation.shop.timezone), now
    )


def next_cron_time(cron: str, tz: ZoneInfo, now: datetime | None = None) -> datetime | None:
    """
    First time strictly after `now` that matches `cron` in timezone `tz`.

    Uses Celery's own parser, and Celery's rule that day-of-month AND day-of-week
    must both match. (Celery's `remaining_estimate` ignores per-schedule
    timezones, so we walk the days ourselves.)
    """
    minute, hour, dom, month, dow = cron.split()
    # Celery's type stubs don't declare the parsed sets (minute, hour…), hence Any.
    parsed: Any = crontab(
        minute=minute, hour=hour, day_of_month=dom, month_of_year=month, day_of_week=dow
    )
    local_now = (now or timezone.now()).astimezone(tz)
    day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(367):
        cron_weekday = (day.weekday() + 1) % 7  # cron: 0 = Sunday; Python: 0 = Monday
        if (
            day.month in parsed.month_of_year
            and day.day in parsed.day_of_month
            and cron_weekday in parsed.day_of_week
        ):
            for h in sorted(parsed.hour):
                for m in sorted(parsed.minute):
                    candidate = day.replace(hour=h, minute=m)
                    if candidate > local_now:
                        return candidate
        day = (day + timedelta(days=1)).replace(tzinfo=tz)
    return None
