from __future__ import annotations

import asyncio
import logging

import httpx
import uvicorn

from liseur_mcp.auth import BearerAuthMiddleware
from liseur_mcp.client import LiseurClient, LiseurError
from liseur_mcp.config import Settings
from liseur_mcp.server import create_http_app, create_server

logger = logging.getLogger(__name__)

# Which tools need which scope; the startup check names the victims of a token
# that is missing one.
SCOPES_TO_TOOLS = {
    "library-read": (
        "list_folders",
        "list_books",
        "search_books",
        "get_book",
        "get_book_text",
        "list_highlights (book_id=...)",
    ),
    "read-insights": ("reading_stats",),
    "sync": ("list_highlights",),
}


async def verify_token(client: LiseurClient) -> None:
    """Draw the interface from one GET /v1/token, before the server starts.

    A refused credential (401/403) is fatal: every tool would fail, so say why
    on stderr and exit. An unreachable instance is not: warn and let the server
    start, because the smoke test and the Docker check point at a dead port and
    the tools report the real reason when they are called.
    """
    try:
        info = await client.token_info()
    except LiseurError as exc:
        if exc.status in (401, 403):
            # Keep exc's own text: a 403 can be a live credential refused for
            # another reason, such as the instance's "https required".
            raise SystemExit(
                f"liseur-sync refused the device token: {exc}. If the credential is "
                "absent, revoked or expired, mint a new one and update LISEUR_TOKEN "
                "or LISEUR_TOKEN_FILE."
            ) from exc
        logger.warning("could not check the device token (%s); continuing", exc)
        return
    except (httpx.HTTPError, ValueError) as exc:
        # A transport failure is not fatal, and neither is a malformed answer:
        # ValueError covers what the JSON and text decoders raise on a broken
        # 200 body.
        logger.warning("could not check the device token (%s); continuing", exc)
        return
    if not isinstance(info, dict):
        logger.warning(
            "GET /v1/token answered %s instead of an object; continuing",
            type(info).__name__,
        )
        return
    logger.info(
        "liseur-sync token: account_id=%s device_id=%s name=%s scopes=%s",
        info.get("account_id"),
        info.get("device_id"),
        info.get("name"),
        info.get("scopes"),
    )
    scopes = info.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        return
    missing = [scope for scope in SCOPES_TO_TOOLS if scope not in scopes]
    if missing:
        tools = sorted({tool for scope in missing for tool in SCOPES_TO_TOOLS[scope]})
        logger.warning(
            "device token is missing scope(s) %s; these tools will fail until it is re-minted: %s",
            ", ".join(missing),
            ", ".join(tools),
        )


async def _check_token(settings: Settings) -> None:
    """Run verify_token on a throwaway client with its own event loop.

    The client handed to create_server() is used later under a different event
    loop (the MCP SDK owns it for stdio, uvicorn for HTTP), so a pooled httpx
    connection opened here must not be carried across; this client is closed in
    the finally below and the server builds its own afterwards.
    """
    client = LiseurClient(
        settings.liseur_url,
        settings.token.get_secret_value(),
        settings.request_timeout,
    )
    try:
        await verify_token(client)
    finally:
        await client.aclose()


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from the environment
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_check_token(settings))
    client = LiseurClient(
        settings.liseur_url,
        settings.token.get_secret_value(),
        settings.request_timeout,
    )
    mcp = create_server(client)
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
        return
    app = BearerAuthMiddleware(
        create_http_app(mcp, settings),
        settings.auth_token.get_secret_value(),
        settings.mcp_path,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
