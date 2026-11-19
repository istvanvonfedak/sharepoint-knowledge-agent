"""Permission-filtered retrieval over the active index generation: vector, keyword, or hybrid (RRF),
then checked live against SharePoint (layer 2)."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg import sql

from . import generations
from .generations import Generation

Mode = Literal["vector", "keyword", "hybrid"]
RRF_K = 60  # standard reciprocal-rank-fusion constant
# Keyword results count for less than vector ones in the fusion. With RRF, a chunk found by both lists beats one
# found by only one of them, so keyword evidence is only worth fusing when it is strong (MIN_MATCH below) and then
# should break ties rather than overrule the vector ranking. Tuned on the evals (retrieval_multilingual,
# answer_quality, acl_scale, retrieval_identifiers): with MIN_MATCH in place, 0.3-1.0 all score within 0.005.
KEYWORD_WEIGHT = 0.5
# A keyword match must cover this share of the query's information (the summed IDF of its terms). Matching any one
# term returned 99% of FinanceBench chunks for a typical question, so keyword results were noise ranked by ties;
# requiring the rare terms keeps identifier lookups ("INC-48213") and drops "shares the word revenue".
MIN_MATCH = 0.95
# HNSW's candidate list per scan. pgvector's default (40) is below the rows a search asks for (50, or 90 with the
# live check's over-fetch), which leaves the rest to iterative scan. Measured on FinanceBench: at 100, a backend's
# first scan found 97% of the true 50 nearest chunks and later scans 100%, so results changed with the connection;
# at 400 every scan found all of them. Latency was the same at 40, 100, 400 and 1000.
EF_SEARCH = 400
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
    exclude_sources: list[int] = (),
) -> list[Hit]:
    """Top-k chunks the caller may read. `principals` = the user's id, their group ids, and any
    org-wide markers; a chunk is visible iff its `acl` overlaps them. The ACL filter is part of the
    SQL WHERE clause, so restricted chunks never enter ranking. There is no unfiltered path: an empty
    principal list is refused rather than treated as "no filter". `exclude_sources` leaves out files the live check
    has already denied, so the next window of results is filled from the rest."""
    if not principals or not all(isinstance(p, str) and p for p in principals):
        raise ValueError("principals are required")
    gen = generation or await generations.active(conn)
    from_ = sql.SQL(_FROM).format(t=gen.table).as_string(conn)
    excluded = list(exclude_sources)
    visible = "c.acl && %(p)s::text[] AND NOT (c.source_id = ANY(%(x)s::bigint[]))"
    ranked: dict[str, list] = {}
    if mode in ("vector", "hybrid"):
        # The HNSW index returns ~ef_search nearest neighbours *before* the ACL filter is applied, so a
        # caller who can read few chunks would get few or no hits. Iterative scan keeps walking the
        # index until `candidates` rows pass the filter (pgvector >= 0.8). SET LOCAL needs a transaction.
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            await conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(min(1000, max(EF_SEARCH, 2 * candidates)))))
            # Ordered by distance alone: a second sort key would stop pgvector using the HNSW index. Exact ties
            # only happen between identical chunks, which rank the same either way.
            cur = await conn.execute(
                f"SELECT {_COLUMNS} {from_} WHERE {visible} ORDER BY c.embedding <=> %(v)s::vector LIMIT %(n)s",
                {"p": principals, "x": excluded, "v": query_vec, "n": candidates},
            )
            ranked["vector"] = await cur.fetchall()
    if mode in ("keyword", "hybrid"):
        ranked["keyword"] = await _keyword(conn, gen, from_, visible, query_text, principals, excluded, candidates)

    scores: dict[int, float] = {}
    rows: dict[int, tuple] = {}
    for name, results in ranked.items():
        weight = KEYWORD_WEIGHT if name == "keyword" and mode == "hybrid" else 1.0
        for rank, row in enumerate(results, 1):
            scores[row[0]] = scores.get(row[0], 0.0) + weight / (RRF_K + rank)
            rows[row[0]] = row
    best = sorted(scores, key=lambda c: (-scores[c], c))[:k]  # equal scores: lowest chunk id, so runs agree
    return [_row_to_hit(rows[i], scores[i]) for i in best]


def _term(i: int) -> str:
    """One query term as a tsquery: its exact form or its English or Spanish stem."""
    return "(" + " || ".join(f"plainto_tsquery('{cfg}', %(t{i})s)" for cfg in TS_CONFIGS) + ")"


async def _keyword(conn, gen: Generation, from_: str, visible: str, query_text: str, principals: list[str],
                   excluded: list[int], candidates: int) -> list:
    """Chunks covering at least MIN_MATCH of the query's information, best first.

    Terms are the query's tokens as Postgres' own parser splits them: chunk text is indexed with that parser, which
    keeps "INC-48213" as `inc` + `-48213` and "user@contoso.com" whole, so splitting the query any other way misses
    exactly the identifiers keyword search is for. Stopwords (in English or Spanish) are dropped, unless that would
    drop every term: "IT" or "US" alone is a Spanish or English stopword but still the whole query. Each term is
    weighted by its rarity (BM25's IDF) among the chunks the caller's permissions let them see, never all chunks: how
    common a word is in files they can't read must not change their ranking. The live check's denied files still
    count, so a backfill round weighs terms exactly as the first round did. A chunk scores the summed IDF of the
    terms it contains."""
    rows = await (await conn.execute(
        "SELECT u.lexeme, numnode(plainto_tsquery('english', u.lexeme)) > 0 AND numnode(plainto_tsquery('spanish', u.lexeme)) > 0 "
        "FROM unnest(to_tsvector('simple', %s)) u ORDER BY u.positions[1]",
        (query_text,),
    )).fetchall()
    terms = ([t for t, content in rows if content] or [t for t, _ in rows])[:MAX_TERMS]
    if not terms:
        return []
    args = {"p": principals, "x": excluded, "n": candidates, "m": MIN_MATCH, **{f"t{i}": t for i, t in enumerate(terms)}}
    values = ", ".join(f"({i}, {_term(i)})" for i in range(len(terms)))
    any_term = " || ".join(_term(i) for i in range(len(terms)))
    cur = await conn.execute(
        f"WITH t(i, q) AS (VALUES {values}), "
        f"n AS (SELECT count(*) AS n {from_} WHERE c.acl && %(p)s::text[]), "
        # A term no visible chunk contains (a typo, a word from another language) can't tell chunks apart: it has
        # no weight, rather than the highest one, which would put every chunk below MIN_MATCH.
        f"w AS (SELECT t.q, ln((n.n - d.df + 0.5) / (d.df + 0.5) + 1) AS idf FROM t, n, "
        f"      LATERAL (SELECT count(*) AS df {from_} WHERE c.acl && %(p)s::text[] AND c.tsv @@ t.q) d WHERE d.df > 0), "
        f"scored AS (SELECT c.id, sum(w.idf) AS score FROM w JOIN {gen.table.as_string(conn)} c ON c.tsv @@ w.q "
        f"           WHERE {visible} GROUP BY c.id) "
        f"SELECT {_COLUMNS} FROM scored JOIN {gen.table.as_string(conn)} c ON c.id = scored.id "
        f"JOIN sources s ON s.id = c.source_id "
        f"WHERE scored.score >= %(m)s * (SELECT sum(idf) FROM w) "
        f"ORDER BY scored.score DESC, ts_rank_cd(c.tsv, {any_term}, 1) DESC, c.id LIMIT %(n)s",
        args,
    )
    return await cur.fetchall()


# Given (drive_id, item_id) pairs, returns the subset the caller can open right now.
Verifier = Callable[[list[tuple[str, str]]], Awaitable[set[tuple[str, str]]]]


def candidate_args(k: int, overfetch: int, kwargs: dict) -> dict:
    wanted = k * overfetch
    # The per-retriever candidate pool must be at least as large as the over-fetch window, or search()
    # silently caps it (default 50) and a stale document can exhaust the window.
    return {**kwargs, "k": wanted, "candidates": max(kwargs.get("candidates", 50), wanted)}
