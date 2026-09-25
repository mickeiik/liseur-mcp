from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx
import pytest

from liseur_mcp.__main__ import _check_token, verify_token
from liseur_mcp.client import LiseurClient, LiseurError
from liseur_mcp.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> LiseurClient:
    return LiseurClient("http://liseur.test", "token", transport=httpx.MockTransport(handler))


def test_verify_token_logs_identity_and_scopes(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/token"
        return httpx.Response(
            200,
            json={
                "id": "t1",
                "account_id": "acc-1",
                "device_id": "dev-1",
                "name": "my laptop",
                "scopes": ["library-read", "read-insights", "sync"],
                "session_active_ms": 1,
            },
        )

    with caplog.at_level(logging.INFO):
        asyncio.run(verify_token(_client(handler)))

    assert "acc-1" in caplog.text
    assert "dev-1" in caplog.text
    assert "my laptop" in caplog.text
    assert caplog.records[-1].levelno == logging.INFO


def test_verify_token_warns_about_a_missing_scope(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"account_id": "acc-1", "scopes": ["library-read", "sync"]},
        )

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "read-insights" in caplog.text
    assert "reading_stats" in caplog.text


def test_verify_token_skips_the_scope_warning_without_a_scope_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"account_id": "acc-1"})

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    assert caplog.records == []


@pytest.mark.parametrize("status", [401, 403])
def test_verify_token_exits_when_the_token_is_refused(status: int) -> None:
    detail = "https required" if status == 403 else "refused"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": detail})

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(verify_token(_client(handler)))

    message = str(excinfo.value)
    assert str(status) in message
    assert detail in message  # a 403 such as "https required" names its own cause
    assert "LISEUR_TOKEN" in message


def test_verify_token_warns_about_library_read_naming_the_book_scoped_branch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scopes": ["sync", "read-insights"]})

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    assert "library-read" in caplog.text
    assert "list_folders" in caplog.text
    # Account-wide list_highlights only needs sync; say which branch does not.
    assert "list_highlights (book_id=...)" in caplog.text


def test_verify_token_warns_and_continues_on_a_malformed_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "application/json"}
        )

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_verify_token_warns_when_the_answer_is_not_an_object(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A proxy login page, say: a 200 the client cannot read as a token.
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bytes" in caplog.text


def test_check_token_closes_its_client_on_success_and_on_a_refused_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class FakeClient:
        def __init__(self, base_url: str, token: str, timeout: float) -> None:
            pass

        async def token_info(self) -> dict[str, object]:
            return {"scopes": ["library-read", "read-insights", "sync"]}

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr("liseur_mcp.__main__.LiseurClient", FakeClient)
    settings = Settings(LISEUR_URL="http://liseur.test", LISEUR_TOKEN="token")
    asyncio.run(_check_token(settings))

    class RefusingClient(FakeClient):
        async def token_info(self) -> dict[str, object]:
            raise LiseurError(401, "liseur-sync answered 401: refused")

    monkeypatch.setattr("liseur_mcp.__main__.LiseurClient", RefusingClient)
    with pytest.raises(SystemExit):
        asyncio.run(_check_token(settings))

    assert closed == [True, True]


def test_verify_token_warns_and_continues_on_a_server_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "unwell"})

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "500" in caplog.text


def test_verify_token_warns_and_continues_when_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with caplog.at_level(logging.WARNING):
        asyncio.run(verify_token(_client(handler)))

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "connect" in caplog.text.lower()
