"""Delegated Entra ID sign-in (auth code + PKCE) with one identity per MCP session."""
import time

import msal

from . import config

_app = msal.ConfidentialClientApplication(
    config.CLIENT_ID,
    client_credential=config.CLIENT_SECRET,
    authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",
)

PENDING_TTL_SECONDS = 600

# OAuth `state` -> (mcp session id, MSAL auth-code flow incl. PKCE verifier, created-at)
_pending: dict[str, tuple[str, dict, float]] = {}
# mcp session id -> MSAL account
_accounts: dict[str, dict] = {}


def get_app_token() -> str:
    """App-only Graph token (client credentials) for background indexing.

    Blocking network call: from async code use `asyncio.to_thread`. Without Graph *application*
    permissions (Sites.Selected or Sites.Read.All + Files.Read.All, admin-consented) the token has
    no roles and every Graph call fails with 401/403 (see README)."""
    result = _app.acquire_token_for_client(["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "App-only token request failed"))
    return result["access_token"]


class NotSignedInError(Exception):
    def __init__(self) -> None:
        super().__init__("Not signed in to Microsoft. Call the sharepoint_login tool first.")


def start_login(session_id: str) -> str:
    _purge_expired()
    flow = _app.initiate_auth_code_flow(config.SCOPES, redirect_uri=config.REDIRECT_URI)
    _pending[flow["state"]] = (session_id, flow, time.monotonic())
    return flow["auth_uri"]


def _purge_expired() -> None:
    cutoff = time.monotonic() - PENDING_TTL_SECONDS
    for state in [s for s, (_, _, created) in list(_pending.items()) if created < cutoff]:
        _pending.pop(state, None)  # snapshot + pop: safe against concurrent worker threads


def cancel_login(state: str) -> None:
    _pending.pop(state, None)


def complete_login(query: dict[str, str]) -> str:
    _purge_expired()
    entry = _pending.pop(query.get("state", ""), None)
    if entry is None:
        raise ValueError("Unknown or expired sign-in state")
    session_id, flow, _ = entry
    result = _app.acquire_token_by_auth_code_flow(flow, query)
    if "access_token" not in result:
        raise ValueError(result.get("error_description", "Token exchange failed"))
    claims = result.get("id_token_claims", {})
    home_account_id = f"{claims.get('oid')}.{claims.get('tid')}"
    username = claims.get("preferred_username")
    accounts = _app.get_accounts()
    account = next((a for a in accounts if a["home_account_id"] == home_account_id), None) or next(
        (a for a in accounts if a["username"] == username), None
    )
    if account is None:
        raise ValueError("Signed in, but the account was not found in the token cache")
    _accounts[session_id] = account
    return account["username"]


def is_signed_in(session_id: str) -> bool:
    return session_id in _accounts


def sign_out(session_id: str) -> None:
    account = _accounts.pop(session_id, None)
    if account is not None and not any(a["home_account_id"] == account["home_account_id"] for a in _accounts.values()):
        _app.remove_account(account)  # drops cached tokens unless another session still uses this user


def get_access_token(session_id: str) -> str:
    """Graph access token for the session's user, refreshed silently from MSAL's cache."""
    account = _accounts.get(session_id)
    if account is None:
        raise NotSignedInError()
    result = _app.acquire_token_silent(config.SCOPES, account=account)
    if not result or "access_token" not in result:
        _accounts.pop(session_id, None)
        raise NotSignedInError()
    return result["access_token"]
