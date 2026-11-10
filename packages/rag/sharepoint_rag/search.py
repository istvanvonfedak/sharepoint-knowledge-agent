"""Permission-filtered retrieval over the chunk index: vector, keyword, or hybrid (RRF)."""
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import psycopg

Mode = Literal["vector", "keyword", "hybrid"]
RRF_K = 60  # standard reciprocal-rank-fusion constant

_COLUMNS = "c.id, s.id, s.name, s.web_url, c.heading_path, c.text, s.drive_id, s.item_id"
_FROM = "FROM chunks c JOIN sources s ON s.id = c.source_id"


@dataclass(frozen=True, kw_only=True)
class Hit:
    chunk_id: int
    source_id: int
    name: str
    web_url: str
    heading_path: str | None
    text: str
    drive_id: str
    item_id: str
    score: float


def _row_to_hit(row, score: float) -> Hit:
    return Hit(
        chunk_id=row[0], source_id=row[1], name=row[2], web_url=row[3], heading_path=row[4],
        text=row[5], drive_id=row[6], item_id=row[7], score=score,
    )


async def search(
    conn: psycopg.AsyncConnection,
    *,
    query_text: str,
    query_vec: list[float],
    principals: list[str],
    k: int = 10,
    mode: Mode = "hybrid",
    candidates: int = 50,
) -> list[Hit]:
    """Top-k chunks the caller may read. `principals` = the user's id, their group ids, and any
    org-wide markers; a chunk is visible iff its `acl` overlaps them. The ACL filter is part of the
    SQL WHERE clause, so restricted chunks never enter ranking."""
    ranked: dict[str, list] = {}
    if mode in ("vector", "hybrid"):
        # The HNSW index returns ~ef_search nearest neighbours *before* the ACL filter is applied, so a
        # caller who can read few chunks would get few or no hits. Iterative scan keeps walking the
        # index until `candidates` rows pass the filter (pgvector >= 0.8). SET LOCAL needs a transaction.
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            cur = await conn.execute(
                f"SELECT {_COLUMNS} {_FROM} WHERE c.acl && %s::text[] ORDER BY c.embedding <=> %s::vector LIMIT %s",
                (principals, query_vec, candidates),
            )
            ranked["vector"] = await cur.fetchall()
    # Match ANY term (ranked by how many match): natural-language questions rarely share every word
    # with the document, so AND semantics would return nothing. Alphanumeric tokens can't contain tsquery operators.
    terms = re.findall(r"[^\W_]+", query_text.lower())  # letters/digits only: never tsquery operators
    if mode in ("keyword", "hybrid") and terms:
        tsquery = " | ".join(terms)
        cur = await conn.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE c.acl && %s::text[] "
            "AND c.tsv @@ to_tsquery('simple', %s) "
            "ORDER BY ts_rank_cd(c.tsv, to_tsquery('simple', %s)) DESC LIMIT %s",
            (principals, tsquery, tsquery, candidates),
        )
        ranked["keyword"] = await cur.fetchall()

    scores: dict[int, float] = {}
    rows: dict[int, tuple] = {}
    for results in ranked.values():
        for rank, row in enumerate(results, 1):
            scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (RRF_K + rank)
            rows[row[0]] = row
    best = sorted(scores, key=scores.__getitem__, reverse=True)[:k]
    return [_row_to_hit(rows[i], scores[i]) for i in best]


# Given (drive_id, item_id) pairs, returns the subset the caller can open right now.
Verifier = Callable[[list[tuple[str, str]]], Awaitable[set[tuple[str, str]]]]


async def search_verified(
    conn: psycopg.AsyncConnection,
    *,
    verify: Verifier,
    k: int = 10,
    overfetch: int = 3,
    **kwargs,
) -> list[Hit]:
    """`search` plus layer-2 enforcement: hits whose file the caller can no longer open are dropped.

    The index ACL can lag behind SharePoint (revoked sharing, deleted files), so each hit's source is
    re-verified live. We over-fetch (`k * overfetch`) so dropped hits can be replaced; if more than
    that many are stale (e.g. one revoked document with many chunks) the result may be shorter than k,
    never wider. A verifier error propagates:
    no answer is better than an unverified one.
    """
    wanted = k * overfetch
    # The per-retriever candidate pool must be at least as large as the over-fetch window, or search()
    # silently caps it (default 50) and a stale document can exhaust the window.
    kwargs["candidates"] = max(kwargs.get("candidates", 50), wanted)
    hits = await search(conn, k=wanted, **kwargs)
    sources = list(dict.fromkeys((h.drive_id, h.item_id) for h in hits))
    allowed = await verify(sources) if sources else set()
    return [h for h in hits if (h.drive_id, h.item_id) in allowed][:k]
