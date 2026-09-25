from __future__ import annotations

import asyncio
import importlib.metadata
from typing import Any

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from liseur_mcp.auth import BearerAuthMiddleware
from liseur_mcp.client import LiseurClient
from liseur_mcp.config import Settings
from liseur_mcp.server import create_http_app, create_server

TOKEN = "s3cret-token"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


class _Recorder:
    """A wrapped ASGI app that records whether it was ever reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        await JSONResponse({"ok": True})(scope, receive, send)


def _scope(
    path: str, headers: list[tuple[bytes, bytes]] | None = None, root_path: str = ""
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": root_path,
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }


def _send(app: Any, scope: dict[str, Any]) -> tuple[int, dict[bytes, bytes]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def _middleware(recorder: _Recorder) -> BearerAuthMiddleware:
    return BearerAuthMiddleware(recorder, TOKEN)


def test_missing_authorization_is_401_and_never_reaches_the_app() -> None:
    recorder = _Recorder()
    status, headers = _send(_middleware(recorder), _scope("/mcp"))
    assert status == 401
    assert headers[b"www-authenticate"] == b"Bearer"
    assert recorder.calls == 0


def test_wrong_token_is_401() -> None:
    recorder = _Recorder()
    status, _ = _send(
        _middleware(recorder),
        _scope("/mcp", [(b"authorization", b"Bearer not-the-token")]),
    )
    assert status == 401
    assert recorder.calls == 0


def test_correct_bearer_reaches_the_app() -> None:
    recorder = _Recorder()
    status, _ = _send(
        _middleware(recorder),
        _scope("/mcp", [(b"authorization", f"Bearer {TOKEN}".encode())]),
    )
    assert status == 200
    assert recorder.calls == 1


def test_root_path_prefixed_mcp_requires_auth() -> None:
    recorder = _Recorder()
    status, _ = _send(
        _middleware(recorder),
        _scope("/liseur/mcp", root_path="/liseur"),
    )
    assert status == 401
    assert recorder.calls == 0


def test_mcp_subpath_requires_auth() -> None:
    recorder = _Recorder()
    status, _ = _send(_middleware(recorder), _scope("/mcp/anything"))
    assert status == 401
    assert recorder.calls == 0


def test_unrelated_path_passes_through() -> None:
    recorder = _Recorder()
    status, _ = _send(_middleware(recorder), _scope("/healthz"))
    assert status == 200
    assert recorder.calls == 1


def test_unrelated_path_under_a_root_path_also_passes_through() -> None:
    # Only the MCP route is gated: a prefix must not turn every path into it.
    recorder = _Recorder()
    status, _ = _send(_middleware(recorder), _scope("/liseur/healthz", root_path="/liseur"))
    assert status == 200
    assert recorder.calls == 1


def _real_app() -> BearerAuthMiddleware:
    settings = Settings(
        LISEUR_URL="http://liseur.test",
        LISEUR_TOKEN="token",
        MCP_TRANSPORT="streamable-http",
        MCP_AUTH_TOKEN=TOKEN,
    )
    client = LiseurClient(
        "http://liseur.test",
        "token",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    mcp = create_server(client)
    return BearerAuthMiddleware(create_http_app(mcp, settings), TOKEN, settings.mcp_path)


def test_empty_token_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        BearerAuthMiddleware(_Recorder(), "")


def test_real_server_wiring_rejects_unauthenticated_and_accepts_bearer() -> None:
    app = _real_app()
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        unauthenticated = test_client.post(
            "/mcp", json=INITIALIZE, headers={"Accept": "application/json, text/event-stream"}
        )
        assert unauthenticated.status_code == 401

        authenticated = test_client.post(
            "/mcp",
            json=INITIALIZE,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert authenticated.status_code == 200
        assert authenticated.json()["result"]["serverInfo"]["name"] == "liseur"
        # 2.x lets the server state its own version; it must state ours, not the SDK's.
        assert (
            authenticated.json()["result"]["serverInfo"]["version"]
            == importlib.metadata.version("liseur-mcp")
        )


def _http_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "LISEUR_URL": "http://liseur.test",
        "LISEUR_TOKEN": "token",
        "MCP_TRANSPORT": "streamable-http",
        "MCP_AUTH_TOKEN": TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


def _http_app(settings: Settings) -> BearerAuthMiddleware:
    client = LiseurClient(
        "http://liseur.test",
        "token",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    mcp = create_server(client)
    return BearerAuthMiddleware(create_http_app(mcp, settings), TOKEN, settings.mcp_path)


def test_origin_is_refused_by_default_and_accepted_when_allowed() -> None:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Origin": "http://client.test",
    }

    with TestClient(_http_app(_http_settings()), base_url="http://127.0.0.1") as client:
        refused = client.post("/mcp", json=INITIALIZE, headers=headers)
    assert refused.status_code == 403

    allowed_app = _http_app(_http_settings(MCP_ALLOWED_ORIGINS="http://client.test"))
    with TestClient(allowed_app, base_url="http://127.0.0.1") as client:
        allowed = client.post("/mcp", json=INITIALIZE, headers=headers)
    assert allowed.status_code == 200


def test_real_server_wiring_rejects_unauthenticated_with_root_path() -> None:
    app = _real_app()
    with TestClient(app, base_url="http://127.0.0.1", root_path="/liseur") as test_client:
        response = test_client.post(
            "/liseur/mcp",
            json=INITIALIZE,
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 401

        # The prefix must not lock the legitimate client out either: a fix that
        # denied every prefixed request would pass the assertion above.
        authenticated = test_client.post(
            "/liseur/mcp",
            json=INITIALIZE,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert authenticated.status_code == 200
        assert authenticated.json()["result"]["serverInfo"]["name"] == "liseur"
