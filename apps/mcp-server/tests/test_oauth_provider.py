import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
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
