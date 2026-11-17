from django.apps import AppConfig


class TriggersConfig(AppConfig):
    """
    Starts agent runs without a chat message (spec §9 "Triggers"):
    schedules (django-celery-beat) and Shopify events (webhooks).
    """

    name = "apps.triggers"
    label = "triggers"

    def ready(self) -> None:
        from apps.shopify_app.webhooks import register_fallback

        from . import receivers  # noqa: F401  (connects signal receivers)
        from .router import route_event

        register_fallback(route_event)
