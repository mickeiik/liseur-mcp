from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator, Iterable

import httpx
import pytest

from liseur_mcp.client import _ERROR_BODY_BYTES, LiseurClient, LiseurError


def _client(transport: httpx.AsyncBaseTransport) -> LiseurClient:
    return LiseurClient("http://liseur.test", "token", transport=transport)


class _Stream(httpx.AsyncByteStream):
    """A one-shot async body, so a test response really streams chunk by chunk."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _streamed(
    status: int, chunks: Iterable[bytes], headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, stream=_Stream(chunks), headers=headers or {})


class _RecordingStream(httpx.AsyncByteStream):
    """An async body that counts how many chunks were actually pulled."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self) -> None:
        pass


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


def test_token_info_gets_v1_token_with_the_bearer_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"account_id": "acc-1", "scopes": ["sync"]})

    info = asyncio.run(_client(httpx.MockTransport(handler)).token_info())
    assert info == {"account_id": "acc-1", "scopes": ["sync"]}
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/token"
    assert seen[0].headers["authorization"] == "Bearer token"


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

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).download("b1", max_bytes=1024)
    )
    assert result == b"epub-bytes"


def test_error_body_keeps_at_most_its_budget_from_one_huge_chunk() -> None:
    # A peer is free to hand over one enormous chunk; the truncation must hold
    # regardless (dropping it retained 10 MB of a 10 MB chunk).
    class _OneBigChunk(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"e" * (10 * 1024 * 1024)

        async def aclose(self) -> None:
            pass

    response = httpx.Response(500, stream=_OneBigChunk())
    body = asyncio.run(LiseurClient._error_body(response))
    assert len(body) <= _ERROR_BODY_BYTES


def test_download_refuses_a_body_over_the_cap() -> None:
    cap = 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (cap + 1),
            headers={"content-type": "application/epub+zip"},
        )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=cap))
    assert "1 MiB cap" in str(excinfo.value)


def test_download_returns_a_multi_chunk_body_under_the_cap() -> None:
    payload = b"x" * 250

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            [payload[0:100], payload[100:200], payload[200:250]],
            {"content-type": "application/epub+zip"},
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).download("b1", max_bytes=300)
    )
    assert result == payload


def test_download_accepts_a_body_exactly_at_the_cap() -> None:
    payload = b"y" * 256

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            [payload[0:100], payload[100:200], payload[200:256]],
            {"content-type": "application/epub+zip"},
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).download("b1", max_bytes=256)
    )
    assert result == payload


def test_download_refuses_a_multi_chunk_body_one_byte_over_the_cap() -> None:
    payload = b"z" * 257

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            [payload[0:100], payload[100:200], payload[200:257]],
            {"content-type": "application/epub+zip"},
        )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=256))
    assert "256 byte cap" in str(excinfo.value)


def test_download_streamed_error_still_reports_the_liseur_message() -> None:
    body = b'{"error": "boom"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(500, [body[:5], body[5:]], {"content-type": "application/json"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=1024))
    assert excinfo.value.status == 500
    assert "boom" in str(excinfo.value)


def test_download_refuses_a_compressed_body_on_the_capped_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            [b"x" * 10],
            {"content-type": "application/epub+zip", "content-encoding": "gzip"},
        )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=1024))
    assert "gzip" in str(excinfo.value)


@pytest.mark.parametrize("status", [500, 301])
def test_non_success_compressed_body_is_refused_before_any_chunk_is_pulled(
    status: int,
) -> None:
    stream = _RecordingStream([gzip.compress(b"x" * 4096)])
    headers = {"content-encoding": "gzip", "location": "https://elsewhere.test/v1"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream, headers=headers)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=1024))
    assert "gzip" in str(excinfo.value)
    assert stream.pulled == 0


def test_non_success_huge_body_is_bounded_and_the_stream_is_barely_pulled() -> None:
    chunks = [b"A" * 8192] * (100 * 8)  # 100x the 64 KiB error-body bound
    stream = _RecordingStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=stream, headers={"content-type": "text/plain"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=1024))
    assert excinfo.value.status == 500
    assert "liseur-sync answered 500" in str(excinfo.value)
    # _error_body stops at 64 KiB = 8 chunks; aread() would drain all 800.
    assert stream.pulled <= 8


def test_download_accepts_an_identity_encoded_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            [b"epub-bytes"],
            {"content-type": "application/epub+zip", "content-encoding": "identity"},
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).download("b1", max_bytes=1024)
    )
    assert result == b"epub-bytes"


def test_download_refuses_an_oversized_json_body_by_the_cap_not_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(200, [b"{" * 100], {"content-type": "application/json"})

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=64))
    assert "64 byte cap" in str(excinfo.value)


def test_capped_path_parses_a_small_json_success_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            [b'{"books": ', b'["b1"]}'],
            {"content-type": "application/json"},
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler))._request("GET", "/v1/books", max_bytes=1024)
    )
    assert result == {"books": ["b1"]}


def test_download_streamed_error_body_is_bounded_but_reported() -> None:
    body = b"A" * (200 * 1024)
    chunks = [body[i : i + 8192] for i in range(0, len(body), 8192)]

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(500, chunks, {"content-type": "text/plain"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=1024))
    assert excinfo.value.status == 500
    assert "liseur-sync answered 500" in str(excinfo.value)


@pytest.mark.parametrize("cap", [None, 0, -1])
def test_download_rejects_a_non_positive_cap(cap: int | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=cap))
    assert "positive integer" in str(excinfo.value)


_CAP_ERROR = "max_bytes must be a positive integer number of bytes"


@pytest.mark.parametrize(
    "cap",
    [None, 0, -1, 0.5, float("inf"), float("nan"), "5", True],
    ids=["none", "zero", "negative", "fraction", "inf", "nan", "string", "bool"],
)
def test_download_rejects_an_invalid_cap(cap: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            _client(httpx.MockTransport(handler)).download("b1", max_bytes=cap)  # type: ignore[arg-type]
        )
    assert str(excinfo.value) == _CAP_ERROR


def test_download_error_body_over_the_cap_still_reports_liseur_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LiseurError) as excinfo:
        asyncio.run(_client(httpx.MockTransport(handler)).download("b1", max_bytes=10))
    assert excinfo.value.status == 500
    assert "boom" in str(excinfo.value)


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
