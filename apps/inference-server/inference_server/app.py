"""inference-server: embedding behind one call, so the hosted-API key stays out of the other services.

    POST /embed   {"model", "texts", "task": "document"|"query", "dimensions"} -> {"vectors"}
    GET  /health  -> {"ok"}

The backend is sharepoint_rag.models: hosted Gemini (chunk text leaves our infrastructure). Callers
authenticate with INFERENCE_API_KEY.
"""
import hmac
import logging
import math
import os

from pydantic import BaseModel, Field
from sharepoint_rag import models
from sharepoint_rag.embeddings import RateLimited
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

API_KEY = os.environ.get("INFERENCE_API_KEY", "")
# Anyone who can reach the service could otherwise spend the hosted-API key; opting out must be explicit.
if not API_KEY and os.environ.get("INFERENCE_ALLOW_ANONYMOUS", "").lower() != "true":
    raise RuntimeError("Set INFERENCE_API_KEY (or INFERENCE_ALLOW_ANONYMOUS=true for a closed test setup)")
MAX_TEXTS = 512
MAX_TEXT_CHARS = 20_000


class EmbedRequest(BaseModel):
    model: str
    texts: list[str] = Field(max_length=MAX_TEXTS)
    task: str = Field(pattern="^(document|query)$")
    dimensions: int = Field(ge=1, le=4096)


def _authorized(request: Request) -> bool:
    if not API_KEY:
        return True
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    return hmac.compare_digest(supplied.encode(), API_KEY.encode())


async def _parse(request: Request, model: type[EmbedRequest]):
    if not _authorized(request):
        return None, JSONResponse({"error": "unauthorized"}, 401)
    try:
        body = model.model_validate(await request.json())
    except ValueError as e:
        return None, JSONResponse({"error": str(e)[:500]}, 400)
    if any(len(t) > MAX_TEXT_CHARS for t in body.texts):
        return None, JSONResponse({"error": f"texts are limited to {MAX_TEXT_CHARS} characters"}, 400)
    return body, None


async def embed(request: Request) -> JSONResponse:
    body, error = await _parse(request, EmbedRequest)
    if error:
        return error
    try:
        vectors = await models.embed(body.model, body.texts, body.task, body.dimensions)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    except RateLimited as e:  # the quota, not the request: tell the caller when to come back
        log.warning("embed rate limited; retry in %.0fs", e.retry_after)
        return JSONResponse({"error": str(e)[:500]}, 429, headers={"Retry-After": str(math.ceil(e.retry_after))})
    except Exception as e:
        log.exception("embed failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"[:500]}, 502)
    return JSONResponse({"vectors": vectors})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/embed", embed, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
])
