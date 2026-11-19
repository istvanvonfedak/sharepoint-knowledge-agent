"""Keyword ranking, fusion and the retriever's live-check backfill, against a real Postgres (see tests/fixtures.py)."""
from datetime import datetime, timezone

import pytest
from tests.testing_support import fake_vector
from sharepoint_rag import index_store, search
from sharepoint_rag.acl import AclRead
from sharepoint_rag.index_store import ChunkRow, Item
from sharepoint_rag.retriever import OVERFETCH, PostgresRetriever

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def add(conn, item_id: str, *texts: str, acl=("user:a",), heading: str | None = None) -> None:
    it = Item("d1", item_id, "s1", f"{item_id}.md", f"https://sp/{item_id}", "e1", T0)
    rows = [ChunkRow(n, t, heading, "simple") for n, t in enumerate(texts)]
    await index_store.upsert_seen(conn, [it], None)
    await index_store.write_index(conn, it, AclRead(tuple(acl), True, T0), rows, {1: [fake_vector(t) for t in texts]})


async def keyword(conn, query: str, principals=("user:a",)) -> list[str]:
    hits = await search.search(conn, query_text=query, query_vec=fake_vector(query), principals=list(principals),
                               mode="keyword")
    return [h.item_id for h in hits]


@pytest.mark.parametrize("ident", ["INC-48213", "v2.3.1", "user@contoso.com", "INV-2024-0173", "SKU-7731-B"])
async def test_keyword_finds_identifiers_as_postgres_indexes_them(pool, ident):
    """Postgres keeps "INC-48213" as `inc` + `-48213` and an email address whole; the query must be split the same way."""
    async with pool.connection() as conn:
        await add(conn, "target", f"Ticket {ident} was closed after the update.")
        await add(conn, "other", "Ticket INC-11111 was closed after the update.", "Another ticket, v9.9.9, nobody@example.org.")
        assert (await keyword(conn, ident))[:1] == ["target"]
        assert (await keyword(conn, f"what happened to {ident}?"))[:1] == ["target"]


async def test_keyword_matches_the_heading_path(pool):
    """A title or email subject is often the only place an identifier appears."""
    async with pool.connection() as conn:
        await add(conn, "t1", "Status: closed.", heading="Ticket INC-48213")
        await add(conn, "t2", "Status: closed.", heading="Ticket INC-11111")
        assert await keyword(conn, "INC-48213") == ["t1"]


def test_an_outdated_keyword_column_is_rebuilt_on_migrate(db_url):
    import psycopg
    from sharepoint_rag import db, generations

    old = "to_tsvector('simple', text) || to_tsvector(language, text)"
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE chunks_g1 DROP COLUMN tsv")
        conn.execute(f"ALTER TABLE chunks_g1 ADD COLUMN tsv tsvector GENERATED ALWAYS AS ({old}) STORED")
        assert generations._tsv_outdated(conn, "chunks_g1")
    db.migrate(db_url)
    with psycopg.connect(db_url, autocommit=True) as conn:
        assert not generations._tsv_outdated(conn, "chunks_g1")
        assert conn.execute("SELECT to_regclass('chunks_g1_tsv_idx') IS NOT NULL").fetchone()[0]


async def test_keyword_needs_the_rare_terms_not_any_term(pool):
    """Sharing one common word is not a keyword match (it used to return nearly every chunk)."""
    async with pool.connection() as conn:
        for i in range(20):
            await add(conn, f"report{i}", f"Quarterly revenue report number {i}.")
        await add(conn, "ulta", "Ulta Beauty revenue grew in fiscal 2023.")
        assert await keyword(conn, "Ulta Beauty revenue 2023") == ["ulta"]
        await add(conn, "it", "IT support hours are 8 to 6.")
        assert await keyword(conn, "IT") == ["it"]  # a stopword in Spanish, but the whole query


async def test_term_rarity_counts_only_files_the_caller_can_see(pool):
    """Hidden files that mention a term must not change how a caller's results rank."""
    async with pool.connection() as conn:
        await add(conn, "alpha", "alpha gamma")
        await add(conn, "beta", "beta gamma")
        before = await keyword(conn, "alpha beta gamma")
        for i in range(30):  # make `alpha` common, but only in files user:a can't read
            await add(conn, f"hidden{i}", f"alpha note {i}", acl=("user:b",))
        assert await keyword(conn, "alpha beta gamma") == before


async def test_backfill_rounds_weigh_terms_the_same(pool):
    """Leaving a denied file out of the next window must not change how rare its words count as."""
    async with pool.connection() as conn:
        await add(conn, "denied", *[f"alpha note {n}" for n in range(20)])
        await add(conn, "keep", "alpha beta")
        await add(conn, "other", "beta gamma")
        source = (await (await conn.execute("SELECT id FROM sources WHERE item_id = 'denied'")).fetchone())[0]
        args = dict(query_text="alpha beta", query_vec=fake_vector("q"), principals=["user:a"], mode="keyword")
        first = [h.item_id for h in await search.search(conn, **args) if h.item_id != "denied"]
        second = [h.item_id for h in await search.search(conn, **args, exclude_sources=[source])]
        assert second == first


async def test_equal_scores_rank_the_same_on_every_run(pool):
    async with pool.connection() as conn:
        for i in range(12):
            await add(conn, f"copy{i}", "Travel policy for contractors.")
        first = await keyword(conn, "travel policy contractors")
        assert first == sorted(first, key=lambda name: int(name[4:]))  # ties broken by chunk id: insertion order
        assert all([await keyword(conn, "travel policy contractors") == first for _ in range(3)])


def allow(*readable: str, calls: list | None = None):
    async def verify(items):
        if calls is not None:
            calls.append(list(items))
        return {(d, i) for d, i in items if i in readable}

    return verify


async def test_live_check_drops_are_backfilled_from_beyond_the_window(pool):
    """A denied file owning the whole first window must not leave the caller with nothing."""
    async with pool.connection() as conn:
        await add(conn, "revoked", *[f"budget plan part {n}" for n in range(3 * 10 * OVERFETCH)])
        await add(conn, "readable", *[f"budget plan summary {n}" for n in range(15)])
    calls = []
    hits = await PostgresRetriever(pool, mode="keyword").search(
        "budget plan", principals=["user:a"], verify=allow("readable", calls=calls), k=10)
    assert len(hits) == 10 and {h.item_id for h in hits} == {"readable"}
    assert ("d1", "revoked") not in {f for c in calls[1:] for f in c}  # a denied file is checked once, then left out


async def test_backfill_stops_when_nothing_else_is_readable(pool):
    async with pool.connection() as conn:
        await add(conn, "revoked", *[f"budget plan part {n}" for n in range(40)])
        await add(conn, "readable", "budget plan summary")
    calls = []
    hits = await PostgresRetriever(pool, mode="keyword").search(
        "budget plan", principals=["user:a"], verify=allow("readable", calls=calls), k=10)
    assert [h.item_id for h in hits] == ["readable"]
    # The second window (denied file left out) held only the readable file, already checked: no second Graph call.
    assert len(calls) == 1


async def test_a_failing_live_check_returns_nothing(pool):
    async with pool.connection() as conn:
        await add(conn, "f1", "budget plan")

    async def outage(items):
        raise RuntimeError("Graph unavailable")

    with pytest.raises(RuntimeError):
        await PostgresRetriever(pool).search("budget plan", principals=["user:a"], verify=outage)
