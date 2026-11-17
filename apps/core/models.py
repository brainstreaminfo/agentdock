"""Shared base models and the platform-wide AuditLog."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ShopScopedQuerySet(models.QuerySet):
    def for_shop(self, shop: Any) -> ShopScopedQuerySet:
        return self.filter(shop=shop)


class ShopScopedModel(TimeStampedModel):
    """
    Base class for every model that belongs to one shop (spec §10).

    Always query shop data through `Model.objects.for_shop(shop)` in views so a
    merchant can never see another shop's rows. The `shop` FK is indexed and
    cascades: deleting a Shop deletes all its data (used by `shop/redact`).
    """

    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="+")

    objects = ShopScopedQuerySet.as_manager()

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Who did what. Written with `apps.core.audit.audit(...)`."""

    shop = models.ForeignKey(
        "shops.Shop", null=True, blank=True, on_delete=models.CASCADE, related_name="audit_logs"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=100, db_index=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M}"
