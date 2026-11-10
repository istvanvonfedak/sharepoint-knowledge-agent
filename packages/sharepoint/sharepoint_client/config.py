import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env var {name} (see .env.example)")
    return value


PORT = int(os.environ.get("PORT", "8080"))
BASE_URL = f"http://localhost:{PORT}"
REDIRECT_URI = f"{BASE_URL}/oauth/callback"
TENANT_ID = _required("AZURE_TENANT_ID")
CLIENT_ID = _required("AZURE_CLIENT_ID")
CLIENT_SECRET = _required("AZURE_CLIENT_SECRET")
# offline_access is added by MSAL automatically.
SCOPES = ["User.Read", "Sites.Read.All", "Files.Read.All"]
