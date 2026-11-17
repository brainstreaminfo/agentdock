"""
Every Shopify tool, through the executor, against the schema-checking fake.
Mutating tools are applied (AUTO) and then undone.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.runs.models import ActionProposal
from apps.tools.executor import execute_tool, undo_proposal

pytestmark = pytest.mark.django_db
NO_MORE = {"hasNextPage": False, "endCursor": None}


def ok(ctx, key: str, args: dict[str, Any] | None = None) -> Any:
    result = execute_tool(ctx, key, args or {})
    assert result.ok, result.error
    return result


def undo(result) -> ActionProposal:
    return undo_proposal(ActionProposal.objects.get(uuid=result.proposal_id))


# =========================================================================== shop
def test_shop_info(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "ShopInfo",
        {
            "shop": {
                "name": "Tea Co",
                "myshopifyDomain": "tea.myshopify.com",
                "primaryDomain": {"url": "https://tea.com"},
                "currencyCode": "GBP",
                "ianaTimezone": "Europe/London",
                "plan": {"publicDisplayName": "Basic", "partnerDevelopment": False},
                "billingAddress": {"countryCodeV2": "GB"},
            }
        },
    )
    data = ok(ctx, "shop.info").data
    assert (
        data["currency"] == "GBP"
        and data["country"] == "GB"
        and data["domain"] == "https://tea.com"
    )


# =========================================================================== products
def test_products_search_builds_query(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "SearchProducts",
        {
            "products": {
                "nodes": [
                    {
                        "id": "gid://shopify/Product/1",
                        "title": "Linen shirt",
                        "status": "ACTIVE",
                        "tags": [],
                        "priceRangeV2": {
                            "minVariantPrice": {"amount": "10.0", "currencyCode": "GBP"},
                            "maxVariantPrice": {"amount": "12.0", "currencyCode": "GBP"},
                        },
                    }
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        },
    )
    data = ok(
        ctx,
        "products.search",
        {
            "query": "shirt",
            "status": "ACTIVE",
            "vendor": 'Acme "UK"',
            "collection_id": "55",
            "limit": 5,
        },
    ).data
    variables = fake_shopify.calls_to("SearchProducts")[0]
    assert (
        variables["query"]
        == 'shirt AND status:"active" AND vendor:"Acme \\"UK\\"" AND collection_id:55'
    )
    assert variables["first"] == 5
    assert data["next_cursor"] == "c1"
    assert data["products"][0]["min_price"] == {"amount": "10.0", "currency": "GBP"}


def test_products_get_not_found(ctx, fake_shopify) -> None:
    fake_shopify.on("GetProduct", {"product": None})
    assert "No product" in execute_tool(ctx, "products.get", {"product_id": "9"}).error


def test_wrong_id_kind_is_explained(ctx, fake_shopify) -> None:
    result = execute_tool(ctx, "products.get", {"product_id": "gid://shopify/ProductVariant/1"})
    assert "Expected a Product id" in result.error


def test_products_get_flattens(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "GetProduct",
        {
            "product": {
                "id": "gid://shopify/Product/1",
                "title": "Shirt",
                "descriptionHtml": "<p>x</p>",
                "variants": {
                    "nodes": [
                        {
                            "id": "gid://shopify/ProductVariant/7",
                            "title": "S",
                            "price": "10.00",
                            "inventoryItem": {"id": "gid://shopify/InventoryItem/70"},
                        }
                    ]
                },
                "media": {
                    "nodes": [
                        {"id": "gid://shopify/MediaImage/3", "alt": "", "image": {"url": "u"}},
                        {},
                    ]
                },
                "collections": {"nodes": []},
            }
        },
    )
    data = ok(ctx, "products.get", {"product_id": "1"}).data
    assert data["variants"][0]["inventory_item_id"] == "gid://shopify/InventoryItem/70"
    assert data["images"] == [{"media_id": "gid://shopify/MediaImage/3", "alt": "", "url": "u"}]


def test_update_seo_and_undo(ctx, fake_shopify) -> None:
    state = {
        "id": "gid://shopify/Product/1",
        "title": "Shirt",
        "seo": {"title": "Old", "description": None},
    }

    def update(v):
        state["seo"] = {**state["seo"], **v["product"]["seo"]}
        return {"productUpdate": {"product": dict(state), "userErrors": []}}

    fake_shopify.on("ProductEditableFields", lambda v: {"product": dict(state)})
    fake_shopify.on("UpdateProduct", update)
    result = ok(ctx, "products.update_seo", {"product_id": "1", "seo_title": "New SEO"})
    assert state["seo"]["title"] == "New SEO"
    undo(result)
    assert state["seo"]["title"] == "Old"


def test_update_images_alt_and_undo(ctx, fake_shopify) -> None:
    alts = {"gid://shopify/MediaImage/3": "old alt"}

    def update(v):
        for f in v["files"]:
            alts[f["id"]] = f["alt"]
        return {
            "fileUpdate": {
                "files": [{"id": i, "alt": a} for i, a in alts.items()],
                "userErrors": [],
            }
        }

    fake_shopify.on("MediaAlts", lambda v: {"nodes": [{"id": i, "alt": alts[i]} for i in v["ids"]]})
    fake_shopify.on("UpdateFilesAlt", update)
    result = ok(
        ctx,
        "products.update_images_alt",
        {"images": [{"media_id": "3", "alt": "Blue linen shirt"}]},
    )
    assert alts["gid://shopify/MediaImage/3"] == "Blue linen shirt"
    undo(result)
    assert alts["gid://shopify/MediaImage/3"] == "old alt"


def test_variant_prices_and_undo(ctx, fake_shopify) -> None:
    prices = {"gid://shopify/ProductVariant/7": {"price": "10.00", "compareAtPrice": None}}

    def update(v):
        for item in v["variants"]:
            prices[item["id"]].update(
                {k: item[k] for k in ("price", "compareAtPrice") if k in item}
            )
        return {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}

    fake_shopify.on(
        "VariantPrices",
        lambda v: {
            "nodes": [
                {"id": i, "title": "S", "product": {"id": "gid://shopify/Product/1"}, **prices[i]}
                for i in v["ids"]
            ]
        },
    )
    fake_shopify.on("UpdateVariantPrices", update)
    result = ok(
        ctx,
        "variants.update_price",
        {
            "product_id": "1",
            "variants": [{"variant_id": "7", "price": "12.50", "compare_at_price": "15.00"}],
        },
    )
    assert prices["gid://shopify/ProductVariant/7"] == {"price": "12.50", "compareAtPrice": "15.00"}
    undo(result)
    assert prices["gid://shopify/ProductVariant/7"] == {"price": "10.00", "compareAtPrice": None}


def test_variant_from_other_product_rejected(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "VariantPrices",
        {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/7",
                    "title": "S",
                    "price": "1",
                    "product": {"id": "gid://shopify/Product/2"},
                }
            ]
        },
    )
    result = execute_tool(
        ctx,
        "variants.update_price",
        {"product_id": "1", "variants": [{"variant_id": "7", "price": "5"}]},
    )
    assert "belong to the given product" in result.error


def test_negative_price_rejected(ctx) -> None:
    result = execute_tool(
        ctx,
        "variants.update_price",
        {"product_id": "1", "variants": [{"variant_id": "7", "price": "-1"}]},
    )
    assert "negative" in result.error


# =========================================================================== collections
def _membership(source_id=None, in_collection=()):
    def answer(v):
        return {
            "collection": {
                "id": v["id"],
                "title": "Summer",
                "sources": [{"id": source_id, "title": "Added by AgentHub"}] if source_id else [],
            },
            "nodes": [
                {"id": p, "title": f"P{p[-1]}", "inCollection": p in in_collection}
                for p in v["productIds"]
            ],
        }

    return answer


def test_add_products_creates_app_source(ctx, fake_shopify) -> None:
    fake_shopify.on("CollectionMembership", _membership(in_collection={"gid://shopify/Product/2"}))
    fake_shopify.on(
        "CollectionAddSource",
        {
            "collectionUpdate": {
                "collection": {"id": "c", "title": "Summer"},
                "job": None,
                "userErrors": [],
            }
        },
    )
    data = ok(
        ctx, "collections.add_products", {"collection_id": "9", "product_ids": ["1", "2"]}
    ).data
    sent = fake_shopify.calls_to("CollectionAddSource")[0]["collection"]
    assert sent["sourcesToCreate"][0]["source"] == {
        "title": "Added by AgentHub",
        "inclusion": {"selections": [{"productId": "gid://shopify/Product/1"}]},
    }
    assert data["added"] == ["gid://shopify/Product/1"]
    assert data["skipped_already_in"] == ["gid://shopify/Product/2"]


def test_add_products_reuses_source_and_undo_removes_only_added(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "CollectionMembership", _membership(source_id="gid://shopify/CollectionConditionsSource/5")
    )
    fake_shopify.on(
        "CollectionAddSource",
        {
            "collectionUpdate": {
                "collection": {"id": "c", "title": "S"},
                "job": {"id": "j", "done": False},
                "userErrors": [],
            }
        },
    )
    result = ok(ctx, "collections.add_products", {"collection_id": "9", "product_ids": ["1"]})
    assert result.data["processing_in_background"] is True
    undo(result)
    add_call, remove_call = fake_shopify.calls_to("CollectionAddSource")
    assert add_call["collection"]["sourcesToUpdate"][0]["condition"]["inclusion"] == {
        "selectionsToAdd": [{"productId": "gid://shopify/Product/1"}]
    }
    assert remove_call["collection"]["sourcesToUpdate"][0]["condition"]["inclusion"] == {
        "selectionsToRemove": [{"productId": "gid://shopify/Product/1"}]
    }


def test_create_collection_and_undo_deletes(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "CreateCollection",
        {
            "collectionCreate": {
                "collection": {
                    "id": "gid://shopify/Collection/77",
                    "title": "Gifts",
                    "handle": "gifts",
                },
                "userErrors": [],
            }
        },
    )
    fake_shopify.on(
        "DeleteCollection", {"collectionDelete": {"deletedCollectionId": "x", "userErrors": []}}
    )
    result = ok(ctx, "collections.create", {"title": "Gifts", "product_ids": ["1"]})
    undo(result)
    assert fake_shopify.calls_to("DeleteCollection") == [
        {"input": {"id": "gid://shopify/Collection/77"}}
    ]


def test_collections_list(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "ListCollections",
        {
            "collections": {
                "nodes": [{"id": "c", "title": "Summer", "productsCount": {"count": 4}}],
                "pageInfo": NO_MORE,
            }
        },
    )
    assert ok(ctx, "collections.list").data["collections"][0]["products_count"] == 4


# =========================================================================== inventory
def test_inventory_adjust_uses_idempotency_and_compare_and_swap(ctx, fake_shopify) -> None:
    stock = {"available": 7}

    def adjust(v):
        change = v["input"]["changes"][0]
        assert change["changeFromQuantity"] == stock["available"]
        stock["available"] += change["delta"]
        return {
            "inventoryAdjustQuantities": {
                "inventoryAdjustmentGroup": {"id": "g", "reason": "x", "changes": []},
                "userErrors": [],
            }
        }

    fake_shopify.on(
        "ItemAvailable",
        lambda v: {
            "inventoryItem": {
                "id": v["itemId"],
                "tracked": True,
                "variant": {"title": "S", "product": {"title": "Shirt"}},
                "inventoryLevel": {
                    "location": {"name": "Shop"},
                    "quantities": [{"name": "available", "quantity": stock["available"]}],
                },
            }
        },
    )
    fake_shopify.on("AdjustInventory", adjust)

    result = ok(
        ctx,
        "inventory.adjust",
        {"inventory_item_id": "70", "location_id": "1", "delta": 5, "reason": "received"},
    )
    assert stock["available"] == 12 and result.data["after"] == 12
    undo(result)
    assert stock["available"] == 7
    apply_key, undo_key = [c["idempotencyKey"] for c in fake_shopify.calls_to("AdjustInventory")]
    assert apply_key == result.proposal_id and undo_key == f"{result.proposal_id}-undo"


def test_inventory_adjust_refuses_untracked(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "ItemAvailable",
        {
            "inventoryItem": {
                "id": "i",
                "tracked": False,
                "inventoryLevel": {
                    "location": {"name": "Shop"},
                    "quantities": [{"name": "available", "quantity": 1}],
                },
            }
        },
    )
    result = execute_tool(
        ctx, "inventory.adjust", {"inventory_item_id": "1", "location_id": "1", "delta": 1}
    )
    assert "isn't tracked" in result.error


def test_inventory_levels_needs_exactly_one_id(ctx) -> None:
    assert "exactly one" in execute_tool(ctx, "inventory.levels", {}).error


def test_inventory_levels(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "VariantInventory",
        {
            "productVariant": {
                "id": "v",
                "title": "S",
                "product": {"id": "p", "title": "Shirt"},
                "inventoryItem": {
                    "id": "i",
                    "sku": "SKU",
                    "tracked": True,
                    "inventoryLevels": {
                        "nodes": [
                            {
                                "location": {"id": "l", "name": "Shop"},
                                "quantities": [
                                    {"name": "available", "quantity": 3},
                                    {"name": "on_hand", "quantity": 4},
                                ],
                            }
                        ]
                    },
                },
            }
        },
    )
    data = ok(ctx, "inventory.levels", {"variant_id": "1"}).data
    assert data["variants"][0]["locations"][0] == {
        "location_id": "l",
        "location": "Shop",
        "available": 3,
        "on_hand": 4,
    }


def test_low_stock_filters_untracked_and_drafts(ctx, fake_shopify) -> None:
    def variant(i, tracked=True, status="ACTIVE"):
        return {
            "id": f"v{i}",
            "title": "S",
            "inventoryQuantity": 1,
            "inventoryItem": {"id": f"i{i}", "tracked": tracked},
            "product": {"id": "p", "title": "Shirt", "status": status},
        }

    fake_shopify.on(
        "LowStockVariants",
        {
            "productVariants": {
                "nodes": [variant(1), variant(2, tracked=False), variant(3, status="DRAFT")],
                "pageInfo": NO_MORE,
            }
        },
    )
    data = ok(ctx, "inventory.low_stock", {"threshold": 3}).data
    assert [v["variant_id"] for v in data["variants"]] == ["v1"]
    assert fake_shopify.calls_to("LowStockVariants")[0]["query"] == "inventory_quantity:<=3"


# =========================================================================== orders
def test_order_by_name_tags_and_undo(ctx, fake_shopify) -> None:
    state = {"id": "gid://shopify/Order/5", "name": "#1001", "tags": ["vip"], "note": None}

    def add(v):
        state["tags"] = state["tags"] + [t for t in v["tags"] if t not in state["tags"]]
        return {"tagsAdd": {"node": {"id": v["id"]}, "userErrors": []}}

    def remove(v):
        state["tags"] = [t for t in state["tags"] if t not in v["tags"]]
        return {"tagsRemove": {"node": {"id": v["id"]}, "userErrors": []}}

    fake_shopify.on(
        "SearchOrders",
        {"orders": {"nodes": [{"id": state["id"], "name": "#1001"}], "pageInfo": NO_MORE}},
    )
    fake_shopify.on("OrderTagsAndNote", lambda v: {"order": dict(state)})
    fake_shopify.on("AddTags", add)
    fake_shopify.on("RemoveTags", remove)

    result = ok(ctx, "orders.add_tags", {"order_id": "#1001", "tags": ["vip", "follow-up"]})
    assert state["tags"] == ["vip", "follow-up"] and result.data["added"] == ["follow-up"]
    assert fake_shopify.calls_to("SearchOrders")[0]["query"] == 'name:"#1001"'
    undo(result)
    assert state["tags"] == ["vip"]  # the pre-existing tag stays


def test_order_note_append_and_undo(ctx, fake_shopify) -> None:
    state = {"id": "gid://shopify/Order/5", "name": "#1001", "tags": [], "note": "Gift wrap"}

    def update(v):
        state["note"] = v["input"]["note"]
        return {"orderUpdate": {"order": dict(state), "userErrors": []}}

    fake_shopify.on("OrderTagsAndNote", lambda v: {"order": dict(state)})
    fake_shopify.on("UpdateOrderNote", update)
    result = ok(ctx, "orders.add_note", {"order_id": "5", "note": "Customer called"})
    assert state["note"] == "Gift wrap\n\nCustomer called"
    undo(result)
    assert state["note"] == "Gift wrap"


def test_tags_reject_commas(ctx) -> None:
    assert (
        "commas" in execute_tool(ctx, "orders.add_tags", {"order_id": "5", "tags": ["a,b"]}).error
    )


def test_orders_search_query(ctx, fake_shopify) -> None:
    fake_shopify.on("SearchOrders", {"orders": {"nodes": [], "pageInfo": NO_MORE}})
    ok(ctx, "orders.search", {"financial_status": "paid", "created_from": "2026-10-01"})
    assert fake_shopify.calls_to("SearchOrders")[0]["query"] == (
        'financial_status:"paid" AND created_at:>=2026-10-01'
    )


# =========================================================================== customers
def test_customer_tags_use_customer_document(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "CustomerTags",
        {"customer": {"id": "gid://shopify/Customer/3", "displayName": "Jo", "tags": []}},
    )
    fake_shopify.on("AddTags", {"tagsAdd": {"node": {"id": "x"}, "userErrors": []}})
    fake_shopify.on("RemoveTags", {"tagsRemove": {"node": {"id": "x"}, "userErrors": []}})
    result = ok(ctx, "customers.add_tags", {"customer_id": "3", "tags": ["wholesale"]})
    undo(result)
    assert fake_shopify.calls_to("RemoveTags") == [
        {"id": "gid://shopify/Customer/3", "tags": ["wholesale"]}
    ]


def test_segments_list(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "ListSegments",
        {"segments": {"nodes": [{"id": "s", "name": "VIP", "query": "x"}], "pageInfo": NO_MORE}},
    )
    assert ok(ctx, "customers.segments_list").data["segments"][0]["name"] == "VIP"


# =========================================================================== discounts
def test_create_percentage_code_and_undo(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "CreateBasicCode",
        {
            "discountCodeBasicCreate": {
                "codeDiscountNode": {
                    "id": "gid://shopify/DiscountCodeNode/9",
                    "codeDiscount": {"title": "SAVE15", "status": "ACTIVE"},
                },
                "userErrors": [],
            }
        },
    )
    fake_shopify.on(
        "DeleteCode", {"discountCodeDelete": {"deletedCodeDiscountId": "x", "userErrors": []}}
    )
    result = ok(
        ctx,
        "discounts.create_code",
        {"code": "SAVE15", "percentage": 15, "minimum_subtotal": "50", "usage_limit": 100},
    )
    sent = fake_shopify.calls_to("CreateBasicCode")[0]["basicCodeDiscount"]
    assert sent["customerGets"]["value"] == {"percentage": 0.15}
    assert sent["minimumRequirement"] == {"subtotal": {"greaterThanOrEqualToSubtotal": "50"}}
    assert sent["context"] == {"all": "ALL"}
    undo(result)
    assert fake_shopify.calls_to("DeleteCode") == [{"id": "gid://shopify/DiscountCodeNode/9"}]


def test_discount_needs_exactly_one_value(ctx) -> None:
    assert "exactly one" in execute_tool(ctx, "discounts.create_code", {"code": "X1X"}).error


def test_deactivate_and_undo_reactivates(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "GetCodeDiscount",
        {
            "codeDiscountNode": {
                "id": "gid://shopify/DiscountCodeNode/9",
                "codeDiscount": {"title": "SAVE", "status": "ACTIVE"},
            }
        },
    )
    fake_shopify.on(
        "DeactivateCode",
        {"discountCodeDeactivate": {"codeDiscountNode": {"id": "x"}, "userErrors": []}},
    )
    fake_shopify.on(
        "ActivateCode",
        {"discountCodeActivate": {"codeDiscountNode": {"id": "x"}, "userErrors": []}},
    )
    undo(ok(ctx, "discounts.deactivate", {"discount_id": "9"}))
    assert len(fake_shopify.calls_to("ActivateCode")) == 1


def test_discounts_list_flattens_union(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "ListDiscounts",
        {
            "discountNodes": {
                "nodes": [
                    {
                        "id": "d",
                        "discount": {
                            "__typename": "DiscountCodeBasic",
                            "title": "T",
                            "status": "ACTIVE",
                            "codes": {"nodes": [{"code": "A"}]},
                        },
                    }
                ],
                "pageInfo": NO_MORE,
            }
        },
    )
    assert ok(ctx, "discounts.list").data["discounts"][0]["codes"] == ["A"]


# =========================================================================== content
def test_page_update_and_undo(ctx, fake_shopify) -> None:
    state = {
        "id": "gid://shopify/Page/4",
        "title": "About",
        "body": "<p>Old</p>",
        "isPublished": True,
    }

    def update(v):
        state.update(v["page"])
        return {"pageUpdate": {"page": dict(state), "userErrors": []}}

    fake_shopify.on("GetPage", lambda v: {"page": dict(state)})
    fake_shopify.on("UpdatePage", update)
    result = ok(ctx, "pages.update", {"page_id": "4", "body_html": "<p>New</p>"})
    assert state["body"] == "<p>New</p>"
    undo(result)
    assert state["body"] == "<p>Old</p>" and state["isPublished"] is True


def test_article_draft_is_unpublished_and_undo_deletes(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "CreateArticle",
        {
            "articleCreate": {
                "article": {"id": "gid://shopify/Article/8", "title": "T", "isPublished": False},
                "userErrors": [],
            }
        },
    )
    fake_shopify.on("DeleteArticle", {"articleDelete": {"deletedArticleId": "x", "userErrors": []}})
    result = ok(
        ctx,
        "articles.create_draft",
        {"blog_id": "2", "title": "Spring care", "body_html": "<p>Hi</p>"},
    )
    assert fake_shopify.calls_to("CreateArticle")[0]["article"]["isPublished"] is False
    undo(result)
    assert fake_shopify.calls_to("DeleteArticle") == [{"id": "gid://shopify/Article/8"}]


def test_page_create_and_undo(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "CreatePage",
        {"pageCreate": {"page": {"id": "gid://shopify/Page/1", "title": "FAQ"}, "userErrors": []}},
    )
    fake_shopify.on("DeletePage", {"pageDelete": {"deletedPageId": "x", "userErrors": []}})
    undo(ok(ctx, "pages.create", {"title": "FAQ", "body_html": "<p>Q</p>"}))
    assert len(fake_shopify.calls_to("DeletePage")) == 1


def test_articles_list_by_blog(ctx, fake_shopify) -> None:
    fake_shopify.on("ListArticles", {"articles": {"nodes": [], "pageInfo": NO_MORE}})
    ok(ctx, "articles.list", {"blog_id": "gid://shopify/Blog/12"})
    assert fake_shopify.calls_to("ListArticles")[0]["query"] == "blog_id:12"


# =========================================================================== metafields
def test_metafields_scope_depends_on_owner(ctx, fake_shopify) -> None:
    ctx.shop.granted_scopes = ["read_products"]
    result = execute_tool(ctx, "metafields.get", {"owner_id": "gid://shopify/Customer/1"})
    assert "read_customers" in result.error
    fake_shopify.on("OwnerMetafields", {"node": {"id": "p", "metafields": {"nodes": []}}})
    assert execute_tool(ctx, "metafields.get", {"owner_id": "gid://shopify/Product/1"}).ok


def test_metafields_set_undo_restores_and_deletes(ctx, fake_shopify) -> None:
    existing = {
        ("custom", "care"): {
            "id": "m1",
            "namespace": "custom",
            "key": "care",
            "type": "single_line_text_field",
            "value": "Hand wash",
        }
    }
    fake_shopify.on(
        "OwnerMetafield",
        lambda v: {
            "node": {"id": v["ownerId"], "metafield": existing.get((v["namespace"], v["key"]))}
        },
    )
    fake_shopify.on("SetMetafields", {"metafieldsSet": {"metafields": [], "userErrors": []}})
    fake_shopify.on(
        "DeleteMetafields", {"metafieldsDelete": {"deletedMetafields": [], "userErrors": []}}
    )
    result = ok(
        ctx,
        "metafields.set",
        {
            "owner_id": "gid://shopify/Product/1",
            "metafields": [
                {
                    "namespace": "custom",
                    "key": "care",
                    "type": "single_line_text_field",
                    "value": "Machine wash",
                },
                {
                    "namespace": "custom",
                    "key": "fabric",
                    "type": "single_line_text_field",
                    "value": "Linen",
                },
            ],
        },
    )
    undo(result)
    restore = fake_shopify.calls_to("SetMetafields")[-1]["metafields"]
    assert restore == [
        {
            "ownerId": "gid://shopify/Product/1",
            "namespace": "custom",
            "key": "care",
            "type": "single_line_text_field",
            "value": "Hand wash",
        }
    ]
    assert fake_shopify.calls_to("DeleteMetafields")[0]["metafields"] == [
        {"ownerId": "gid://shopify/Product/1", "namespace": "custom", "key": "fabric"}
    ]


def test_metafields_reserved_namespace(ctx) -> None:
    result = execute_tool(
        ctx,
        "metafields.set",
        {
            "owner_id": "gid://shopify/Product/1",
            "metafields": [{"namespace": "shopify", "key": "xx", "type": "json", "value": "{}"}],
        },
    )
    assert "reserved" in result.error


# =========================================================================== files
def test_files_list(ctx, fake_shopify) -> None:
    fake_shopify.on(
        "ListFiles",
        {
            "files": {
                "nodes": [
                    {
                        "__typename": "MediaImage",
                        "id": "f",
                        "alt": "a",
                        "fileStatus": "READY",
                        "image": {"url": "u", "width": 10, "height": 5},
                    }
                ],
                "pageInfo": NO_MORE,
            }
        },
    )
    data = ok(ctx, "files.list", {"type": "image"}).data
    assert data["files"][0]["url"] == "u"
    assert fake_shopify.calls_to("ListFiles")[0]["query"] == "media_type:IMAGE"
