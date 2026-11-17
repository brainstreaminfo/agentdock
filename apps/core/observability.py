"""Sentry + Celery logging hooks. Called once from `CoreConfig.ready()`."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.core.logging import bind_celery_task_context, clear_context, get_logger

log = get_logger(__name__)
_initialised = False


def init_sentry() -> None:
    global _initialised
    if _initialised or not settings.SENTRY_DSN:
        return
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT,
        integrations=[DjangoIntegration(), CeleryIntegration(monitor_beat_tasks=True)],
        traces_sample_rate=0.1,
        send_default_pii=False,  # never send merchant emails/IPs to Sentry
    )
    _initialised = True


def connect_celery_signals() -> None:
    from celery.signals import task_postrun, task_prerun

    @task_prerun.connect(weak=False)
    def _bind(task_id: str | None = None, task: Any = None, **_: Any) -> None:
        bind_celery_task_context(task_id, getattr(task, "name", None))

    @task_postrun.connect(weak=False)
    def _clear(**_: Any) -> None:
        clear_context()
