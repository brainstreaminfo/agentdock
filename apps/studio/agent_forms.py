"""Forms for the Agent Studio builder (spec §7.1), one per section."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from celery.schedules import crontab
from django import forms
from django.conf import settings
from django.utils.text import slugify

from apps.agents.models import (
    AgentDefinition,
    AgentTestCase,
    AgentVersion,
    Autonomy,
    Effort,
    OutputType,
)
from apps.agents.settings_schema import (
    defaults_from_schema,
    fields_to_schema,
    validate_fields,
)
from apps.billing.plans import PLAN_CHOICES
from apps.shopify_app.topics import AGENT_EVENT_TOPICS
from apps.shops.models import Shop
from apps.tools.models import ToolDefinition


class StyledFormMixin:
    """Adds our CSS classes to every widget, so templates can render forms generically."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():  # type: ignore[attr-defined]
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput | forms.CheckboxSelectMultiple):
                continue
            css = widget.attrs.get("class", "")
            widget.attrs["class"] = f"input {css}".strip()


def set_choices(field: forms.Field, choices: Any) -> None:
    assert isinstance(field, forms.ChoiceField)
    field.choices = choices


def lines(value: str) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


# --------------------------------------------------------------------------- create
class NewAgentForm(StyledFormMixin, forms.Form):
    name = forms.CharField(max_length=120)
    slug = forms.SlugField(max_length=80, required=False, help_text="Leave empty to generate.")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        slug = data.get("slug") or slugify(data.get("name", ""))
        if not slug:
            raise forms.ValidationError("Give the agent a name.")
        if AgentDefinition.objects.filter(slug=slug).exists():
            self.add_error("slug", "An agent with this slug already exists.")
        data["slug"] = slug
        return data


