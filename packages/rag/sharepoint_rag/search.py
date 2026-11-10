"""Permission-filtered retrieval over the chunk index: vector, keyword, or hybrid (RRF)."""
import re
from dataclasses import dataclass
from typing import Literal

import psycopg

Mode = Literal["vector", "keyword", "hybrid"]
RRF_K = 60  # standard reciprocal-rank-fusion constant

_COLUMNS = "c.id, s.id, s.name, s.web_url, c.heading_path, c.text"
_FROM = "FROM chunks c JOIN sources s ON s.id = c.source_id"


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    source_id: int
    name: str
    web_url: str
    heading_path: str | None
    text: str
    score: float


def _row_to_hit(row, score: float) -> Hit:
    return Hit(row[0], row[1], row[2], row[3], row[4], row[5], score)


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
