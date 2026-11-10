"""Gemini embeddings via the Google AI Studio (Generative Language) API."""
import asyncio
import math

import httpx

from . import config

BASE = "https://generativelanguage.googleapis.com/v1beta"
BATCH_SIZE = 100  # batchEmbedContents limit
MAX_ATTEMPTS = 5


def _normalize(vector: list[float]) -> list[float]:
    # Gemini only returns unit-length vectors at full size; truncated dims must be re-normalised.
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    res: httpx.Response | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            res = await client.post(url, json=body, headers={"x-goog-api-key": config.require_gemini_key()})
        except httpx.TransportError:  # timeouts, connection resets
            res = None
            if attempt == MAX_ATTEMPTS - 1:
                raise
        if res is not None and res.status_code not in (429, 500, 502, 503, 504):
            break
        if attempt < MAX_ATTEMPTS - 1:
            retry_after = res.headers.get("Retry-After", "") if res is not None else ""
            await asyncio.sleep(min(float(retry_after) if retry_after.isdigit() else 2**attempt, 60))
    assert res is not None
    if res.is_error:
        try:
            detail = res.json().get("error", {}).get("message", res.text)
        except ValueError:  # non-JSON body, e.g. an HTML error page from a proxy
            detail = res.text[:200]
        raise RuntimeError(f"Gemini {res.status_code}: {detail}")
    return res.json()


async def _embed(texts: list[str], task_type: str) -> list[list[float]]:
    if any(not t.strip() for t in texts):
        raise ValueError("Cannot embed empty text; filter blank chunks before embedding")
    model = f"models/{config.EMBEDDING_MODEL}"
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(texts), BATCH_SIZE):
            body = {
                "requests": [
                    {
                        "model": model,
                        "content": {"parts": [{"text": t}]},
                        "taskType": task_type,
                        "outputDimensionality": config.EMBEDDING_DIMENSIONS,
                    }
                    for t in texts[i : i + BATCH_SIZE]
                ]
            }
            data = await _post(client, f"{BASE}/{model}:batchEmbedContents", body)
            batch = data["embeddings"]
            if len(batch) != len(body["requests"]):
                raise RuntimeError(f"Gemini returned {len(batch)} embeddings for {len(body['requests'])} inputs")
            vectors += [_normalize(e["values"]) for e in batch]
    return vectors


async def embed_documents(texts: list[str]) -> list[list[float]]:
    return await _embed(texts, "RETRIEVAL_DOCUMENT")


async def embed_query(text: str) -> list[float]:
    return (await _embed([text], "RETRIEVAL_QUERY"))[0]
