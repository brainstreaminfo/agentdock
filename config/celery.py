"""
Celery application.

Every task log line is tagged with `task_id` and `task_name` (see
`apps.core.logging.bind_celery_task_context`), so you can follow one task
through the logs with: `make logs | grep <task_id>`.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("agenthub")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
