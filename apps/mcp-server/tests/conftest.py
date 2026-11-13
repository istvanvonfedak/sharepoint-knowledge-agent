import sys
import types

# The real auth module builds an MSAL app (network authority discovery) at import; tests use a fake.
fake = types.ModuleType("sharepoint_client.auth")
fake.pending = {}
fake.signed_out = []


def start_login(context):
    state = f"state{len(fake.pending)}"
    fake.pending[state] = context
    return f"https://entra.test/authorize?state={state}"


def cancel_login(state):
    return fake.pending.pop(state, None)


def complete_login(query):
    return fake.pending.pop(query["state"]), "user-1", "user@example.com"


fake.start_login, fake.cancel_login, fake.complete_login = start_login, cancel_login, complete_login
fake.sign_out = fake.signed_out.append
sys.modules["sharepoint_client.auth"] = fake
