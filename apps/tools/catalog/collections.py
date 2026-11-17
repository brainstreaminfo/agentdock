"""Collection tools: list, read, add products, create."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from pydantic import Field

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import PageInput, gid, load_document, nodes, page

DOC = load_document("collections")


def source_title() -> str:
    """The collection source that holds products added by this app."""
    return f"Added by {settings.APP_NAME}"


class ListCollectionsInput(PageInput):
    query: str = Field("", description="Filter by title text.")


@tool(
    key="collections.list",
    name="List collections",
    description="List collections (id, title, handle, number of products).",
    category="Collections",
    scopes=["read_products"],
)
def list_collections(ctx: ToolContext, args: ListCollectionsInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "ListCollections",
        {"first": args.limit, "after": args.cursor, "query": args.query or None},
    )
    result = page(data["collections"], "collections")
    for c in result["collections"]:
        c["products_count"] = (c.pop("productsCount") or {}).get("count")
    return result


class GetCollectionInput(ToolInput):
    collection_id: str
    products_limit: int = Field(50, ge=0, le=100)


@tool(
    key="collections.get",
    name="Get collection",
    description="One collection with its description, SEO and (first) products.",
    category="Collections",
    scopes=["read_products"],
)
def get_collection(ctx: ToolContext, args: GetCollectionInput) -> dict[str, Any]:
    c = ctx.gql(
        DOC,
        "GetCollection",
        {"id": gid("Collection", args.collection_id), "productsFirst": args.products_limit},
    )["collection"]
    if c is None:
        raise ToolError(f"No collection found with id {args.collection_id}.")
    c["products_count"] = (c.pop("productsCount") or {}).get("count")
    c["products"] = nodes(c.get("products"))
    return dict(c)


# --------------------------------------------------------------------------- add products
class AddProductsInput(ToolInput):
    collection_id: str
    product_ids: list[str] = Field(min_length=1, max_length=50)


def _membership(ctx: ToolContext, args: AddProductsInput) -> dict[str, Any]:
    product_ids = [gid("Product", p) for p in args.product_ids]
    data = ctx.gql(
        DOC,
        "CollectionMembership",
        {"id": gid("Collection", args.collection_id), "productIds": product_ids},
    )
    collection = data["collection"]
    if collection is None:
        raise ToolError(f"No collection found with id {args.collection_id}.")
    products = [n for n in data["nodes"] if n]
    if len(products) != len(product_ids):
        raise ToolError("Some product ids were not found.")
    source = next((s for s in collection["sources"] if s["title"] == source_title()), None)
    return {
        "collection": {"id": collection["id"], "title": collection["title"]},
        "source_id": source["id"] if source else None,
        "already_in": [p["id"] for p in products if p["inCollection"]],
        "to_add": [p["id"] for p in products if not p["inCollection"]],
        "titles": {p["id"]: p["title"] for p in products},
    }


def _change_selections(
    ctx: ToolContext, collection_id: str, source_id: str | None, add: list[str], remove: list[str]
) -> dict[str, Any]:
    update: dict[str, Any] = {"id": collection_id}
    if source_id is None:
        update["sourcesToCreate"] = [
            {
                "source": {
                    "title": source_title(),
                    "inclusion": {"selections": [{"productId": p} for p in add]},
                }
            }
        ]
    else:
        inclusion: dict[str, Any] = {}
        if add:
            inclusion["selectionsToAdd"] = [{"productId": p} for p in add]
        if remove:
            inclusion["selectionsToRemove"] = [{"productId": p} for p in remove]
        update["sourcesToUpdate"] = [{"condition": {"id": source_id, "inclusion": inclusion}}]
    data = ctx.gql(
        DOC, "CollectionAddSource", {"collection": update}, user_errors_at="collectionUpdate"
    )
    payload = data["collectionUpdate"]
    return {
        "collection": payload["collection"],
        # Big changes can be processed in the background by Shopify.
        "processing_in_background": bool(payload.get("job") and not payload["job"]["done"]),
    }


@tool(
    key="collections.add_products",
    name="Add products to collection",
    description="""Add products to a collection. Products already in it are skipped.
    Large changes may take a few seconds to show in Shopify.""",
    category="Collections",
    scopes=["write_products"],
    mutating=True,
)
def add_products(ctx: ToolContext, args: AddProductsInput) -> dict[str, Any]:
    m = _membership(ctx, args)
    if not m["to_add"]:
        return {"added": [], "message": "All products were already in the collection."}
    result = _change_selections(ctx, m["collection"]["id"], m["source_id"], m["to_add"], [])
    return {**result, "added": m["to_add"], "skipped_already_in": m["already_in"]}


@add_products.preview
def _preview_add(ctx: ToolContext, args: AddProductsInput) -> dict[str, Any]:
    m = _membership(ctx, args)
    return {
        "summary": f"Add {len(m['to_add'])} product(s) to “{m['collection']['title']}”",
        "target": {"type": "collection", **m["collection"]},
        "add": [{"id": p, "title": m["titles"][p]} for p in m["to_add"]],
        "skipped_already_in": [{"id": p, "title": m["titles"][p]} for p in m["already_in"]],
    }


@add_products.snapshot
def _snapshot_add(ctx: ToolContext, args: AddProductsInput) -> dict[str, Any]:
    m = _membership(ctx, args)
    return {"collection_id": m["collection"]["id"], "to_add": m["to_add"]}


@add_products.undo
def _undo_add(ctx: ToolContext, args: AddProductsInput, before: dict, after: dict) -> Any:
    added = after.get("added") or []
    if not added:
        return {"removed": []}
    m = _membership(ctx, args)
    if m["source_id"] is None:
        raise ToolError("The app's product list on this collection no longer exists.")
    _change_selections(ctx, before["collection_id"], m["source_id"], [], added)
    return {"removed": added}


# --------------------------------------------------------------------------- create
class CreateCollectionInput(ToolInput):
    title: str = Field(min_length=1, max_length=255)
    description_html: str = ""
    product_ids: list[str] = Field(default_factory=list, max_length=50)
    seo_title: str | None = Field(None, max_length=70)
    seo_description: str | None = Field(None, max_length=320)


@tool(
    key="collections.create",
    name="Create collection",
    description="""Create a new collection, optionally with products in it. It is
    created unpublished from sales channels; the merchant publishes it.""",
    category="Collections",
    scopes=["write_products"],
    mutating=True,
)
def create_collection(ctx: ToolContext, args: CreateCollectionInput) -> dict[str, Any]:
    collection: dict[str, Any] = {"title": args.title, "descriptionHtml": args.description_html}
    if args.seo_title or args.seo_description:
        collection["seo"] = {"title": args.seo_title, "description": args.seo_description}
    if args.product_ids:
        collection["sources"] = [
            {
                "source": {
                    "title": source_title(),
                    "inclusion": {
                        "selections": [{"productId": gid("Product", p)} for p in args.product_ids]
                    },
                }
            }
        ]
    data = ctx.gql(
        DOC, "CreateCollection", {"collection": collection}, user_errors_at="collectionCreate"
    )
    return {"collection": data["collectionCreate"]["collection"]}


@create_collection.preview
def _preview_create(ctx: ToolContext, args: CreateCollectionInput) -> dict[str, Any]:
    return {
        "summary": f"Create collection “{args.title}” with {len(args.product_ids)} product(s)",
        "changes": [
            {"field": "title", "before": None, "after": args.title},
            {"field": "description_html", "before": None, "after": args.description_html},
        ],
        "products": args.product_ids,
    }


@create_collection.undo
def _undo_create(ctx: ToolContext, args: CreateCollectionInput, before: dict, after: dict) -> Any:
    collection_id = after["collection"]["id"]
    return ctx.gql(
        DOC, "DeleteCollection", {"input": {"id": collection_id}}, user_errors_at="collectionDelete"
    )
