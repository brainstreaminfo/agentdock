"""
Turns a tool key into something the executor can run, whether the tool is
built-in Python code (registry) or a custom GraphQL tool made in the Studio
(ToolDefinition row). Both expose the same methods.
"""

from __future__ import annotations

from typing import Any, Protocol

from .context import ToolContext
from .models import ToolDefinition
from .registry import ToolSpec, registry


class ResolvedTool(Protocol):
    key: str
    name: str
    scopes: list[str]
    mutating: bool

    @property
    def can_undo(self) -> bool: ...
    def validate(self, raw: dict[str, Any]) -> Any: ...
    def payload(self, args: Any) -> dict[str, Any]: ...
    def run(self, ctx: ToolContext, args: Any) -> Any: ...
    def snapshot(self, ctx: ToolContext, args: Any) -> dict[str, Any]: ...
    def undo(self, ctx: ToolContext, args: Any, before: dict, after: dict) -> Any: ...
    def preview(self, ctx: ToolContext, args: Any) -> dict[str, Any]: ...


class CodeTool:
    """Adapter around a registered Python tool (ToolSpec)."""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec
        self.key = spec.key
        self.name = spec.name
        self.scopes = spec.scopes
        self.mutating = spec.mutating

    @property
    def can_undo(self) -> bool:
        return self.spec.can_undo

    def validate(self, raw: dict[str, Any]) -> Any:
        return self.spec.input_model.model_validate(raw)

    def payload(self, args: Any) -> dict[str, Any]:
        return dict(args.model_dump(mode="json", exclude_none=True))

    def run(self, ctx: ToolContext, args: Any) -> Any:
        result = self.spec.func(ctx, args)
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    def snapshot(self, ctx: ToolContext, args: Any) -> dict[str, Any]:
        return self.spec.snapshot_fn(ctx, args) if self.spec.snapshot_fn else {}

    def undo(self, ctx: ToolContext, args: Any, before: dict, after: dict) -> Any:
        assert self.spec.undo_fn is not None
        return self.spec.undo_fn(ctx, args, before, after)

    def preview(self, ctx: ToolContext, args: Any) -> dict[str, Any]:
        if self.spec.preview_fn:
            return dict(self.spec.preview_fn(ctx, args))
        return {"tool": self.name, "changes": self.payload(args)}


def resolve_tool(key: str) -> ResolvedTool | None:
    """Return the tool for `key`, or None if it doesn't exist or is switched off."""
    definition = ToolDefinition.objects.filter(key=key).first()
    spec = registry.get(key)

    if spec is not None:
        enabled = definition.enabled if definition is not None else spec.enabled_by_default
        return CodeTool(spec) if enabled else None

    if definition is not None and definition.kind == ToolDefinition.Kind.GRAPHQL:
        if not definition.enabled:
            return None
        from .custom import CustomGraphQLTool

        return CustomGraphQLTool(definition)
    return None
