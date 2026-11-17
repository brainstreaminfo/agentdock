"""Alerts shown to merchants (low stock, missing permissions, agent findings…)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

from apps.core.models import ShopScopedModel

if TYPE_CHECKING:
    from apps.shops.models import Shop


class Alert(ShopScopedModel):
    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARNING = "WARNING", "Warning"
        CRITICAL = "CRITICAL", "Critical"

    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="alerts")
    agent_slug = models.CharField(max_length=100, blank=True)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.INFO)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    # Same dedupe_key + unread => we don't create a duplicate (e.g. "missing scope X").
    dedupe_key = models.CharField(max_length=200, blank=True, db_index=True)
    action_url = models.CharField(
        max_length=500, blank=True, help_text="Optional button link, e.g. grant permissions."
    )
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title}"


def create_alert(
    shop: Shop,
    title: str,
    body: str = "",
    *,
    severity: str = Alert.Severity.INFO,
    agent_slug: str = "",
    dedupe_key: str = "",
    action_url: str = "",
) -> Alert:
    """Create an alert, or return the existing unread one with the same dedupe_key."""
    if dedupe_key:
        existing = Alert.objects.filter(
            shop=shop, dedupe_key=dedupe_key, read_at__isnull=True
        ).first()
        if existing is not None:
            return existing
    return Alert.objects.create(
        shop=shop,
        title=title[:200],
        body=body,
        severity=severity,
        agent_slug=agent_slug,
        dedupe_key=dedupe_key,
        action_url=action_url,
    )
