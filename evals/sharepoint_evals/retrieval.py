import asyncio

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool
from sharepoint_rag import db, generations, inference
from sharepoint_rag.retriever import PostgresRetriever
from sharepoint_rag.search import Hit, Mode, Verifier, search

from sharepoint_evals.dataset import PERSONAS
from sharepoint_evals.store import ensure_index

_lock = asyncio.Lock()
_url: str | None = None
_pools: dict[int, AsyncConnectionPool] = {}  # per event loop: Inspect may run tasks on separate loops
_query_vectors: dict[str, list[float]] = {}  # same question is asked once per search mode


async def index_url() -> str:
    """Indexes the dataset on first use; concurrent samples wait for the same build."""
    global _url
    async with _lock:
        if _url is None:
            _url = await ensure_index()
    return _url


async def _pool() -> AsyncConnectionPool:
    key = id(asyncio.get_running_loop())
    if key not in _pools:
        pool = db.create_pool(await index_url(), min_size=1, max_size=8)
        await pool.open(wait=True)
        _pools[key] = pool
    return _pools[key]


async def retrieve(
    question: str, persona: str, mode: Mode, k: int = 10, verify: Verifier | None = None
) -> list[Hit]:
    """Layer 1 only by default. With `verify`, the production path: `PostgresRetriever` (active generation,
    then the layer-2 live check)."""
    if verify is not None:
        retriever = PostgresRetriever(await _pool(), mode=mode)
        return await retriever.search(question, principals=PERSONAS[persona], verify=verify, k=k)
    async with await psycopg.AsyncConnection.connect(await index_url()) as conn:
        await register_vector_async(conn)
        gen = await generations.active(conn)
        if question not in _query_vectors:  # embedded with the generation's own model, as in production
            _query_vectors[question] = (await inference.embed(gen.model, [question], "query", gen.dimensions))[0]
        args = dict(query_text=question, query_vec=_query_vectors[question], principals=PERSONAS[persona], k=k, mode=mode)
        hits = await search(conn, generation=gen, **args)
    return hits


def ranked_docs(hits: list[Hit]) -> list[str]:
    """Document-level ranking: each doc at the position of its best chunk."""
    return list(dict.fromkeys(h.name for h in hits))
