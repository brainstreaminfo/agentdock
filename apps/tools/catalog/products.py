"""Product tools: search, read, and edit products, SEO, image alt text and prices."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import PageInput, build_query, changed_fields, gid, load_document, money, nodes, page

DOC = load_document("products")

ProductStatus = Literal["ACTIVE", "DRAFT", "ARCHIVED"]


def _summary(p: dict[str, Any]) -> dict[str, Any]:
    price = p.get("priceRangeV2") or {}
    return {
        "id": p["id"],
        "title": p["title"],
        "handle": p.get("handle"),
        "status": p.get("status"),
        "vendor": p.get("vendor"),
        "product_type": p.get("productType"),
        "tags": p.get("tags", []),
        "total_inventory": p.get("totalInventory"),
        "min_price": money(price.get("minVariantPrice")),
        "max_price": money(price.get("maxVariantPrice")),
        "image_url": (((p.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get(
            "url"
        ),
        "updated_at": p.get("updatedAt"),
    }


# --------------------------------------------------------------------------- search
class SearchProductsInput(PageInput):
    query: str = Field("", description="Free text, e.g. 'linen shirt'.")
    status: ProductStatus | None = None
    vendor: str | None = None
    product_type: str | None = None
    tag: str | None = None
    collection_id: str | None = Field(None, description="Only products in this collection.")
    sort: Literal["RELEVANCE", "TITLE", "UPDATED_AT", "CREATED_AT", "INVENTORY_TOTAL"] = "RELEVANCE"


@tool(
    key="products.search",
    name="Search products",
    description="""Search products by text, status, collection, vendor or tag.
    Returns up to `limit` products with id, title, status, price range, inventory.
    Use `next_cursor` to get the next page.""",
    category="Products",
    scopes=["read_products"],
)
def search_products(ctx: ToolContext, args: SearchProductsInput) -> dict[str, Any]:
    collection = (
        gid("Collection", args.collection_id).rsplit("/", 1)[1] if args.collection_id else None
    )
    query = build_query(
        args.query,
        status=args.status.lower() if args.status else None,
        vendor=args.vendor,
        product_type=args.product_type,
        tag=args.tag,
    )
    if collection:
        query = (
            f"{query} AND collection_id:{collection}" if query else f"collection_id:{collection}"
        )
    data = ctx.gql(
        DOC,
        "SearchProducts",
        {
            "first": args.limit,
            "after": args.cursor,
            "query": query or None,
            "sortKey": args.sort,
            "reverse": args.sort in ("UPDATED_AT", "CREATED_AT"),
        },
    )
    result = page(data["products"], "products")
    result["products"] = [_summary(p) for p in result["products"]]
    return result


# --------------------------------------------------------------------------- get
class ProductIdInput(ToolInput):
    product_id: str = Field(description="Product id (gid://shopify/Product/123 or 123).")


@tool(
    key="products.get",
    name="Get product details",
    description="""Everything about one product: description (HTML), SEO title and
    description, options, variants (price, SKU, stock), images with alt text, and
    collections.""",
    category="Products",
    scopes=["read_products"],
)
def get_product(ctx: ToolContext, args: ProductIdInput) -> dict[str, Any]:
    p = ctx.gql(DOC, "GetProduct", {"id": gid("Product", args.product_id)})["product"]
    if p is None:
        raise ToolError(f"No product found with id {args.product_id}.")
    out = _summary(p)
    out.update(
        {
            "description_html": p.get("descriptionHtml", ""),
            "online_store_url": p.get("onlineStoreUrl"),
            "created_at": p.get("createdAt"),
            "seo": p.get("seo"),
            "options": p.get("options", []),
            "collections": nodes(p.get("collections")),
            "variants": [
                {
                    "id": v["id"],
                    "title": v["title"],
                    "sku": v.get("sku"),
                    "price": v.get("price"),
                    "compare_at_price": v.get("compareAtPrice"),
                    "inventory_quantity": v.get("inventoryQuantity"),
                    "inventory_item_id": (v.get("inventoryItem") or {}).get("id"),
                }
                for v in nodes(p.get("variants"))
            ],
            "images": [
                {"media_id": m["id"], "alt": m.get("alt"), "url": (m.get("image") or {}).get("url")}
                for m in nodes(p.get("media"))
                if m  # non-image media come back as {}
            ],
        }
    )
    return out


# --------------------------------------------------------------------------- update
class UpdateProductInput(ProductIdInput):
    title: str | None = Field(None, min_length=1, max_length=255)
    description_html: str | None = Field(None, description="Full new description (HTML).")
    tags: list[str] | None = Field(None, description="REPLACES all tags with this list.")
    vendor: str | None = None
    product_type: str | None = None
    status: ProductStatus | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> UpdateProductInput:
        if not changed_fields(self, _PRODUCT_FIELDS):
            raise ValueError("Provide at least one field to change.")
        return self


_PRODUCT_FIELDS = {
    "title": "title",
    "description_html": "descriptionHtml",
    "tags": "tags",
    "vendor": "vendor",
    "product_type": "productType",
    "status": "status",
}


def _editable(ctx: ToolContext, product_id: str) -> dict[str, Any]:
    p = ctx.gql(DOC, "ProductEditableFields", {"id": gid("Product", product_id)})["product"]
    if p is None:
        raise ToolError(f"No product found with id {product_id}.")
    return dict(p)


def _update(ctx: ToolContext, product: dict[str, Any]) -> dict[str, Any]:
    data = ctx.gql(DOC, "UpdateProduct", {"product": product}, user_errors_at="productUpdate")
    return dict(data["productUpdate"]["product"])


@tool(
    key="products.update",
    name="Update product",
    description="""Change a product's title, description (HTML), tags, vendor,
    product type or status. Only the fields you pass are changed. `tags` replaces
    the whole tag list. Needs the merchant's approval unless the agent runs in AUTO.""",
    category="Products",
    scopes=["write_products"],
    mutating=True,
)
def update_product(ctx: ToolContext, args: UpdateProductInput) -> dict[str, Any]:
    return _update(
        ctx, {"id": gid("Product", args.product_id), **changed_fields(args, _PRODUCT_FIELDS)}
    )


