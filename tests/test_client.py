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


def test_annotation_changes_follows_the_documented_cursor() -> None:
    live = [{"id": f"a{i:04d}", "kind": "highlight", "rev": 1, "seq": i + 1} for i in range(600)]
    rows = [*live, {"id": "tomb", "rev": 1, "seq": 601, "deleted": True}]
    high_water = rows[-1]["seq"]  # the account's global max seq, constant on every page
    seen_since: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        since = int(request.url.params["since"])
        limit = int(request.url.params["limit"])
        seen_since.append(since)
        remaining = [row for row in rows if row["seq"] > since]
        return httpx.Response(
            200,
            json={
                "annotations": remaining[:limit],
                "high_water": high_water,
                "has_more": len(remaining) > limit,
            },
        )

    annotations = asyncio.run(_client(httpx.MockTransport(handler)).annotation_changes())
    assert seen_since == [0, 500]
    assert [annotation["id"] for annotation in annotations] == [row["id"] for row in live]


def test_annotation_changes_collapses_same_id_edits_and_late_tombstones() -> None:
    rows = [
        {"id": "a0", "kind": "note", "rev": 1, "seq": 1},
        {"id": "a1", "kind": "note", "rev": 1, "seq": 2},
        {"id": "a0", "kind": "note", "rev": 2, "seq": 3},
        {"id": "a2", "kind": "note", "rev": 1, "seq": 4},
        {"id": "a2", "rev": 2, "seq": 5, "deleted": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        since = int(request.url.params["since"])
        return httpx.Response(
            200,
            json={
                "annotations": [row for row in rows if row["seq"] > since],
                "high_water": rows[-1]["seq"],
                "has_more": False,
            },
        )

    annotations = asyncio.run(_client(httpx.MockTransport(handler)).annotation_changes())
    assert [annotation["id"] for annotation in annotations] == ["a0", "a1"]
    assert annotations[0]["rev"] == 2


def test_annotation_changes_stops_when_a_page_cannot_advance_the_cursor() -> None:
    seen_since: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # A server that ignores `since` and keeps claiming there is more.
        seen_since.append(request.url.params["since"])
        if len(seen_since) > 3:
            raise AssertionError("cursor never advanced: the pull is spinning")
        return httpx.Response(
            200,
            json={
                "annotations": [{"id": "a1", "kind": "note", "rev": 1, "seq": 1}],
                "high_water": 1,
                "has_more": True,
            },
        )

    annotations = asyncio.run(_client(httpx.MockTransport(handler)).annotation_changes())
    assert seen_since == ["0", "1"]
    assert [annotation["id"] for annotation in annotations] == ["a1"]


def test_download_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"epub-bytes",
            headers={"content-type": "application/epub+zip"},
        )

    assert asyncio.run(_client(httpx.MockTransport(handler)).download("b1")) == b"epub-bytes"


def test_forbidden_scope_error_mentions_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "insufficient scope"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).insights_summary("30d"))
    assert excinfo.value.status == 403
    assert "scope" in str(excinfo.value)


def test_forbidden_https_required_does_not_mention_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "https required"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).insights_summary("30d"))
    assert excinfo.value.status == 403
    assert "https required" in str(excinfo.value)
    assert "scope" not in str(excinfo.value)


def test_redirect_raises_liseur_error_not_attribute_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301,
            text="<html><body>moved</body></html>",
            headers={"content-type": "text/html", "location": "https://elsewhere.test/v1"},
        )

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).folders())
    assert excinfo.value.status == 301
    assert "LISEUR_URL" in str(excinfo.value)


@pytest.mark.parametrize("body", [["not", "an", "object"], "just a string"])
def test_non_dict_json_error_body_is_reported_cleanly(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=body)

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).folders())
    assert excinfo.value.status == 500
    assert "liseur-sync answered 500" in str(excinfo.value)
