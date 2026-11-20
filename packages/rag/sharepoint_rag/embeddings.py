"""Gemini embeddings via the Google AI Studio (Generative Language) API.

Gemini's embedding quota is per minute and counts every text, also inside a batch call (Tier 1: 3,000 a
minute). Callers in one process share it through `LIMITER`: indexing waits for room instead of colliding at
the limit, and a slice is kept free for search queries, so a user's search never waits behind a crawl.

When the quota runs out anyway (another user of the key, a daily cap), a document request fails fast with
`RateLimited`: the sync-worker defers the job and resumes it from its cached batches, so nothing waits long inside
a request. A query retries briefly (QUERY_MAX_WAIT): a user is waiting for it.

Each batch is traced in LangSmith (when tracing is on) as an `embed` run with its token count, which Gemini's embed
response doesn't give: a countTokens call alongside it does (free, and not part of the embedding quota). Document text
is never sent to LangSmith (`trace_inputs`)."""
import asyncio
import math
import os
import re
import time

import httpx
import langsmith as ls

from . import config

BASE = "https://generativelanguage.googleapis.com/v1beta"
BATCH_SIZE = 100  # batchEmbedContents limit
MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 60.0  # the quota window is a minute: no single wait needs to be longer
QUERY_MAX_WAIT = 2.0  # per retry of a rate-limited query
UNHINTED_429_WAIT = 10.0
# A document request waits out a 429 this short itself (the minute window turning); a longer one, or none given,
# defers the job instead: holding the request open wouldn't help, and the job resumes from its saved batches.
SHORT_429_WAIT = 10.0
MAX_ACQUIRE_WAIT = 30.0  # a document batch facing a longer pause after a 429 is deferred instead
# Texts per minute this process may send. Below the quota, so other callers of the key and clock skew have room.
# Each process has its own budget: with several inference-server replicas, divide the quota between them.
TEXTS_PER_MINUTE = int(os.environ.get("GEMINI_EMBED_PER_MINUTE", "2700"))
QUERY_RESERVE = 0.1  # share of the budget documents may not use


class RateLimited(RuntimeError):
    """The quota is used up: try again in `retry_after` seconds. Not a failure of the request itself."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Limiter:
    """A token bucket in texts per minute, refilled continuously. Queries may use the whole bucket; documents
    must leave `reserve` of it, so queries find room even while indexing runs flat out. After a 429, `pause`
    holds documents back until the quota resets (the bucket only knows this process's traffic); queries are
    never held back."""

    def __init__(self, per_minute: int, reserve: float, clock=time.monotonic) -> None:
        self.capacity = float(max(1, per_minute))
        self.rate = self.capacity / 60.0
        self.reserve = self.capacity * reserve
        self._clock = clock
        self._tokens = self.capacity
        self._updated = clock()
        self._paused_until = 0.0
        # Waiters are served in order, so a big batch isn't starved by small ones; one lock per kind, so a query
        # never queues behind a document batch that is waiting for tokens. Made per event loop: a lock belongs to
        # the loop that first waited on it, and evals run several loops in turn.
        self._locks: dict[bool, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def wait_for(self, n: int, query: bool) -> float:
        """Seconds until `n` texts may be sent (0: now). Takes the tokens when it returns 0."""
        self._refill()
        n = min(float(n), self.capacity - (0 if query else self.reserve))  # a batch bigger than the bucket waits for a full one
        floor = 0.0 if query else self.reserve
        if not query and (pause := self.paused_for()) > 0:
            return pause
        # A tolerance: refilled tokens are floats, and a batch capped at `capacity - reserve` must fit a full
        # bucket exactly, not miss it by a rounding error and wait a few ulps forever.
        if self._tokens - n >= floor - 1e-6:
            self._tokens = max(0.0, self._tokens - n)
            return 0.0
        return max((n + floor - self._tokens) / self.rate, 0.01)

    def _lock(self, query: bool) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop, self._locks = loop, {True: asyncio.Lock(), False: asyncio.Lock()}
        return self._locks[query]

    async def acquire(self, n: int, query: bool) -> None:
        """Waits until `n` texts may be sent. A document batch facing a long pause after a 429 raises RateLimited
        instead: its caller defers the work rather than hold a request open. An ordinary refill wait is never
        that long (at most a minute, however small the budget) and is simply waited out."""
        async with self._lock(query):
            while (wait := self.wait_for(n, query)) > 0:
                if not query and self.paused_for() > MAX_ACQUIRE_WAIT:
                    raise RateLimited(f"embedding quota: {wait:.0f}s until there is room", retry_after=wait)
                await asyncio.sleep(wait)

    def paused_for(self) -> float:
        return self._paused_until - self._clock()

    def pause(self, seconds: float) -> None:
        """After a 429: nothing is sent for `seconds`, and the bucket starts empty when it ends. The refill clock
        restarts too, or the time before the 429 would be credited back as room the quota doesn't have."""
        now = self._clock()
        self._paused_until = max(self._paused_until, now + seconds)
        self._tokens, self._updated = 0.0, now


LIMITER = Limiter(TEXTS_PER_MINUTE, QUERY_RESERVE)


def retry_delay(res: httpx.Response) -> float | None:
    """How long to wait before trying again: a Retry-After header (our inference-server's, which already has its
    margin added), Gemini's RetryInfo detail ("retryDelay": "4.03s"), or the same in its message ("Please retry in
    4.03s."). None if the response doesn't say."""
    header = res.headers.get("Retry-After", "")
    if header.replace(".", "", 1).isdigit():
        return float(header)
    try:
        body = res.json()
    except ValueError:
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):  # our inference-server's {"error": "<message>"}
        error = {"message": error}
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("RetryInfo"):
            if m := re.fullmatch(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay", ""))):
                return float(m.group(1))
    if m := re.search(r"retry in (\d+(?:\.\d+)?)\s*s\b", str(error.get("message", "")), re.I):
        return float(m.group(1))
    return None


async def _post(client: httpx.AsyncClient, url: str, body: dict, *, query: bool = False) -> dict:
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
        hinted = retry_delay(res) if res is not None else None
        if res is not None and res.status_code == 429:
            # The full delay (plus a second: Gemini's figure is when the window turns), not capped like the sleeps
            # here: a daily cap's "retry in 3600s" means an hour, and the sync-worker defers its jobs that long
            # instead of re-downloading their files every minute. No hint: the window is a minute.
            retry_after = hinted + 1.0 if hinted is not None else UNHINTED_429_WAIT
            LIMITER.pause(retry_after)  # hold the other document batches back too: they'd only use up the same quota
            if query:
                wait = min(retry_after, QUERY_MAX_WAIT)  # a user is waiting: retry soon, give up soon
            elif hinted is not None and retry_after <= SHORT_429_WAIT:
                wait = retry_after  # the window is about to turn: cheaper to wait than to defer and redo the download
            else:
                break  # deferred by the caller, which resumes from its saved batches
        else:
            wait = hinted if hinted is not None else 2**attempt
        wait = min(wait, MAX_WAIT_SECONDS)
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(wait)
    assert res is not None
    if res.is_error:
        try:
            body = res.json()
            detail = body["error"].get("message", res.text) if isinstance(body.get("error"), dict) else res.text[:200]
        except (ValueError, AttributeError):  # not a JSON object, e.g. an HTML error page from a proxy
            detail = res.text[:200]
        if res.status_code == 429:
            raise RateLimited(f"Gemini 429: {detail}", retry_after=retry_after)
        raise RuntimeError(f"Gemini {res.status_code}: {detail}")
    return res.json()


async def embed(texts: list[str], task_type: str, model_name: str, dimensions: int) -> list[list[float]]:
    """`task_type` is RETRIEVAL_DOCUMENT or RETRIEVAL_QUERY."""
    if any(not t.strip() for t in texts):
        raise ValueError("Cannot embed empty text; filter blank chunks before embedding")
    model = f"models/{model_name}"
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(texts), BATCH_SIZE):
            body = {
                "requests": [
                    {
                        "model": model,
                        "content": {"parts": [{"text": t}]},
                        "taskType": task_type,
                        "outputDimensionality": dimensions,
                    }
                    for t in texts[i : i + BATCH_SIZE]
                ]
            }
            query = task_type == "RETRIEVAL_QUERY"
            await LIMITER.acquire(len(body["requests"]), query=query)
            batch = await _request(client, texts[i : i + BATCH_SIZE], body, model=model_name, dimensions=dimensions,
                                   query=query, langsmith_extra={"metadata": trace_metadata("google_genai", model_name)})
            vectors += [_normalize(e["values"]) for e in batch["embeddings"]]
    return vectors