@update_product.preview
def _preview_update(ctx: ToolContext, args: UpdateProductInput) -> dict[str, Any]:
    current = _editable(ctx, args.product_id)
    new = changed_fields(args, _PRODUCT_FIELDS)
    return {
        "summary": f"Update product “{current['title']}”",
        "target": {"type": "product", "id": current["id"], "title": current["title"]},
        "changes": [
            {"field": field, "before": current.get(field), "after": value}
            for field, value in new.items()
        ],
    }


@update_product.snapshot
def _snapshot_update(ctx: ToolContext, args: UpdateProductInput) -> dict[str, Any]:
    return _editable(ctx, args.product_id)


@update_product.undo
def _undo_update(ctx: ToolContext, args: UpdateProductInput, before: dict, after: dict) -> Any:
    fields = changed_fields(args, _PRODUCT_FIELDS)
    return _update(ctx, {"id": before["id"], **{f: before.get(f) for f in fields}})


# --------------------------------------------------------------------------- SEO
class UpdateSeoInput(ProductIdInput):
    seo_title: str | None = Field(None, max_length=70, description="≤ 70 characters.")
    seo_description: str | None = Field(None, max_length=320, description="≤ 320 characters.")

    @model_validator(mode="after")
    def _something_to_change(self) -> UpdateSeoInput:
        if self.seo_title is None and self.seo_description is None:
            raise ValueError("Provide seo_title and/or seo_description.")
        return self


def _seo_input(args: UpdateSeoInput) -> dict[str, str]:
    seo = {}
    if args.seo_title is not None:
        seo["title"] = args.seo_title
    if args.seo_description is not None:
        seo["description"] = args.seo_description
    return seo


@tool(
    key="products.update_seo",
    name="Update product SEO",
    description="""Set the search-engine title (≤ 70 chars) and/or meta description
    (≤ 320 chars) of a product.""",
    category="Products",
    scopes=["write_products"],
    mutating=True,
)
def update_seo(ctx: ToolContext, args: UpdateSeoInput) -> dict[str, Any]:
    return _update(ctx, {"id": gid("Product", args.product_id), "seo": _seo_input(args)})


@update_seo.preview
def _preview_seo(ctx: ToolContext, args: UpdateSeoInput) -> dict[str, Any]:
    current = _editable(ctx, args.product_id)
    seo = current.get("seo") or {}
    return {
        "summary": f"Update SEO for “{current['title']}”",
        "target": {"type": "product", "id": current["id"], "title": current["title"]},
        "changes": [
            {"field": f"seo.{k}", "before": seo.get(k), "after": v}
            for k, v in _seo_input(args).items()
        ],
    }


@update_seo.snapshot
def _snapshot_seo(ctx: ToolContext, args: UpdateSeoInput) -> dict[str, Any]:
    return _editable(ctx, args.product_id)


@update_seo.undo
def _undo_seo(ctx: ToolContext, args: UpdateSeoInput, before: dict, after: dict) -> Any:
    old = before.get("seo") or {}
    return _update(
        ctx, {"id": before["id"], "seo": {k: old.get(k) or "" for k in _seo_input(args)}}
    )


# --------------------------------------------------------------------------- image alt
class ImageAlt(ToolInput):
    media_id: str = Field(description="MediaImage id from products.get → images[].media_id")
    alt: str = Field(max_length=512)


class UpdateImagesAltInput(ToolInput):
    images: list[ImageAlt] = Field(min_length=1, max_length=50)


@tool(
    key="products.update_images_alt",
    name="Update image alt text",
    description="""Set the alt text (accessibility + SEO) of product images. Get the
    media ids from products.get → images[].media_id.""",
    category="Products",
    scopes=["write_products", "write_files"],
    mutating=True,
)
def update_images_alt(ctx: ToolContext, args: UpdateImagesAltInput) -> dict[str, Any]:
    files = [{"id": gid("MediaImage", i.media_id), "alt": i.alt} for i in args.images]
    data = ctx.gql(DOC, "UpdateFilesAlt", {"files": files}, user_errors_at="fileUpdate")
    return {"images": data["fileUpdate"]["files"]}


