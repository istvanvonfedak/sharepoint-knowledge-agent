"""Client for embedding: the inference-server when INFERENCE_URL is set, otherwise in-process."""
import httpx
import langsmith as ls

from . import config, models
from .embeddings import RateLimited, retry_delay
from .models import Task


async def _call(path: str, body: dict) -> dict:
    headers = {"Authorization": f"Bearer {config.INFERENCE_API_KEY}"} if config.INFERENCE_API_KEY else {}
    if run := ls.get_current_run_tree():  # the inference-server traces the embedding inside the caller's trace
        headers.update(run.to_headers())
    async with httpx.AsyncClient(timeout=300) as client:
        res = await client.post(f"{config.INFERENCE_URL}{path}", json=body, headers=headers)
    if res.status_code == 429:
        raise RateLimited(f"inference-server {path} 429: {res.text[:300]}", retry_after=retry_delay(res) or 60.0)
    if res.is_error:
        raise RuntimeError(f"inference-server {path} {res.status_code}: {res.text[:300]}")
    return res.json()


REQUEST_BATCH = 256  # texts per /embed call; the inference-server accepts up to 512


async def embed(model: str, texts: list[str], task: Task, dimensions: int) -> list[list[float]]:
    if not texts:
        return []
    if config.INFERENCE_URL:
        vectors = []
        for i in range(0, len(texts), REQUEST_BATCH):
            body = {"model": model, "texts": texts[i : i + REQUEST_BATCH], "task": task, "dimensions": dimensions}
            vectors += (await _call("/embed", body))["vectors"]
    else:
        vectors = await models.embed(model, texts, task, dimensions)
    if len(vectors) != len(texts) or any(len(v) != dimensions for v in vectors):
        raise RuntimeError(f"Embedding returned {len(vectors)} vectors for {len(texts)} texts or the wrong size")
    return vectors

