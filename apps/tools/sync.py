"""Copy built-in tools from the Python registry into the ToolDefinition table."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from .models import ToolDefinition
from .registry import registry


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # code tools no longer in the registry


@transaction.atomic
def sync_tools() -> SyncReport:
    report = SyncReport()
    for spec in registry.all():
        existing = ToolDefinition.objects.filter(key=spec.key).first()
        if existing is not None and existing.kind != ToolDefinition.Kind.CODE:
            raise ValueError(
                f"Custom tool '{spec.key}' has the same key as a built-in tool. Rename it."
            )
        values = {
            "name": spec.name,
            "description": spec.description,
            "category": spec.category,
            "kind": ToolDefinition.Kind.CODE,
            "scopes": spec.scopes,
            "mutating": spec.mutating,
            "input_schema": spec.input_schema(),
            "can_undo": spec.can_undo,
        }
        if existing is None:
            ToolDefinition.objects.create(key=spec.key, enabled=spec.enabled_by_default, **values)
            report.created.append(spec.key)
        else:
            # `enabled` is never overwritten: staff may have switched a tool off.
            changed = [k for k, v in values.items() if getattr(existing, k) != v]
            if changed:
                for k in changed:
                    setattr(existing, k, values[k])
                existing.save()
                report.updated.append(spec.key)

    stale = ToolDefinition.objects.filter(kind=ToolDefinition.Kind.CODE).exclude(
        key__in=registry.keys()
    )
    report.removed = list(stale.values_list("key", flat=True))
    stale.update(enabled=False)
    return report
