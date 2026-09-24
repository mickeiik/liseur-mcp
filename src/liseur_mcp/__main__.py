from __future__ import annotations

import logging

import uvicorn

from liseur_mcp.auth import BearerAuthMiddleware
from liseur_mcp.client import LiseurClient
from liseur_mcp.config import Settings
from liseur_mcp.server import create_server


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from the environment
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = LiseurClient(
        settings.liseur_url,
        settings.token.get_secret_value(),
        settings.request_timeout,
    )
    mcp = create_server(client, settings)
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
        return
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(),
        settings.auth_token.get_secret_value(),
        settings.mcp_path,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
