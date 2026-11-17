"""Online store content tools: pages, blogs and blog articles."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from apps.tools.context import ToolContext
from apps.tools.errors import ToolError
from apps.tools.registry import ToolInput, tool

from ._common import PageInput, changed_fields, gid, load_document, page

DOC = load_document("content")


# =========================================================================== pages
class ListPagesInput(PageInput):
    query: str = ""


@tool(
    key="pages.list",
    name="List pages",
    description="Online store pages (About us, FAQ, …), most recently updated first.",
    category="Content",
    scopes=["read_content"],
)
def list_pages(ctx: ToolContext, args: ListPagesInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC, "ListPages", {"first": args.limit, "after": args.cursor, "query": args.query or None}
    )
    return page(data["pages"], "pages")


class PageIdInput(ToolInput):
    page_id: str


def _get_page(ctx: ToolContext, page_id: str) -> dict[str, Any]:
    p = ctx.gql(DOC, "GetPage", {"id": gid("Page", page_id)})["page"]
    if p is None:
        raise ToolError(f"No page found with id {page_id}.")
    return dict(p)


@tool(
    key="pages.get",
    name="Get page",
    description="One online store page including its full body (HTML).",
    category="Content",
    scopes=["read_content"],
)
def get_page(ctx: ToolContext, args: PageIdInput) -> dict[str, Any]:
    return _get_page(ctx, args.page_id)


class CreatePageInput(ToolInput):
    title: str = Field(min_length=1, max_length=255)
    body_html: str = ""
    is_published: bool = Field(False, description="Default: saved as hidden draft.")


@tool(
    key="pages.create",
    name="Create page",
    description="Create an online store page. Hidden (unpublished) unless is_published.",
    category="Content",
    scopes=["write_content"],
    mutating=True,
)
def create_page(ctx: ToolContext, args: CreatePageInput) -> dict[str, Any]:
    data = ctx.gql(
        DOC,
        "CreatePage",
        {"page": {"title": args.title, "body": args.body_html, "isPublished": args.is_published}},
        user_errors_at="pageCreate",
    )
    return {"page": data["pageCreate"]["page"]}


@create_page.undo
def _undo_create_page(ctx: ToolContext, args: CreatePageInput, before: dict, after: dict) -> Any:
    return ctx.gql(DOC, "DeletePage", {"id": after["page"]["id"]}, user_errors_at="pageDelete")


_PAGE_FIELDS = {"title": "title", "body_html": "body", "is_published": "isPublished"}


class UpdatePageInput(PageIdInput):
    title: str | None = Field(None, min_length=1, max_length=255)
    body_html: str | None = None
    is_published: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> UpdatePageInput:
        if not changed_fields(self, _PAGE_FIELDS):
            raise ValueError("Provide at least one field to change.")
        return self


def _update_page(ctx: ToolContext, page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    data = ctx.gql(DOC, "UpdatePage", {"id": page_id, "page": fields}, user_errors_at="pageUpdate")
    return {"page": data["pageUpdate"]["page"]}


@tool(
    key="pages.update",
    name="Update page",
    description="Change a page's title, body (HTML) and/or visibility.",
    category="Content",
    scopes=["write_content"],
    mutating=True,
)
def update_page(ctx: ToolContext, args: UpdatePageInput) -> dict[str, Any]:
    return _update_page(ctx, gid("Page", args.page_id), changed_fields(args, _PAGE_FIELDS))


@update_page.preview
def _preview_update_page(ctx: ToolContext, args: UpdatePageInput) -> dict[str, Any]:
    current = _get_page(ctx, args.page_id)
    return {
        "summary": f"Update page “{current['title']}”",
        "changes": [
            {"field": f, "before": current.get(f), "after": v}
            for f, v in changed_fields(args, _PAGE_FIELDS).items()
        ],
    }


@update_page.snapshot
def _snapshot_page(ctx: ToolContext, args: UpdatePageInput) -> dict[str, Any]:
    return _get_page(ctx, args.page_id)


@update_page.undo
def _undo_update_page(ctx: ToolContext, args: UpdatePageInput, before: dict, after: dict) -> Any:
    fields = changed_fields(args, _PAGE_FIELDS)
    return _update_page(ctx, before["id"], {f: before.get(f) for f in fields})


# =========================================================================== blogs & articles
@tool(
    key="blogs.list",
    name="List blogs",
    description="The store's blogs (most stores have one, often called 'News').",
    category="Content",
    scopes=["read_content"],
)
def list_blogs(ctx: ToolContext, args: PageInput) -> dict[str, Any]:
    data = ctx.gql(DOC, "ListBlogs", {"first": args.limit, "after": args.cursor})
    result = page(data["blogs"], "blogs")
    for b in result["blogs"]:
        b["articles_count"] = (b.pop("articlesCount") or {}).get("count")
    return result


class ListArticlesInput(PageInput):
    blog_id: str | None = None
    query: str = ""


@tool(
    key="articles.list",
    name="List blog articles",
    description="Blog articles, most recently updated first, optionally for one blog.",
    category="Content",
    scopes=["read_content"],
)
def list_articles(ctx: ToolContext, args: ListArticlesInput) -> dict[str, Any]:
    parts = [args.query] if args.query else []
    if args.blog_id:
        parts.append(f"blog_id:{gid('Blog', args.blog_id).rsplit('/', 1)[1]}")
    data = ctx.gql(
        DOC,
        "ListArticles",
        {"first": args.limit, "after": args.cursor, "query": " AND ".join(parts) or None},
    )
    return page(data["articles"], "articles")


class ArticleIdInput(ToolInput):
    article_id: str


def _get_article(ctx: ToolContext, article_id: str) -> dict[str, Any]:
    a = ctx.gql(DOC, "GetArticle", {"id": gid("Article", article_id)})["article"]
    if a is None:
        raise ToolError(f"No article found with id {article_id}.")
    return dict(a)


@tool(
    key="articles.get",
    name="Get blog article",
    description="One blog article with body, summary, tags, author and image.",
    category="Content",
    scopes=["read_content"],
)
def get_article(ctx: ToolContext, args: ArticleIdInput) -> dict[str, Any]:
    return _get_article(ctx, args.article_id)


class CreateArticleInput(ToolInput):
    blog_id: str
    title: str = Field(min_length=1, max_length=255)
    body_html: str
    summary_html: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)
    author_name: str = Field("Store team", max_length=100)


@tool(
    key="articles.create_draft",
    name="Create blog article (draft)",
    description="""Write a new blog article as an unpublished DRAFT. The merchant
    reviews and publishes it in Shopify.""",
    category="Content",
    scopes=["write_content"],
    mutating=True,
)
def create_article(ctx: ToolContext, args: CreateArticleInput) -> dict[str, Any]:
    article: dict[str, Any] = {
        "blogId": gid("Blog", args.blog_id),
        "title": args.title,
        "body": args.body_html,
        "tags": args.tags,
        "isPublished": False,
        "author": {"name": args.author_name},
    }
    if args.summary_html:
        article["summary"] = args.summary_html
    data = ctx.gql(DOC, "CreateArticle", {"article": article}, user_errors_at="articleCreate")
    return {"article": data["articleCreate"]["article"]}


@create_article.undo
def _undo_create_article(
    ctx: ToolContext, args: CreateArticleInput, before: dict, after: dict
) -> Any:
    return ctx.gql(
        DOC, "DeleteArticle", {"id": after["article"]["id"]}, user_errors_at="articleDelete"
    )


_ARTICLE_FIELDS = {
    "title": "title",
    "body_html": "body",
    "summary_html": "summary",
    "tags": "tags",
    "is_published": "isPublished",
}


class UpdateArticleInput(ArticleIdInput):
    title: str | None = Field(None, min_length=1, max_length=255)
    body_html: str | None = None
    summary_html: str | None = None
    tags: list[str] | None = Field(None, description="REPLACES all tags.")
    is_published: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> UpdateArticleInput:
        if not changed_fields(self, _ARTICLE_FIELDS):
            raise ValueError("Provide at least one field to change.")
        return self


def _update_article(ctx: ToolContext, article_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    data = ctx.gql(
        DOC, "UpdateArticle", {"id": article_id, "article": fields}, user_errors_at="articleUpdate"
    )
    return {"article": data["articleUpdate"]["article"]}


@tool(
    key="articles.update",
    name="Update blog article",
    description="Change an article's title, body, summary, tags or visibility.",
    category="Content",
    scopes=["write_content"],
    mutating=True,
)
def update_article(ctx: ToolContext, args: UpdateArticleInput) -> dict[str, Any]:
    return _update_article(
        ctx, gid("Article", args.article_id), changed_fields(args, _ARTICLE_FIELDS)
    )


@update_article.preview
def _preview_update_article(ctx: ToolContext, args: UpdateArticleInput) -> dict[str, Any]:
    current = _get_article(ctx, args.article_id)
    return {
        "summary": f"Update article “{current['title']}”",
        "changes": [
            {"field": f, "before": current.get(f), "after": v}
            for f, v in changed_fields(args, _ARTICLE_FIELDS).items()
        ],
    }


@update_article.snapshot
def _snapshot_article(ctx: ToolContext, args: UpdateArticleInput) -> dict[str, Any]:
    return _get_article(ctx, args.article_id)


@update_article.undo
def _undo_update_article(
    ctx: ToolContext, args: UpdateArticleInput, before: dict, after: dict
) -> Any:
    fields = changed_fields(args, _ARTICLE_FIELDS)
    return _update_article(ctx, before["id"], {f: before.get(f) for f in fields})
