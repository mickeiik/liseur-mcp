from __future__ import annotations

import functools
import importlib.metadata
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from .client import LiseurClient
from .config import Settings
from .epub import parse_epub


def _surface_failure(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Keep a handler failure's message on the wire.

    SDK 2.x treats any exception other than ToolError as a crash and reports it
    as a bare "Error executing tool <name>", hiding the text that 1.x surfaced.
    Re-raising as ToolError restores the old contract for the anticipated
    ValueErrors below and for upstream LiseurError/httpx failures alike.
    ToolError and MCPError pass through untouched: the SDK reserves the latter
    for protocol-level answers (the SDK's own dispatch does the same).
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ToolError, MCPError):
            raise
        except Exception as exc:
            raise ToolError(str(exc)) from exc

    return wrapper

DEFAULT_TEXT_CHARS = 20_000
MAX_TEXT_CHARS = 100_000
MAX_STATS_WORKS = 50
MAX_HIGHLIGHTS = 500
MAX_HIGHLIGHT_BODY = 2_000
MAX_RANGE_DAYS = 3_660
# Bounds what one get_book_text call may buffer: a text-bearing EPUB is a few MB,
# and a 128 MB illustrated book yields no useful text anyway. The peak memory is
# about twice this cap: the accumulation buffer plus the returned copy.
MAX_EPUB_BYTES = 128 * 1024 * 1024


def _validate_range(span: str) -> None:
    """Refuse a span the upstream summary and works endpoints read differently.

    Both endpoints fall back silently for a bad range — the summary to 30 days,
    the works list to unbounded — so an unvalidated value would mix two spans.
    """
    if span == "all":
        return
    days = span[:-1] if span.endswith("d") else ""
    if days.isascii() and days.isdigit() and len(days) <= 4 and 1 <= int(days) <= MAX_RANGE_DAYS:
        return
    raise ValueError(f'range must be "all" or "<days>d" with 1-{MAX_RANGE_DAYS} days')


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


def _package_version() -> str:
    try:
        return importlib.metadata.version("liseur-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def create_server(client: LiseurClient) -> MCPServer:
    mcp = MCPServer(
        "liseur",
        instructions=(
            "Read-only access to a liseur-sync library: catalog browsing, reading "
            "statistics, highlights and EPUB chapter text. Start with list_folders "
            "or search_books; get_book_text reads a book one chapter at a time."
        ),
        version=_package_version(),
    )

    @mcp.tool()
    @_surface_failure
    async def list_folders() -> list[dict[str, Any]]:
        """List the library folders this account can read.

        Each folder carries a folder_id to pass to list_books and search_books.
        """
        return [
            {"folder_id": folder["folder_id"], "name": folder["name"], "kind": folder["kind"]}
            for folder in await client.folders()
        ]

    @mcp.tool()
    @_surface_failure
    async def list_books(folder_id: str, order: str = "recent", limit: int = 50) -> dict[str, Any]:
        """List books in one folder.

        order: "recent" (newest first, default) or "oldest". limit: 1-200; a
        value outside that range is refused, not clamped. Returns full catalog
        records: book_id, title, contributors, series, tags.
        """
        books = await client.books(folder_id, order=order, limit=limit)
        return {"count": len(books), "books": books}

    @mcp.tool()
    @_surface_failure
    async def search_books(
        query: str, folder_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Search titles, descriptions, series, contributors and tags.

        Searches every folder when folder_id is omitted. Results are best
        matches per folder, not alphabetically ordered, gathered folder by
        folder: earlier folders can fill the limit and later folders then
        contribute nothing (truncated says the answer was cut). limit: 1-100
        (default 20); a value outside that range is refused, not clamped.
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
    @_surface_failure
    async def get_book(book_id: str) -> dict[str, Any]:
        """Fetch one catalog record by book_id (same shape list_books returns)."""
        return await client.book(book_id)

    @mcp.tool()
    @_surface_failure
    async def reading_stats(range: str = "30d") -> dict[str, Any]:  # noqa: A002
        """Reading totals, streak and pace over a span, plus per-work rows.

        range is "all" or a number of days from 1 to 3660 ("7d", "30d"); any
        other value is refused rather than silently defaulted, because the
        upstream summary and works endpoints would then disagree on the span.
        Per-work rows are ordered by time read and capped at 50;
        current_progression is always the latest position regardless of the
        span.
        """
        _validate_range(range)
        summary = await client.insights_summary(range)
        works = (await client.insights_works(range)).get("works", [])
        return {
            "summary": summary,
            "works": works[:MAX_STATS_WORKS],
            "works_total": len(works),
        }

    @mcp.tool()
    @_surface_failure
    async def list_highlights(
        book_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """List highlights, notes and bookmarks.

        With book_id: the annotations of that book, in the server's document
        order (by progression). The first call joins the catalog book to your
        reading work — a per-user mapping; nothing shared changes. If the
        catalog match is too weak to store (confidence "low"), nothing is
        returned and the answer says so; annotations may still exist under the
        returned work_id, which the note points out.

        Without book_id: every live annotation on the account, most recently
        changed first (by the server's internal sequence), each carrying its
        work_id. Both branches return at most limit annotations (1-500, cap
        500); a limit below 1 and an offset below 0 are refused. count, total
        and truncated are reported; total is the size of the whole set and does
        not depend on limit or offset, while truncated says more annotations
        remain beyond this page.

        Paging: pass the next_offset of a previous page as offset to read on;
        next_offset is present only when more annotations remain, and every
        response echoes the offset its page started at. The list is fetched
        fresh on every call, so if annotations change between pages an item can
        shift — pass back the next_offset you were given rather than computing
        your own.
        """
        if limit < 1:
            raise ValueError(f"limit must be between 1 and {MAX_HIGHLIGHTS}")
        if offset < 0:
            raise ValueError("offset must be 0 or greater")
        if book_id:
            resolution = await client.resolve_book(book_id)
            if resolution.get("confidence") == "low":
                return {
                    "book_id": book_id,
                    "work_id": resolution.get("work_id"),
                    "count": 0,
                    "total": 0,
                    "truncated": False,
                    "annotations": [],
                    "note": (
                        "match rests on title and author alone and was not stored; "
                        "annotations may still exist under the returned work_id"
                    ),
                }
            annotations = await client.work_annotations(resolution["work_id"])
        else:
            annotations = sorted(
                await client.annotation_changes(),
                key=lambda annotation: annotation["seq"],
                reverse=True,
            )
        start = min(offset, len(annotations))
        selected = annotations[start : start + min(limit, MAX_HIGHLIGHTS)]
        more = start + len(selected) < len(annotations)
        result: dict[str, Any] = {
            "count": len(selected),
            "total": len(annotations),
            "truncated": more,
            "offset": start,
            "annotations": [_annotation_view(annotation) for annotation in selected],
        }
        if more:
            result["next_offset"] = start + len(selected)
        return result

    @mcp.tool()
    @_surface_failure
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
        downloaded from the server and parsed on each call; a download larger
        than the 128 MiB cap is refused.
        """
        data = await client.download(book_id, max_bytes=MAX_EPUB_BYTES)
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


def create_http_app(mcp: MCPServer, settings: Settings) -> Starlette:
    return mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )
