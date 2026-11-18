"""mcp-server settings for the delegated (user) Entra app. The sync-worker has its own app registration and
never imports this module, so it never needs this app's secret (and vice versa). The mcp-server loads them from
.env.mcp."""
import os


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env var {name} (see .env.mcp.example)")
    return value


PORT = int(os.environ.get("PORT", "8080"))
# Loopback unless overridden; the Docker image sets 0.0.0.0 and publishes the port on 127.0.0.1 only.
BIND_HOST = os.environ.get("MCP_BIND_HOST", "127.0.0.1")
# Where clients reach the server: the OAuth issuer, resource URL and Entra redirect URI all derive from it.
# Behind a TLS proxy set it to the public https URL, e.g. https://sharepoint-mcp.example.com
BASE_URL = (os.environ.get("PUBLIC_URL") or f"http://localhost:{PORT}").rstrip("/")
REDIRECT_URI = f"{BASE_URL}/oauth/callback"
TENANT_ID = _required("AZURE_TENANT_ID")
CLIENT_ID = _required("AZURE_CLIENT_ID")
CLIENT_SECRET = _required("AZURE_CLIENT_SECRET")
# offline_access is added by MSAL automatically. User.Read also covers /me/transitiveMemberOf (the group lookup
# for search permissions). Extra scopes can be added with GRAPH_EXTRA_SCOPES (space separated).
SCOPES = ["User.Read", "Sites.Read.All", "Files.Read.All", *os.environ.get("GRAPH_EXTRA_SCOPES", "").split()]
