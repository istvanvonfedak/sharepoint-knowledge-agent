import importlib.util
from pathlib import Path

import msal
import pytest
import sharepoint_client.config  # noqa: F401  (the copy of auth.py loaded below does `from . import config`)

AUTH_PATH = Path(__file__).parents[1] / "sharepoint_client" / "auth.py"


class FakeApp:
    def __init__(self, *args, **kwargs):
        self.silent_result = None

    def acquire_token_silent(self, scopes, account):
        return self.silent_result


@pytest.fixture
def real_auth(monkeypatch):
    """Loads the real auth.py with MSAL faked (the real app does a network authority lookup on import)."""
    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    spec = importlib.util.spec_from_file_location("sharepoint_client._auth_under_test", AUTH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._accounts["u"] = {"home_account_id": "u", "username": "u@example.com"}
    return module


def test_token_returned_when_refresh_succeeds(real_auth):
    real_auth._app.silent_result = {"access_token": "t"}
    assert real_auth.get_access_token("u") == "t"


@pytest.mark.parametrize("result", [None, {"error": "invalid_grant"}, {"error": "interaction_required"}])
def test_terminal_refresh_failure_drops_the_account(real_auth, result):
    real_auth._app.silent_result = result
    with pytest.raises(real_auth.NotSignedInError):
        real_auth.get_access_token("u")
    assert not real_auth.is_signed_in("u")


def test_transient_refresh_failure_keeps_the_account(real_auth):
    real_auth._app.silent_result = {"error": "temporarily_unavailable", "error_description": "try later"}
    with pytest.raises(RuntimeError, match="try later"):
        real_auth.get_access_token("u")
    assert real_auth.is_signed_in("u")


def test_pending_sign_ins_are_capped(real_auth, monkeypatch):
    monkeypatch.setattr(real_auth, "MAX_PENDING", 3)
    counter = iter(range(100))
    real_auth._app.initiate_auth_code_flow = lambda scopes, redirect_uri: {"state": f"s{next(counter)}", "auth_uri": "u"}
    for i in range(10):
        real_auth.start_login(i)
    assert list(real_auth._pending) == ["s7", "s8", "s9"]  # oldest are dropped first
