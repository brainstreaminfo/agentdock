"""
Chat conversations.

`Message` rows are the exact transcript sent to Claude, in order: user text,
assistant content blocks (text, thinking, tool_use…), and user tool_result
blocks. They are APPEND-ONLY: never edit or delete rows of a live conversation,
because Claude checks that earlier turns (and their thinking blocks) are
unchanged when it continues a conversation.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models

from apps.core.models import ShopScopedModel


class Conversation(ShopScopedModel):
    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="conversations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    # null = "My Team" (orchestrator, Phase 5)
    installation = models.ForeignKey(
        "agents.AgentInstallation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    title = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title or f"Conversation {self.pk}"


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    run = models.ForeignKey(
        "runs.Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    role = models.CharField(max_length=9, choices=Role.choices)
    # Exactly what goes into the API: a string, or a list of content blocks.
    content = models.JSONField()
    agent_slug = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["conversation", "id"]

    def __str__(self) -> str:
        return f"{self.role} message {self.pk}"

    def as_api_message(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}
