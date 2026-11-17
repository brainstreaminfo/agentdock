"""
The tool registry and the `@tool` decorator.

A built-in tool is a Python function plus metadata:

    class SearchProductsInput(ToolInput):
        query: str = ""

    @tool(
        key="products.search",
        name="Search products",
        description="Search products by text, status, collection, vendor or tag.",
        scopes=["read_products"],
        mutating=False,
        category="Products",
    )
    def search_products(ctx: ToolContext, args: SearchProductsInput) -> dict: ...

The input class is read from the function's type hint. Mutating tools register
extra functions on the returned spec:

    @update_product.snapshot   # (ctx, args) -> dict: store data BEFORE the change
    @update_product.undo       # (ctx, args, before, after) -> dict: put it back
    @update_product.preview    # (ctx, args) -> dict: what the merchant approves
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_type_hints

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .context import ToolContext

ToolFunc = Callable[..., Any]


class ToolInput(BaseModel):
    """Base class for tool inputs. Unknown fields are rejected (helps catch AI typos)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoInput(ToolInput):
    pass


@dataclass
class ToolSpec:
    key: str
    name: str
    description: str
    category: str
    scopes: list[str]
    mutating: bool
    func: ToolFunc
    input_model: type[ToolInput]
    enabled_by_default: bool = True
    snapshot_fn: ToolFunc | None = None
    undo_fn: ToolFunc | None = None
    preview_fn: ToolFunc | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- decorators for mutating tools
    def snapshot(self, fn: ToolFunc) -> ToolFunc:
        self.snapshot_fn = fn
        return fn

    def undo(self, fn: ToolFunc) -> ToolFunc:
        self.undo_fn = fn
        return fn

    def preview(self, fn: ToolFunc) -> ToolFunc:
        self.preview_fn = fn
        return fn

    # -- helpers
    @property
    def can_undo(self) -> bool:
        return self.undo_fn is not None

    def input_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def __call__(self, ctx: ToolContext, args: ToolInput) -> Any:
        return self.func(ctx, args)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.key in self._tools and self._tools[spec.key].func is not spec.func:
            raise ValueError(f"Duplicate tool key: {spec.key}")
        self._tools[spec.key] = spec
        return spec

    def get(self, key: str) -> ToolSpec | None:
        return self._tools.get(key)

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: (t.category, t.key))

    def keys(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, key: str) -> bool:
        return key in self._tools

    def __len__(self) -> int:
        return len(self._tools)


registry = ToolRegistry()


def tool(
    *,
    key: str,
    name: str,
    description: str,
    category: str,
    scopes: list[str] | None = None,
    mutating: bool = False,
    enabled_by_default: bool = True,
) -> Callable[[ToolFunc], ToolSpec]:
    """Register a function as a tool. See the module docstring for an example."""

    def decorator(func: ToolFunc) -> ToolSpec:
        hints = get_type_hints(func)
        params = list(inspect.signature(func).parameters)
        input_model = hints.get(params[1]) if len(params) > 1 else NoInput
        if not (isinstance(input_model, type) and issubclass(input_model, ToolInput)):
            raise TypeError(f"{key}: second argument must be typed with a ToolInput subclass")
        spec = ToolSpec(
            key=key,
            name=name,
            description=inspect.cleandoc(description),
            category=category,
            scopes=sorted(scopes or []),
            mutating=mutating,
            func=func,
            input_model=input_model,
            enabled_by_default=enabled_by_default,
        )
        return registry.register(spec)

    return decorator
