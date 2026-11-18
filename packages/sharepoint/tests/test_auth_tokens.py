"""EntraAuth against a real Postgres (see tests/fixtures.py) with MSAL faked."""
import os

os.environ.setdefault("AZURE_TENANT_ID", "test-tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("AZURE_CLIENT_SECRET", "test-secret")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from sharepoint_client import auth  # noqa: E402


class FakeApp:
    """Records what the code under test asks MSAL for; `script` sets the answers."""

    script: dict = {}

    def __init__(self, cache=None) -> None:
        self.cache = cache

    def initiate_auth_code_flow(self, scopes, redirect_uri):
        n = FakeApp.script.setdefault("n", 0)
        FakeApp.script["n"] = n + 1
        return {"state": f"s{n}", "auth_uri": f"https://entra.test/?state=s{n}", "code_verifier": "secret-verifier"}

    def acquire_token_by_auth_code_flow(self, flow, query):
        assert flow["code_verifier"] == "secret-verifier"  # the stored flow came back intact
        return {"access_token": "t", "id_token_claims": {"oid": "oid1", "tid": "tid1"}}

    def get_accounts(self):
        return FakeApp.script.get("accounts", [])

    def acquire_token_silent_with_error(self, scopes, account):
        return FakeApp.script.get("silent")


@pytest.fixture
async def entra(pool, monkeypatch):
    FakeApp.script = {}
    monkeypatch.setattr(auth, "_new_app", lambda cache=None: FakeApp(cache))
    key = Fernet.generate_key().decode()
    entra = auth.EntraAuth(pool, key)
    entra.test_key = key
    return entra


async def sign_in(entra) -> str:
    FakeApp.script["accounts"] = [{"home_account_id": "oid1.tid1", "username": "u@example.com"}]
    url = await entra.start_login({"client": "c1"})
    context, user_id, _ = await entra.complete_login({"state": url.rsplit("=", 1)[1], "code": "x"})
    assert context == {"client": "c1"}
    return user_id


async def test_pending_flow_and_token_cache_are_encrypted(entra, pool):
    user_id = await sign_in(entra)
    await entra.start_login({"client": "c2"})
    async with pool.connection() as conn:
        blobs = [bytes(r[0]) for r in await (await conn.execute("SELECT blob FROM oauth_pending UNION ALL SELECT blob FROM msal_cache")).fetchall()]
    assert len(blobs) == 2 and not any(b"secret-verifier" in b or b"u@example.com" in b for b in blobs)
    assert await entra.is_signed_in(user_id)


async def test_state_is_single_use_and_survives_a_replica(entra, pool):
    url = await entra.start_login({"client": "c1"})
    state = url.rsplit("=", 1)[1]
    replica = auth.EntraAuth(pool, entra.test_key)
    FakeApp.script["accounts"] = [{"home_account_id": "oid1.tid1", "username": "u@example.com"}]
    await replica.complete_login({"state": state})
    with pytest.raises(ValueError, match="Unknown or expired"):
        await entra.complete_login({"state": state})


async def test_token_returned_when_refresh_succeeds(entra):
    user_id = await sign_in(entra)
    FakeApp.script["silent"] = {"access_token": "fresh"}
    assert await entra.get_access_token(user_id) == "fresh"


@pytest.mark.parametrize("result", [None, {"error": "invalid_grant"}, {"error": "interaction_required"}])
async def test_terminal_refresh_failure_signs_out(entra, result):
    user_id = await sign_in(entra)
    FakeApp.script["silent"] = result
    with pytest.raises(auth.NotSignedInError):
        await entra.get_access_token(user_id)
    assert not await entra.is_signed_in(user_id)


async def test_transient_refresh_failure_keeps_the_sign_in(entra):
    user_id = await sign_in(entra)
    FakeApp.script["silent"] = {"error": "temporarily_unavailable", "error_description": "try later"}
    with pytest.raises(RuntimeError, match="try later"):
        await entra.get_access_token(user_id)
    assert await entra.is_signed_in(user_id)


async def test_pending_sign_ins_are_capped(entra, pool, monkeypatch):
    monkeypatch.setattr(auth, "MAX_PENDING", 3)
    for i in range(10):
        await entra.start_login({"i": i})
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM oauth_pending")).fetchone())[0] == 3


async def test_login_never_falls_back_to_a_matching_username(entra):
    # Same username, different object id: another user's cached account must not be adopted.
    FakeApp.script["accounts"] = [{"home_account_id": "other.tid1", "username": "u@example.com"}]
    url = await entra.start_login({})
    with pytest.raises(ValueError):
        await entra.complete_login({"state": url.rsplit("=", 1)[1]})
    assert not await entra.is_signed_in("other.tid1")


async def test_sign_out_revokes_mcp_tokens_too(entra, pool):
    user_id = await sign_in(entra)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO oauth_clients (client_id, info) VALUES ('c1', '{}')")
        await conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, subject, data, expires_at) "
            "VALUES ('h', 'refresh', 'c1', %s, '{}', now() + interval '1 day')", (user_id,))
    await entra.sign_out(user_id)
    await sign_in(entra)  # signing in again must not revive the old client token
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM oauth_tokens")).fetchone())[0] == 0


async def test_cache_under_another_key_counts_as_signed_out(entra, pool):
    user_id = await sign_in(entra)
    other = auth.EntraAuth(pool, Fernet.generate_key().decode())
    with pytest.raises(auth.NotSignedInError):
        await other.get_access_token(user_id)


def test_bad_key_is_rejected():
    with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
        auth.EntraAuth(None, "not-a-key")

