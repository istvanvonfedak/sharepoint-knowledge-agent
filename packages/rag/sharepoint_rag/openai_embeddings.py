"""OpenAI embeddings through LangChain (`langchain_openai.OpenAIEmbeddings`).

Each request is traced in LangSmith (when tracing is on) as one run with the model and its token count, so embedding
shows up with its cost next to the agent's: indexing as runs of their own, a search query's inside the trace of the
call that asked for it (`sharepoint_rag.inference` passes the trace along). Document text is never sent to LangSmith,
only how many texts and tokens; a query's text is, as the agent's trace has it anyway.

OpenAI's quota is in tokens per minute, so `LIMITER` (embeddings.Limiter, the same bucket as Gemini's) counts tokens
here. A 429 the client's own retries don't get past raises `RateLimited`, as for Gemini: the sync-worker defers the
job, and a query has already been retried briefly.
"""
import os
import re

import httpx
import langsmith as ls
import tiktoken
from langchain_openai import OpenAIEmbeddings
from openai import APIStatusError, RateLimitError

from .embeddings import Limiter, RateLimited, _normalize, trace_inputs, trace_metadata, usage

ENCODING = tiktoken.get_encoding("cl100k_base")  # text-embedding-3-*'s tokenizer
MAX_INPUT_TOKENS = 8191  # per text; longer ones fail the whole request, so they are cut
BATCH_TEXTS, BATCH_TOKENS = 512, 250_000  # per request (OpenAI's limits: 2048 inputs, 300k tokens)
# Below the account's quota, so other callers of the key have room. Per process, like Gemini's.
TOKENS_PER_MINUTE = int(os.environ.get("OPENAI_EMBED_TOKENS_PER_MINUTE", "900000"))
LIMITER = Limiter(TOKENS_PER_MINUTE, 0.1)
UNHINTED_429_WAIT = 20.0


def _fit(text: str) -> tuple[str, int]:
    tokens = ENCODING.encode(text, disallowed_special=())
    if len(tokens) > MAX_INPUT_TOKENS:
        return ENCODING.decode(tokens[:MAX_INPUT_TOKENS]), MAX_INPUT_TOKENS
    return text, len(tokens)


def _batches(texts: list[str]) -> list[tuple[list[str], int]]:
    batches, batch, size = [], [], 0
    for text, n in map(_fit, texts):
        if batch and (len(batch) == BATCH_TEXTS or size + n > BATCH_TOKENS):
            batches.append((batch, size))
            batch, size = [], 0
        batch.append(text)
        size += n
    if batch:
        batches.append((batch, size))
    return batches


def retry_after(e: APIStatusError) -> float:
    """OpenAI's Retry-After header, or "Please try again in 1.5s" / "in 20ms" in its message."""
    header = e.response.headers.get("retry-after", "") if e.response is not None else ""
    if header.replace(".", "", 1).isdigit():
        return float(header)
    if m := re.search(r"try again in (\d+(?:\.\d+)?)(ms|s)\b", str(e.message), re.I):
        return float(m.group(1)) / (1000 if m.group(2).lower() == "ms" else 1)
    return UNHINTED_429_WAIT


@ls.traceable(run_type="llm", name="embed", process_inputs=trace_inputs,
              process_outputs=lambda o: {k: v for k, v in o.items() if k != "vectors"})
async def _request(texts: list[str], *, model: str, dimensions: int, tokens: int, query: bool) -> dict:
    # Its own HTTP client per request: an evals run uses several event loops, and a pooled client belongs to one.
    async with httpx.AsyncClient(timeout=60) as http:
        client = OpenAIEmbeddings(model=model, dimensions=dimensions, check_embedding_ctx_length=False,
                                  chunk_size=len(texts), max_retries=1 if query else 2, http_async_client=http)
        vectors = await client.aembed_documents(texts)
    if len(vectors) != len(texts):
        raise RuntimeError(f"OpenAI returned {len(vectors)} embeddings for {len(texts)} inputs")
    return {"vectors": vectors, **usage(tokens)}


async def embed(texts: list[str], model: str, dimensions: int, query: bool) -> list[list[float]]:
    if any(not t.strip() for t in texts):
        raise ValueError("Cannot embed empty text; filter blank chunks before embedding")
    vectors: list[list[float]] = []
    for batch, tokens in _batches(texts):
        await LIMITER.acquire(tokens, query=query)
        try:
            out = await _request(batch, model=model, dimensions=dimensions, tokens=tokens, query=query,
                                 langsmith_extra={"metadata": trace_metadata("openai", model)})
        except RateLimitError as e:
            wait = retry_after(e)
            LIMITER.pause(wait)  # the other document batches would only use up the same quota
            raise RateLimited(f"OpenAI 429: {e.message}", retry_after=wait) from None
        vectors += [_normalize(v) for v in out["vectors"]]
    return vectors
