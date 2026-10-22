from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from liseur_mcp.client import LiseurClient

# Import the shipped canary, not a copy of its validation logic.
_spec = importlib.util.spec_from_file_location(
    "live_check", Path(__file__).resolve().parents[1] / "scripts" / "live_check.py"
)
assert _spec is not None and _spec.loader is not None
live_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live_check)


@pytest.fixture(autouse=True)
def _isolate_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_check, "_failures", [])


def _responses() -> dict[str, Any]:
    return {
        "/v1/token": {
            "id": "t1",
            "account_id": "a1",
            "device_id": "d1",
            "name": "test",
            "scopes": ["library-read", "read-insights", "sync"],
            "session_active_ms": 0,
        },
        "/v1/folders": {"folders": [{"folder_id": "f1", "name": "test", "kind": "plain"}]},
        "/v1/folders/f1/books": {"books": []},
        "/v1/insights/summary": {
            "total_active_minutes": 1.5,
            "total_pages": 0.5,
            "sessions": 1,
            "streak_days": 1,
            "speed_prog_per_hour": 0.25,
        },
        "/v1/insights/works": {"works": []},
        "/v1/annotations/changes": {"annotations": [], "high_water": 0, "has_more": False},
    }


def _client(responses: dict[str, Any], seen: list[httpx.Request]) -> LiseurClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = responses[request.url.path]
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload)
        # Explicit JSON also lets malformed null/scalar envelopes reach the real client.
        return httpx.Response(
            200,
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )

    return LiseurClient("http://liseur.test", "token", transport=httpx.MockTransport(handler))


def _run_checks(responses: dict[str, Any]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def run() -> None:
        client = _client(responses, seen)
        try:
            await live_check.run_checks(client)
        finally:
            await client.aclose()

    asyncio.run(run())
    return seen


def test_run_checks_rejects_a_missing_works_list() -> None:
    responses = _responses()
    responses["/v1/insights/works"] = {}

    seen = _run_checks(responses)

    assert live_check._failures == [
        "GET /v1/insights/works carries an object list under works — NoneType"
    ]
    assert [
        request.url.params["range"] for request in seen if "/insights/" in request.url.path
    ] == ["30d", "30d"]


def test_run_checks_accepts_an_explicit_empty_works_list() -> None:
    _run_checks(_responses())

    assert live_check._failures == []


def _book_responses() -> dict[str, Any]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "content.opf",
            '<package><manifest><item id="c1" href="c1.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        archive.writestr("c1.xhtml", "<html><body><p>Test.</p></body></html>")
    return {
        **_responses(),
        "/v1/folders/f1/books": {"books": [{"book_id": "b1", "title": "test"}]},
        "/v1/folders/f1/search": {"books": []},
        "/v1/books/b1": {"book_id": "b1"},
        "/v1/books/b1/resolve": {"confidence": "low"},
        "/v1/books/b1/download": buffer.getvalue(),
    }


def test_run_checks_rejects_a_missing_search_books_list() -> None:
    responses = _book_responses()
    responses["/v1/folders/f1/search"] = {}

    _run_checks(responses)

    assert live_check._failures == [
        "the search answer carries an object list under books — NoneType"
    ]


def test_run_checks_accepts_an_explicit_empty_search_books_list() -> None:
    _run_checks(_book_responses())

    assert live_check._failures == []


SUMMARY_FIELDS = (
    "total_active_minutes",
    "total_pages",
    "sessions",
    "streak_days",
    "speed_prog_per_hour",
)


@pytest.mark.parametrize("field", SUMMARY_FIELDS)
def test_run_checks_rejects_a_missing_summary_number(field: str) -> None:
    responses = _responses()
    del responses["/v1/insights/summary"][field]

    _run_checks(responses)

    assert len(live_check._failures) == 1
    assert live_check._failures[0].startswith(f"summary {field} is ")


@pytest.mark.parametrize("field", SUMMARY_FIELDS)
@pytest.mark.parametrize("value", [None, "1", True, [], {}])
def test_run_checks_rejects_a_non_numeric_summary_field(field: str, value: Any) -> None:
    responses = _responses()
    responses["/v1/insights/summary"][field] = value

    _run_checks(responses)

    assert len(live_check._failures) == 1
    assert live_check._failures[0].startswith(f"summary {field} is ")


@pytest.mark.parametrize("field", ["sessions", "streak_days"])
def test_run_checks_rejects_a_fractional_summary_count(field: str) -> None:
    responses = _responses()
    responses["/v1/insights/summary"][field] = 1.5

    _run_checks(responses)

    assert live_check._failures == [f"summary {field} is an integer — float"]


def test_run_checks_accepts_a_zero_summary() -> None:
    responses = _responses()
    responses["/v1/insights/summary"] = dict.fromkeys(SUMMARY_FIELDS, 0)

    _run_checks(responses)

    assert live_check._failures == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        123,
        [],
        {},
        "",
        "2026-02-30",
        "2026-13-01",
        "2026-1-01",
        "2026-10-22T00:00:00Z",
        "20261022",
        "2026-W43-4",
        "2026-10-22 ",
    ],
)
def test_run_checks_rejects_an_invalid_optional_first_read_day(value: Any) -> None:
    responses = _responses()
    responses["/v1/insights/works"]["works"] = [
        {"work_id": "w1"},
        {"work_id": "w2", "first_read_day": value},
    ]

    _run_checks(responses)

    assert live_check._failures == ["every optional first_read_day is a YYYY-MM-DD date"]


