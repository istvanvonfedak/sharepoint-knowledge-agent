"""The `Retriever` interface the mcp-server searches through. Both permission layers are mandatory:
`principals` (layer 1, inside the query) and `verify` (layer 2, live check as the user)."""
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from . import generations, inference
from . import search as search_module
from .search import Hit, Mode, Verifier, candidate_args


OVERFETCH = 3  # candidates per result kept, so hits dropped by the live check can be replaced
# When the live check drops more than the over-fetch covers, search again without the denied files. Each round is
# one live check (a Graph call), so it is capped; normally nothing is denied and only one round runs.
MAX_ROUNDS = 4
POOL = 50  # search_with_files: the files among this many of the fused candidates


class Retriever(Protocol):
    async def search(self, query: str, *, principals: list[str], verify: Verifier, k: int = 10) -> list[Hit]: ...

    async def nearby(self, hit: Hit, *, principals: list[str], verify: Verifier, before: int = 1,
                     after: int = 1) -> list[Hit]: ...

    async def search_with_files(self, query: str, *, principals: list[str], verify: Verifier,
                                k: int = 10) -> tuple[list[Hit], list[Hit]]: ...

    async def read_files(self, files: list[Hit], *, principals: list[str], verify: Verifier) -> dict[int, list[Hit]]: ...


class PostgresRetriever:
    """Hybrid search over the active generation in Postgres."""

    def __init__(self, pool: AsyncConnectionPool, *, mode: Mode = "hybrid") -> None:
        self._pool = pool
        self._mode = mode

    async def search(self, query: str, *, principals: list[str], verify: Verifier, k: int = 10) -> list[Hit]:
        """The top k chunks the caller can open right now. The window is counted in chunks, and a denied file can
        own most of it, so a window that leaves fewer than k is searched again with the denied files left out,
        until k are found, the results run out or MAX_ROUNDS live checks have run."""
        return (await self._search(query, principals, verify, k, with_files=False))[0]

    async def search_with_files(self, query: str, *, principals: list[str], verify: Verifier,
                                k: int = 10) -> tuple[list[Hit], list[Hit]]:
        """search(), and the files the caller can open among the top POOL candidates it ranked, best first, each as
        its best hit: what a fact ranked just below the top k is likely in. They go through the same live check."""
        return await self._search(query, principals, verify, k, with_files=True)

    async def _search(self, query: str, principals: list[str], verify: Verifier, k: int,
                      with_files: bool) -> tuple[list[Hit], list[Hit]]:
        query = query[: search_module.MAX_QUERY_CHARS]
        if not query.strip():
            return [], []
        allowed: set[tuple[str, str]] = set()
        denied: dict[tuple[str, str], int] = {}  # file -> its source id, to leave out of the next window
        vectors: dict[int, list[float]] = {}  # per generation: the query is embedded once, not once per round
        pool: dict[tuple[str, str], Hit] = {}
        for _ in range(MAX_ROUNDS):
            hits, files = await self._window(query, principals, k, list(denied.values()), vectors, with_files)
            for f in files:
                pool.setdefault((f.drive_id, f.item_id), f)
            unchecked = [f for f in dict.fromkeys((h.drive_id, h.item_id) for h in hits) if f not in allowed]
            # Pool files are checked in the same call, but only the hits' files decide whether to search again.
            extra = [f for f in pool if f not in allowed and f not in denied and f not in unchecked]
            if unchecked or extra:
                ok = await verify(unchecked + extra)  # with the connection back in the pool: a network call
                allowed |= ok & set(unchecked + extra)
                for h in [*hits, *pool.values()]:
                    if (h.drive_id, h.item_id) not in allowed:
                        denied[(h.drive_id, h.item_id)] = h.source_id
            kept = [h for h in hits if (h.drive_id, h.item_id) in allowed]
            if len(kept) >= k or len(hits) < k * OVERFETCH or not unchecked:
                break  # enough, or nothing more to find
        return kept[:k], [f for key, f in pool.items() if key in allowed]

    async def nearby(self, hit: Hit, *, principals: list[str], verify: Verifier, before: int = 1,
                     after: int = 1) -> list[Hit]:
        """The chunks around `hit` in its file (search_module.nearby), behind both permission layers like search:
        nothing if the caller can no longer open the file."""
        async with self._pool.connection() as conn:
            hits = await search_module.nearby(conn, source_id=hit.source_id, ordinal=hit.ordinal, principals=principals,
                                              before=before, after=after)
        if not hits:
            return []
        file = (hits[0].drive_id, hits[0].item_id)
        return hits if file in await verify([file]) else []

    async def read_files(self, files: list[Hit], *, principals: list[str], verify: Verifier) -> dict[int, list[Hit]]:
        """Each file's chunks in file order (search_module.file_chunks), keyed by source id, behind both permission
        layers: a file the caller can no longer open is left out. `files` are hits, one per file."""
        if not files:
            return {}
        ok = await verify(list(dict.fromkeys((f.drive_id, f.item_id) for f in files)))
        ids = list(dict.fromkeys(f.source_id for f in files if (f.drive_id, f.item_id) in ok))
        if not ids:
            return {}
        async with self._pool.connection() as conn:
            chunks = await search_module.file_chunks(conn, source_ids=ids, principals=principals)
        # The live check passed for a file, not a source id: a chunk must belong to the file that was checked.
        return {sid: hs for sid, hs in chunks.items() if (hs[0].drive_id, hs[0].item_id) in ok}

    async def _window(self, query: str, principals: list[str], k: int, exclude: list[int],
                      vectors: dict[int, list[float]], with_files: bool) -> tuple[list[Hit], list[Hit]]:
        for _ in range(2):
            async with self._pool.connection() as conn:
                gen = await generations.active(conn)
            if gen.id not in vectors:
                # Embed outside the connection: it is a network call to the inference-server.
                vectors[gen.id] = (await inference.embed(gen.model, [query], "query", gen.dimensions))[0]
            args = dict(query_text=query, query_vec=vectors[gen.id], principals=principals, mode=self._mode, generation=gen,
                        exclude_sources=exclude)
            async with self._pool.connection() as conn, conn.transaction():
                window = candidate_args(k, OVERFETCH, args)
                ranked = await search_module.ranked(conn, **{n: v for n, v in window.items() if n != "k"})
                # A generation switched during the embed call no longer gets permission writes: search again.
                if (await generations.active(conn)).id == gen.id:
                    break
        else:
            raise RuntimeError("The index generation changed twice during one search; try again")
        return ranked[:window["k"]], search_module.files(ranked, POOL) if with_files else []
