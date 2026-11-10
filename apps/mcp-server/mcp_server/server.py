import asyncio
import html
import json
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from sharepoint_client import auth, config, graph

mcp = FastMCP(
    "sharepoint-knowledge-agent",
    host="127.0.0.1",
    port=config.PORT,
    streamable_http_path="/api/mcp",
)


def _session_id(ctx: Context) -> str:
    request = ctx.request_context.request
    sid = request.headers.get("mcp-session-id") if request is not None else None
    if not sid:
        raise RuntimeError("No MCP session")
    return sid


async def _as_user(ctx: Context, call: Callable[[str], Awaitable[Any]]) -> str:
    """Runs `call` with the session user's Graph token and returns the result as JSON text."""
    token = await asyncio.to_thread(auth.get_access_token, _session_id(ctx))
    return json.dumps(await call(token), indent=2)


@mcp.tool()
async def sharepoint_login(ctx: Context) -> str:
    """Sign in to Microsoft Entra ID. Returns a URL the user must open in a browser."""
    sid = _session_id(ctx)
    if auth.is_signed_in(sid):
        return "Already signed in."
    url = await asyncio.to_thread(auth.start_login, sid)
    return f"Open this URL to sign in, then retry your request:\n{url}"


@mcp.tool()
async def sharepoint_logout(ctx: Context) -> str:
    """Forget the Microsoft sign-in for this session."""
    auth.sign_out(_session_id(ctx))
    return "Signed out."


@mcp.tool()
async def sharepoint_whoami(ctx: Context) -> str:
    """Show the signed-in Microsoft user for this session."""
    return await _as_user(ctx, graph.me)


@mcp.tool()
async def sharepoint_search(ctx: Context, query: str, limit: int = 10) -> str:
    """Full-text search across SharePoint/OneDrive files and list items the user can access."""
    return await _as_user(ctx, lambda t: graph.search_content(t, query, max(1, min(limit, 25))))


@mcp.tool()
async def sharepoint_find_sites(ctx: Context, query: str = "*") -> str:
    """Find SharePoint sites by keyword."""
    return await _as_user(ctx, lambda t: graph.find_sites(t, query))


@mcp.tool()
async def sharepoint_list_drives(ctx: Context, site_id: str) -> str:
    """List document libraries in a site."""
    return await _as_user(ctx, lambda t: graph.list_drives(t, site_id))


@mcp.tool()
async def sharepoint_list_items(ctx: Context, drive_id: str, item_id: str | None = None) -> str:
    """List files/folders in a document library, optionally within a folder."""
    return await _as_user(ctx, lambda t: graph.list_children(t, drive_id, item_id))


@mcp.tool()
async def sharepoint_read_file(ctx: Context, drive_id: str, item_id: str, max_bytes: int = 100_000) -> str:
    """Read a text-based file (txt, md, csv, json, html, ...) by drive_id and item_id."""
    return await _as_user(ctx, lambda t: graph.read_file_text(t, drive_id, item_id, max(1, min(max_bytes, 1_000_000))))


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> Response:
    params = request.query_params
    if "error" in params:
        auth.cancel_login(params.get("state", ""))
        detail = html.escape(f"{params['error']}: {params.get('error_description', '')}")
        return HTMLResponse(f"Sign-in failed: {detail}", 400)
    try:
        user = await asyncio.to_thread(auth.complete_login, dict(params))
    except Exception as e:  # surfaced to the user in the browser
        return HTMLResponse(f"Sign-in failed: {html.escape(str(e))}", 400)
    return HTMLResponse(f"<h3>Signed in as {html.escape(user)}</h3><p>You can close this tab and return to your MCP client.</p>")


def main() -> None:
    print(f"MCP server:     {config.BASE_URL}/api/mcp")
    print(f"OAuth callback: {config.REDIRECT_URI}")
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=config.PORT)


if __name__ == "__main__":
    main()
