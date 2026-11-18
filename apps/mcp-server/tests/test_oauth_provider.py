"""OAuth provider against a real Postgres (see tests/fixtures.py) with Entra faked."""
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from mcp_server.oauth_provider import EntraProxyProvider

RESOURCE = "http://localhost:8080/api/mcp"
REDIRECT = "http://localhost:9999/callback"


class FakeEntra:
    """Stands in for sharepoint_client.auth.EntraAuth; a signed-in user has a msal_cache row, as with the real one."""

    def __init__(self, pool) -> None:
        self.pool = pool
        self.pending: dict[str, dict] = {}
        self.signed_out: list[str] = []

    async def start_login(self, context: dict) -> str:
        state = f"state{len(self.pending)}"
        self.pending[state] = context
        return f"https://entra.test/authorize?state={state}"

    async def cancel_login(self, state: str):
        return self.pending.pop(state, None)

    async def complete_login(self, query):
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO msal_cache (user_id, blob) VALUES ('user-1', 'x') ON CONFLICT DO NOTHING")
        return self.pending.pop(query["state"]), "user-1", "user@example.com"

    async def sign_out(self, user_id: str) -> None:
        self.signed_out.append(user_id)
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM msal_cache WHERE user_id = %s", (user_id,))


@pytest.fixture
async def entra(pool):
    return FakeEntra(pool)


@pytest.fixture
async def provider(pool, entra):
    return EntraProxyProvider(pool, entra, RESOURCE, max_clients=1000)


def new_client(client_id="c1", redirect=REDIRECT):
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=[redirect], token_endpoint_auth_method="none")


@pytest.fixture
async def client(provider):
    info = new_client()
    await provider.register_client(info)
    return info


def params(resource=RESOURCE):
    return AuthorizationParams(
        state="client-state", scopes=["x"], code_challenge="chal",
        redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True, resource=resource,
    )


async def sign_in(provider, client) -> str:
    entra_url = await provider.authorize(client, params())
    entra_state = parse_qs(urlparse(entra_url).query)["state"][0]
    back, user_id = await provider.finish_authorization({"state": entra_state, "code": "entra-code"})
    q = parse_qs(urlparse(back).query)
    assert back.startswith(REDIRECT) and q["state"] == ["client-state"] and user_id == "user-1"
    return q["code"][0]


async def tokens(provider, client):
    return await provider.exchange_authorization_code(client, await provider.load_authorization_code(client, await sign_in(provider, client)))


async def expire_all(pool, kind=None):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE oauth_tokens SET expires_at = now() - interval '1 second' WHERE %s::text IS NULL OR kind = %s", (kind, kind)
        )


async def test_full_flow_issues_tokens_bound_to_user(provider, client, pool):
    code = await sign_in(provider, client)
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code and auth_code.code_challenge == "chal" and auth_code.subject == "user-1"

    token = await provider.exchange_authorization_code(client, auth_code)
    access = await provider.load_access_token(token.access_token)
    assert access and access.subject == "user-1" and access.resource == RESOURCE

    with pytest.raises(TokenError):  # codes are single use
        await provider.exchange_authorization_code(client, auth_code)
    async with pool.connection() as conn:  # only hashes are stored
        stored = [r[0] for r in await (await conn.execute("SELECT token_hash FROM oauth_tokens")).fetchall()]
    assert token.access_token not in stored and token.refresh_token not in stored


async def test_code_is_bound_to_client_and_expires(provider, client, pool):
    code = await sign_in(provider, client)
    other = new_client("c2")
    await provider.register_client(other)
    assert await provider.load_authorization_code(other, code) is None
    await expire_all(pool, "code")
    assert await provider.load_authorization_code(client, code) is None


async def test_refresh_rotates_tokens(provider, client):
    first = await tokens(provider, client)
    old = await provider.load_refresh_token(client, first.refresh_token)
    second = await provider.exchange_refresh_token(client, old, [])
    assert second.refresh_token != first.refresh_token
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    with pytest.raises(TokenError):  # a replayed refresh token loses
        await provider.exchange_refresh_token(client, old, [])
    assert (await provider.load_access_token(second.access_token)).subject == "user-1"


