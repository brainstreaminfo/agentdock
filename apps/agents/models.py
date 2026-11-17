"""
Agent models. Phase 2 only needs `Autonomy` and `ShopMemory`; AgentDefinition,
AgentVersion and AgentInstallation arrive in Phase 3.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import ShopScopedModel


class Autonomy(models.TextChoices):
    SUGGEST = "SUGGEST", "Suggest only"
    APPROVE = "APPROVE", "Ask before changing"
    AUTO = "AUTO", "Act automatically"


class ShopMemory(ShopScopedModel):
    """
    Brand memory shared by all agents of a shop: brand voice, audience,
    policies, banned words… One row per key.
    """

    class Source(models.TextChoices):
        MERCHANT = "MERCHANT", "Merchant"
        AGENT = "AGENT", "Agent (approved)"

    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="memories")
    key = models.SlugField(max_length=100)
    value = models.TextField()
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MERCHANT)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["shop", "key"], name="unique_shop_memory_key")
        ]

    def __str__(self) -> str:
        return f"{self.key}"
