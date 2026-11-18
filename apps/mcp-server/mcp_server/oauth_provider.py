"""MCP-native OAuth: this server is the authorization server for its MCP clients and proxies sign-in to Entra.

Entra has no dynamic client registration, so clients register here, are sent to Entra via the single
Entra app, and get back opaque tokens issued by this server. All state is in Postgres (any replica can
serve any request); tokens and codes are stored only as SHA-256 hashes.
"""
import hashlib
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from sharepoint_client.auth import EntraAuth

CODE_TTL_SECONDS = 600
ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def redirect_allowed(uri: str, allowed: set[str]) -> bool:
    """Loopback (the user's own machine, any port) or an exact URI the operator allowed."""
    parsed = urlparse(uri)
    if parsed.hostname in LOOPBACK_HOSTS and parsed.scheme in ("http", "https"):
        return True
    return uri in allowed


class EntraProxyProvider:
    def __init__(
        self, pool: AsyncConnectionPool, auth: EntraAuth, resource_url: str, *,
        allowed_redirect_uris: set[str] | None = None, max_clients: int = 1000,
    ) -> None:
        self._pool = pool
        self._auth = auth
        self._resource = resource_url.rstrip("/")
        self._allowed = allowed_redirect_uris or set()
        self._max_clients = max_clients

    # ---- clients -----------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT info FROM oauth_clients WHERE client_id = %s", (client_id,))).fetchone()
        return OAuthClientInformationFull.model_validate(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        # Open registration plus a hostile redirect_uri would hand a victim's auth code to an attacker.
        for uri in client_info.redirect_uris or []:
            if not redirect_allowed(str(uri), self._allowed):
                raise RegistrationError("invalid_redirect_uri", "Redirect URI not allowed (loopback or OAUTH_ALLOWED_REDIRECT_URIS only)")
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE")  # serialise the cap check
            await self._purge(conn)
            count = (await (await conn.execute("SELECT count(*) FROM oauth_clients")).fetchone())[0]
            if count >= self._max_clients:
                # Make room by dropping clients that never got tokens; younger ones may still be on the Entra login page.
                await conn.execute(
                    "DELETE FROM oauth_clients c WHERE c.registered_at < now() - %s * interval '1 second' "
                    "AND NOT EXISTS (SELECT 1 FROM oauth_tokens t WHERE t.client_id = c.client_id)",
                    (CODE_TTL_SECONDS,),
                )
                count = (await (await conn.execute("SELECT count(*) FROM oauth_clients")).fetchone())[0]
            full = count >= self._max_clients
            if not full:
                await conn.execute(
                    "INSERT INTO oauth_clients (client_id, info) VALUES (%s, %s) "
                    "ON CONFLICT (client_id) DO UPDATE SET info = EXCLUDED.info",
                    (client_info.client_id, Jsonb(client_info.model_dump(mode="json"))),
                )
        # Raised outside the transaction: the SDK's error type is a frozen dataclass that context managers can't annotate.
        if full:
            raise RegistrationError("invalid_client_metadata", "Too many registered clients")

    # ---- authorization -----------------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self._resource:
            raise AuthorizeError("invalid_request", "Unknown resource")
        # Entra's `state` keys the client's redirect_uri/PKCE/state until /oauth/callback brings the user back.
        return await self._auth.start_login({"client_id": client.client_id, "params": params.model_dump(mode="json")})

    async def finish_authorization(self, query: dict[str, str]) -> tuple[str, str | None]:
        """Handles Entra's redirect to /oauth/callback. Returns (URL to send the user back to, user id or None)."""
        if "error" in query:
            context = await self._auth.cancel_login(query.get("state", ""))
            if context is None:
                raise ValueError(f"{query['error']}: {query.get('error_description', '')}")
            params = AuthorizationParams.model_validate(context["params"])
            return construct_redirect_uri(
                str(params.redirect_uri), error="access_denied",
                error_description=query.get("error_description"), state=params.state,
            ), None
        context, user_id, _ = await self._auth.complete_login(query)
        params = AuthorizationParams.model_validate(context["params"])
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_TTL_SECONDS, client_id=context["client_id"],
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self._resource, subject=user_id,
        )
        async with self._pool.connection() as conn:
            await self._store(conn, "code", auth_code, code)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), user_id

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = await self._load("code", authorization_code, client.client_id)
        return AuthorizationCode.model_validate({**data, "code": authorization_code}) if data else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not await self._take("code", authorization_code.code):  # single use, even across replicas
            raise TokenError("invalid_grant", "Authorization code already used")
        return await self._issue(client.client_id, authorization_code.scopes, authorization_code.subject)

    # ---- tokens ------------------------------------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = await self._load("refresh", refresh_token, client.client_id, require_signed_in=True)
        return RefreshToken.model_validate({**data, "token": refresh_token}) if data else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not await self._take("refresh", refresh_token.token):  # rotate; a replayed token loses the race
            raise TokenError("invalid_grant", "Refresh token already used")
        return await self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Without an Entra session, 401 makes the client restart OAuth instead of failing every tool call.
        data = await self._load("access", token, None, require_signed_in=True)
        return AccessToken.model_validate({**data, "token": token}) if data else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.sign_out(token)

    async def sign_out(self, token: AccessToken | RefreshToken) -> bool:
        """Revokes the user's tokens for this client. Returns True if the user's Entra sign-in was dropped too,
        which only happens once no other client holds tokens for them."""
        async with self._pool.connection() as conn, conn.transaction():
            await self._purge(conn)  # expired tokens of other clients must not count as "still connected"
            await conn.execute(
                "DELETE FROM oauth_tokens WHERE subject = %s AND client_id = %s AND kind IN ('access', 'refresh')",
                (token.subject, token.client_id),
            )
            still_used = await (await conn.execute(
                "SELECT 1 FROM oauth_tokens WHERE subject = %s AND kind IN ('access', 'refresh') LIMIT 1", (token.subject,)
            )).fetchone()
        if token.subject is None or still_used:
            return False
        await self._auth.sign_out(token.subject)
        return True

    # ---- storage -----------------------------------------------------------------------------------

    async def _store(self, conn, kind: str, obj, plaintext: str) -> None:
        field = "code" if kind == "code" else "token"
        await conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, subject, data, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (_hash(plaintext), kind, obj.client_id, obj.subject, Jsonb(obj.model_dump(mode="json", exclude={field})), _ts(obj.expires_at)),
        )

    async def _load(self, kind: str, plaintext: str, client_id: str | None, *, require_signed_in: bool = False) -> dict | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT t.data, t.client_id, t.subject, (m.user_id IS NOT NULL) FROM oauth_tokens t "
                "LEFT JOIN msal_cache m ON m.user_id = t.subject "
                "WHERE t.token_hash = %s AND t.kind = %s AND t.expires_at > now()",
                (_hash(plaintext), kind),
            )).fetchone()
        if row is None or (client_id is not None and row[1] != client_id):
            return None
        if require_signed_in and (row[2] is None or not row[3]):
            return None  # Entra session is gone; the client must run the full sign-in again
        return row[0]

    async def _take(self, kind: str, plaintext: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM oauth_tokens WHERE token_hash = %s AND kind = %s AND expires_at > now()", (_hash(plaintext), kind)
            )
        return cur.rowcount == 1

    async def _purge(self, conn) -> None:
        await conn.execute("DELETE FROM oauth_tokens WHERE expires_at < now()")

    async def _issue(self, client_id: str | None, scopes: list[str], subject: str | None) -> OAuthToken:
        assert client_id is not None
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with self._pool.connection() as conn, conn.transaction():
            await self._purge(conn)
            await self._store(conn, "access", AccessToken(
                token=access, client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL_SECONDS,
                resource=self._resource, subject=subject,
            ), access)
            await self._store(conn, "refresh", RefreshToken(
                token=refresh, client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL_SECONDS,
                resource=self._resource, subject=subject,
            ), refresh)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL_SECONDS, scope=" ".join(scopes), refresh_token=refresh)
