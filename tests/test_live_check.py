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


def _work(**fields: Any) -> dict[str, Any]:
    return {
        "work_id": "w1",
        "sessions": 1,
        "total_active_minutes": 1.5,
        "total_pages": 0.5,
        "current_progression": 0.25,
        "eta_seconds": None,
        "last_read_at": None,
        **fields,
    }


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
            content_type = (
                "application/epub+zip" if request.url.path.endswith("/download")
                else "application/json"
            )
            return httpx.Response(200, content=payload, headers={"content-type": content_type})
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


@pytest.mark.parametrize("field", ["total_active_minutes", "total_pages", "speed_prog_per_hour"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e309", "-1e309"])
def test_run_checks_rejects_non_finite_summary_numbers_from_raw_json(
    field: str, constant: str, capsys: pytest.CaptureFixture[str],
) -> None:
    responses = _responses()
    summary = responses["/v1/insights/summary"]
    summary[field] = "RAW_NUMBER"
    responses["/v1/insights/summary"] = (
        json.dumps(summary).replace('"RAW_NUMBER"', constant).encode()
    )

    seen = _run_checks(responses)

    assert live_check._failures == [f"summary {field} is a number — float"]
    assert constant not in capsys.readouterr().out
    assert seen[-1].url.path == "/v1/annotations/changes"


@pytest.mark.parametrize("value", [10**400, -(10**400), -1.25, 0, 1.5])
def test_run_checks_accepts_finite_numbers_without_new_range_limits(value: int | float) -> None:
    responses = _responses()
    for field in ("total_active_minutes", "total_pages", "speed_prog_per_hour"):
        responses["/v1/insights/summary"][field] = value

    _run_checks(responses)

    assert live_check._failures == []


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
        _work(),
        _work(work_id="w2", first_read_day=value),
    ]

    _run_checks(responses)

    assert live_check._failures == [
        f"works[1] first_read_day is a YYYY-MM-DD date — {type(value).__name__}"
    ]


@pytest.mark.parametrize("value", ["2024-02-29", "2026-10-22"])
def test_run_checks_accepts_a_valid_optional_first_read_day(value: str) -> None:
    responses = _responses()
    responses["/v1/insights/works"]["works"] = [
        _work(first_read_day=value),
        _work(work_id="w2"),
    ]

    _run_checks(responses)

    assert live_check._failures == []


def test_run_checks_accepts_legacy_work_rows_without_first_read_day() -> None:
    responses = _responses()
    responses["/v1/insights/works"]["works"] = [_work()]

    _run_checks(responses)

    assert live_check._failures == []


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("/v1/folders", {"folders": []}),
        ("/v1/folders", {}),
        ("/v1/folders", None),
        ("/v1/folders", []),
        ("/v1/folders", "PRIVATE_SHAPE"),
        ("/v1/folders", {"folders": None}),
        ("/v1/folders", {"folders": [None]}),
        ("/v1/folders", {"folders": "PRIVATE_SHAPE"}),
        ("/v1/folders", {"folders": [{"name": "PRIVATE_SHAPE", "kind": "plain"}]}),
        ("/v1/folders", {"folders": [{"folder_id": 1, "name": "x", "kind": "plain"}]}),
        ("/v1/folders/f1/books", {"books": []}),
        ("/v1/folders/f1/books", {}),
        ("/v1/folders/f1/books", None),
        ("/v1/folders/f1/books", []),
        ("/v1/folders/f1/books", "PRIVATE_SHAPE"),
        ("/v1/folders/f1/books", {"books": None}),
        ("/v1/folders/f1/books", {"books": [None]}),
        ("/v1/folders/f1/books", {"books": "PRIVATE_SHAPE"}),
        ("/v1/folders/f1/books", {"books": [{}]}),
        ("/v1/folders/f1/books", {"books": [{"book_id": None}]}),
    ],
)
def test_run_checks_keeps_independent_checks_after_empty_or_malformed_catalog_shapes(
    endpoint: str, payload: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    responses = _responses()
    responses[endpoint] = payload
    responses["/v1/insights/summary"] = {}
    responses["/v1/insights/works"] = {}
    responses["/v1/annotations/changes"]["annotations"] = [{"seq": 1}]

    try:
        seen = _run_checks(responses)
    except (AttributeError, KeyError, TypeError) as exc:
        pytest.fail(f"catalog shape must not abort independent checks: {type(exc).__name__}")

    assert [request.url.path for request in seen[-3:]] == [
        "/v1/insights/summary", "/v1/insights/works", "/v1/annotations/changes",
    ]
    for field in SUMMARY_FIELDS:
        assert any(f"summary {field} is " in message for message in live_check._failures)
    assert "GET /v1/insights/works carries an object list under works — NoneType" in (
        live_check._failures
    )
    assert any("annotations/changes" in message for message in live_check._failures)
    assert "PRIVATE_SHAPE" not in capsys.readouterr().out


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

    if endpoint == "/v1/insights/works" and isinstance(payload, list):
        assert live_check._failures == [f"works[0] is an object — {type(payload[0]).__name__}"]
    else:
        assert live_check._failures == [f"{description} — {type(payload).__name__}"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("work_id", "a string"), ("sessions", "an integer"),
        ("total_active_minutes", "a number"), ("total_pages", "a number"),
        ("current_progression", "a number"), ("eta_seconds", "a number or null"),
        ("last_read_at", "a string or null"),
    ],
)
def test_run_checks_rejects_missing_required_work_fields_on_late_rows(
    field: str, expected: str,
) -> None:
    responses = _responses()
    works = [_work(work_id=f"w{i}") for i in range(51)]
    del works[50][field]
    responses["/v1/insights/works"] = {"works": works}

    seen = _run_checks(responses)

    assert live_check._failures == [f"works[50] {field} is {expected} — missing"]
    assert seen[-1].url.path == "/v1/annotations/changes"


@pytest.mark.parametrize(
    "field", ["total_active_minutes", "total_pages", "current_progression", "eta_seconds"],
)
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_run_checks_rejects_non_finite_work_numbers_from_raw_json(
    field: str, constant: str,
) -> None:
    responses = _responses()
    works = [_work(work_id=f"w{i}") for i in range(51)]
    works[50][field] = "RAW_NUMBER"
    responses["/v1/insights/works"] = (
        json.dumps({"works": works}).replace('"RAW_NUMBER"', constant).encode()
    )

    seen = _run_checks(responses)

    expected = "a number or null" if field == "eta_seconds" else "a number"
    assert live_check._failures == [f"works[50] {field} is {expected} — float"]
    assert seen[-1].url.path == "/v1/annotations/changes"


@pytest.mark.parametrize("field", ["title", "author"])
def test_run_checks_rejects_null_optional_work_strings(field: str) -> None:
    responses = _responses()
    responses["/v1/insights/works"] = {"works": [_work(**{field: None})]}

    seen = _run_checks(responses)

    assert live_check._failures == [f"works[0] {field} is a string — NoneType"]
    assert seen[-1].url.path == "/v1/annotations/changes"


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
        ("/v1/insights/works", {"works": [_work(first_read_day="invalid")]}, 1),
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
