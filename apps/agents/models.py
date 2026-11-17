"""
Agents (spec §7, §10).

* `AgentDefinition`   the agent's listing: name, description, visibility…
* `AgentVersion`      the agent's "brain": instructions, model, tools, settings
                      form, triggers. Each agent has ONE editable draft
                      (`is_draft=True`, `number=None`). Publishing copies the
                      draft into an immutable numbered version (v1, v2…).
* `AgentInstallation` a published agent installed in one shop, pinned to a version.
* `ShopMemory`        brand memory shared by all agents of a shop.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings
from django.db import models, transaction
from django.utils import timezone

from apps.core.models import ShopScopedModel, TimeStampedModel
from apps.shopify_app import scopes as scope_utils


class Autonomy(models.TextChoices):
    SUGGEST = "SUGGEST", "Suggest only"
    APPROVE = "APPROVE", "Ask before changing"
    AUTO = "AUTO", "Act automatically"


class Effort(models.TextChoices):
    """How hard the model thinks (replaces 'temperature', see DECISIONS.md)."""

    LOW = "low", "Low (fast, cheap)"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    XHIGH = "xhigh", "Extra high"
    MAX = "max", "Max (slow, expensive)"


def default_model() -> str:
    return str(django_settings.DEFAULT_MODEL)


def default_triggers() -> dict[str, Any]:
    return {"chat": True, "schedule_cron": "", "events": []}


def default_allowed_autonomy() -> list[str]:
    return [Autonomy.SUGGEST, Autonomy.APPROVE]


class AgentDefinition(TimeStampedModel):
    class Visibility(models.TextChoices):
        PRIVATE = "PRIVATE", "Private (selected shops)"
        BETA = "BETA", "Beta"
        PUBLIC = "PUBLIC", "Public"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        ARCHIVED = "ARCHIVED", "Archived"

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    icon = models.CharField(max_length=60, default="bot", help_text="Lucide icon name")
    category = models.CharField(max_length=60, blank=True)
    tagline = models.CharField(max_length=200, blank=True)
    description_md = models.TextField(blank=True)
    screenshots = models.JSONField(default=list, blank=True)
    capabilities = models.JSONField(default=list, blank=True, help_text='"What it can do" bullets')
    example_prompts = models.JSONField(default=list, blank=True)
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    allowed_shops = models.ManyToManyField("shops.Shop", blank=True, related_name="+")
    featured = models.BooleanField(default=False)
    plans = models.JSONField(default=list, blank=True)
    extra_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    auto_upgrade = models.BooleanField(
        default=True, help_text="Upgrade installs automatically when scopes don't change."
    )
    is_template = models.BooleanField(default=False, help_text="Example to clone; never published.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def draft(self) -> AgentVersion:
        draft = self.versions.filter(is_draft=True).first()
        if draft is None:
            draft = AgentVersion.objects.create(agent=self, is_draft=True)
        return draft

    @property
    def latest_version(self) -> AgentVersion | None:
        return self.versions.filter(is_draft=False).order_by("-number").first()


class AgentVersion(TimeStampedModel):
    agent = models.ForeignKey(AgentDefinition, on_delete=models.CASCADE, related_name="versions")
    is_draft = models.BooleanField(default=False)
    number = models.PositiveIntegerField(null=True, blank=True)  # None for the draft
    changelog = models.TextField(blank=True)

    # -- brain
    instructions_md = models.TextField(blank=True)
    model = models.CharField(max_length=60, default=default_model)
    effort = models.CharField(max_length=6, choices=Effort.choices, default=Effort.MEDIUM)
    max_steps = models.PositiveIntegerField(default=12, help_text="Max LLM turns per run.")
    max_tokens = models.PositiveIntegerField(
        default=60_000, help_text="Max output tokens per run (all turns together)."
    )

    # -- tools & permissions
    tools = models.ManyToManyField("tools.ToolDefinition", blank=True, related_name="versions")
    required_scopes = models.JSONField(default=list, blank=True)

    # -- merchant settings (JSON Schema form)
    settings_schema = models.JSONField(default=dict, blank=True)
    settings_defaults = models.JSONField(default=dict, blank=True)

    # -- triggers & behaviour
    triggers = models.JSONField(default=default_triggers, blank=True)
    default_autonomy = models.CharField(
        max_length=8, choices=Autonomy.choices, default=Autonomy.APPROVE
    )
    allowed_autonomy = models.JSONField(default=default_allowed_autonomy, blank=True)

    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        ordering = ["agent", "-is_draft", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "number"], name="unique_agent_version_number"),
            models.UniqueConstraint(
                fields=["agent"], condition=models.Q(is_draft=True), name="one_draft_per_agent"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.agent.slug} {'draft' if self.is_draft else f'v{self.number}'}"

    @property
    def label(self) -> str:
        return "draft" if self.is_draft else f"v{self.number}"

    @property
    def chat_enabled(self) -> bool:
        return bool((self.triggers or {}).get("chat", True))

    def compute_required_scopes(self) -> list[str]:
        """Union of the scopes of every selected tool (spec §7.1: calculated automatically)."""
        return scope_utils.union(*[t.scopes for t in self.tools.all()])

    @transaction.atomic
    def publish(self, *, user: Any = None, changelog: str = "") -> AgentVersion:
        """Copy this draft into a new immutable numbered version."""
        if not self.is_draft:
            raise ValueError("Only the draft can be published.")
        last = (
            AgentVersion.objects.select_for_update()
            .filter(agent=self.agent, is_draft=False)
            .aggregate(m=models.Max("number"))["m"]
        )
        version = AgentVersion.objects.create(
            agent=self.agent,
            is_draft=False,
            number=(last or 0) + 1,
            changelog=changelog,
            instructions_md=self.instructions_md,
            model=self.model,
            effort=self.effort,
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
            required_scopes=self.compute_required_scopes(),
            settings_schema=self.settings_schema,
            settings_defaults=self.settings_defaults,
            triggers=self.triggers,
            default_autonomy=self.default_autonomy,
            allowed_autonomy=self.allowed_autonomy,
            published_at=timezone.now(),
            published_by=user if getattr(user, "is_authenticated", False) else None,
        )
        version.tools.set(self.tools.all())
        AgentDefinition.objects.filter(pk=self.agent_id).update(
            status=AgentDefinition.Status.PUBLISHED
        )
        return version


class AgentInstallation(ShopScopedModel):
    """A published agent installed in a shop (install UI arrives in Phase 5)."""

    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="installations")
    agent = models.ForeignKey(AgentDefinition, on_delete=models.PROTECT, related_name="installs")
    version = models.ForeignKey(AgentVersion, on_delete=models.PROTECT, related_name="installs")
    settings = models.JSONField(default=dict, blank=True)
    autonomy = models.CharField(max_length=8, choices=Autonomy.choices, default=Autonomy.APPROVE)
    enabled = models.BooleanField(default=True)
    schedule_enabled = models.BooleanField(default=False)
    installed_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    installed_at = models.DateTimeField(auto_now_add=True)
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["agent__name"]
        constraints = [
            models.UniqueConstraint(fields=["shop", "agent"], name="one_install_per_shop_agent")
        ]

    def __str__(self) -> str:
        return f"{self.agent} @ {self.shop}"

    def effective_settings(self) -> dict[str, Any]:
        return {**(self.version.settings_defaults or {}), **(self.settings or {})}


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
