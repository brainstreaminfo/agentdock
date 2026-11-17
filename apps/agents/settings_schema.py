"""
Merchant settings for an agent (spec §7.1 "Merchant settings").

Staff build a form in the Studio as a list of FIELDS:

    {"key": "tone", "label": "Tone", "type": "select", "options": ["friendly", "formal"],
     "default": "friendly", "required": true, "help": "How the agent writes"}

`fields_to_schema()` turns them into JSON Schema (stored on AgentVersion), and
`schema_to_fields()` turns a schema back into fields for editing. `build_form()`
renders a schema as a Django form (playground now, merchant install page in
Phase 5) and `validate_settings()` checks values against it.

Field types: text, textarea, number, select, multiselect, toggle, product,
collection. Product/collection pickers store lists of Shopify ids.

Postgres JSON doesn't keep key order, so the field order is stored in "x-order".
"""

from __future__ import annotations

import json
import re
from typing import Any

from django import forms
from django.utils.safestring import SafeString, mark_safe
from jsonschema import Draft202012Validator

FIELD_TYPES = {
    "text": "Short text",
    "textarea": "Long text",
    "number": "Number",
    "select": "Choose one",
    "multiselect": "Choose several",
    "toggle": "On / off",
    "product": "Product picker",
    "collection": "Collection picker",
}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PICKERS = {"product": "product-picker", "collection": "collection-picker"}


# --------------------------------------------------------------------------- fields -> schema
def validate_fields(fields: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for i, f in enumerate(fields, start=1):
        key, kind = str(f.get("key", "")), f.get("type")
        where = f"Field {i} ({key or 'no key'})"
        if not KEY_RE.match(key):
            errors.append(f"{where}: key must be lowercase letters, digits and _ (e.g. tone).")
        if key in seen:
            errors.append(f"{where}: duplicate key.")
        seen.add(key)
        if not str(f.get("label", "")).strip():
            errors.append(f"{where}: needs a label.")
        if kind not in FIELD_TYPES:
            errors.append(f"{where}: unknown type {kind!r}.")
            continue
        options = [str(o) for o in f.get("options") or [] if str(o).strip()]
        if kind in ("select", "multiselect") and not options:
            errors.append(f"{where}: add at least one option.")
        default = f.get("default")
        if default in (None, "", []):
            continue
        if kind == "number":
            try:
                float(default)
            except (TypeError, ValueError):
                errors.append(f"{where}: default must be a number.")
        elif kind == "select" and str(default) not in options:
            errors.append(f"{where}: default must be one of the options.")
        elif kind == "multiselect" and not set(_as_list(default)) <= set(options):
            errors.append(f"{where}: defaults must be among the options.")
    return errors


def fields_to_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in fields:
        kind = f["type"]
        prop: dict[str, Any] = {"title": str(f["label"]).strip()}
        if f.get("help"):
            prop["description"] = str(f["help"]).strip()
        options = [str(o).strip() for o in f.get("options") or [] if str(o).strip()]
        if kind == "text":
            prop.update(type="string", maxLength=500)
        elif kind == "textarea":
            prop.update(type="string", maxLength=5000, **{"x-widget": "textarea"})
        elif kind == "number":
            prop["type"] = "integer" if f.get("integer") else "number"
            if f.get("min") not in (None, ""):
                prop["minimum"] = _number(f["min"])
            if f.get("max") not in (None, ""):
                prop["maximum"] = _number(f["max"])
        elif kind == "select":
            prop.update(type="string", enum=options)
        elif kind == "multiselect":
            prop.update(type="array", items={"type": "string", "enum": options}, uniqueItems=True)
        elif kind == "toggle":
            prop["type"] = "boolean"
        elif kind in PICKERS:
            prop.update(type="array", items={"type": "string"}, **{"x-widget": PICKERS[kind]})
        default = _typed_default(kind, f.get("default"), integer=bool(f.get("integer")))
        if default is not None:
            prop["default"] = default
        properties[f["key"]] = prop
        if f.get("required"):
            required.append(f["key"])
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "x-order": list(properties),
    }
    if required:
        schema["required"] = required
    return schema


def defaults_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        key: prop["default"]
        for key, prop in (schema.get("properties") or {}).items()
        if "default" in prop
    }


