"""Permission-filtered retrieval over the active index generation: vector, keyword, or hybrid (RRF),
then checked live against SharePoint (layer 2)."""
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg import sql

from . import generations
from .generations import Generation

Mode = Literal["vector", "keyword", "hybrid"]
RRF_K = 60  # standard reciprocal-rank-fusion constant
# Query terms are matched exactly and in English and Spanish stemmed form; chunks store both (generations._ddl).
TS_CONFIGS = ("simple", "english", "spanish")
MAX_TERMS = 64
MAX_QUERY_CHARS = 2000  # callers truncate queries to this

_COLUMNS = "c.id, s.id, s.name, s.web_url, c.heading_path, c.text, s.drive_id, s.item_id"
_FROM = "FROM {t} c JOIN sources s ON s.id = c.source_id"


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
    generation: Generation | None = None,
) -> list[Hit]:
    """Top-k chunks the caller may read. `principals` = the user's id, their group ids, and any
    org-wide markers; a chunk is visible iff its `acl` overlaps them. The ACL filter is part of the
    SQL WHERE clause, so restricted chunks never enter ranking. There is no unfiltered path: an empty
    principal list is refused rather than treated as "no filter"."""
    if not principals or not all(isinstance(p, str) and p for p in principals):
        raise ValueError("principals are required")
    gen = generation or await generations.active(conn)
    from_ = sql.SQL(_FROM).format(t=gen.table).as_string(conn)
    ranked: dict[str, list] = {}
    if mode in ("vector", "hybrid"):
        # The HNSW index returns ~ef_search nearest neighbours *before* the ACL filter is applied, so a
        # caller who can read few chunks would get few or no hits. Iterative scan keeps walking the
        # index until `candidates` rows pass the filter (pgvector >= 0.8). SET LOCAL needs a transaction.
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            cur = await conn.execute(
                f"SELECT {_COLUMNS} {from_} WHERE c.acl && %s::text[] ORDER BY c.embedding <=> %s::vector LIMIT %s",
                (principals, query_vec, candidates),
            )
            ranked["vector"] = await cur.fetchall()
    # Match ANY term (ranked by how many match): natural-language questions rarely share every word
    # with the document, so AND semantics would return nothing. Alphanumeric tokens can't contain tsquery operators.
    # Letters/digits only (never tsquery operators), deduplicated and capped: a huge OR across three configs
    # exhausts Postgres memory.
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query_text.lower())))[:MAX_TERMS]
    if mode in ("keyword", "hybrid") and terms:
        tsquery = " | ".join(terms)
        q = " || ".join(f"to_tsquery('{cfg}', %(q)s)" for cfg in TS_CONFIGS)
        cur = await conn.execute(
            f"SELECT {_COLUMNS} {from_} WHERE c.acl && %(p)s::text[] AND c.tsv @@ ({q}) "
            f"ORDER BY ts_rank_cd(c.tsv, {q}) DESC LIMIT %(n)s",
            {"p": principals, "q": tsquery, "n": candidates},
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


def candidate_args(k: int, overfetch: int, kwargs: dict) -> dict:
    wanted = k * overfetch
    # The per-retriever candidate pool must be at least as large as the over-fetch window, or search()
    # silently caps it (default 50) and a stale document can exhaust the window.
    return {**kwargs, "k": wanted, "candidates": max(kwargs.get("candidates", 50), wanted)}


async def keep_verified(hits: list[Hit], verify: Verifier, k: int) -> list[Hit]:
    """The first k hits whose file the caller can open right now (layer 2)."""
    sources = list(dict.fromkeys((h.drive_id, h.item_id) for h in hits))
    allowed = await verify(sources) if sources else set()
    return [h for h in hits if (h.drive_id, h.item_id) in allowed][:k]
