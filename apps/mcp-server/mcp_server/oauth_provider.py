"""MCP-native OAuth: this server is the authorization server for its MCP clients and proxies sign-in to Entra.

Entra has no dynamic client registration, so clients register here, are sent to Entra via the single
Entra app, and get back opaque tokens issued by this server. State is in memory: a restart signs everyone out.
"""
import asyncio
import secrets
import time
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

from sharepoint_client import auth

CODE_TTL_SECONDS = 600
ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_CLIENTS = 100


class EntraProxyProvider:
    def __init__(self, resource_url: str) -> None:
        self._resource = resource_url.rstrip("/")
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required")
        # Open registration plus a hostile redirect_uri would hand a victim's auth code to an attacker.
        for uri in client_info.redirect_uris or []:
            if urlparse(str(uri)).hostname not in LOOPBACK_HOSTS:
                raise RegistrationError("invalid_redirect_uri", "Only loopback redirect URIs are allowed")
        self._purge()
        if len(self._clients) >= MAX_CLIENTS:  # make room by dropping clients that never finished signing in
            active = {t.client_id for store in (self._access, self._refresh) for t in store.values()}
            for client_id in [c for c in list(self._clients) if c not in active]:
                self._clients.pop(client_id, None)
        if len(self._clients) >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "Too many registered clients")
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self._resource:
            raise AuthorizeError("invalid_request", "Unknown resource")
        self._purge()
        # Entra's `state` keys the client's redirect_uri/PKCE/state until /oauth/callback brings the user back.
        return await asyncio.to_thread(auth.start_login, (client.client_id, params))

    async def finish_authorization(self, query: dict[str, str]) -> str:
        """Handles Entra's redirect to /oauth/callback; returns the URL to send the user back to the MCP client."""
        if "error" in query:
            context = auth.cancel_login(query.get("state", ""))
            if context is None:
                raise ValueError(f"{query['error']}: {query.get('error_description', '')}")
            _, params = context
            return construct_redirect_uri(
                str(params.redirect_uri),
                error="access_denied",
                error_description=query.get("error_description"),
                state=params.state,
            )
        (client_id, params), user_id, _ = await asyncio.to_thread(auth.complete_login, query)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL_SECONDS,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self._resource,
            subject=user_id,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:  # single use
            raise TokenError("invalid_grant", "Authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token = self._refresh.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            self._refresh.pop(refresh_token, None)
            return None
        if token.subject is None or not auth.is_signed_in(token.subject):
            return None  # Entra session is gone; the client must run the full sign-in again
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._refresh.pop(refresh_token.token, None)  # rotate
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._access.get(token)
        if access is None:
            return None
        if access.expires_at is not None and access.expires_at < time.time():
            self._access.pop(token, None)
            return None
        if access.subject is None or not auth.is_signed_in(access.subject):
            return None  # 401 makes the client restart OAuth instead of failing every tool call
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.sign_out(token)

    async def sign_out(self, token: AccessToken | RefreshToken) -> bool:
        """Revokes the user's tokens for this client. Returns True if the user's Entra sign-in was dropped too,
        which only happens once no other client holds tokens for them."""
        for store in (self._access, self._refresh):
            for key in [k for k, t in list(store.items()) if t.subject == token.subject and t.client_id == token.client_id]:
                store.pop(key, None)
        still_used = any(t.subject == token.subject for store in (self._access, self._refresh) for t in store.values())
        if token.subject is None or still_used:
            return False
        auth.sign_out(token.subject)
        return True

    def _purge(self) -> None:
        """Drops expired codes and tokens."""
        now = time.time()
        for store in (self._codes, self._access, self._refresh):
            for key in [k for k, v in list(store.items()) if v.expires_at is not None and v.expires_at < now]:
                store.pop(key, None)

    def _issue(self, client_id: str | None, scopes: list[str], subject: str | None) -> OAuthToken:
        assert client_id is not None
        self._purge()
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL_SECONDS,
            resource=self._resource, subject=subject,
        )
        self._refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL_SECONDS,
            resource=self._resource, subject=subject,
        )
        return OAuthToken(
            access_token=access, expires_in=ACCESS_TTL_SECONDS, scope=" ".join(scopes), refresh_token=refresh
        )
