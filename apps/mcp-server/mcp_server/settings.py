"""mcp-server settings beyond the Entra app (sharepoint_client.config). Loaded from .env.mcp (see __init__)."""
import json
import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


# Fernet key for the Entra token cache and sign-ins in progress. Keep it out of the database (and its backups):
# a database leak plus this key exposes every signed-in user's access.
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
# Key for the audit log's query HMACs. Defaults to one derived from TOKEN_ENCRYPTION_KEY.
AUDIT_HMAC_KEY = os.environ.get("AUDIT_HMAC_KEY", "")

# Client registration is open (MCP clients register themselves), but only to these redirect URIs. Loopback
# URIs (any port) are always allowed: a code sent there only reaches the user's own machine. Add hosted
# clients' callbacks here, comma separated, e.g. https://claude.ai/api/mcp/auth_callback
ALLOWED_REDIRECT_URIS = {u.strip() for u in os.environ.get("OAUTH_ALLOWED_REDIRECT_URIS", "").split(",") if u.strip()}
MAX_CLIENTS = _int("OAUTH_MAX_CLIENTS", 1000)

# MCP sessions live in one process's memory. Behind several replicas, route each client to the same replica
# (sticky routing, preferred: `sharepoint_ask` progress updates need a long-lived connection), or set this to
# run the SDK in stateless mode.
STATELESS = os.environ.get("MCP_STATELESS", "false").lower() == "true"

# "index": our permission-filtered index (needs the sync-worker). "graph": Microsoft Search, live.
SEARCH_BACKEND = os.environ.get("SEARCH_BACKEND", "index")
if SEARCH_BACKEND not in ("index", "graph"):
    raise RuntimeError("SEARCH_BACKEND must be 'index' or 'graph'")
GROUP_CACHE_SECONDS = _int("GROUP_CACHE_SECONDS", 300)

# Per-user limits, shared by all replicas through Postgres.
SEARCH_PER_MINUTE = _int("RATE_SEARCH_PER_MINUTE", 30)
ASK_PER_HOUR = _int("RATE_ASK_PER_HOUR", 20)
ASK_TOKENS_PER_DAY = _int("RATE_ASK_TOKENS_PER_DAY", 2_000_000)

# sharepoint_ask sends text the user may read to the model's provider: Gemini by default, with GEMINI_API_KEY
# (the embedding key). Without a key it stays off; ASK_MODEL=none turns it off explicitly. Any other LangChain
# chat model works as "<provider>:<model>" once its langchain-<provider> package is installed in the image and
# its own key variable is set (e.g. anthropic:claude-opus-5-5 with ANTHROPIC_API_KEY).
DEFAULT_ASK_MODEL = "google_genai:gemini-3.8-flash"
ASK_MODEL = os.environ.get("ASK_MODEL", DEFAULT_ASK_MODEL).strip()
if ASK_MODEL.lower() == "none":
    ASK_MODEL = ""
# langchain-google-genai reads GOOGLE_API_KEY, then GEMINI_API_KEY.
if ASK_MODEL.startswith("google_genai:") and not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
    ASK_MODEL = ""
# Extra keyword arguments for init_chat_model, as JSON, e.g. {"temperature": 0} or {"max_tokens": 8000}.
ASK_MODEL_KWARGS = json.loads(os.environ.get("ASK_MODEL_KWARGS") or "{}")
ASK_MAX_STEPS = _int("ASK_MAX_STEPS", 8)
# Tokens one sharepoint_ask may use across all its steps (input + output; cache reads count a tenth).
ASK_MAX_TOKENS = _int("ASK_MAX_TOKENS", 300_000)
ASK_TIMEOUT_SECONDS = _int("ASK_TIMEOUT_SECONDS", 120)