# --------------------------------------------------------------------------- listing
class ListingForm(StyledFormMixin, forms.ModelForm):
    capabilities_text = forms.CharField(
        label='"What it can do"',
        required=False,
        help_text="One bullet per line.",
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    example_prompts_text = forms.CharField(
        label="Example prompts",
        required=False,
        help_text="One per line. Shown as buttons in chat.",
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    screenshot = forms.ImageField(required=False, help_text="Add a screenshot (PNG/JPG, max 3 MB).")

    class Meta:
        model = AgentDefinition
        fields = ["name", "icon", "category", "tagline", "description_md"]
        labels = {"description_md": "Long description (Markdown)", "icon": "Icon (Lucide name)"}
        widgets = {"description_md": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["capabilities_text"].initial = "\n".join(self.instance.capabilities or [])
        self.fields["example_prompts_text"].initial = "\n".join(self.instance.example_prompts or [])

    def clean_screenshot(self) -> Any:
        image = self.cleaned_data.get("screenshot")
        if image and image.size > 3 * 1024 * 1024:
            raise forms.ValidationError("Max 3 MB.")
        return image

    def save(self, commit: bool = True) -> AgentDefinition:
        agent = super().save(commit=False)
        agent.capabilities = lines(self.cleaned_data["capabilities_text"])
        agent.example_prompts = lines(self.cleaned_data["example_prompts_text"])
        if commit:
            agent.save()
        return agent


# --------------------------------------------------------------------------- brain
class BrainForm(StyledFormMixin, forms.ModelForm):
    model = forms.ChoiceField(choices=[])
    max_steps = forms.IntegerField(min_value=1, max_value=50, help_text="Model turns per run.")
    max_tokens = forms.IntegerField(
        min_value=2_000, max_value=1_000_000, help_text="Output tokens per run, all turns together."
    )

    class Meta:
        model = AgentVersion
        fields = ["instructions_md", "model", "effort", "max_steps", "max_tokens"]
        labels = {"instructions_md": "System instructions", "effort": "Thinking effort"}
        widgets = {
            "instructions_md": forms.Textarea(attrs={"rows": 16, "class": "font-mono text-xs"})
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        models_ = list(settings.ALLOWED_MODELS)
        if self.instance.model and self.instance.model not in models_:
            models_.append(self.instance.model)
        set_choices(self.fields["model"], [(m, m) for m in models_])
        set_choices(self.fields["effort"], Effort.choices)


# --------------------------------------------------------------------------- tools
class ToolsForm(forms.Form):
    tools = forms.ModelMultipleChoiceField(
        queryset=ToolDefinition.objects.filter(enabled=True), required=False
    )


# --------------------------------------------------------------------------- merchant settings
class SettingsBuilderForm(forms.Form):
    fields_json = forms.CharField(widget=forms.HiddenInput)

    def clean_fields_json(self) -> list[dict[str, Any]]:
        try:
            fields = json.loads(self.cleaned_data["fields_json"] or "[]")
        except json.JSONDecodeError as exc:
            raise forms.ValidationError("Invalid form data.") from exc
        if not isinstance(fields, list):
            raise forms.ValidationError("Invalid form data.")
        errors = validate_fields(fields)
        if errors:
            raise forms.ValidationError(errors)
        return fields

    def schema_and_defaults(self) -> tuple[dict[str, Any], dict[str, Any]]:
        schema = fields_to_schema(self.cleaned_data["fields_json"])
        return schema, defaults_from_schema(schema)


# --------------------------------------------------------------------------- triggers
CRON_PRESETS = [
    ("", "No schedule"),
    ("0 8 * * *", "Every day at 08:00"),
    ("0 * * * *", "Every hour"),
    ("0 8 * * 1", "Every Monday at 08:00"),
    ("0 8 1 * *", "First day of the month at 08:00"),
    ("custom", "Custom (cron)"),
]


def validate_cron(value: str) -> str:
    parts = value.split()
    if len(parts) != 5:
        raise forms.ValidationError("A cron expression has 5 parts: minute hour day month weekday.")
    try:
        crontab(*parts)
    except Exception as exc:  # celery raises ValueError subclasses with clear text
        raise forms.ValidationError(f"Invalid cron: {exc}") from exc
    return " ".join(parts)


class TriggersForm(forms.Form):
    chat = forms.BooleanField(required=False, label="Merchants can chat with this agent")
    schedule = forms.ChoiceField(
        choices=CRON_PRESETS,
        required=False,
        label="Schedule",
        widget=forms.Select(attrs={"class": "input"}),
    )
    custom_cron = forms.CharField(
        required=False,
        label="Custom cron",
        help_text="minute hour day month weekday, in the shop's timezone. E.g. 30 7 * * 1-5",
        widget=forms.TextInput(attrs={"class": "input font-mono"}),
    )
    events = forms.MultipleChoiceField(
        choices=[(k, f"{v['label']} ({k})") for k, v in AGENT_EVENT_TOPICS.items()],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Shopify events",
    )

    @classmethod
    def from_triggers(cls, triggers: dict[str, Any]) -> TriggersForm:
        cron = triggers.get("schedule_cron") or ""
        preset = cron if cron in dict(CRON_PRESETS) else ("custom" if cron else "")
        return cls(
            initial={
                "chat": triggers.get("chat", True),
                "schedule": preset,
                "custom_cron": cron if preset == "custom" else "",
                "events": triggers.get("events") or [],
            }
        )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        schedule = data.get("schedule") or ""
        if schedule == "custom":
            data["schedule_cron"] = validate_cron(data.get("custom_cron") or "")
        else:
            data["schedule_cron"] = schedule
        return data

    def triggers(self) -> dict[str, Any]:
        return {
            "chat": bool(self.cleaned_data.get("chat")),
            "schedule_cron": self.cleaned_data["schedule_cron"],
            "events": list(self.cleaned_data.get("events") or []),
        }


# --------------------------------------------------------------------------- behaviour
class BehaviourForm(forms.Form):
    default_autonomy = forms.ChoiceField(
        choices=Autonomy.choices, widget=forms.Select(attrs={"class": "input"})
    )
    allowed_autonomy = forms.MultipleChoiceField(
        choices=Autonomy.choices, widget=forms.CheckboxSelectMultiple, label="Merchants may choose"
    )
    output_types = forms.MultipleChoiceField(
        choices=OutputType.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="What it produces",
    )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("default_autonomy") not in (data.get("allowed_autonomy") or []):
            self.add_error("default_autonomy", "The default must be one of the allowed levels.")
        return data


# --------------------------------------------------------------------------- availability
class AvailabilityForm(StyledFormMixin, forms.ModelForm):
    plans = forms.MultipleChoiceField(
        choices=PLAN_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Plans that include this agent.",
    )
    allowed_shops = forms.ModelMultipleChoiceField(
        queryset=Shop.objects.installed(),
        required=False,
        help_text="Only for 'Private': shops that may see it.",
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )
    extra_price = forms.DecimalField(
        min_value=Decimal(0), max_digits=8, decimal_places=2, label="Extra price per month (USD)"
    )

    class Meta:
        model = AgentDefinition
        fields = ["visibility", "allowed_shops", "featured", "plans", "extra_price", "auto_upgrade"]


# --------------------------------------------------------------------------- tests & publish
class TestCaseForm(StyledFormMixin, forms.ModelForm):
    must_call = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    must_not_call = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    reply_contains = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="One text per line (case-insensitive).",
    )
    reply_not_contains = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), help_text="One text per line."
    )
    max_cost_usd = forms.DecimalField(
        required=False, min_value=Decimal(0), decimal_places=4, label="Max cost (USD)"
    )
    settings_json = forms.CharField(
        required=False,
        label="Merchant settings for this test (JSON)",
        widget=forms.Textarea(attrs={"rows": 2, "class": "font-mono text-xs"}),
    )

    class Meta:
        model = AgentTestCase
        fields = ["name", "prompt"]
        widgets = {"prompt": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args: Any, tool_keys: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        choices = [(k, k) for k in tool_keys]
        set_choices(self.fields["must_call"], choices)
        set_choices(self.fields["must_not_call"], choices)
        checks = self.instance.checks or {}
        self.fields["must_call"].initial = checks.get("must_call", [])
        self.fields["must_not_call"].initial = checks.get("must_not_call", [])
        self.fields["reply_contains"].initial = "\n".join(checks.get("reply_contains", []))
        self.fields["reply_not_contains"].initial = "\n".join(checks.get("reply_not_contains", []))
        self.fields["max_cost_usd"].initial = checks.get("max_cost_usd")
        self.fields["settings_json"].initial = (
            json.dumps(self.instance.settings) if self.instance.settings else ""
        )

    def clean_settings_json(self) -> dict[str, Any]:
        raw = self.cleaned_data.get("settings_json") or ""
        if not raw.strip():
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Not valid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise forms.ValidationError('Must be a JSON object, e.g. {"tone": "formal"}.')
        return value

    def save(self, commit: bool = True) -> AgentTestCase:
        case = super().save(commit=False)
        d = self.cleaned_data
        checks: dict[str, Any] = {
            "must_call": list(d.get("must_call") or []),
            "must_not_call": list(d.get("must_not_call") or []),
            "reply_contains": lines(d.get("reply_contains") or ""),
            "reply_not_contains": lines(d.get("reply_not_contains") or ""),
        }
        if d.get("max_cost_usd") is not None:
            checks["max_cost_usd"] = str(d["max_cost_usd"])
        case.checks = {k: v for k, v in checks.items() if v}
        case.settings = d.get("settings_json") or {}
        if commit:
            case.save()
        return case


class ShopChoiceForm(forms.Form):
    shop = forms.ModelChoiceField(
        queryset=Shop.objects.none(),
        widget=forms.Select(attrs={"class": "input"}),
        help_text="Only stores you're a member of.",
    )

    def __init__(self, *args: Any, user: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["shop"]
        assert isinstance(field, forms.ModelChoiceField)
        field.queryset = Shop.objects.installed().filter(memberships__user=user)
        if field.queryset.count() == 1:
            field.initial = field.queryset.first()


class PublishForm(StyledFormMixin, forms.Form):
    changelog = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What changed? Shown to staff (and later to merchants).",
    )
