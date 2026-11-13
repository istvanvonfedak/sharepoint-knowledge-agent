import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from mcp_server.oauth_provider import EntraProxyProvider

RESOURCE = "http://localhost:8080/api/mcp"
REDIRECT = "http://localhost:9999/callback"


@pytest.fixture
def provider():
    from sharepoint_client import auth

    auth.pending.clear()
    auth.signed_out.clear()
    return EntraProxyProvider(RESOURCE)


@pytest.fixture
async def client(provider):
    info = OAuthClientInformationFull(client_id="c1", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")
    await provider.register_client(info)
    return info


def params(resource=RESOURCE):
    return AuthorizationParams(
        state="client-state", scopes=["x"], code_challenge="chal",
        redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True, resource=resource,
    )


async def sign_in(provider, client):
    entra_url = await provider.authorize(client, params())
    entra_state = parse_qs(urlparse(entra_url).query)["state"][0]
    back = await provider.finish_authorization({"state": entra_state, "code": "entra-code"})
    q = parse_qs(urlparse(back).query)
    assert back.startswith(REDIRECT) and q["state"] == ["client-state"]
    return q["code"][0]


async def test_full_flow_issues_tokens_bound_to_user(provider, client):
    code = await sign_in(provider, client)
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code and auth_code.code_challenge == "chal" and auth_code.subject == "user-1"

    token = await provider.exchange_authorization_code(client, auth_code)
    access = await provider.load_access_token(token.access_token)
    assert access and access.subject == "user-1" and access.resource == RESOURCE

    with pytest.raises(TokenError):  # codes are single use
        await provider.exchange_authorization_code(client, auth_code)


async def test_code_is_bound_to_client_and_expires(provider, client):
    code = await sign_in(provider, client)
    other = OAuthClientInformationFull(client_id="c2", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")
    assert await provider.load_authorization_code(other, code) is None

    provider._codes[code].expires_at = time.time() - 1
    assert await provider.load_authorization_code(client, code) is None


async def test_refresh_rotates_tokens(provider, client):
    auth_code = await provider.load_authorization_code(client, await sign_in(provider, client))
    first = await provider.exchange_authorization_code(client, auth_code)

    old = await provider.load_refresh_token(client, first.refresh_token)
    second = await provider.exchange_refresh_token(client, old, [])
    assert second.refresh_token != first.refresh_token
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    assert (await provider.load_access_token(second.access_token)).subject == "user-1"


async def test_expired_access_token_is_rejected(provider, client):
    auth_code = await provider.load_authorization_code(client, await sign_in(provider, client))
    token = await provider.exchange_authorization_code(client, auth_code)
    provider._access[token.access_token].expires_at = int(time.time()) - 1
    assert await provider.load_access_token(token.access_token) is None


async def test_wrong_resource_is_rejected(provider, client):
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, params(resource="http://evil.test/mcp"))


async def test_entra_error_redirects_to_client(provider, client):
    entra_url = await provider.authorize(client, params())
    entra_state = parse_qs(urlparse(entra_url).query)["state"][0]
    back = await provider.finish_authorization({"state": entra_state, "error": "access_denied"})
    q = parse_qs(urlparse(back).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["client-state"]


async def test_revoke_drops_tokens_and_entra_session(provider, client):
    from sharepoint_client import auth

    auth_code = await provider.load_authorization_code(client, await sign_in(provider, client))
    token = await provider.exchange_authorization_code(client, auth_code)
    await provider.revoke_token(await provider.load_access_token(token.access_token))
    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_refresh_token(client, token.refresh_token) is None
    assert auth.signed_out == ["user-1"]


async def test_non_loopback_redirect_uri_is_rejected(provider):
    evil = OAuthClientInformationFull(
        client_id="evil", redirect_uris=["https://attacker.example/cb"], token_endpoint_auth_method="none"
    )
    with pytest.raises(RegistrationError):
        await provider.register_client(evil)


async def test_tokens_are_rejected_once_entra_session_is_gone(provider, client):
    from sharepoint_client import auth

    auth_code = await provider.load_authorization_code(client, await sign_in(provider, client))
    token = await provider.exchange_authorization_code(client, auth_code)
    auth.live_users.discard("user-1")
    try:
        assert await provider.load_access_token(token.access_token) is None
        assert await provider.load_refresh_token(client, token.refresh_token) is None
    finally:
        auth.live_users.add("user-1")


async def test_revoke_keeps_entra_session_while_another_client_holds_tokens(provider, client):
    from sharepoint_client import auth

    other = OAuthClientInformationFull(client_id="c2", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")
    await provider.register_client(other)
    a = await provider.exchange_authorization_code(
        client, await provider.load_authorization_code(client, await sign_in(provider, client))
    )
    await provider.exchange_authorization_code(
        other, await provider.load_authorization_code(other, await sign_in(provider, other))
    )
    await provider.revoke_token(await provider.load_access_token(a.access_token))
    assert auth.signed_out == []


async def test_sign_out_reports_whether_entra_session_was_dropped(provider, client):
    other = OAuthClientInformationFull(client_id="c2", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")
    await provider.register_client(other)
    a = await provider.exchange_authorization_code(
        client, await provider.load_authorization_code(client, await sign_in(provider, client))
    )
    b = await provider.exchange_authorization_code(
        other, await provider.load_authorization_code(other, await sign_in(provider, other))
    )
    assert await provider.sign_out(await provider.load_access_token(a.access_token)) is False
    assert await provider.sign_out(await provider.load_access_token(b.access_token)) is True


async def test_expired_entries_are_purged_without_being_looked_up(provider, client):
    auth_code = await provider.load_authorization_code(client, await sign_in(provider, client))
    token = await provider.exchange_authorization_code(client, auth_code)
    await sign_in(provider, client)  # leaves an unredeemed code behind
    past = time.time() - 1
    for store in (provider._codes, provider._access, provider._refresh):
        for entry in store.values():
            entry.expires_at = past
    await provider.authorize(client, params())  # any flow step sweeps expired entries
    assert not provider._codes and not provider._access and not provider._refresh


def _new_client(i):
    return OAuthClientInformationFull(client_id=f"c{i}", redirect_uris=[REDIRECT], token_endpoint_auth_method="none")


async def _sign_in_client(provider, client):
    code = await provider.load_authorization_code(client, await sign_in(provider, client))
    await provider.exchange_authorization_code(client, code)


async def test_registration_at_cap_evicts_clients_that_never_signed_in(provider, monkeypatch):
    monkeypatch.setattr("mcp_server.oauth_provider.MAX_CLIENTS", 3)
    clients = [_new_client(i) for i in range(3)]
    for c in clients:
        await provider.register_client(c)
    await _sign_in_client(provider, clients[0])

    await provider.register_client(_new_client(3))
    assert await provider.get_client("c0") and await provider.get_client("c3")
    assert await provider.get_client("c1") is None and await provider.get_client("c2") is None


async def test_registration_is_rejected_when_every_client_is_active(provider, monkeypatch):
    monkeypatch.setattr("mcp_server.oauth_provider.MAX_CLIENTS", 2)
    for i in range(2):
        c = _new_client(i)
        await provider.register_client(c)
        await _sign_in_client(provider, c)
    with pytest.raises(RegistrationError):
        await provider.register_client(_new_client(2))
