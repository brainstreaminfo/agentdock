"""
Agent Studio (staff only, spec §7). Phase 2 provides the Tool Catalog pages:
browse built-in tools, switch tools on/off, create custom GraphQL tools, and
test any tool against your own connected store. Agents arrive in Phase 4.
"""

from __future__ import annotations

import json
from itertools import groupby
from typing import Any

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.decorators import staff_required
from apps.runs.models import Run, RunStep
from apps.shopify_app.scopes import describe
from apps.tools.context import ToolContext
from apps.tools.executor import execute_tool
from apps.tools.models import ToolDefinition
from apps.tools.registry import registry
from apps.tools.sync import sync_tools

from .forms import CustomToolForm, ToolTestForm


@staff_required
def tool_list(request: HttpRequest) -> HttpResponse:
    if not ToolDefinition.objects.filter(kind=ToolDefinition.Kind.CODE).exists():
        sync_tools()  # first visit on a fresh database
    tools = list(ToolDefinition.objects.order_by("category", "key"))
    q = request.GET.get("q", "").strip().lower()
    if q:
        tools = [
            t for t in tools if q in t.key or q in t.name.lower() or q in t.description.lower()
        ]
    groups = [(cat, list(items)) for cat, items in groupby(tools, key=lambda t: t.category)]
    return render(request, "studio/tool_list.html", {"groups": groups, "q": q, "count": len(tools)})


def describe_inputs(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a JSON Schema into readable rows: name, type, required, notes."""
    defs = schema.get("$defs") or {}
    required = set(schema.get("required") or [])
    rows = []
    for name, prop in (schema.get("properties") or {}).items():
        options = prop.get("anyOf") or [prop]
        types = []
        for option in options:
            if "$ref" in option:
                ref = defs.get(option["$ref"].rsplit("/", 1)[-1], {})
                types.append(ref.get("title", "object"))
            elif option.get("type") == "array":
                item = option.get("items") or {}
                inner = item.get("type") or defs.get(
                    str(item.get("$ref", "")).rsplit("/", 1)[-1], {}
                ).get("title", "item")
                types.append(f"list of {inner}")
            elif option.get("type") != "null":
                types.append(option.get("type", "any"))
        notes = []
        for option in options:
            if "enum" in option:
                notes.append("one of: " + ", ".join(str(v) for v in option["enum"]))
            for k, label in (("minimum", "min"), ("maximum", "max"), ("maxLength", "max length")):
                if k in option:
                    notes.append(f"{label} {option[k]}")
        default = prop.get("default")
        if default not in (None, "", []) and name not in required:
            notes.append(f"default {default}")
        rows.append(
            {
                "name": name,
                "type": " or ".join(types) or "any",
                "required": name in required,
                "description": prop.get("description", ""),
                "notes": "; ".join(notes),
            }
        )
    return sorted(rows, key=lambda r: (not r["required"], r["name"]))


def _get_tool(key: str) -> ToolDefinition:
    tool = ToolDefinition.objects.filter(key=key).first()
    if tool is None and key in registry:
        sync_tools()  # built-in tool not synced yet (fresh database)
        tool = ToolDefinition.objects.filter(key=key).first()
    if tool is None:
        raise Http404("No such tool")
    return tool


@staff_required
def tool_detail(request: HttpRequest, key: str) -> HttpResponse:
    tool = _get_tool(key)
    test_form = ToolTestForm(user=request.user, mutating=tool.mutating)
    example = {}
    spec = registry.get(key)
    if spec is not None:
        example = {
            name: field.get("default", f"<{field.get('type', 'value')}>")
            for name, field in (tool.input_schema.get("properties") or {}).items()
            if name in (tool.input_schema.get("required") or [])
        }
    test_form.fields["arguments"].initial = json.dumps(example, indent=2)
    return render(
        request,
        "studio/tool_detail.html",
        {
            "tool": tool,
            "is_code": tool.kind == ToolDefinition.Kind.CODE,
            "scopes": [{"scope": s, "label": describe(s)} for s in tool.scopes],
            "schema_json": json.dumps(tool.input_schema, indent=2),
            "inputs": describe_inputs(tool.input_schema),
            "test_form": test_form,
        },
    )


@staff_required
@require_POST
def tool_toggle(request: HttpRequest, key: str) -> HttpResponse:
    tool = _get_tool(key)
    tool.enabled = not tool.enabled
    tool.save(update_fields=["enabled", "updated_at"])
    audit("studio.tool_toggled", user=request.user, tool=key, enabled=tool.enabled)
    messages.success(request, f"{tool.name} is now {'enabled' if tool.enabled else 'disabled'}.")
    return redirect("studio:tool_detail", key=key)


@staff_required
def tool_create(request: HttpRequest) -> HttpResponse:
    form = CustomToolForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        tool = form.save(commit=False)
        tool.created_by_id = request.user.pk
        tool.save()
        audit("studio.custom_tool_created", user=request.user, tool=tool.key)
        messages.success(request, f"Custom tool {tool.key} created. Test it below.")
        return redirect("studio:tool_detail", key=tool.key)
    return render(request, "studio/tool_form.html", {"form": form, "creating": True})


@staff_required
def tool_edit(request: HttpRequest, key: str) -> HttpResponse:
    tool = get_object_or_404(ToolDefinition, key=key, kind=ToolDefinition.Kind.GRAPHQL)
    form = CustomToolForm(request.POST or None, instance=tool)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit("studio.custom_tool_updated", user=request.user, tool=key)
        messages.success(request, "Saved.")
        return redirect("studio:tool_detail", key=key)
    return render(request, "studio/tool_form.html", {"form": form, "tool": tool})


@staff_required
@require_POST
def tool_test(request: HttpRequest, key: str) -> HttpResponse:
    """HTMX: run the tool once against the chosen store and show the result."""
    tool = _get_tool(key)
    form = ToolTestForm(request.POST, user=request.user, mutating=tool.mutating)
    if not form.is_valid():
        return render(request, "studio/_tool_test_result.html", {"form_errors": form.errors})

    shop = form.cleaned_data["shop"]
    run = Run.objects.create(
        shop=shop,
        trigger=Run.Trigger.TOOL_TEST,
        status=Run.Status.RUNNING,
        input={"tool": key, "arguments": form.cleaned_data["arguments"]},
        started_at=timezone.now(),
    )
    # The staff member ticked "I understand this changes data" -> apply directly.
    ctx = ToolContext(shop=shop, run=run, autonomy="AUTO", agent_slug="studio-test")
    result = execute_tool(ctx, key, form.cleaned_data["arguments"])
    run.status = Run.Status.SUCCEEDED if result.status != "error" else Run.Status.FAILED
    run.output = result.to_dict()
    run.error = result.error
    run.finished_at = timezone.now()
    run.save()
    audit("studio.tool_tested", shop=shop, user=request.user, tool=key, status=result.status)
    return render(
        request,
        "studio/_tool_test_result.html",
        {
            "result": result,
            "result_json": json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str),
            "step": RunStep.objects.filter(run=run).first(),
            "shop": shop,
        },
    )
