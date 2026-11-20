"""A persistent embedding cache for the evals, so a suite is embedded once and later runs cost nothing.

Keyed by model, dimensions, task (document or query) and the exact text embedded (`Chunk.embedding_text`:
heading path plus text), so a chunking or heading change re-embeds only the chunks it changed. Stored in SQLite
under data/.cache/ (EVAL_CACHE_DIR), next to the datasets it holds vectors of, and never committed.

`install()` puts the cache in front of `sharepoint_rag.inference.embed`, so the production retriever
(`PostgresRetriever`, which the layer-2 and agent evals run through) uses it too.
"""
import array
import asyncio
import hashlib
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path

from sharepoint_rag import inference

from sharepoint_evals.corpus import DATA

PATH = Path(os.environ.get("EVAL_CACHE_DIR") or DATA / ".cache") / "embeddings.sqlite3"
BATCH = 500  # texts per call to the real embedder
# Gemini counts every text as one request (Tier 1: 3,000 a minute), so a full suite build would hit the quota.
PER_MINUTE = int(os.environ.get("EVAL_EMBED_PER_MINUTE", "2500"))
QUOTA_RETRIES = 10
_uncached = inference.embed
_sent: deque[tuple[float, int]] = deque()  # (when, texts) over the last minute
stats = {"hits": 0, "misses": 0}


async def _paced(model: str, texts: list[str], task: str, dimensions: int) -> list[list[float]]:
    """The real embedder, kept under PER_MINUTE texts a minute, waiting out a quota error rather than failing.
    Waiting for room in the budget is not an attempt; only a quota error (another process using the same key)
    counts toward QUOTA_RETRIES. OpenAI's quota is in tokens, which its backend paces itself."""
    paced = not model.startswith("openai:")
    for attempt in range(QUOTA_RETRIES):
        while paced:
            while _sent and time.monotonic() - _sent[0][0] > 60:
                _sent.popleft()
            if not _sent or sum(n for _, n in _sent) + len(texts) <= PER_MINUTE:
                break
            await asyncio.sleep(60 - (time.monotonic() - _sent[0][0]) + 0.1)
        _sent.append((time.monotonic(), len(texts)))
        try:
            return await _uncached(model, texts, task, dimensions)
        except RuntimeError as e:
            if " 429" not in str(e) or attempt == QUOTA_RETRIES - 1:
                raise
            print("[embed] provider quota reached (another process on the same key?); waiting a minute", file=sys.stderr)
            await asyncio.sleep(60)
    raise AssertionError("unreachable")


def _key(model: str, dimensions: int, task: str, text: str) -> bytes:
    return hashlib.sha256(f"{model}\0{dimensions}\0{task}\0{text}".encode()).digest()


_db: tuple[Path, sqlite3.Connection] | None = None
_db_lock = threading.Lock()  # one connection, used from worker threads: one at a time


def _connect() -> sqlite3.Connection:
    """One connection per process (and per PATH, which tests change)."""
    global _db
    if _db is None or _db[0] != PATH:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(PATH, timeout=60, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")  # the live_check run is a second process using the same file
        db.execute("CREATE TABLE IF NOT EXISTS embeddings (key BLOB PRIMARY KEY, vector BLOB NOT NULL)")
        _db = (PATH, db)
    return _db[1]


def _lookup(keys: list[bytes]) -> dict[bytes, list[float]]:
    found: dict[bytes, list[float]] = {}
    with _db_lock, _connect() as db:
        for i in range(0, len(keys), 900):  # SQLite's bound-parameter limit
            part = keys[i:i + 900]
            rows = db.execute(f"SELECT key, vector FROM embeddings WHERE key IN ({','.join('?' * len(part))})", part)
            found.update((k, array.array("f", v).tolist()) for k, v in rows)
    return found


def _store(rows: list[tuple[bytes, list[float]]]) -> None:
    with _db_lock, _connect() as db:
        db.executemany("INSERT OR REPLACE INTO embeddings VALUES (?, ?)",
                       [(k, array.array("f", v).tobytes()) for k, v in rows])


async def embed(model: str, texts: list[str], task: str, dimensions: int) -> list[list[float]]:
    keys = [_key(model, dimensions, task, t) for t in texts]
    found = await asyncio.to_thread(_lookup, list(set(keys)))  # off the loop: another process may hold the write lock
    missing = list(dict.fromkeys((k, t) for k, t in zip(keys, texts) if k not in found))
    stats["hits"] += len(texts) - len(missing)
    stats["misses"] += len(missing)
    for i in range(0, len(missing), BATCH):
        part = missing[i:i + BATCH]
        vectors = await _paced(model, [t for _, t in part], task, dimensions)
        # Stored per batch, so an interrupted run keeps its work.
        await asyncio.to_thread(_store, [(k, v) for (k, _), v in zip(part, vectors)])
        found.update((k, v) for (k, _), v in zip(part, vectors))
    return [found[k] for k in keys]


def install() -> None:
    inference.embed = embed
