from __future__ import annotations

import asyncio

import httpx
import pytest

from liseur_mcp.client import LiseurClient, LiseurError


def _client(transport: httpx.AsyncBaseTransport) -> LiseurClient:
    return LiseurClient("http://liseur.test", "token", transport=transport)


def test_folders_follow_cursor_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") == "cursor-1":
            return httpx.Response(
                200, json={"folders": [{"folder_id": "f2", "name": "B", "kind": "plain"}]}
            )
        return httpx.Response(
            200,
            json={
                "folders": [{"folder_id": "f1", "name": "A", "kind": "plain"}],
                "next_after": "cursor-1",
            },
        )

    folders = asyncio.run(_client(httpx.MockTransport(handler)).folders())
    assert [folder["folder_id"] for folder in folders] == ["f1", "f2"]


def test_annotation_changes_drops_tombstones_and_pages() -> None:
    pages = {
        0: {
            "annotations": [
                {"id": "a1", "kind": "highlight"},
                {"id": "a2", "kind": "highlight", "deleted": True},
            ],
            "high_water": 7,
            "has_more": True,
        },
        7: {"annotations": [{"id": "a3", "kind": "note"}], "high_water": 9, "has_more": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["since"])])

    annotations = asyncio.run(_client(httpx.MockTransport(handler)).annotation_changes())
    assert [annotation["id"] for annotation in annotations] == ["a1", "a3"]


def test_download_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"epub-bytes",
            headers={"content-type": "application/epub+zip"},
        )

    assert asyncio.run(_client(httpx.MockTransport(handler)).download("b1")) == b"epub-bytes"


def test_forbidden_error_mentions_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).insights_summary("30d"))
    assert excinfo.value.status == 403
    assert "scope" in str(excinfo.value)
