"""Celery tasks for agents."""

from __future__ import annotations

from celery import shared_task

from apps.core.logging import get_logger
from apps.runs.models import Run

from .runtime import execute_run

log = get_logger(__name__)


@shared_task(bind=True, acks_late=True)
def execute_run_task(self, run_id: int) -> str:  # type: ignore[no-untyped-def]
    """Execute one queued run. Idempotent: a run that already started is skipped."""
    updated = Run.objects.filter(pk=run_id, status=Run.Status.QUEUED).update(
        celery_task_id=self.request.id or ""
    )
    run = (
        Run.objects.select_related("shop", "version__agent", "installation")
        .filter(pk=run_id)
        .first()
    )
    if run is None or not updated:
        log.info("run.skipped_not_queued", run_id=run_id)
        return "skipped"
    return execute_run(run).status