async def test_expired_access_token_is_rejected(provider, client, pool):
    token = await tokens(provider, client)
    await expire_all(pool, "access")
    assert await provider.load_access_token(token.access_token) is None


async def test_wrong_resource_is_rejected(provider, client):
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, params(resource="http://evil.test/mcp"))


async def test_entra_error_redirects_to_client(provider, client):
    entra_url = await provider.authorize(client, params())
    entra_state = parse_qs(urlparse(entra_url).query)["state"][0]
    back, user_id = await provider.finish_authorization({"state": entra_state, "error": "access_denied"})
    q = parse_qs(urlparse(back).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["client-state"] and user_id is None


async def test_revoke_drops_tokens_and_entra_session(provider, client, entra):
    token = await tokens(provider, client)
    await provider.revoke_token(await provider.load_access_token(token.access_token))
    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_refresh_token(client, token.refresh_token) is None
    assert entra.signed_out == ["user-1"]


@pytest.mark.parametrize("uri", ["https://attacker.example/cb", "javascript://localhost/x"])
async def test_redirect_uri_must_be_loopback_or_allowed(provider, uri):
    with pytest.raises(RegistrationError):
        await provider.register_client(new_client("evil", uri))


async def test_allowed_hosted_redirect_uri(pool, entra):
    hosted = "https://claude.ai/api/mcp/auth_callback"
    provider = EntraProxyProvider(pool, entra, RESOURCE, allowed_redirect_uris={hosted})
    await provider.register_client(new_client("hosted", hosted))
    assert await provider.get_client("hosted")
    with pytest.raises(RegistrationError):
        await provider.register_client(new_client("near-miss", hosted + "/../evil"))


async def test_tokens_are_rejected_once_entra_session_is_gone(provider, client, pool):
    token = await tokens(provider, client)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM msal_cache")
    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_refresh_token(client, token.refresh_token) is None


async def test_sign_out_keeps_entra_session_while_another_client_holds_tokens(provider, client, entra):
    other = new_client("c2")
    await provider.register_client(other)
    a = await tokens(provider, client)
    b = await tokens(provider, other)
    assert await provider.sign_out(await provider.load_access_token(a.access_token)) is False
    assert entra.signed_out == []
    assert await provider.sign_out(await provider.load_access_token(b.access_token)) is True
    assert entra.signed_out == ["user-1"]


async def test_sign_out_ignores_expired_tokens_of_other_clients(provider, client, entra, pool):
    other = new_client("c9")
    await provider.register_client(other)
    await tokens(provider, other)
    await expire_all(pool)  # the other client's tokens are long expired
    token = await tokens(provider, client)
    assert await provider.sign_out(await provider.load_access_token(token.access_token)) is True
    assert entra.signed_out == ["user-1"]


async def test_registration_at_cap_evicts_clients_that_never_signed_in(pool, entra):
    provider = EntraProxyProvider(pool, entra, RESOURCE, max_clients=3)
    clients = [new_client(f"c{i}") for i in range(3)]
    for c in clients:
        await provider.register_client(c)
    await tokens(provider, clients[0])
    async with pool.connection() as conn:
        await conn.execute("UPDATE oauth_clients SET registered_at = now() - interval '1 hour'")
    await provider.register_client(new_client("c3"))
    assert await provider.get_client("c0") and await provider.get_client("c3")
    assert await provider.get_client("c1") is None and await provider.get_client("c2") is None


async def test_registration_at_cap_keeps_recent_clients_that_may_be_signing_in(pool, entra):
    provider = EntraProxyProvider(pool, entra, RESOURCE, max_clients=2)
    for i in range(2):
        await provider.register_client(new_client(f"c{i}"))
    with pytest.raises(RegistrationError):
        await provider.register_client(new_client("c2"))
    assert await provider.get_client("c0") and await provider.get_client("c1")


async def test_state_survives_a_new_provider_instance(pool, entra, client):
    """Another replica (a fresh provider on the same database) accepts the tokens."""
    token = await tokens(EntraProxyProvider(pool, entra, RESOURCE), client)
    replica = EntraProxyProvider(pool, entra, RESOURCE)
    assert (await replica.load_access_token(token.access_token)).subject == "user-1"
