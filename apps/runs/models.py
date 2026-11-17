"""
Runs, their steps, and actions waiting for (or after) approval.

* `Run`            one execution of an agent (Phase 3 adds installation/version links).
* `RunStep`        one LLM turn or one tool call inside a run (the "trace").
* `ActionProposal` a change an agent wants to make in the store. It waits for a
                   merchant to approve, unless the agent runs in AUTO mode.
* `ActionSnapshot` the store data before/after an applied action (used by undo).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.core.models import ShopScopedModel


class Run(ShopScopedModel):
    class Trigger(models.TextChoices):
        CHAT = "CHAT", "Chat"
        SCHEDULE = "SCHEDULE", "Schedule"
        EVENT = "EVENT", "Shopify event"
        MANUAL = "MANUAL", "Run now"
        PLAYGROUND = "PLAYGROUND", "Playground"
        TOOL_TEST = "TOOL_TEST", "Tool test (Studio)"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="runs")
    parent_run = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="child_runs"
    )
    agent_slug = models.CharField(max_length=100, blank=True, db_index=True)
    trigger = models.CharField(max_length=12, choices=Trigger.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    input = models.JSONField(default=dict, blank=True)
    output = models.JSONField(default=dict, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    celery_task_id = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    traceback = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Run {self.pk} ({self.trigger}, {self.status})"


class RunStep(models.Model):
    class Type(models.TextChoices):
        LLM = "LLM", "LLM turn"
        TOOL = "TOOL", "Tool call"

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="steps")
    idx = models.PositiveIntegerField()
    type = models.CharField(max_length=4, choices=Type.choices)
    name = models.CharField(max_length=150)  # tool key or model id
    input = models.JSONField(default=dict, blank=True)
    output = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run", "idx"]
        constraints = [models.UniqueConstraint(fields=["run", "idx"], name="unique_run_step_idx")]

    def __str__(self) -> str:
        return f"#{self.idx} {self.type} {self.name}"


class ActionProposal(ShopScopedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for approval"
        APPLIED = "APPLIED", "Applied"
        REJECTED = "REJECTED", "Rejected"
        FAILED = "FAILED", "Failed"
        UNDONE = "UNDONE", "Undone"

    # Public id: also used as the Shopify idempotency key for the change.
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="proposals")
    run = models.ForeignKey(
        Run, null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals"
    )
    tool = models.CharField(max_length=100)  # tool key, e.g. "products.update"
    agent_slug = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict)  # validated tool input
    preview = models.JSONField(default=dict, blank=True)  # what the merchant sees
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    auto_applied = models.BooleanField(default=False, help_text="Applied without approval (AUTO).")
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.tool} ({self.status})"


class ActionSnapshot(models.Model):
    proposal = models.OneToOneField(
        ActionProposal, on_delete=models.CASCADE, related_name="snapshot"
    )
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Snapshot for {self.proposal}"
