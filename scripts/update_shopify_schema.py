"""
Download Shopify's Admin GraphQL schema for SHOPIFY_API_VERSION and save it as
compressed SDL in apps/shopify_app/schema/. Tests validate every .graphql
document against it, and the Studio validates custom tools.

    python scripts/update_shopify_schema.py 2026-10

Uses the public proxy behind shopify.dev's GraphiQL explorer (no token needed).
"""

import gzip
import sys
from pathlib import Path

import httpx
from graphql import build_client_schema, get_introspection_query, print_schema

version = sys.argv[1] if len(sys.argv) > 1 else "2026-10"
url = f"https://shopify.dev/admin-graphql-direct-proxy/{version}"
resp = httpx.post(url, json={"query": get_introspection_query(descriptions=False)}, timeout=120)
resp.raise_for_status()
payload = resp.json()
if "errors" in payload:
    raise SystemExit(f"Introspection failed: {payload['errors']}")

sdl = print_schema(build_client_schema(payload["data"]))
out = (
    Path(__file__).resolve().parent.parent
    / "apps/shopify_app/schema"
    / f"admin-{version}.graphql.gz"
)
out.write_bytes(gzip.compress(sdl.encode(), mtime=0))
print(f"Saved {out} ({len(sdl) // 1024} KB SDL, {out.stat().st_size // 1024} KB gzipped)")
