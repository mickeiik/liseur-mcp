"""ASGI bearer authentication for the streamable-http transport."""

from __future__ import annotations

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    """Reject requests to the MCP endpoint without the shared bearer token.

    Only HTTP requests are inspected; the lifespan scope passes through so
    the MCP session manager still starts.
    """

    def __init__(self, app: ASGIApp, token: str, mcp_path: str = "/mcp") -> None:
        self.app = app
        self._expected = token.encode("utf-8")
        self._path = mcp_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == self._path or path.startswith(self._path + "/"):
                headers = dict(scope.get("headers", []))
                authorization = headers.get(b"authorization", b"")
                scheme, separator, supplied = authorization.partition(b" ")
                # Always execute compare_digest, including malformed or missing credentials.
                token_matches = hmac.compare_digest(supplied, self._expected)
                if not (separator == b" " and scheme.lower() == b"bearer" and token_matches):
                    response = JSONResponse(
                        {"error": "authentication_failed"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
