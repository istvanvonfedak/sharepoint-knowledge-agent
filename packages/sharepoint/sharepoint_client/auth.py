"""Delegated Entra ID sign-in (auth code + PKCE) with state in Postgres, so any replica can serve any user.

Sign-ins in progress (`oauth_pending`) and each user's MSAL token cache (`msal_cache`, which holds their Entra
refresh token) are encrypted with TOKEN_ENCRYPTION_KEY, a key kept outside the database.
"""
import asyncio
import hashlib
import json
from typing import Any

import msal
from cryptography.fernet import Fernet, InvalidToken
from psycopg_pool import AsyncConnectionPool

from . import config

PENDING_TTL_SECONDS = 600
MAX_PENDING = 1000
# MSAL errors that mean the user has to sign in again; anything else (throttling, 5xx) is transient.
TERMINAL_ERRORS = {"invalid_grant", "interaction_required", "consent_required", "login_required"}
_AUTHORITY = f"https://login.microsoftonline.com/{config.TENANT_ID}"
_http_cache: dict = {}  # shared by every MSAL app instance, so authority discovery happens once per process


class NotSignedInError(Exception):
    def __init__(self) -> None:
        super().__init__("Not signed in to Microsoft. Re-authenticate this MCP server from your client (e.g. /mcp in Claude Code).")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_app(cache: msal.SerializableTokenCache | None = None) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        config.CLIENT_ID, client_credential=config.CLIENT_SECRET, authority=_AUTHORITY,
        token_cache=cache, http_cache=_http_cache,
    )


class EntraAuth:
    def __init__(self, pool: AsyncConnectionPool, encryption_key: str) -> None:
        self._pool = pool
        try:
            self._fernet = Fernet(encryption_key)
        except (ValueError, TypeError) as e:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY must be a Fernet key (see .env.mcp.example)") from e
        self._login_app: msal.ConfidentialClientApplication | None = None

    def _encrypt(self, obj: Any) -> bytes:
        return self._fernet.encrypt(json.dumps(obj).encode())

    def _decrypt(self, blob: bytes) -> Any:
        return json.loads(self._fernet.decrypt(bytes(blob)))

    async def start_login(self, context: dict) -> str:
        """Begins the Entra sign-in; `context` (JSON) is handed back by `complete_login` for the same `state`."""
        if self._login_app is None:
            self._login_app = await asyncio.to_thread(_new_app)
        flow = await asyncio.to_thread(self._login_app.initiate_auth_code_flow, config.SCOPES, redirect_uri=config.REDIRECT_URI)
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM oauth_pending WHERE created_at < now() - %s * interval '1 second'", (PENDING_TTL_SECONDS,)
            )
            # Bound storage against a client looping on /authorize: drop the oldest beyond the cap.
            await conn.execute(
                "DELETE FROM oauth_pending WHERE state_hash IN ("
                "  SELECT state_hash FROM oauth_pending ORDER BY created_at DESC OFFSET %s)",
                (MAX_PENDING - 1,),
            )
            await conn.execute(
                "INSERT INTO oauth_pending (state_hash, blob) VALUES (%s, %s)",
                (_hash(flow["state"]), self._encrypt({"context": context, "flow": flow})),
            )
        return flow["auth_uri"]

    async def _take_pending(self, state: str) -> dict | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "DELETE FROM oauth_pending WHERE state_hash = %s AND created_at >= now() - %s * interval '1 second' RETURNING blob",
                (_hash(state), PENDING_TTL_SECONDS),
            )).fetchone()
        if row is None:
            return None
        try:
            return self._decrypt(row[0])
        except InvalidToken:  # written under another key
            return None

    async def cancel_login(self, state: str) -> dict | None:
        entry = await self._take_pending(state)
        return entry["context"] if entry else None

    async def complete_login(self, query: dict[str, str]) -> tuple[dict, str, str]:
        """Finishes the sign-in. Returns (context from start_login, user id, username)."""
        entry = await self._take_pending(query.get("state", ""))
        if entry is None:
            raise ValueError("Unknown or expired sign-in state")
        cache = msal.SerializableTokenCache()
        app = await asyncio.to_thread(_new_app, cache)
        result = await asyncio.to_thread(app.acquire_token_by_auth_code_flow, entry["flow"], query)
        if "access_token" not in result:
            raise ValueError(result.get("error_description", "Token exchange failed"))
        claims = result.get("id_token_claims", {})
        home_account_id = f"{claims.get('oid')}.{claims.get('tid')}"
        # Match only on the immutable object id + tenant id; a username is mutable and not unique across tenants.
        account = next((a for a in app.get_accounts() if a["home_account_id"] == home_account_id), None)
        if account is None:
            raise ValueError("Signed in, but the account was not found in the token cache")
        await self._save(home_account_id, account, cache)
        return entry["context"], home_account_id, account["username"]

    async def _save(self, user_id: str, account: dict, cache: msal.SerializableTokenCache, *, create: bool = True) -> None:
        """`create=False` (a refresh) only updates: a sign-out that happened meanwhile must stay signed out."""
        blob = self._encrypt({"account": account, "cache": cache.serialize()})
        async with self._pool.connection() as conn:
            if create:
                await conn.execute(
                    "INSERT INTO msal_cache (user_id, blob) VALUES (%s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET blob = EXCLUDED.blob, updated_at = now()",
                    (user_id, blob),
                )
            else:
                await conn.execute("UPDATE msal_cache SET blob = %s, updated_at = now() WHERE user_id = %s", (blob, user_id))

    async def is_signed_in(self, user_id: str) -> bool:
        async with self._pool.connection() as conn:
            return bool(await (await conn.execute("SELECT 1 FROM msal_cache WHERE user_id = %s", (user_id,))).fetchone())

    async def sign_out(self, user_id: str) -> None:
        """Drops the user's Entra tokens and every MCP token issued for them, so no old client token comes
        back to life when the user signs in again."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM msal_cache WHERE user_id = %s", (user_id,))
            await conn.execute("DELETE FROM oauth_tokens WHERE subject = %s", (user_id,))

    async def get_access_token(self, user_id: str) -> str:
        """Graph access token for the user, refreshed silently from their MSAL cache."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT blob FROM msal_cache WHERE user_id = %s", (user_id,))).fetchone()
        if row is None:
            raise NotSignedInError()
        try:
            stored = self._decrypt(row[0])
        except InvalidToken:
            await self.sign_out(user_id)
            raise NotSignedInError() from None
        cache = msal.SerializableTokenCache()
        cache.deserialize(stored["cache"])
        app = await asyncio.to_thread(_new_app, cache)
        # The _with_error variant: plain acquire_token_silent returns None for *every* failure, so a passing
        # Entra outage would look like an expired sign-in. None here means no usable refresh token is cached.
        result = await asyncio.to_thread(app.acquire_token_silent_with_error, config.SCOPES, account=stored["account"])
        if cache.has_state_changed:  # refreshed: keep the new refresh token
            await self._save(user_id, stored["account"], cache, create=False)
        if result and "access_token" in result:
            return result["access_token"]
        if result and result.get("error") not in TERMINAL_ERRORS:
            raise RuntimeError(f"Microsoft token refresh failed, try again: {result.get('error_description', result.get('error'))}")
        await self.sign_out(user_id)
        raise NotSignedInError()
