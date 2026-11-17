from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        # Sentry and Celery log context are wired here so they apply to both the
        # web process and the worker.
        from apps.core import observability

        observability.init_sentry()
        observability.connect_celery_signals()
