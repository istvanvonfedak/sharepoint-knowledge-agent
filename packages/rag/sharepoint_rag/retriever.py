"""The `Retriever` interface the mcp-server searches through. Both permission layers are mandatory:
`principals` (layer 1, inside the query) and `verify` (layer 2, live check as the user)."""
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from . import generations, inference
from . import search as search_module
from .search import Hit, Mode, Verifier, candidate_args, keep_verified, search


OVERFETCH = 3  # candidates per result kept, so hits dropped by the live check can be replaced


class Retriever(Protocol):
    async def search(self, query: str, *, principals: list[str], verify: Verifier, k: int = 10) -> list[Hit]: ...


class PostgresRetriever:
    """Hybrid search over the active generation in Postgres."""

    def __init__(self, pool: AsyncConnectionPool, *, mode: Mode = "hybrid") -> None:
        self._pool = pool
        self._mode = mode

    async def search(self, query: str, *, principals: list[str], verify: Verifier, k: int = 10) -> list[Hit]:
        query = query[: search_module.MAX_QUERY_CHARS]
        if not query.strip():
            return []
        for _ in range(2):
            async with self._pool.connection() as conn:
                gen = await generations.active(conn)
            # Embed outside the connection: it is a network call to the inference-server.
            query_vec = (await inference.embed(gen.model, [query], "query", gen.dimensions))[0]
            args = dict(query_text=query, query_vec=query_vec, principals=principals, mode=self._mode, generation=gen)
            async with self._pool.connection() as conn, conn.transaction():
                hits = await search(conn, **candidate_args(k, OVERFETCH, args))
                # A generation switched during the embed call no longer gets permission writes: search again.
                if (await generations.active(conn)).id == gen.id:
                    break
        else:
            raise RuntimeError("The index generation changed twice during one search; try again")
        # The live check is a network call, so it runs with the connection returned to the pool.
        return await keep_verified(hits, verify, k)
