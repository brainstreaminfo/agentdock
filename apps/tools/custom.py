"""
Custom GraphQL tools (spec §6.2): created by staff in the Studio, no code.

`check_custom_tool(...)` validates a tool before it is saved:
* the document has exactly ONE query or mutation, and it is valid against
  Shopify's real schema for our API version;
* `mutating` is forced on for any mutation (it can't be switched off);
* the input JSON Schema is valid and matches the operation's variables;
* at least one scope is declared, and each one is a real Shopify scope.

`CustomGraphQLTool` runs a saved tool through the same executor as built-in
tools (scopes, approval, logging…). It refuses to run a mutation that isn't
marked mutating, even if someone edited the database by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from graphql import OperationDefinitionNode, OperationType, parse
from graphql.error import GraphQLSyntaxError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from apps.shopify_app import scopes as scope_utils
from apps.shopify_app.schema import validate_document

from .context import ToolContext
from .errors import ToolError
from .models import ToolDefinition

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


# --------------------------------------------------------------------------- analysis
@dataclass
class DocumentInfo:
    operation_type: str = ""  # "query" | "mutation"
    operation_name: str = ""
    variables: dict[str, bool] = field(default_factory=dict)  # name -> required?
    errors: list[str] = field(default_factory=list)

    @property
    def is_mutation(self) -> bool:
        return self.operation_type == "mutation"


def analyze_document(document: str) -> DocumentInfo:
    info = DocumentInfo()
    try:
        ast = parse(document)
    except GraphQLSyntaxError as exc:
        info.errors.append(f"Syntax error: {exc.message}")
        return info
    operations = [d for d in ast.definitions if isinstance(d, OperationDefinitionNode)]
    if len(operations) != 1:
        info.errors.append("The document must contain exactly one query or mutation.")
        return info
    op = operations[0]
    if op.operation == OperationType.SUBSCRIPTION:
        info.errors.append("Subscriptions are not supported.")
        return info
    info.operation_type = op.operation.value
    info.operation_name = op.name.value if op.name else ""
    for var in op.variable_definitions or []:
        required = type(var.type).__name__ == "NonNullTypeNode" and var.default_value is None
        info.variables[var.variable.name.value] = required
    info.errors.extend(validate_document(document))
    return info


def check_custom_tool(
    *, key: str, document: str, input_schema: dict[str, Any], scopes: list[str]
) -> tuple[list[str], DocumentInfo]:
    """Return (problems, document info). An empty problem list means it can be saved."""
    problems: list[str] = []
    if not KEY_RE.match(key):
        problems.append("Key must look like 'category.action' (lowercase, dots, underscores).")
    info = analyze_document(document)
    problems.extend(info.errors)

    try:
        Draft202012Validator.check_schema(input_schema)
    except SchemaError as exc:
        problems.append(f"Input schema is not valid JSON Schema: {exc.message}")
    else:
        if input_schema.get("type") != "object":
            problems.append('Input schema must have "type": "object".')
        props = set((input_schema.get("properties") or {}).keys())
        required = set(input_schema.get("required") or [])
        if not info.errors:
            for name, is_required in info.variables.items():
                if name not in props:
                    problems.append(f"Variable ${name} is missing from the input schema.")
                elif is_required and name not in required:
                    problems.append(
                        f'Variable ${name} is required in GraphQL; add it to "required".'
                    )
            for extra in sorted(props - set(info.variables)):
                problems.append(f"Input '{extra}' is not a variable of the GraphQL operation.")

    if not scopes:
        problems.append("Declare at least one Shopify scope this tool needs.")
    for scope in scopes:
        if scope not in scope_utils.KNOWN_SCOPES:
            problems.append(f"Unknown Shopify scope: {scope}")
    if info.is_mutation and not any(s.startswith("write_") for s in scopes):
        problems.append("A mutation needs at least one write_* scope.")
    return problems, info


# --------------------------------------------------------------------------- output path
_SEGMENT_RE = re.compile(r"^([^\[\]]+)?((?:\[(?:\*|\d+)\])*)$")


def apply_output_path(data: Any, path: str) -> Any:
    """
    Trim a response with a simple path, e.g. `data.products.edges[*].node`.
    `[*]` maps over a list, `[0]` picks one item. A leading `data.` is optional.
    """
    path = path.strip()
    if not path:
        return data
    if path.startswith("data."):
        path = path[5:]
    elif path == "data":
        return data

    def walk(value: Any, segments: list[str]) -> Any:
        if not segments:
            return value
        seg, rest = segments[0], segments[1:]
        match = _SEGMENT_RE.match(seg)
        if not match:
            raise ToolError(f"Invalid output path segment: {seg}")
        name, brackets = match.group(1), match.group(2)
        if name:
            value = value.get(name) if isinstance(value, dict) else None
        for index in re.findall(r"\[(\*|\d+)\]", brackets):
            if not isinstance(value, list):
                return None
            if index == "*":
                return [walk(item, rest) for item in value]
            value = value[int(index)] if int(index) < len(value) else None
        return walk(value, rest)

    return walk(data, path.split("."))


# --------------------------------------------------------------------------- runtime
class CustomGraphQLTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition
        self.key = definition.key
        self.name = definition.name
        self.scopes = list(definition.scopes or [])
        self.mutating = definition.mutating

    @property
    def can_undo(self) -> bool:
        return False  # we can't know how to reverse an arbitrary mutation

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        validator = Draft202012Validator(self.definition.input_schema or {"type": "object"})
        errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
        if errors:
            raise ToolError(
                "Invalid input: "
                + "; ".join(
                    f"{'.'.join(str(p) for p in e.path) or 'input'}: {e.message}" for e in errors
                )
            )
        return dict(raw)

    def payload(self, args: dict[str, Any]) -> dict[str, Any]:
        return dict(args)

    def run(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        info = analyze_document(self.definition.graphql_document)
        if info.errors:
            raise ToolError("This custom tool's GraphQL is invalid: " + "; ".join(info.errors))
        if info.is_mutation and not self.definition.mutating:
            raise ToolError("Refused: this tool runs a mutation but is not marked as mutating.")
        data = ctx.gql(self.definition.graphql_document, info.operation_name or None, args)
        return apply_output_path(data, self.definition.output_path)

    def snapshot(self, ctx: ToolContext, args: Any) -> dict[str, Any]:
        return {}

    def undo(self, ctx: ToolContext, args: Any, before: dict, after: dict) -> Any:
        raise ToolError("Custom tools can't be undone automatically.")

    def preview(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"tool": self.name, "description": self.definition.description, "inputs": args}
