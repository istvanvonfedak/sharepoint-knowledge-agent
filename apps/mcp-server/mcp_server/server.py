import asyncio
import html
import json
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from sharepoint_client import auth, config, graph

from .oauth_provider import EntraProxyProvider

MCP_URL = f"{config.BASE_URL}/api/mcp"
provider = EntraProxyProvider(MCP_URL)

mcp = FastMCP(
    "sharepoint-knowledge-agent",
    host="127.0.0.1",
    port=config.PORT,
    streamable_http_path="/api/mcp",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=config.BASE_URL,
        resource_server_url=MCP_URL,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


def _user_id() -> str:
    token = get_access_token()
    if token is None or token.subject is None:
        raise auth.NotSignedInError()
    return token.subject


async def _as_user(call: Callable[[str], Awaitable[Any]]) -> str:
    """Runs `call` with the signed-in user's Graph token and returns the result as JSON text."""
    token = await asyncio.to_thread(auth.get_access_token, _user_id())
    return json.dumps(await call(token), indent=2)


@mcp.tool()
async def sharepoint_logout() -> str:
    """Forget the Microsoft sign-in; the client must re-authenticate before the next call."""
    token = get_access_token()
    if token is None:
        return "Not signed in."
    if not await provider.sign_out(token):
        return "Signed out this client. The Microsoft session was kept because another client is still connected."
    return "Signed out."


@mcp.tool()
async def sharepoint_whoami() -> str:
    """Show the signed-in Microsoft user for this session."""
    return await _as_user(graph.me)


@mcp.tool()
async def sharepoint_search(query: str, limit: int = 10) -> str:
    """Full-text search across SharePoint/OneDrive files and list items the user can access."""
    return await _as_user(lambda t: graph.search_content(t, query, max(1, min(limit, 25))))


@mcp.tool()
async def sharepoint_find_sites(query: str = "*") -> str:
    """Find SharePoint sites by keyword."""
    return await _as_user(lambda t: graph.find_sites(t, query))


@mcp.tool()
async def sharepoint_list_drives(site_id: str) -> str:
    """List document libraries in a site."""
    return await _as_user(lambda t: graph.list_drives(t, site_id))


@mcp.tool()
async def sharepoint_list_items(drive_id: str, item_id: str | None = None) -> str:
    """List files/folders in a document library, optionally within a folder."""
    return await _as_user(lambda t: graph.list_children(t, drive_id, item_id))


@mcp.tool()
async def sharepoint_read_file(drive_id: str, item_id: str, max_bytes: int = 100_000) -> str:
    """Read a text-based file (txt, md, csv, json, html, ...) by drive_id and item_id."""
    return await _as_user(lambda t: graph.read_file_text(t, drive_id, item_id, max(1, min(max_bytes, 1_000_000))))


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> Response:
    try:
        redirect_url = await provider.finish_authorization(dict(request.query_params))
    except Exception as e:  # surfaced to the user in the browser
        return HTMLResponse(f"Sign-in failed: {html.escape(str(e))}", 400)
    return RedirectResponse(redirect_url, status_code=302)


def main() -> None:
    print(f"MCP server:     {config.BASE_URL}/api/mcp")
    print(f"OAuth callback: {config.REDIRECT_URI}")
    uvicorn.run(mcp.streamable_http_app(), host=config.BIND_HOST, port=config.PORT)


if __name__ == "__main__":
    main()
