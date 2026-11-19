"""Searching a suite's eval index: layer 1 alone (the SQL permission filter), or the production path
(`PostgresRetriever`: active generation, over-fetch, then the layer-2 live check)."""
import asyncio

from psycopg_pool import AsyncConnectionPool
from sharepoint_rag import db, generations, inference
from sharepoint_rag.retriever import PostgresRetriever
from sharepoint_rag.search import Hit, Mode, Verifier, search

from sharepoint_evals import embed_cache
from sharepoint_evals.corpus import Corpus
from sharepoint_evals.store import ensure_index

POOL_SIZE = 8
_locks: dict[str, asyncio.Lock] = {}
_urls: dict[str, str] = {}
# Per event loop: each inspect_eval call runs on its own loop. `reset()` drops them after every call, so a pool or
# lock never outlives its loop (or is handed back for a new loop that happens to reuse the old one's id).
_pools: dict[tuple[int, str], AsyncConnectionPool] = {}


def reset() -> None:
    """Forget the pools and locks of event loops that have finished. Their connections close as they are freed."""
    _pools.clear()
    _locks.clear()


def indexed(corpus: Corpus, url: str) -> None:
    """Records that the caller just brought the suite's index up to date, so tasks don't check it again."""
    _urls[corpus.name] = url


async def index_url(corpus: Corpus) -> str:
    """Indexes the suite on first use; concurrent samples wait for the same build."""
    async with _locks.setdefault(f"index:{id(asyncio.get_running_loop())}:{corpus.name}", asyncio.Lock()):
        if corpus.name not in _urls:
            _urls[corpus.name] = await ensure_index(corpus)
    return _urls[corpus.name]


async def pool(corpus: Corpus) -> AsyncConnectionPool:
    """One pool per suite and event loop. Created under a lock: hundreds of samples start at once, and each making
    its own pool would exhaust Postgres' connections."""
    embed_cache.install()  # the production retriever embeds through inference.embed: give it the cache
    key = (id(asyncio.get_running_loop()), corpus.name)
    async with _locks.setdefault(f"pool:{key}", asyncio.Lock()):
        if key not in _pools:
            p = db.create_pool(await index_url(corpus), min_size=1, max_size=POOL_SIZE)
            await p.open(wait=True)
            _pools[key] = p
    return _pools[key]


async def retrieve(corpus: Corpus, question: str, principals: list[str], mode: Mode, k: int = 10,
                   verify: Verifier | None = None, candidates: int = 50) -> list[Hit]:
    """Layer 1 only by default. With `verify`, the production path."""
    p = await pool(corpus)
    if verify is not None:
        return await PostgresRetriever(p, mode=mode).search(question, principals=principals, verify=verify, k=k)
    async with p.connection() as conn:
        gen = await generations.active(conn)
    vec = (await inference.embed(gen.model, [question], "query", gen.dimensions))[0]  # the generation's own model
    async with p.connection() as conn:
        return await search(conn, query_text=question, query_vec=vec, principals=principals, k=k, mode=mode,
                            candidates=max(candidates, k), generation=gen)


def ranked_docs(hits: list[Hit]) -> list[str]:
    """Document-level ranking: each doc (by item id, its path in the suite) at the position of its best chunk."""
    return list(dict.fromkeys(h.item_id for h in hits))


class SimulatedOutage(RuntimeError):
    """Raised by an `allow(..., raises=True)` stub. Fail-closed checks expect exactly this: any other exception means
    the search broke before the live check ran, which proves nothing about failing closed."""


def allow(readable: set[str], *, raises: bool = False) -> Verifier:
    """A layer-2 stub: SharePoint lets the caller open exactly `readable` (item ids) right now."""
    async def verify(items):
        if raises:
            raise SimulatedOutage("simulated Graph outage")
        return {(drive, item) for drive, item in items if item in readable}

    return verify
