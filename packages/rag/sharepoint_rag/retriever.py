"""The `Retriever` interface the mcp-server searches through. Both permission layers are mandatory:
`principals` (layer 1, inside the query) and `verify` (layer 2, live check as the user)."""
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from . import generations, inference
from . import search as search_module
from .search import Hit, Mode, Verifier, candidate_args, search


OVERFETCH = 3  # candidates per result kept, so hits dropped by the live check can be replaced
# When the live check drops more than the over-fetch covers, search again without the denied files. Each round is
# one live check (a Graph call), so it is capped; normally nothing is denied and only one round runs.
MAX_ROUNDS = 4


class Retriever(Protocol):
    async def search(self, query: str, *, principals: list[str], verify: Verifier, k: int = 10) -> list[Hit]: ...


class PostgresRetriever:
    """Hybrid search over the active generation in Postgres."""

    def __init__(self, pool: AsyncConnectionPool, *, mode: Mode = "hybrid") -> None:
        self._pool = pool
        self._mode = mode

    async def search(self, query: str, *, principals: list[str], verify: Verifier, k: int = 10) -> list[Hit]:
        """The top k chunks the caller can open right now. The window is counted in chunks, and a denied file can
        own most of it, so a window that leaves fewer than k is searched again with the denied files left out,
        until k are found, the results run out or MAX_ROUNDS live checks have run."""
        query = query[: search_module.MAX_QUERY_CHARS]
        if not query.strip():
            return []
        allowed: set[tuple[str, str]] = set()
        denied: dict[tuple[str, str], int] = {}  # file -> its source id, to leave out of the next window
        vectors: dict[int, list[float]] = {}  # per generation: the query is embedded once, not once per round
        for _ in range(MAX_ROUNDS):
            hits = await self._window(query, principals, k, list(denied.values()), vectors)
            unchecked = [f for f in dict.fromkeys((h.drive_id, h.item_id) for h in hits) if f not in allowed]
            if unchecked:
                ok = await verify(unchecked)  # with the connection back in the pool: a network call
                allowed |= ok & set(unchecked)
                for h in hits:
                    if (h.drive_id, h.item_id) not in allowed:
                        denied[(h.drive_id, h.item_id)] = h.source_id
            kept = [h for h in hits if (h.drive_id, h.item_id) in allowed]
            if len(kept) >= k or len(hits) < k * OVERFETCH or not unchecked:
                return kept[:k]  # enough, or nothing more to find
        return kept[:k]

    async def _window(self, query: str, principals: list[str], k: int, exclude: list[int],
                      vectors: dict[int, list[float]]) -> list[Hit]:
        for _ in range(2):
            async with self._pool.connection() as conn:
                gen = await generations.active(conn)
            if gen.id not in vectors:
                # Embed outside the connection: it is a network call to the inference-server.
                vectors[gen.id] = (await inference.embed(gen.model, [query], "query", gen.dimensions))[0]
            args = dict(query_text=query, query_vec=vectors[gen.id], principals=principals, mode=self._mode, generation=gen,
                        exclude_sources=exclude)
            async with self._pool.connection() as conn, conn.transaction():
                hits = await search(conn, **candidate_args(k, OVERFETCH, args))
                # A generation switched during the embed call no longer gets permission writes: search again.
                if (await generations.active(conn)).id == gen.id:
                    break
        else:
            raise RuntimeError("The index generation changed twice during one search; try again")
        return hits
