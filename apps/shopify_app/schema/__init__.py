"""
Shopify's Admin GraphQL schema (downloaded by `scripts/update_shopify_schema.py`).

Used to check GraphQL documents BEFORE they reach Shopify:
* tests validate every built-in tool's queries against it;
* the Studio validates custom GraphQL tools when staff save them.
"""

from __future__ import annotations

import gzip
from functools import cache
from pathlib import Path

from django.conf import settings
from graphql import GraphQLSchema, build_schema, parse, validate
from graphql.error import GraphQLSyntaxError

_DIR = Path(__file__).parent


@cache
def load_schema(version: str | None = None) -> GraphQLSchema | None:
    path = _DIR / f"admin-{version or settings.SHOPIFY_API_VERSION}.graphql.gz"
    if not path.exists():
        return None
    # assume_valid: Shopify's schema breaks a strict schema-consistency rule
    # (deprecated fields implementing non-deprecated interface fields). That
    # doesn't affect checking OUR queries against it.
    return build_schema(gzip.decompress(path.read_bytes()).decode(), assume_valid=True)


def validate_document(document: str, version: str | None = None) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    try:
        ast = parse(document)
    except GraphQLSyntaxError as exc:
        return [
            f"Syntax error: {exc.message} (line {exc.locations[0].line if exc.locations else '?'})"
        ]
    schema = load_schema(version)
    if schema is None:
        return []  # schema file missing for this version: only syntax was checked
    return [error.message for error in validate(schema, ast)]
