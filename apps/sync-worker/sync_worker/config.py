"""sync-worker settings. It uses its own Entra app registration (SYNC_AZURE_*), separate from the mcp-server's,
so a compromised mcp-server can't mint tenant-wide app tokens. Never put the mcp-server's secret here."""
import os


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env var {name} (see .env.worker.example)")
    return value


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


TENANT_ID = os.environ.get("SYNC_AZURE_TENANT_ID") or _required("AZURE_TENANT_ID")
CLIENT_ID = _required("SYNC_AZURE_CLIENT_ID")
# PEM file holding the app's private key and its certificate (uploaded to the app registration). A certificate,
# not a client secret: SharePoint's own REST API rejects app-only tokens obtained with a secret.
CERT_PATH = _required("SYNC_AZURE_CERT_PATH")

# Sites to index, comma separated: Graph site ids or `hostname:/sites/path`. Added to the `sites` table on each
# discovery run; sites can also be managed with `python -m sync_worker sites add|disable|list`.
SITES = [s.strip() for s in os.environ.get("SYNC_SITES", "").split(",") if s.strip()]
# With tenant-wide Sites.Read.All the worker can list every site itself instead of using the admin's list.
DISCOVER_ALL_SITES = os.environ.get("SYNC_DISCOVER_ALL_SITES", "false").lower() == "true"

CRAWL_INTERVAL = _int("SYNC_CRAWL_INTERVAL_SECONDS", 300)
DISCOVER_INTERVAL = _int("SYNC_DISCOVER_INTERVAL_SECONDS", 3600)
SWEEP_INTERVAL = _int("SYNC_ACL_SWEEP_INTERVAL_SECONDS", 86400)
CONCURRENCY = _int("SYNC_CONCURRENCY", 4)  # jobs run at once per replica
GRAPH_CONCURRENCY = _int("SYNC_GRAPH_CONCURRENCY", 8)  # Graph requests in flight per replica
MAX_FILE_BYTES = _int("SYNC_MAX_FILE_BYTES", 50_000_000)
