"""Every webhook Shopify sends us is saved here first, then processed by Celery."""

from __future__ import annotations

from django.db import models


class WebhookEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        IGNORED = "IGNORED", "Ignored"

    shop = models.ForeignKey(
        "shops.Shop", null=True, blank=True, on_delete=models.CASCADE, related_name="webhook_events"
    )
    shop_domain = models.CharField(max_length=255, db_index=True)
    topic = models.CharField(max_length=100, db_index=True)
    # X-Shopify-Event-Id (falls back to X-Shopify-Webhook-Id). Unique => duplicates
    # delivered by Shopify's retries are stored once and processed once.
    shopify_event_id = models.CharField(max_length=255, unique=True)
    api_version = models.CharField(max_length=20, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.topic} from {self.shop_domain} ({self.status})"
