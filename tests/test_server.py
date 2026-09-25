from __future__ import annotations

import asyncio
import importlib.metadata
import io
import zipfile
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from liseur_mcp import server as server_module
from liseur_mcp.client import LiseurClient
from liseur_mcp.config import Settings
from liseur_mcp.server import _surface_failure, create_server

TOOL_NAMES = {
    "list_folders",
    "list_books",
    "search_books",
    "get_book",
    "reading_stats",
    "list_highlights",
    "get_book_text",
}

Handler = Callable[[httpx.Request], httpx.Response]

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title>
  </metadata>
  <manifest>
    <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>"""

CHAPTER_0 = "One\n\nHello world.\n\nSecond & last."
CHAPTER_1 = "Another chapter."


def _epub() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr(
            "OEBPS/ch1.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            "<title>First</title></head><body><h1>One</h1>"
            "<p>Hello world.</p><p>Second &amp; last.</p></body></html>",
        )
        archive.writestr(
            "OEBPS/ch2.xhtml",
            "<html><head><title></title></head><body><p>Another chapter.</p></body></html>",
        )
    return buffer.getvalue()


def _server(handler: Handler) -> MCPServer:
    client = LiseurClient("http://liseur.test", "token", transport=httpx.MockTransport(handler))
    return create_server(client)


def _call(mcp: MCPServer, name: str, arguments: dict[str, Any]) -> Any:
    result = asyncio.run(mcp.call_tool(name, arguments))
    return result.structured_content


def test_server_registers_the_read_only_tool_surface() -> None:
    client = LiseurClient(
        "http://liseur.test",
        "token",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    mcp = create_server(client)
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == TOOL_NAMES


def test_surface_failure_keeps_messages_and_lets_protocol_errors_through() -> None:
    @_surface_failure
    async def raises_value() -> None:
        raise ValueError("range must be \"all\" or \"<days>d\"")

    @_surface_failure
    async def raises_tool() -> None:
        raise ToolError("tool-level")

    @_surface_failure
    async def raises_mcp() -> None:
        raise MCPError(-32042, "protocol-level")

    with pytest.raises(ToolError, match="range must be"):
        asyncio.run(raises_value())
    with pytest.raises(ToolError, match="tool-level"):
        asyncio.run(raises_tool())
    # MCPError answers the JSON-RPC layer; converting it to a tool error would
    # answer the client with the wrong kind of failure.
    with pytest.raises(MCPError, match="protocol-level"):
        asyncio.run(raises_mcp())


def test_upstream_failure_message_reaches_the_tool_error() -> None:
    mcp = _server(lambda request: httpx.Response(500, text="upstream is unwell"))

    with pytest.raises(ToolError) as excinfo:
        _call(mcp, "list_folders", {})

    assert "liseur-sync answered 500: upstream is unwell" in str(excinfo.value)


def test_package_version_falls_back_when_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert server_module._package_version() == "0.0.0"


def test_http_transport_requires_an_auth_token() -> None:
    try:
        Settings(
            LISEUR_URL="http://liseur.test",
            LISEUR_TOKEN="token",
            MCP_TRANSPORT="streamable-http",
        )
    except ValueError as exc:
        assert "MCP_AUTH_TOKEN" in str(exc)
    else:
        raise AssertionError("streamable-http must refuse to start without MCP_AUTH_TOKEN")


def _download_handler(handler: Handler) -> Handler:
    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            return handler(request)
        return httpx.Response(404)

    return routed


def test_get_book_text_reads_toc_and_slices_chapters() -> None:
    def epub(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_epub(), headers={"content-type": "application/epub+zip"}
        )

    mcp = _server(_download_handler(epub))

    toc = _call(mcp, "get_book_text", {"book_id": "b1"})
    assert toc["title"] == "Test Book"
    assert toc["total_chars"] == len(CHAPTER_0) + len(CHAPTER_1)
    assert [chapter["index"] for chapter in toc["chapters"]] == [0, 1]
    assert [chapter["title"] for chapter in toc["chapters"]] == ["First", "Chapter 2"]
    assert toc["chapters"][0]["chars"] == len(CHAPTER_0)

    first = _call(mcp, "get_book_text", {"book_id": "b1", "chapter": 0})
    assert first["chapter"]["index"] == 0
    assert first["text"] == CHAPTER_0
    assert "next_offset" not in first

    sliced = _call(
        mcp, "get_book_text", {"book_id": "b1", "chapter": 0, "offset": 2, "max_chars": 5}
    )
    assert sliced["offset"] == 2
    assert sliced["text"] == CHAPTER_0[2:7]
    assert sliced["next_offset"] == 7


def test_get_book_text_refuses_bad_chapter_and_non_epub() -> None:
    def epub(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_epub())

    out_of_range = _server(_download_handler(epub))
    with pytest.raises(ToolError) as excinfo:
        _call(out_of_range, "get_book_text", {"book_id": "b1", "chapter": 9})
    assert "chapter must be between 0 and 1" in str(excinfo.value)

    def not_an_epub(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not an epub", headers={"content-type": "application/octet-stream"}
        )

    broken = _server(_download_handler(not_an_epub))
    with pytest.raises(ToolError) as excinfo:
        _call(broken, "get_book_text", {"book_id": "b1"})
    assert "could not parse the EPUB" in str(excinfo.value)


def test_get_book_text_refuses_an_epub_over_the_buffer_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = 1024 * 1024
    monkeypatch.setattr(server_module, "MAX_EPUB_BYTES", cap)

    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (cap + 1),
            headers={"content-type": "application/epub+zip"},
        )

    mcp = _server(_download_handler(oversized))
    with pytest.raises(ToolError) as excinfo:
        _call(mcp, "get_book_text", {"book_id": "b1"})
    assert "1 MiB cap" in str(excinfo.value)


def _changes_handler(rows: list[dict[str, Any]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        since = int(request.url.params["since"])
        remaining = [row for row in rows if row["seq"] > since]
        return httpx.Response(
            200,
            json={
                "annotations": remaining,
                "high_water": rows[-1]["seq"],
                "has_more": False,
            },
        )

    return handler


# The feed is seq-ascending: the oldest annotation is served first.
_FEED: list[dict[str, Any]] = [
    {"id": "old", "kind": "highlight", "work_id": "w1", "seq": 1, "rev": 1},
    {"id": "mid", "kind": "note", "work_id": "w1", "seq": 2, "rev": 1},
    {"id": "new", "kind": "highlight", "work_id": "w2", "seq": 3, "rev": 1},
    {"id": "gone", "rev": 1, "seq": 4, "deleted": True},
]


def test_list_highlights_account_wide_is_newest_first_with_honest_counts() -> None:
    mcp = _server(_changes_handler(_FEED))

    capped = _call(mcp, "list_highlights", {"limit": 2})
    assert [annotation["id"] for annotation in capped["annotations"]] == ["new", "mid"]
    assert capped["count"] == 2
    assert capped["total"] == 3
    assert capped["truncated"] is True

    full = _call(mcp, "list_highlights", {})
    assert [annotation["id"] for annotation in full["annotations"]] == ["new", "mid", "old"]
    assert full["count"] == 3
    assert full["total"] == 3
    assert full["truncated"] is False
    assert "seq" not in full["annotations"][0]


def test_list_highlights_low_confidence_match_returns_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"confidence": "low", "work_id": "w1"})

    mcp = _server(handler)
    result = _call(mcp, "list_highlights", {"book_id": "b1"})
    assert result["count"] == 0
    assert result["total"] == 0
    assert result["truncated"] is False
    assert result["annotations"] == []
    assert result["book_id"] == "b1"
    assert result["work_id"] == "w1"
    assert "not stored" in result["note"]
    assert "work_id" in result["note"]


def test_reading_stats_accepts_range_all() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "range": request.url.params.get("range", "")})
        if request.url.path.endswith("/insights/summary"):
            return httpx.Response(200, json={"sessions": 1})
        return httpx.Response(200, json={"works": [{"work_id": "w1"}]})

    mcp = _server(handler)
    result = _call(mcp, "reading_stats", {"range": "all"})
    assert result["works_total"] == 1
    assert seen == [
        {"path": "/v1/insights/summary", "range": "all"},
        {"path": "/v1/insights/works", "range": "all"},
    ]


def test_list_highlights_next_offset_respects_the_cap() -> None:
    # next_offset must be the page's real end (the 500 cap), not start + limit:
    # a cap-unaware value would silently skip everything between them.
    rows = [
        {"id": f"a{i}", "kind": "highlight", "work_id": "w1", "seq": i, "rev": 1}
        for i in range(1, 1201)
    ]
    mcp = _server(_changes_handler(rows))

    first = _call(mcp, "list_highlights", {"limit": 600})
    assert first["count"] == 500
    assert first["next_offset"] == 500

    walked = [annotation["id"] for annotation in first["annotations"]]
    offset = first["next_offset"]
    while True:
        page = _call(mcp, "list_highlights", {"limit": 600, "offset": offset})
        walked += [annotation["id"] for annotation in page["annotations"]]
        if "next_offset" not in page:
            break
        offset = page["next_offset"]
    assert walked == [row["id"] for row in reversed(rows)]


def test_list_highlights_refuses_limit_below_one_and_caps_at_max() -> None:
    rows = [
        {"id": f"a{i}", "kind": "highlight", "work_id": "w1", "seq": i, "rev": 1}
        for i in range(1, 601)
    ]
    mcp = _server(_changes_handler(rows))

    with pytest.raises(ToolError) as excinfo:
        _call(mcp, "list_highlights", {"limit": 0})
    assert "limit must be" in str(excinfo.value)

    capped = _call(mcp, "list_highlights", {"limit": 600})
    assert capped["count"] == 500
    assert capped["total"] == 600
    assert capped["truncated"] is True


def test_reading_stats_caps_works_and_validates_range() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("range", ""))
        if request.url.path.endswith("/insights/summary"):
            return httpx.Response(200, json={"sessions": 1})
        return httpx.Response(200, json={"works": [{"work_id": f"w{i}"} for i in range(60)]})

    mcp = _server(handler)

    result = _call(mcp, "reading_stats", {"range": "30d"})
    assert len(result["works"]) == 50
    assert result["works_total"] == 60
    assert seen == ["30d", "30d"]

    calls_before = len(seen)
    with pytest.raises(ToolError) as excinfo:
        _call(mcp, "reading_stats", {"range": "banana"})
    assert "range must be" in str(excinfo.value)
    assert len(seen) == calls_before

    with pytest.raises(ToolError) as excinfo:
        _call(mcp, "reading_stats", {"range": "1" + "0" * 9999 + "d"})
    assert "range must be" in str(excinfo.value)
    assert len(seen) == calls_before


def test_list_books_forwards_order_and_limit() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"books": [{"book_id": "b1"}]})

    mcp = _server(handler)
    result = _call(mcp, "list_books", {"folder_id": "f1", "order": "oldest", "limit": 7})
    assert result["count"] == 1
    assert seen == [{"order": "oldest", "limit": "7"}]


def test_search_books_forwards_query_and_merges_two_folders() -> None:
    searches: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/folders":
            return httpx.Response(
                200,
                json={"folders": [{"folder_id": "f1"}, {"folder_id": "f2"}]},
            )
        searches.append(dict(request.url.params))
        if request.url.path.split("/")[3] == "f1":
            return httpx.Response(
                200,
                json={"books": [{"book_id": "b1"}, {"book_id": "b2"}], "truncated": False},
            )
        return httpx.Response(
            200,
            json={"books": [{"book_id": "b2"}, {"book_id": "b3"}], "truncated": False},
        )

    mcp = _server(handler)
    result = _call(mcp, "search_books", {"query": "dune", "limit": 10})
    assert [book["book_id"] for book in result["books"]] == ["b1", "b2", "b3"]
    assert result["count"] == 3
    assert result["truncated"] is False
    assert searches == [{"q": "dune", "limit": "10"}, {"q": "dune", "limit": "10"}]


def test_list_highlights_account_wide_pages_cover_every_annotation_once() -> None:
    rows = [
        {"id": f"a{i}", "kind": "highlight", "work_id": "w1", "seq": i, "rev": 1}
        for i in range(1, 1201)
    ]
    mcp = _server(_changes_handler(rows))

    first = _call(mcp, "list_highlights", {"limit": 500})
    assert first["count"] == 500
    assert first["total"] == 1200
    assert first["offset"] == 0
    assert first["truncated"] is True
    assert first["next_offset"] == 500

    second = _call(mcp, "list_highlights", {"limit": 500, "offset": 500})
    assert second["count"] == 500
    assert second["offset"] == 500
    assert second["next_offset"] == 1000

    third = _call(mcp, "list_highlights", {"limit": 500, "offset": 1000})
    assert third["count"] == 200
    assert third["offset"] == 1000
    assert third["truncated"] is False
    assert "next_offset" not in third

    walked = [
        annotation["id"]
        for page in (first, second, third)
        for annotation in page["annotations"]
    ]
    assert walked == [f"a{i}" for i in range(1200, 0, -1)]
    assert len(set(walked)) == len(walked) == 1200


def test_list_highlights_book_scoped_pages_in_document_order() -> None:
    rows = [
        {"id": f"b{i}", "kind": "highlight", "work_id": "w1", "seq": i, "rev": 1}
        for i in range(5)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resolve"):
            return httpx.Response(200, json={"confidence": "high", "work_id": "w1"})
        return httpx.Response(200, json={"annotations": rows})

    mcp = _server(handler)

    first = _call(mcp, "list_highlights", {"book_id": "b1", "limit": 2})
    assert [annotation["id"] for annotation in first["annotations"]] == ["b0", "b1"]
    assert first["count"] == 2
    assert first["total"] == 5
    assert first["offset"] == 0
    assert first["next_offset"] == 2

    second = _call(mcp, "list_highlights", {"book_id": "b1", "limit": 2, "offset": 2})
    assert [annotation["id"] for annotation in second["annotations"]] == ["b2", "b3"]
    assert second["offset"] == 2
    assert second["next_offset"] == 4

    third = _call(mcp, "list_highlights", {"book_id": "b1", "limit": 2, "offset": 4})
    assert [annotation["id"] for annotation in third["annotations"]] == ["b4"]
    assert third["count"] == 1
    assert third["truncated"] is False
    assert "next_offset" not in third


def test_list_highlights_offset_past_the_end_is_an_empty_page() -> None:
    mcp = _server(_changes_handler(_FEED))

    result = _call(mcp, "list_highlights", {"offset": 999})
    assert result["count"] == 0
    assert result["total"] == 3
    assert result["truncated"] is False
    assert result["offset"] == 3
    assert result["annotations"] == []
    assert "next_offset" not in result


def test_list_highlights_refuses_negative_offset() -> None:
    mcp = _server(_changes_handler(_FEED))

    with pytest.raises(ToolError) as excinfo:
        _call(mcp, "list_highlights", {"offset": -1})
    assert "offset must be" in str(excinfo.value)


def test_list_highlights_total_ignores_offset_and_limit() -> None:
    mcp = _server(_changes_handler(_FEED))

    one = _call(mcp, "list_highlights", {"limit": 1})
    two = _call(mcp, "list_highlights", {"limit": 2, "offset": 1})
    assert one["total"] == two["total"] == 3
    assert one["count"] == 1
    assert two["count"] == 2
