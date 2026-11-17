"""
The built-in tool catalog. Importing this package registers every tool.
Add a new domain: create `<domain>.py` (+ `<domain>.graphql`) and import it here.
"""

from . import (  # noqa: F401
    analytics,
    collections,
    content,
    customers,
    discounts,
    files,
    inventory,
    metafields,
    orders,
    platform,
    products,
    shop,
)