def trace_inputs(inputs: dict) -> dict:
    """What an embedding run shows in LangSmith: how many texts, never a document's text; a query's, as the agent's
    trace has it anyway."""
    texts = inputs["texts"]
    shown = {"model": inputs["model"], "dimensions": inputs["dimensions"], "texts": len(texts)}
    if inputs["query"]:
        shown["query"] = texts[0] if len(texts) == 1 else texts
    return shown


def trace_metadata(provider: str, model: str) -> dict:
    """LangSmith prices a run from these and its outputs' usage_metadata."""
    return {"ls_provider": provider, "ls_model_name": model, "ls_model_type": "embedding"}


def usage(tokens: int, estimated: bool = False) -> dict:
    out = {"usage_metadata": {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}}
    return out | ({"tokens_estimated": True} if estimated else {})


async def _count_tokens(client: httpx.AsyncClient, model: str, texts: list[str]) -> tuple[int, bool]:
    """The batch's input tokens, for its trace; an estimate (4 characters a token) if Gemini can't say."""
    try:
        res = await client.post(f"{BASE}/models/{model}:countTokens", headers={"x-goog-api-key": config.require_gemini_key()},
                                json={"contents": [{"parts": [{"text": t} for t in texts]}]})
        res.raise_for_status()
        return int(res.json()["totalTokens"]), False
    except (httpx.HTTPError, KeyError, ValueError):
        return sum(len(t) for t in texts) // 4 + 1, True


@ls.traceable(run_type="llm", name="embed", process_inputs=trace_inputs,
              process_outputs=lambda o: {k: v for k, v in o.items() if k != "embeddings"})
async def _request(client: httpx.AsyncClient, texts: list[str], body: dict, *, model: str, dimensions: int,
                   query: bool) -> dict:
    url = f"{BASE}/models/{model}:batchEmbedContents"
    # Counting only for a trace: untraced, it would be a request for nothing.
    if ls.get_current_run_tree() is None:
        data = await _post(client, url, body, query=query)
        tokens = None
    else:
        data, tokens = await asyncio.gather(_post(client, url, body, query=query), _count_tokens(client, model, texts))
    batch = data["embeddings"]
    if len(batch) != len(texts):
        raise RuntimeError(f"Gemini returned {len(batch)} embeddings for {len(texts)} inputs")
    return {"embeddings": batch, **(usage(*tokens) if tokens else {})}


def _normalize(vector: list[float]) -> list[float]:
    # Gemini only returns unit-length vectors at full size; truncated dims must be re-normalised.
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
