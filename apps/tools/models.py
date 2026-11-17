"""
ToolDefinition: every tool agents can use, stored in the database.

* CODE tools are written in Python (apps/tools/catalog/). `manage.py sync_tools`
  copies them from the registry into this table on every deploy, so the Studio
  can list them and staff can switch individual tools off (`enabled`).
* GRAPHQL tools are created by staff in the Studio: a GraphQL document, an input
  JSON Schema and the scopes it needs. No code.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class ToolDefinition(TimeStampedModel):
    class Kind(models.TextChoices):
        CODE = "CODE", "Built-in (code)"
        GRAPHQL = "GRAPHQL", "Custom GraphQL"

    key = models.CharField(
        max_length=100, unique=True, help_text="Dotted id the AI uses, e.g. products.search"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(
        help_text="Shown to the AI: what the tool does and when to use it."
    )
    category = models.CharField(max_length=50, db_index=True)
    kind = models.CharField(max_length=7, choices=Kind.choices, default=Kind.CODE)
    scopes = models.JSONField(default=list, blank=True)
    mutating = models.BooleanField(default=False, help_text="Changes data in the store.")
    input_schema = models.JSONField(default=dict, blank=True)
    graphql_document = models.TextField(blank=True)
    output_path = models.CharField(
        max_length=255, blank=True, help_text="Optional, e.g. data.products.edges[*].node"
    )
    can_undo = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["category", "key"]

    def __str__(self) -> str:
        return self.key
