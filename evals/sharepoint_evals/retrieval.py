import asyncio

import psycopg
from pgvector.psycopg import register_vector_async
from sharepoint_rag import embeddings
from sharepoint_rag.search import Hit, Mode, Verifier, search, search_verified

from sharepoint_evals.dataset import PERSONAS
from sharepoint_evals.store import ensure_index

_lock = asyncio.Lock()
_url: str | None = None
_query_vectors: dict[str, list[float]] = {}  # same question is asked once per search mode


async def index_url() -> str:
    """Indexes the dataset on first use; concurrent samples wait for the same build."""
    global _url
    async with _lock:
        if _url is None:
            _url = await ensure_index()
    return _url


async def retrieve(question: str, persona: str, mode: Mode, k: int = 10, verify: Verifier | None = None) -> list[Hit]:
    """Layer 1 only by default; pass `verify` to also apply the layer-2 live access check."""
    if question not in _query_vectors:
        _query_vectors[question] = await embeddings.embed_query(question)
    query_vec = _query_vectors[question]
    async with await psycopg.AsyncConnection.connect(await index_url()) as conn:
        await register_vector_async(conn)
        args = dict(query_text=question, query_vec=query_vec, principals=PERSONAS[persona], k=k, mode=mode)
        if verify is None:
            return await search(conn, **args)
        return await search_verified(conn, verify=verify, **args)


def ranked_docs(hits: list[Hit]) -> list[str]:
    """Document-level ranking: each doc at the position of its best chunk."""
    return list(dict.fromkeys(h.name for h in hits))