@pytest.mark.parametrize("value", ["2024-02-29", "2026-10-22"])
def test_run_checks_accepts_a_valid_optional_first_read_day(value: str) -> None:
    responses = _responses()
    responses["/v1/insights/works"]["works"] = [
        {"work_id": "w1", "first_read_day": value},
        {"work_id": "w2"},
    ]

    _run_checks(responses)

    assert live_check._failures == []


def test_run_checks_accepts_legacy_work_rows_without_first_read_day() -> None:
    responses = _responses()
    responses["/v1/insights/works"]["works"] = [{"work_id": "w1"}]

    _run_checks(responses)

    assert live_check._failures == []


@pytest.mark.parametrize("payload", [None, [], "invalid", 0])
def test_run_checks_reports_non_object_envelopes_without_aborting(payload: Any) -> None:
    responses = _book_responses()
    responses["/v1/folders/f1/search"] = payload
    responses["/v1/insights/summary"] = payload
    responses["/v1/insights/works"] = payload

    seen = _run_checks(responses)

    assert live_check._failures == [
        "GET /v1/folders/{id}/search is an object",
        f"GET /v1/insights/summary is an object — {type(payload).__name__}",
        f"GET /v1/insights/works is an object — {type(payload).__name__}",
    ]
    assert seen[-1].url.path == "/v1/annotations/changes"


@pytest.mark.parametrize(
    ("endpoint", "field", "description"),
    [
        (
            "/v1/insights/works",
            "works",
            "GET /v1/insights/works carries an object list under works",
        ),
        (
            "/v1/folders/f1/search",
            "books",
            "the search answer carries an object list under books",
        ),
    ],
)
@pytest.mark.parametrize("payload", [None, {}, [None], ["invalid"]])
def test_run_checks_rejects_malformed_collection_contents(
    endpoint: str,
    field: str,
    description: str,
    payload: Any,
) -> None:
    responses = _book_responses()
    responses[endpoint] = {field: payload}

    _run_checks(responses)

    assert live_check._failures == [f"{description} — {type(payload).__name__}"]


def test_run_checks_reports_every_missing_summary_number() -> None:
    responses = _responses()
    responses["/v1/insights/summary"] = {}

    seen = _run_checks(responses)

    assert len(live_check._failures) == len(SUMMARY_FIELDS)
    assert all(
        any(f"summary {field} is " in message for message in live_check._failures)
        for field in SUMMARY_FIELDS
    )
    assert seen[-1].url.path == "/v1/annotations/changes"


@pytest.mark.parametrize(
    ("endpoint", "payload", "exit_code"),
    [
        ("/v1/insights/works", {"works": []}, 0),
        ("/v1/insights/works", {}, 1),
        ("/v1/insights/summary", {}, 1),
        ("/v1/insights/works", {"works": [{"first_read_day": "invalid"}]}, 1),
    ],
)
def test_main_returns_a_shape_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    payload: Any,
    exit_code: int,
) -> None:
    responses = _responses()
    responses[endpoint] = payload
    seen: list[httpx.Request] = []
    client = _client(responses, seen)
    monkeypatch.setenv("LISEUR_URL", "http://liseur.test")
    monkeypatch.setenv("LISEUR_TOKEN", "token")
    # Only substitute connection setup: main still runs the real client and checks.
    monkeypatch.setattr(live_check, "LiseurClient", lambda url, token, timeout: client)

    assert asyncio.run(live_check.main()) == exit_code
    assert client._http.is_closed
