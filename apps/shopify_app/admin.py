from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import WebhookEvent
from .tasks import process_webhook_event


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["topic", "shop_domain", "status", "attempts", "received_at", "processed_at"]
    list_filter = ["status", "topic"]
    search_fields = ["shop_domain", "shopify_event_id"]
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]
    actions = ["replay"]

    @admin.action(description="Replay selected webhooks")
    def replay(self, request: HttpRequest, queryset: QuerySet[WebhookEvent]) -> None:
        for event in queryset:
            event.status = WebhookEvent.Status.PENDING
            event.save(update_fields=["status"])
            process_webhook_event.delay(event.pk)
        messages.success(request, f"Queued {queryset.count()} webhook(s) for replay.")
