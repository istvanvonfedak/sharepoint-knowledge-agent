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


@pytest.mark.parametrize("query", ["2520-01-149-1221", "2520011491221", "NSN 2520-01-149-1221?"])
async def test_hyphenated_and_plain_numbers_find_each_other(pool, query):
    """A parts list prints a stock number as 2520011491221, its index as 2520-01-149-1221: either finds both."""
    async with pool.connection() as conn:
        await add(conn, "parts", "2 PAHZZ 2520011491221 7X677 930679 .ROLLER ASSEMBLY,CLU 1")
        await add(conn, "index", "STOCK NUMBER FIG. ITEM 2520-01-149-1221 106 2")
        await add(conn, "other", "3 PAHZZ 5325011548561 7X677 8627657 .RING,RETAINING 1 and 5310-01-148-2687")
        assert sorted(await keyword(conn, query)) == ["index", "parts"]


def test_only_long_hyphenated_numbers_are_joined_in_queries():
    assert search.compact_ids("NSN 2520-01-149-1221 and INV-2024-0173") == "NSN 2520011491221 and INV-20240173"
    assert search.compact_ids("steps 1-2 on page 2-91, TM 9-2320-280-24P") == "steps 1-2 on page 2-91, TM 9232028024P"


async def test_hybrid_keeps_exact_matches_only_keyword_search_finds(pool):
    """The vector list is always full, so a keyword-only hit must still be able to outrank its tail: a part number
    in a parts table loses to look-alike tables on meaning, and only keyword search knows which table has it."""
    async with pool.connection() as conn:
        for i in range(60):
            await add(conn, f"table{i}", f"SECTION II parts list page {i}: SCREW, WASHER, NUT, BEARING")
        query_vec = fake_vector("SECTION II parts list page 0: SCREW, WASHER, NUT, BEARING")
        it = Item("d1", "target", "s1", "target.md", "https://sp/target", "e1", T0)
        await index_store.upsert_seen(conn, [it], None)
        await index_store.write_index(conn, it, AclRead(("user:a",), True, T0),
                                      [ChunkRow(0, "2 PAHZZ 2520011491221 7X677 930679 .ROLLER ASSEMBLY,CLU", None, "simple")],
                                      {1: [[-x for x in query_vec]]})  # as far from the query as a vector gets
        hits = await search.search(conn, query_text="930679", query_vec=query_vec, principals=["user:a"], k=5, mode="hybrid")
        assert "target" in [h.item_id for h in hits]


async def test_nearby_returns_the_chunks_around_one_in_file_order(pool):
    async with pool.connection() as conn:
        await add(conn, "manual", *[f"page {n}" for n in range(10)])
        await add(conn, "secret", *[f"page {n}" for n in range(10)], acl=("user:b",))
        hits = await search.search(conn, query_text="page 4", query_vec=fake_vector("page 4"), principals=["user:a"], mode="keyword")
        [anchor] = [h for h in hits if h.text == "page 4"]

        async def around(hit, before, after, principals=("user:a",)):
            hits = await search.nearby(conn, source_id=hit.source_id, ordinal=hit.ordinal, principals=list(principals),
                                       before=before, after=after)
            return [h.text for h in hits]

        assert await around(anchor, 1, 2) == ["page 3", "page 4", "page 5", "page 6"]
        assert await around(anchor, 99, 0) == ["page 1", "page 2", "page 3", "page 4"]  # capped at MAX_NEARBY
        assert await around(anchor, 0, 99) == ["page 4", "page 5", "page 6", "page 7"]
        assert await around(anchor, 1, 1, principals=("user:b",)) == []  # same filter as search: not user:b's file
        with pytest.raises(ValueError):
            await around(anchor, 1, 1, principals=())


async def test_retriever_nearby_needs_the_live_check_too(pool):
    async with pool.connection() as conn:
        await add(conn, "manual", "page 0", "page 1", "page 2")
    retriever = PostgresRetriever(pool, mode="keyword")
    [anchor] = await retriever.search("page 1", principals=["user:a"], verify=allow("manual"), k=1)
    calls = []
    hits = await retriever.nearby(anchor, principals=["user:a"], verify=allow("manual", calls=calls), before=1, after=1)
    assert [h.text for h in hits] == ["page 0", "page 1", "page 2"] and calls == [[("d1", "manual")]]
    assert await retriever.nearby(anchor, principals=["user:a"], verify=allow(), before=1, after=1) == []  # revoked


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


async def test_search_with_files_returns_the_readable_files_ranked_below_the_top_k(pool):
    """The fact ranked 7th of 5: its file must still reach the sandbox. Files the caller can't open stay out."""
    async with pool.connection() as conn:
        await add(conn, "top", *[f"oil capacity table {n}" for n in range(5)])
        await add(conn, "seventh", "oil capacity is 7 quarts with filter")
        await add(conn, "revoked", "oil capacity of the secret engine")
        await add(conn, "hidden", "oil capacity in a file the user is not in the ACL of", acl=("user:b",))
    calls = []
    hits, files = await PostgresRetriever(pool, mode="keyword").search_with_files(
        "oil capacity", principals=["user:a"], verify=allow("top", "seventh", calls=calls), k=5)
    assert {h.item_id for h in hits} == {"top"}
    assert {f.item_id for f in files} == {"top", "seventh"}
    assert len(calls) == 1  # the pool's files are checked in the same live check as the hits'
    assert await PostgresRetriever(pool, mode="keyword").search(
        "oil capacity", principals=["user:a"], verify=allow("top", "seventh"), k=5) == hits


async def test_read_files_returns_whole_files_behind_both_layers(pool):
    async with pool.connection() as conn:
        await add(conn, "f1", "page one", "page two", "page three")
        await add(conn, "revoked", "secret page")
        await add(conn, "hidden", "other user's page", acl=("user:b",))
    retriever = PostgresRetriever(pool, mode="keyword")
    files = {}
    for item in ("f1", "revoked", "hidden"):
        _, found = await retriever.search_with_files("page", principals=["user:a", "user:b"], verify=allow(item), k=5)
        files.update({f.item_id: f for f in found})
    out = await retriever.read_files(list(files.values()), principals=["user:a"], verify=allow("f1", "hidden"))
    assert [[h.text for h in hs] for hs in out.values()] == [["page one", "page two", "page three"]]
    assert await retriever.read_files(list(files.values()), principals=["user:a"], verify=allow()) == {}