def _current_alts(ctx: ToolContext, args: UpdateImagesAltInput) -> dict[str, Any]:
    ids = [gid("MediaImage", i.media_id) for i in args.images]
    found = [n for n in ctx.gql(DOC, "MediaAlts", {"ids": ids})["nodes"] if n]
    if len(found) != len(ids):
        raise ToolError("Some media ids were not found (or are not images).")
    return {n["id"]: n.get("alt") for n in found}


@update_images_alt.preview
def _preview_alts(ctx: ToolContext, args: UpdateImagesAltInput) -> dict[str, Any]:
    current = _current_alts(ctx, args)
    return {
        "summary": f"Update alt text of {len(args.images)} image(s)",
        "changes": [
            {
                "field": "alt",
                "target": gid("MediaImage", i.media_id),
                "before": current.get(gid("MediaImage", i.media_id)),
                "after": i.alt,
            }
            for i in args.images
        ],
    }


@update_images_alt.snapshot
def _snapshot_alts(ctx: ToolContext, args: UpdateImagesAltInput) -> dict[str, Any]:
    return {"alts": _current_alts(ctx, args)}


@update_images_alt.undo
def _undo_alts(ctx: ToolContext, args: UpdateImagesAltInput, before: dict, after: dict) -> Any:
    files = [{"id": media_id, "alt": alt or ""} for media_id, alt in before["alts"].items()]
    return ctx.gql(DOC, "UpdateFilesAlt", {"files": files}, user_errors_at="fileUpdate")


# --------------------------------------------------------------------------- prices
class VariantPrice(ToolInput):
    variant_id: str
    price: str = Field(description="New price as a decimal string, e.g. '19.99'.")
    compare_at_price: str | None = Field(
        None, description="'Was' price shown crossed out. Empty string removes it."
    )

    @field_validator("price")
    @classmethod
    def _positive(cls, value: str) -> str:
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("price must be a number like 19.99") from exc
        if amount < 0:
            raise ValueError("price can't be negative")
        return str(amount)


class UpdatePricesInput(ToolInput):
    product_id: str
    variants: list[VariantPrice] = Field(min_length=1, max_length=100)


@tool(
    key="variants.update_price",
    name="Update variant prices",
    description="""Change the price (and optionally the compare-at price) of one or
    more variants of ONE product. Get variant ids from products.get.""",
    category="Products",
    scopes=["write_products"],
    mutating=True,
)
def update_prices(ctx: ToolContext, args: UpdatePricesInput) -> dict[str, Any]:
    variants = []
    for v in args.variants:
        item: dict[str, Any] = {"id": gid("ProductVariant", v.variant_id), "price": v.price}
        if v.compare_at_price is not None:
            item["compareAtPrice"] = v.compare_at_price or None
        variants.append(item)
    return _bulk_update_prices(ctx, args.product_id, variants)


def _bulk_update_prices(
    ctx: ToolContext, product_id: str, variants: list[dict[str, Any]]
) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "UpdateVariantPrices",
        {"productId": gid("Product", product_id), "variants": variants},
        user_errors_at="productVariantsBulkUpdate",
    )
    return {"variants": data["productVariantsBulkUpdate"]["productVariants"]}


def _current_prices(ctx: ToolContext, args: UpdatePricesInput) -> dict[str, dict[str, Any]]:
    ids = [gid("ProductVariant", v.variant_id) for v in args.variants]
    found = [n for n in ctx.gql(DOC, "VariantPrices", {"ids": ids})["nodes"] if n]
    product = gid("Product", args.product_id)
    if len(found) != len(ids) or any(n["product"]["id"] != product for n in found):
        raise ToolError("Every variant must exist and belong to the given product.")
    return {n["id"]: n for n in found}


@update_prices.preview
def _preview_prices(ctx: ToolContext, args: UpdatePricesInput) -> dict[str, Any]:
    current = _current_prices(ctx, args)
    changes = []
    for v in args.variants:
        old = current[gid("ProductVariant", v.variant_id)]
        changes.append(
            {"field": "price", "target": old["title"], "before": old["price"], "after": v.price}
        )
        if v.compare_at_price is not None:
            changes.append(
                {
                    "field": "compare_at_price",
                    "target": old["title"],
                    "before": old.get("compareAtPrice"),
                    "after": v.compare_at_price or None,
                }
            )
    return {"summary": f"Change prices of {len(args.variants)} variant(s)", "changes": changes}


@update_prices.snapshot
def _snapshot_prices(ctx: ToolContext, args: UpdatePricesInput) -> dict[str, Any]:
    return {
        "variants": [
            {"id": n["id"], "price": n["price"], "compareAtPrice": n.get("compareAtPrice")}
            for n in _current_prices(ctx, args).values()
        ]
    }


@update_prices.undo
def _undo_prices(ctx: ToolContext, args: UpdatePricesInput, before: dict, after: dict) -> Any:
    return _bulk_update_prices(ctx, args.product_id, before["variants"])
