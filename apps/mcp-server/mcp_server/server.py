import asyncio
import base64
import hashlib
import html
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sharepoint_rag import acl, db, metrics
from sharepoint_rag.retriever import PostgresRetriever
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from sharepoint_client import auth, config, graph

from . import settings
from .oauth_provider import EntraProxyProvider
from .usage import RateLimited, Usage

log = logging.getLogger(__name__)

MCP_URL = f"{config.BASE_URL}/api/mcp"
if not settings.TOKEN_ENCRYPTION_KEY:
    raise RuntimeError(
        "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
        "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
    )


def _derived_key(info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(base64.urlsafe_b64decode(settings.TOKEN_ENCRYPTION_KEY))


pool = db.create_pool(min_size=1, max_size=20)
entra = auth.EntraAuth(pool, settings.TOKEN_ENCRYPTION_KEY)
usage = Usage(pool, settings.AUDIT_HMAC_KEY.encode() if settings.AUDIT_HMAC_KEY else _derived_key(b"audit-hmac"))
provider = EntraProxyProvider(
    pool, entra, MCP_URL, allowed_redirect_uris=settings.ALLOWED_REDIRECT_URIS, max_clients=settings.MAX_CLIENTS
)
retriever = PostgresRetriever(pool)

_public = urlparse(config.BASE_URL)
mcp = FastMCP(
    "sharepoint-knowledge-agent",
    host="127.0.0.1",
    port=config.PORT,
    streamable_http_path="/api/mcp",
    stateless_http=settings.STATELESS,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=config.BASE_URL,
        resource_server_url=MCP_URL,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
    # DNS-rebinding protection: only our own host names may reach the server.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", _public.netloc],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", f"{_public.scheme}://{_public.netloc}"],
    ),
)


def _user_id() -> str:
    token = get_access_token()
    if token is None or token.subject is None:
        raise auth.NotSignedInError()
    return token.subject


async def _graph_token(user_id: str) -> str:
    return await entra.get_access_token(user_id)


async def _as_user(call: Callable[[str], Awaitable[Any]]) -> str:
    """Runs `call` with the signed-in user's Graph token and returns the result as JSON text."""
    return json.dumps(await call(await _graph_token(_user_id())), indent=2)


# ---- permissions (query side) ---------------------------------------------------------------------

MAX_CACHED_USERS = 10_000
_principals: dict[str, tuple[float, list[str]]] = {}


async def principals_for(user_id: str, token: str) -> list[str]:
    """The user's id, every group they're in (nested) and org markers, cached for GROUP_CACHE_SECONDS.
    A failed lookup raises: searching with a partial list would make group-shared files silently vanish."""
    cached = _principals.get(user_id)
    if cached and time.monotonic() - cached[0] < settings.GROUP_CACHE_SECONDS:
        return cached[1]
    object_id, groups, is_member = await graph.my_memberships(token)
    principals = acl.query_principals(object_id, groups, is_member)
    while len(_principals) >= MAX_CACHED_USERS:
        _principals.pop(next(iter(_principals)))
    _principals[user_id] = (time.monotonic(), principals)
    return principals


def _verifier(token: str):
    """Layer 2: drop anything the user can't open in SharePoint right now. Errors propagate (fail closed)."""
    async def verify(items: list[tuple[str, str]]) -> set[tuple[str, str]]:
        allowed = await graph.verify_access(token, items)
        async with pool.connection() as conn:
            await metrics.incr(conn, "layer2_checked", len(items))
            await metrics.incr(conn, "layer2_dropped", len(items) - len(allowed))
        return allowed

    return verify


async def search_index(user_id: str, token: str, query: str, limit: int) -> list:
    principals = await principals_for(user_id, token)
    return await retriever.search(query, principals=principals, verify=_verifier(token), k=limit)


def _hit_json(h) -> dict:
    return {
        "name": h.name, "webUrl": h.web_url, "driveId": h.drive_id, "itemId": h.item_id,
        "section": h.heading_path, "snippet": h.text[:800], "score": round(h.score, 4),
    }


# ---- tools ----------------------------------------------------------------------------------------

@mcp.tool()
async def sharepoint_logout() -> str:
    """Forget the Microsoft sign-in; the client must re-authenticate before the next call."""
    token = get_access_token()
    if token is None:
        return "Not signed in."
    dropped = await provider.sign_out(token)
    await usage.audit(token.subject, "logout")
    if not dropped:
        return "Signed out this client. The Microsoft session was kept because another client is still connected."
    return "Signed out."


@mcp.tool()
async def sharepoint_whoami() -> str:
    """Show the signed-in Microsoft user for this session."""
    return await _as_user(graph.me)


