"""Studio forms: custom GraphQL tools and the tool "Test" form."""

from __future__ import annotations

import json
from typing import Any

from django import forms

from apps.shopify_app import scopes as scope_utils
from apps.shops.models import Shop
from apps.tools.custom import check_custom_tool
from apps.tools.models import ToolDefinition
from apps.tools.registry import registry

EXAMPLE_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {"first": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
        "required": ["first"],
    },
    indent=2,
)

EXAMPLE_QUERY = """query RecentProducts($first: Int!) {
  products(first: $first, sortKey: CREATED_AT, reverse: true) {
    nodes { id title status }
  }
}"""


class JSONField(forms.CharField):
    widget = forms.Textarea(attrs={"rows": 10, "class": "input font-mono text-xs"})

    def to_python(self, value: Any) -> Any:
        value = super().to_python(value)
        if not value:
            return {}
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Not valid JSON: {exc.msg} (line {exc.lineno})") from exc

    def prepare_value(self, value: Any) -> Any:
        return json.dumps(value, indent=2) if isinstance(value, dict | list) else value


class CustomToolForm(forms.ModelForm):
    scopes_text = forms.CharField(
        label="Required scopes",
        help_text="Comma-separated, e.g. read_products, write_products",
        widget=forms.TextInput(attrs={"class": "input font-mono"}),
    )
    input_schema = JSONField(
        label="Input (JSON Schema)",
        help_text="One property per GraphQL variable. The AI fills these in.",
        initial=EXAMPLE_SCHEMA,
    )

    class Meta:
        model = ToolDefinition
        labels = {"graphql_document": "GraphQL document"}
        fields = [
            "key",
            "name",
            "description",
            "category",
            "graphql_document",
            "input_schema",
            "output_path",
            "enabled",
        ]
        widgets = {
            "key": forms.TextInput(
                attrs={"class": "input font-mono", "placeholder": "products.recent"}
            ),
            "name": forms.TextInput(attrs={"class": "input"}),
            "category": forms.TextInput(attrs={"class": "input", "placeholder": "Products"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 3}),
            "graphql_document": forms.Textarea(
                attrs={"class": "input font-mono text-xs", "rows": 14, "placeholder": EXAMPLE_QUERY}
            ),
            "output_path": forms.TextInput(
                attrs={"class": "input font-mono", "placeholder": "products.nodes"}
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["key"].disabled = True  # agents refer to tools by key
            self.fields["scopes_text"].initial = ", ".join(self.instance.scopes)
        self.document_info: Any = None

    def clean_key(self) -> str:
        key = str(self.cleaned_data["key"]).strip()
        if not self.instance.pk and key in registry:
            raise forms.ValidationError("A built-in tool already uses this key.")
        return key

    def clean_scopes_text(self) -> list[str]:
        return scope_utils.parse(self.cleaned_data["scopes_text"])

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if self.errors:
            return data
        problems, info = check_custom_tool(
            key=data["key"],
            document=data["graphql_document"],
            input_schema=data["input_schema"],
            scopes=data["scopes_text"],
        )
        for problem in problems:
            self.add_error(None, problem)
        self.document_info = info
        return data

    def save(self, commit: bool = True) -> ToolDefinition:
        tool = super().save(commit=False)
        tool.kind = ToolDefinition.Kind.GRAPHQL
        tool.scopes = self.cleaned_data["scopes_text"]
        # Spec §6.2: any mutation is mutating and that can't be switched off.
        tool.mutating = bool(self.document_info and self.document_info.is_mutation)
        tool.can_undo = False
        if commit:
            tool.save()
        return tool


class ToolTestForm(forms.Form):
    shop = forms.ModelChoiceField(
        queryset=Shop.objects.none(),
        help_text="Only stores you're a member of (connect your dev store first).",
        widget=forms.Select(attrs={"class": "input"}),
    )
    arguments = JSONField(label="Input (JSON)", required=False, initial="{}")
    confirm_changes = forms.BooleanField(
        required=False,
        label="I understand this tool CHANGES data in the selected store",
    )

    def __init__(self, *args: Any, user: Any, mutating: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        shop_field = self.fields["shop"]
        assert isinstance(shop_field, forms.ModelChoiceField)
        shop_field.queryset = Shop.objects.installed().filter(memberships__user=user)
        if shop_field.queryset.count() == 1:
            shop_field.initial = shop_field.queryset.first()
        self.mutating = mutating
        if not mutating:
            del self.fields["confirm_changes"]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if self.mutating and not data.get("confirm_changes"):
            self.add_error("confirm_changes", "Tick this box to run a tool that changes data.")
        if not isinstance(data.get("arguments", {}), dict):
            self.add_error("arguments", 'Input must be a JSON object, e.g. {"limit": 5}.')
        return data
