"""
Shopify webhook topics an agent can be triggered by (Studio → Triggers).

Each topic needs a read scope to subscribe to it; that scope is added to the
agent's required scopes automatically. Phase 5 registers these per shop.
"""

from __future__ import annotations

AGENT_EVENT_TOPICS: dict[str, dict[str, str]] = {
    "orders/create": {"label": "Order created", "scope": "read_orders"},
    "orders/paid": {"label": "Order paid", "scope": "read_orders"},
    "orders/fulfilled": {"label": "Order fulfilled", "scope": "read_orders"},
    "orders/cancelled": {"label": "Order cancelled", "scope": "read_orders"},
    "refunds/create": {"label": "Refund created", "scope": "read_orders"},
    "products/create": {"label": "Product created", "scope": "read_products"},
    "products/update": {"label": "Product updated", "scope": "read_products"},
    "inventory_levels/update": {"label": "Inventory level changed", "scope": "read_inventory"},
    "customers/create": {"label": "Customer created", "scope": "read_customers"},
    "customers/update": {"label": "Customer updated", "scope": "read_customers"},
    "collections/create": {"label": "Collection created", "scope": "read_products"},
    "discounts/create": {"label": "Discount created", "scope": "read_discounts"},
}

# Noisy topics are debounced by the trigger router (Phase 5): seconds per resource.
DEBOUNCE_SECONDS = {"products/update": 60, "inventory_levels/update": 60, "customers/update": 60}


def scopes_for_topics(topics: list[str]) -> list[str]:
    return sorted({AGENT_EVENT_TOPICS[t]["scope"] for t in topics if t in AGENT_EVENT_TOPICS})