# --------------------------------------------------------------------------- schema -> fields
def ordered_properties(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    props = schema.get("properties") or {}
    order = [k for k in schema.get("x-order") or [] if k in props]
    order += [k for k in props if k not in order]
    return [(k, props[k]) for k in order]


def schema_to_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    required = set(schema.get("required") or [])
    fields = []
    for key, prop in ordered_properties(schema):
        widget = prop.get("x-widget")
        kind = prop.get("type")
        field: dict[str, Any] = {
            "key": key,
            "label": prop.get("title", key),
            "help": prop.get("description", ""),
            "required": key in required,
            "options": [],
            "default": prop.get("default", ""),
            "min": prop.get("minimum", ""),
            "max": prop.get("maximum", ""),
            "integer": kind == "integer",
        }
        if widget == "textarea":
            field["type"] = "textarea"
        elif widget in PICKERS.values():
            field["type"] = next(k for k, v in PICKERS.items() if v == widget)
        elif kind in ("number", "integer"):
            field["type"] = "number"
        elif kind == "boolean":
            field["type"] = "toggle"
        elif kind == "array":
            field["type"] = "multiselect"
            field["options"] = list((prop.get("items") or {}).get("enum") or [])
        elif "enum" in prop:
            field["type"] = "select"
            field["options"] = list(prop["enum"])
        else:
            field["type"] = "text"
        fields.append(field)
    return fields


# --------------------------------------------------------------------------- rendering & validation
def build_form(
    schema: dict[str, Any],
    data: Any = None,
    initial: dict | None = None,
    *,
    picker_urls: dict[str, str] | None = None,
) -> forms.Form:
    """
    A Django form for the schema. `cleaned_data` is the settings dict.

    `picker_urls` ({"product": url, "collection": url}) turns product/collection
    pickers into search boxes (merchant pages). Without it they are plain
    comma-separated id boxes (Studio).
    """
    required = set(schema.get("required") or [])
    form_fields: dict[str, forms.Field] = {}
    for key, prop in ordered_properties(schema):
        common = {
            "label": prop.get("title", key),
            "help_text": prop.get("description", ""),
            "required": key in required,
        }
        widget = prop.get("x-widget")
        kind = prop.get("type")
        field: forms.Field
        if widget in PICKERS.values():
            noun = "product" if widget == "product-picker" else "collection"
            picker_widget: forms.Widget
            if picker_urls and noun in picker_urls:
                picker_widget = PickerWidget(noun=noun, search_url=picker_urls[noun])
            else:
                picker_widget = forms.TextInput(
                    attrs={"class": "input", "placeholder": f"Shopify {noun} ids, comma-separated"}
                )
            field = PickerField(**common, widget=picker_widget)
        elif widget == "textarea":
            field = forms.CharField(
                **common,
                max_length=prop.get("maxLength"),
                widget=forms.Textarea(attrs={"class": "input", "rows": 3}),
            )
        elif kind == "integer":
            field = forms.IntegerField(
                **common,
                min_value=prop.get("minimum"),
                max_value=prop.get("maximum"),
                widget=forms.NumberInput(attrs={"class": "input"}),
            )
        elif kind == "number":
            field = forms.FloatField(
                **common,
                min_value=prop.get("minimum"),
                max_value=prop.get("maximum"),
                widget=forms.NumberInput(attrs={"class": "input", "step": "any"}),
            )
        elif kind == "boolean":
            field = forms.BooleanField(**{**common, "required": False})
        elif kind == "array":
            choices = [(o, o) for o in (prop.get("items") or {}).get("enum") or []]
            field = forms.MultipleChoiceField(
                **common, choices=choices, widget=forms.CheckboxSelectMultiple
            )
        elif "enum" in prop:
            field = forms.ChoiceField(
                **common,
                choices=[(o, o) for o in prop["enum"]],
                widget=forms.Select(attrs={"class": "input"}),
            )
        else:
            field = forms.CharField(
                **common,
                max_length=prop.get("maxLength"),
                widget=forms.TextInput(attrs={"class": "input"}),
            )
        form_fields[key] = field
    form_class = type("AgentSettingsForm", (forms.Form,), form_fields)
    values = {**defaults_from_schema(schema), **(initial or {})}
    return form_class(data=data, initial=values)


class PickerWidget(forms.Widget):
    """
    Search-and-pick box for products / collections (templates/components/picker.html).
    Submits the picked ids comma-separated, like the plain text box.
    """

    def __init__(self, *, noun: str, search_url: str) -> None:
        super().__init__()
        self.noun = noun
        self.search_url = search_url

    def format_value(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value]
        return [v.strip() for v in str(value or "").split(",") if v.strip()]

    def render(self, name: str, value: Any, attrs: Any = None, renderer: Any = None) -> SafeString:
        from django.template.loader import render_to_string

        ids = self.format_value(value)
        return mark_safe(  # noqa: S308 (rendered by our own escaped template)
            render_to_string(
                "components/picker.html",
                {
                    "name": name,
                    "noun": self.noun,
                    "search_url": self.search_url,
                    # Titles of already-picked items aren't stored: show the id.
                    "initial": json.dumps([{"id": i, "title": i.rsplit("/", 1)[-1]} for i in ids]),
                },
            )
        )


class PickerField(forms.CharField):
    """Comma-separated Shopify ids; cleaned to a list."""

    def to_python(self, value: Any) -> list[str]:  # type: ignore[override]
        if isinstance(value, list):
            return [str(v) for v in value]
        return [v.strip() for v in str(value or "").split(",") if v.strip()]

    def prepare_value(self, value: Any) -> Any:
        return ", ".join(value) if isinstance(value, list) else value


def validate_settings(schema: dict[str, Any], values: dict[str, Any]) -> list[str]:
    clean_schema = {k: v for k, v in schema.items() if not k.startswith("x-")}
    validator = Draft202012Validator(clean_schema or {"type": "object"})
    return [
        f"{'.'.join(str(p) for p in e.path) or 'settings'}: {e.message}"
        for e in validator.iter_errors(values)
    ]


# --------------------------------------------------------------------------- helpers
def _number(value: Any) -> float | int:
    number = float(value)
    return int(number) if number.is_integer() else number


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _typed_default(kind: str, value: Any, *, integer: bool) -> Any:
    if value is None or value == "" or value == []:
        return None
    if kind == "number":
        number = _number(value)
        return int(number) if integer else number
    if kind == "toggle":
        return value in (True, "true", "True", "on", "1", 1)
    if kind in ("multiselect", "product", "collection"):
        return _as_list(value)
    return str(value)