@mcp.tool()
async def sharepoint_search(query: str, limit: int = 10) -> str:
    """Search SharePoint/OneDrive files the user can access (keyword + semantic). Returns cited passages:
    file name, link, driveId/itemId (for sharepoint_read_file), section and snippet."""
    user_id = _user_id()
    limit = max(1, min(limit, 25))
    await usage.hit(user_id, "search", settings.SEARCH_PER_MINUTE, 60)
    token = await _graph_token(user_id)
    if settings.SEARCH_BACKEND == "graph":
        results = await graph.search_content(token, query, limit)
        await usage.audit(user_id, "search", query=query, detail={"backend": "graph", "results": len(results)})
        return json.dumps(results, indent=2)
    hits = await search_index(user_id, token, query, limit)
    await usage.audit(user_id, "search", query=query, source_ids=list(dict.fromkeys(h.source_id for h in hits)))
    return json.dumps([_hit_json(h) for h in hits], indent=2)


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
    user_id = _user_id()
    result = await graph.read_file_text(await _graph_token(user_id), drive_id, item_id, max(1, min(max_bytes, 1_000_000)))
    await usage.audit(user_id, "read", detail={"drive_id": drive_id, "item_id": item_id})
    return json.dumps(result, indent=2)


if settings.ASK_MODEL:
    from langchain.chat_models import init_chat_model

    from . import agent

    _ask_model = init_chat_model(settings.ASK_MODEL, **settings.ASK_MODEL_KWARGS)

    @mcp.tool()
    async def sharepoint_ask(question: str, ctx: Context) -> str:
        """Research a question across the SharePoint content the user can access and answer it with sources
        (title, link, quote). Slower than sharepoint_search; use it when you want an answer, not raw results.
        Answers say so when the content doesn't cover the question."""
        user_id = _user_id()
        await usage.hit(user_id, "ask", settings.ASK_PER_HOUR, 3600)
        if await usage.tokens_used(user_id, "ask_tokens", 86400) >= settings.ASK_TOKENS_PER_DAY:
            raise RateLimited("Daily sharepoint_ask budget used up. Try again tomorrow or use sharepoint_search.")
        token = await _graph_token(user_id)
        tools = agent.AskTools(
            search=lambda q, k: search_index(user_id, token, q, k),
            read_file=lambda d, i: graph.read_file_text(token, d, i, agent.MAX_FILE_CHARS),
            list_items=lambda d, i: graph.list_children(token, d, i),
        )

        async def progress(step: float, total: float | None, message: str) -> None:
            await ctx.report_progress(step, total, message)

        try:
            result = await agent.ask(
                _ask_model, question, tools, max_steps=settings.ASK_MAX_STEPS,
                max_tokens=settings.ASK_MAX_TOKENS, timeout_seconds=settings.ASK_TIMEOUT_SECONDS, progress=progress,
                # A pseudonym, not the Entra id: traces of one user's runs group together without naming them.
                metadata={"user": hashlib.sha256(user_id.encode()).hexdigest()[:16], "model": settings.ASK_MODEL},
            )
        except Exception as e:  # e.g. the user's Graph token was revoked mid-run
            await usage.audit(user_id, "ask", query=question, detail={"status": "error", "error": type(e).__name__})
            raise
        await usage.add_tokens(user_id, "ask_tokens", 86400, result.tokens)
        async with pool.connection() as conn:
            await metrics.incr(conn, "agent_tokens", result.tokens)
        await usage.audit(user_id, "ask", query=question, tokens=result.tokens,
                          detail={"status": result.status, "steps": result.steps, "sources": len(result.sources)})
        return json.dumps({"status": result.status, "answer": result.answer, "sources": result.sources}, indent=2)


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> Response:
    try:
        redirect_url, user_id = await provider.finish_authorization(dict(request.query_params))
    except Exception as e:  # surfaced to the user in the browser
        return HTMLResponse(f"Sign-in failed: {html.escape(str(e))}", 400)
    if user_id:
        await usage.audit(user_id, "login")
    return RedirectResponse(redirect_url, status_code=302)


async def _housekeeping() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await usage.prune()
        except Exception:
            log.exception("housekeeping failed")


async def _serve() -> None:
    await asyncio.to_thread(db.migrate)
    await pool.open()
    housekeeping = asyncio.create_task(_housekeeping())
    try:
        server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host=config.BIND_HOST, port=config.PORT))
        await server.serve()
    finally:
        housekeeping.cancel()
        await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    print(f"MCP server:     {MCP_URL}")
    print(f"OAuth callback: {config.REDIRECT_URI}")
    print(f"sharepoint_ask: {'on (' + settings.ASK_MODEL + ')' if settings.ASK_MODEL else 'off (set GEMINI_API_KEY, or ASK_MODEL and its key)'}")
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
