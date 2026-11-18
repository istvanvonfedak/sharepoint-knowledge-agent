import os

from dotenv import load_dotenv

load_dotenv()  # the shared .env; each service loads its own file (.env.mcp, .env.worker) first

# Optional at startup so the plain SharePoint tools run without them.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# The Gemini model of the first index generation (`gemini:<model>` or a bare name).
# Later generations are created with `python -m sharepoint_rag.generations create`.
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# inference-server base URL (e.g. http://inference-server:8000). Unset: embed in-process.
INFERENCE_URL = os.environ.get("INFERENCE_URL", "").rstrip("/")
INFERENCE_API_KEY = os.environ.get("INFERENCE_API_KEY", "")
# What to do with files whose permissions could not be fully resolved: "hide" (nobody sees them in search) or
# "live_check" (everyone's search may match them, and only the live Graph check decides). See docs/architecture.md.
UNRESOLVED_ACL_POLICY = os.environ.get("UNRESOLVED_ACL_POLICY", "hide")
if UNRESOLVED_ACL_POLICY not in ("hide", "live_check"):
    raise RuntimeError("UNRESOLVED_ACL_POLICY must be 'hide' or 'live_check'")


def require_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. See .env.example (postgresql://postgres:<POSTGRES_PASSWORD>@localhost:5432/sharepoint_rag)")
    return DATABASE_URL


def require_gemini_key() -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Create one at https://aistudio.google.com/apikey")
    return GEMINI_API_KEY
