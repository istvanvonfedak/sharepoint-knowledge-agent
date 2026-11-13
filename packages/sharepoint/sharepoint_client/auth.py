"""Delegated Entra ID sign-in (auth code + PKCE); one MSAL account per signed-in user."""
import time
from typing import Any

import msal

from . import config

_app = msal.ConfidentialClientApplication(
    config.CLIENT_ID,
    client_credential=config.CLIENT_SECRET,
    authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",
)

PENDING_TTL_SECONDS = 600

# OAuth `state` -> (caller context, MSAL auth-code flow incl. PKCE verifier, created-at)
_pending: dict[str, tuple[Any, dict, float]] = {}
# MSAL home_account_id -> MSAL account
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
        super().__init__("Not signed in to Microsoft. Re-authenticate this MCP server from your client (e.g. /mcp in Claude Code).")


def start_login(context: Any) -> str:
    """Begins the Entra sign-in; `context` is handed back by `complete_login` for the same `state`."""
    _purge_expired()
    flow = _app.initiate_auth_code_flow(config.SCOPES, redirect_uri=config.REDIRECT_URI)
    _pending[flow["state"]] = (context, flow, time.monotonic())
    return flow["auth_uri"]


def _purge_expired() -> None:
    cutoff = time.monotonic() - PENDING_TTL_SECONDS
    for state in [s for s, (_, _, created) in list(_pending.items()) if created < cutoff]:
        _pending.pop(state, None)  # snapshot + pop: safe against concurrent worker threads


def cancel_login(state: str) -> Any | None:
    entry = _pending.pop(state, None)
    return entry[0] if entry else None


def complete_login(query: dict[str, str]) -> tuple[Any, str, str]:
    """Finishes the sign-in. Returns (context from start_login, user id, username)."""
    _purge_expired()
    entry = _pending.pop(query.get("state", ""), None)
    if entry is None:
        raise ValueError("Unknown or expired sign-in state")
    context, flow, _ = entry
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
    _accounts[account["home_account_id"]] = account
    return context, account["home_account_id"], account["username"]


def is_signed_in(user_id: str) -> bool:
    return user_id in _accounts


def sign_out(user_id: str) -> None:
    account = _accounts.pop(user_id, None)
    if account is not None:
        _app.remove_account(account)  # drops this user's cached tokens


def get_access_token(user_id: str) -> str:
    """Graph access token for the user, refreshed silently from MSAL's cache."""
    account = _accounts.get(user_id)
    if account is None:
        raise NotSignedInError()
    result = _app.acquire_token_silent(config.SCOPES, account=account)
    if not result or "access_token" not in result:
        _accounts.pop(user_id, None)
        raise NotSignedInError()
    return result["access_token"]
