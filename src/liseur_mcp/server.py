from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .client import LiseurClient
from .config import Settings
from .epub import parse_epub

DEFAULT_TEXT_CHARS = 20_000
MAX_TEXT_CHARS = 100_000
MAX_STATS_WORKS = 50
MAX_HIGHLIGHTS = 500
MAX_HIGHLIGHT_BODY = 2_000


def _annotation_view(annotation: dict[str, Any]) -> dict[str, Any]:
    view = {
        key: annotation[key]
        for key in (
            "id",
            "work_id",
            "kind",
            "excerpt",
            "body",
            "color",
            "progression",
            "client_ts",
            "updated_at",
        )
        if annotation.get(key) is not None
    }
    body = view.get("body")
    if isinstance(body, str) and len(body) > MAX_HIGHLIGHT_BODY:
        view["body"] = body[:MAX_HIGHLIGHT_BODY] + "…"
    return view


def create_server(client: LiseurClient, settings: Settings) -> FastMCP:
    mcp = FastMCP(
        "liseur",
        instructions=(
            "Read-only access to a liseur-sync library: catalog browsing, reading "
            "statistics, highlights and EPUB chapter text. Start with list_folders "
            "or search_books; get_book_text reads a book one chapter at a time."
        ),
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
        ),
    )

    @mcp.tool()
    async def list_folders() -> list[dict[str, Any]]:
        """List the library folders this account can read.

        Each folder carries a folder_id to pass to list_books and search_books.
        """
        return [
            {"folder_id": folder["folder_id"], "name": folder["name"], "kind": folder["kind"]}
            for folder in await client.folders()
        ]

    @mcp.tool()
    async def list_books(folder_id: str, order: str = "recent", limit: int = 50) -> dict[str, Any]:
        """List books in one folder.

        order: "recent" (newest first, default) or "oldest". limit: 1-200.
        Returns full catalog records: book_id, title, contributors, series, tags.
        """
        books = await client.books(folder_id, order=order, limit=limit)
        return {"count": len(books), "books": books}

    @mcp.tool()
    async def search_books(
        query: str, folder_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Search titles, descriptions, series, contributors and tags.

        Searches every folder when folder_id is omitted. Results are best
        matches per folder, not alphabetically ordered.
        """
        folders = [{"folder_id": folder_id}] if folder_id else await client.folders()
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        for folder in folders:
            result = await client.search(folder["folder_id"], query, limit)
            truncated = truncated or bool(result.get("truncated"))
            for book in result.get("books", []):
                merged.setdefault(book["book_id"], book)
        books = list(merged.values())[:limit]
        return {"count": len(books), "truncated": truncated or len(merged) > limit, "books": books}

    @mcp.tool()
    async def get_book(book_id: str) -> dict[str, Any]:
        """Fetch one catalog record by book_id (same shape list_books returns)."""
        return await client.book(book_id)

    @mcp.tool()
    async def reading_stats(range: str = "30d") -> dict[str, Any]:  # noqa: A002
        """Reading totals, streak and pace over a span, plus per-work rows.

        range is a number of days ("7d", "30d") or "all". Per-work rows are
        ordered by time read and capped at 50; current_progression is always
        the latest position regardless of the span.
        """
        summary = await client.insights_summary(range)
        works = (await client.insights_works(range)).get("works", [])
        return {
            "summary": summary,
            "works": works[:MAX_STATS_WORKS],
            "works_total": len(works),
        }

    @mcp.tool()
    async def list_highlights(book_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """List highlights, notes and bookmarks.

        With book_id: the annotations of that book. The first call joins the
        catalog book to your reading work — a per-user mapping; nothing shared
        changes. Without book_id: every live annotation on the account, each
        carrying its work_id.
        """
        if book_id:
            resolution = await client.resolve_book(book_id)
            if resolution.get("confidence") == "low":
                return {
                    "book_id": book_id,
                    "work_id": resolution.get("work_id"),
                    "count": 0,
                    "annotations": [],
                    "note": "match rests on title and author alone and was not stored",
                }
            annotations = await client.work_annotations(resolution["work_id"])
        else:
            annotations = await client.annotation_changes()
        selected = annotations[: max(1, min(limit, MAX_HIGHLIGHTS))]
        return {
            "count": len(selected),
            "annotations": [_annotation_view(annotation) for annotation in selected],
        }

    @mcp.tool()
    async def get_book_text(
        book_id: str,
        chapter: int | None = None,
        max_chars: int = DEFAULT_TEXT_CHARS,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read the text of a book, one chapter at a time.

        Without chapter: returns the table of contents (index, title, chars).
        With chapter: that chapter's text, cut to max_chars from offset;
        next_offset continues when the chapter is longer. The EPUB is
        downloaded from the server and parsed on each call.
        """
        data = await client.download(book_id)
        try:
            title, chapters = parse_epub(data)
        except Exception as exc:
            raise ValueError(f"could not parse the EPUB: {exc}") from exc
        if not chapters:
            raise ValueError("the EPUB has no readable chapters")
        if chapter is None:
            return {
                "book_id": book_id,
                "title": title,
                "total_chars": sum(len(item.text) for item in chapters),
                "chapters": [
                    {"index": item.index, "title": item.title, "chars": len(item.text)}
                    for item in chapters
                ],
            }
        if not 0 <= chapter < len(chapters):
            raise ValueError(f"chapter must be between 0 and {len(chapters) - 1}")
        selected = chapters[chapter]
        start = max(0, min(offset, len(selected.text)))
        end = min(start + max(1, min(max_chars, MAX_TEXT_CHARS)), len(selected.text))
        result: dict[str, Any] = {
            "book_id": book_id,
            "book_title": title,
            "chapter": {
                "index": selected.index,
                "title": selected.title,
                "chars": len(selected.text),
            },
            "offset": start,
            "text": selected.text[start:end],
        }
        if end < len(selected.text):
            result["next_offset"] = end
        return result

    return mcp
