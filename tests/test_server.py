from __future__ import annotations

import asyncio

import httpx

from liseur_mcp.client import LiseurClient
from liseur_mcp.config import Settings
from liseur_mcp.server import create_server

TOOL_NAMES = {
    "list_folders",
    "list_books",
    "search_books",
    "get_book",
    "reading_stats",
    "list_highlights",
    "get_book_text",
}


def _settings() -> Settings:
    return Settings(LISEUR_URL="http://liseur.test", LISEUR_TOKEN="token")


def test_server_registers_the_read_only_tool_surface() -> None:
    client = LiseurClient(
        "http://liseur.test",
        "token",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    mcp = create_server(client, _settings())
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == TOOL_NAMES


def test_http_transport_requires_an_auth_token() -> None:
    try:
        Settings(
            LISEUR_URL="http://liseur.test",
            LISEUR_TOKEN="token",
            MCP_TRANSPORT="streamable-http",
        )
    except ValueError as exc:
        assert "MCP_AUTH_TOKEN" in str(exc)
    else:
        raise AssertionError("streamable-http must refuse to start without MCP_AUTH_TOKEN")
