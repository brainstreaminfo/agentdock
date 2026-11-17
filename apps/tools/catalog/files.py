"""File tools: list files uploaded to the store (images, documents, videos)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from apps.tools.context import ToolContext
from apps.tools.registry import tool

from ._common import PageInput, load_document, page

DOC = load_document("files")

_TYPE_QUERY = {
    "image": "media_type:IMAGE",
    "video": "media_type:VIDEO",
    "file": "media_type:GENERIC_FILE",
}


class ListFilesInput(PageInput):
    query: str = Field("", description="Filename text to search for.")
    type: Literal["image", "video", "file"] | None = None


@tool(
    key="files.list",
    name="List files",
    description="""Files in Shopify Admin → Content → Files, newest first: images
    (with size and alt text), videos and other documents.""",
    category="Files",
    scopes=["read_files"],
)
def list_files(ctx: ToolContext, args: ListFilesInput) -> dict[str, Any]:
    parts = [args.query] if args.query else []
    if args.type:
        parts.append(_TYPE_QUERY[args.type])
    data = ctx.gql(
        DOC,
        "ListFiles",
        {"first": args.limit, "after": args.cursor, "query": " AND ".join(parts) or None},
    )
    result = page(data["files"], "files")
    flat = []
    for f in result["files"]:
        image = f.get("image") or {}
        flat.append(
            {
                "id": f["id"],
                "type": f["__typename"],
                "alt": f.get("alt"),
                "status": f.get("fileStatus"),
                "created_at": f.get("createdAt"),
                "url": image.get("url") or f.get("url"),
                "width": image.get("width"),
                "height": image.get("height"),
                "mime_type": f.get("mimeType"),
                "filename": f.get("filename"),
            }
        )
    result["files"] = flat
    return result
