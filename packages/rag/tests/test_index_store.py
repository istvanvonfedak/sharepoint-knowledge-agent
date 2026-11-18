"""Index writes, permission rules, generations and search against a real Postgres (see tests/fixtures.py)."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from tests.testing_support import TEST_DIMENSIONS, fake_vector
from sharepoint_rag import acl, generations, index_store, search
from sharepoint_rag.acl import AclRead
from sharepoint_rag.index_store import ChunkRow, Item

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def item(item_id="f1", modified=T0, etag="e1", parent=None, folder=False) -> Item:
    return Item("d1", item_id, "s1", f"{item_id}.md", f"https://sp/{item_id}", etag, modified, parent, folder)


def read(*principals, at=T0, resolved=True) -> AclRead:
    return AclRead(tuple(principals), resolved, at)


def chunks(*texts, lang="simple") -> list[ChunkRow]:
    return [ChunkRow(n, t, None, lang) for n, t in enumerate(texts)]


def vectors(gen_ids, rows) -> dict:
    return {g: [fake_vector(r.text) for r in rows] for g in gen_ids}


async def write(conn, it: Item, *args, **kwargs):
    """write_index for a file the crawl has recorded (it never creates rows)."""
    await index_store.upsert_seen(conn, [it], None)
    return await index_store.write_index(conn, it, *args, **kwargs)


async def chunk_acls(conn, gen_id=1) -> list[list[str]]:
    rows = await (await conn.execute(f"SELECT acl FROM chunks_g{gen_id} ORDER BY id")).fetchall()
    return [r[0] for r in rows]


async def test_write_index_replaces_chunks_and_sets_acl(pool):
    async with pool.connection() as conn:
        rows = chunks("first version")
        assert await write(conn, item(), read("user:a"), rows, vectors([1], rows)) == "written"
        rows = chunks("second", "version")
        await write(conn, item(etag="e2", modified=T0 + timedelta(1)), read("user:a", "group:g", at=T0 + timedelta(1)), rows, vectors([1], rows))
        texts = [r[0] for r in await (await conn.execute("SELECT text FROM chunks_g1 ORDER BY ordinal")).fetchall()]
        assert texts == ["second", "version"]
        assert await chunk_acls(conn) == [["group:g", "user:a"]] * 2
        assert await index_store.is_current(conn, "d1", "f1", "e2")
        assert not await index_store.is_current(conn, "d1", "f1", "e1")


async def test_older_permission_read_never_overwrites_a_newer_one(pool):
    """A slow index_item that read permissions before a revocation must not write the old list back."""
    async with pool.connection() as conn:
        rows = chunks("text")
        await write(conn, item(), read("user:a", "user:b", at=T0), rows, vectors([1], rows))
        assert await index_store.write_acl(conn, "d1", "f1", read("user:a", at=T0 + timedelta(minutes=5)))  # b revoked
        # The slow job finishes with its stale read (taken at T0 + 1 min, before the revocation was read).
        await write(conn, item(etag="e2", modified=T0 + timedelta(1)), read("user:a", "user:b", at=T0 + timedelta(minutes=1)), rows, vectors([1], rows))
        assert await chunk_acls(conn) == [["user:a"]]
        assert not await index_store.write_acl(conn, "d1", "f1", read("user:a", "user:b", at=T0 + timedelta(minutes=2)))
        assert await chunk_acls(conn) == [["user:a"]]


async def test_a_file_deleted_while_being_indexed_stays_deleted(pool):
    async with pool.connection() as conn:
        await index_store.upsert_seen(conn, [item()], None)
        await index_store.delete_items(conn, "d1", ["f1"])  # the crawl saw it deleted mid-job
        rows = chunks("secret text")
        assert await index_store.write_index(conn, item(), read("user:a"), rows, vectors([1], rows)) == "gone"
        assert (await (await conn.execute("SELECT count(*) FROM sources")).fetchone())[0] == 0


async def test_older_content_is_not_written_over_newer(pool):
    async with pool.connection() as conn:
        new = chunks("new text")
        await write(conn, item(etag="e2", modified=T0 + timedelta(1)), read("user:a"), new, vectors([1], new))
        old = chunks("old text")
        assert await write(conn, item(etag="e1", modified=T0), read("user:a"), old, vectors([1], old)) == "stale"
        assert [r[0] for r in await (await conn.execute("SELECT text FROM chunks_g1")).fetchall()] == ["new text"]


async def test_unresolved_permissions_hide_the_file(pool):
    async with pool.connection() as conn:
        rows = chunks("secret")
        await write(conn, item(), read("user:a", resolved=False), rows, vectors([1], rows))
        assert await chunk_acls(conn) == [[acl.UNRESOLVED]]
        hits = await search.search(conn, query_text="secret", query_vec=fake_vector("secret"), principals=["user:a"], mode="keyword")
        assert hits == []


async def test_search_requires_principals(pool):
    async with pool.connection() as conn:
        with pytest.raises(ValueError):
            await search.search(conn, query_text="x", query_vec=fake_vector("x"), principals=[])


async def test_keyword_search_matches_spanish_word_forms(pool):
    async with pool.connection() as conn:
        rows = chunks("Los contratos de trabajo se firman en la oficina", lang="spanish")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))
        hits = await search.search(conn, query_text="contrato", query_vec=fake_vector("q"), principals=["user:a"], mode="keyword")
        assert [h.item_id for h in hits] == ["f1"]


async def test_folder_delete_removes_its_subtree(pool):
    async with pool.connection() as conn:
        await index_store.upsert_seen(conn, [item("folder", folder=True), item("sub", folder=True, parent="folder")], None)
        rows = chunks("deep")
        await write(conn, item("f1", parent="sub"), read("user:a"), rows, vectors([1], rows))
        await write(conn, item("f2", parent=None), read("user:a"), rows, vectors([1], rows))
        assert await index_store.delete_items(conn, "d1", ["folder"]) == 3
        left = [r[0] for r in await (await conn.execute("SELECT item_id FROM sources")).fetchall()]
        assert left == ["f2"]
        assert (await (await conn.execute("SELECT count(*) FROM chunks_g1")).fetchone())[0] == 1


async def test_full_crawl_deletes_only_unseen_files(pool):
    async with pool.connection() as conn:
        rows = chunks("x")
        for i in ("keep", "gone"):
            await write(conn, item(i), read("user:a"), rows, vectors([1], rows))
        await index_store.upsert_seen(conn, [item("keep")], "crawl-2")
        assert await index_store.finish_full_crawl(conn, "d1", "crawl-2") == 1
        assert [r[0] for r in await (await conn.execute("SELECT item_id FROM sources")).fetchall()] == ["keep"]


async def test_not_current_lists_changed_and_new_files(pool):
    async with pool.connection() as conn:
        rows = chunks("x")
        await write(conn, item("same", etag="e1"), read("user:a"), rows, vectors([1], rows))
        await write(conn, item("changed", etag="e1"), read("user:a"), rows, vectors([1], rows))
        changed = await index_store.not_current(conn, "d1", [("same", "e1"), ("changed", "e2"), ("new", "e1")])
        assert sorted(changed) == ["changed", "new"]


# ---- generations ----------------------------------------------------------------------------------

def _sync(db_url):
    return psycopg.connect(db_url, autocommit=True)


async def test_generation_backfill_check_and_activate(pool, db_url):
    async with pool.connection() as conn:
        rows = chunks("alpha", "beta")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))
    with _sync(db_url) as sconn:
        gen_id = generations.create(sconn, "gemini:new-model", TEST_DIMENSIONS)
        assert generations.check(sconn, gen_id)  # nothing backfilled yet
        with pytest.raises(ValueError, match="Not activating"):
            generations.activate(sconn, gen_id)

    async with pool.connection() as conn:
        gen = next(g for g in await generations.live(conn) if g.id == gen_id)
        # A permission change while building goes to every live generation.
        [src] = await index_store.backfill_candidates(conn, gen, 10)
        assert await index_store.write_acl(conn, "d1", "f1", read("user:b", at=T0 + timedelta(1)))
        assert await index_store.write_backfill(conn, gen_id, src, [fake_vector(c.text) for c in src.chunks])
        assert await chunk_acls(conn, gen_id) == [["user:b"]] * 2  # taken from sources at write time
        assert await index_store.backfill_candidates(conn, gen, 10) == []

    with _sync(db_url) as sconn:
        assert generations.check(sconn, gen_id) == []
        generations.activate(sconn, gen_id)
    async with pool.connection() as conn:
        assert (await generations.active(conn)).id == gen_id
        assert [g.id for g in await generations.live(conn)] == [gen_id]


async def test_generation_check_catches_permission_drift(pool, db_url):
    async with pool.connection() as conn:
        rows = chunks("alpha")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))
    with _sync(db_url) as sconn:
        gen_id = generations.create(sconn, "gemini:new-model", TEST_DIMENSIONS)
    async with pool.connection() as conn:
        gen = next(g for g in await generations.live(conn) if g.id == gen_id)
        [src] = await index_store.backfill_candidates(conn, gen, 10)
        await index_store.write_backfill(conn, gen_id, src, [fake_vector("alpha")])
        await conn.execute(f"UPDATE chunks_g{gen_id} SET acl = '{{user:everyone-oops}}'")
    with _sync(db_url) as sconn:
        assert any("permissions differ" in p for p in generations.check(sconn, gen_id))


async def test_backfill_skips_a_file_changed_since_its_text_was_read(pool, db_url):
    async with pool.connection() as conn:
        rows = chunks("v1")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))
    with _sync(db_url) as sconn:
        gen_id = generations.create(sconn, "gemini:new-model", TEST_DIMENSIONS)
    async with pool.connection() as conn:
        gen = next(g for g in await generations.live(conn) if g.id == gen_id)
        [src] = await index_store.backfill_candidates(conn, gen, 10)
        rows = chunks("v2")
        await write(conn, item(etag="e2", modified=T0 + timedelta(1)), None, rows, vectors([1, gen_id], rows))
        assert not await index_store.write_backfill(conn, gen_id, src, [fake_vector("v1")])
        texts = [r[0] for r in await (await conn.execute(f"SELECT text FROM chunks_g{gen_id}")).fetchall()]
        assert texts == ["v2"]


async def test_a_write_that_skips_a_building_generation_queues_its_backfill(pool, db_url):
    with _sync(db_url) as sconn:
        gen_id = generations.create(sconn, "gemini:new-model", TEST_DIMENSIONS)
        sconn.execute("UPDATE jobs SET state = 'done'")  # the first backfill already finished
    async with pool.connection() as conn:
        rows = chunks("text")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))  # embedded before the generation existed
        queued = await (await conn.execute("SELECT kind, payload FROM jobs WHERE state = 'queued'")).fetchall()
        assert queued == [("backfill_generation", {"generation_id": gen_id})]


async def test_a_generation_activated_mid_write_is_not_emptied(pool, db_url):
    async with pool.connection() as conn:
        rows = chunks("text")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))
    with _sync(db_url) as sconn:
        gen_id = generations.create(sconn, "gemini:new-model", TEST_DIMENSIONS)
    async with pool.connection() as conn:
        gen = next(g for g in await generations.live(conn) if g.id == gen_id)
        [src] = await index_store.backfill_candidates(conn, gen, 10)
        assert await index_store.write_backfill(conn, gen_id, src, [fake_vector(c.text) for c in src.chunks])
    with _sync(db_url) as sconn:
        generations.activate(sconn, gen_id)  # meanwhile, an index_item embedded only for generation 1
    async with pool.connection() as conn:
        rows = chunks("v2")
        with pytest.raises(index_store.MissingVectors):
            await write(conn, item(etag="e2"), read("user:a"), rows, vectors([1], rows))
        texts = [r[0] for r in await (await conn.execute(f"SELECT text FROM chunks_g{gen_id}")).fetchall()]
        assert texts == ["text"]  # still served; the job's retry embeds for the active generation


async def test_write_index_leaves_name_and_folder_to_the_crawl(pool):
    async with pool.connection() as conn:
        rows = chunks("text")
        await index_store.upsert_seen(conn, [item(parent="p2")], None)  # the crawl saw the move to p2
        await index_store.write_index(conn, item(parent="p1"), read("user:a"), rows, vectors([1], rows))  # older read
        parent = await (await conn.execute("SELECT parent_id FROM sources")).fetchone()
        assert parent == ("p2",)


def test_migrate_skips_an_unchanged_schema(db_url):
    from sharepoint_rag import db

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE sources ADD COLUMN canary int")  # schema.sql would not remove it either way
        conn.execute("DROP INDEX sources_status_idx")  # ...but it would re-create this
    db.migrate(db_url)
    with psycopg.connect(db_url, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('sources_status_idx')").fetchone()[0] is None
        conn.execute("UPDATE schema_version SET digest = 'old'")
    db.migrate(db_url)  # a changed schema is applied
    with psycopg.connect(db_url, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('sources_status_idx')").fetchone()[0] is not None


async def test_huge_keyword_queries_are_capped(pool):
    async with pool.connection() as conn:
        rows = chunks("zz")
        await write(conn, item(), read("user:a"), rows, vectors([1], rows))
        query = " ".join(f"w{i}" for i in range(7000)) + " zz"
        hits = await search.search(conn, query_text=query, query_vec=fake_vector("q"), principals=["user:a"], mode="keyword")
        assert isinstance(hits, list)  # no "invalid memory alloc request" from an unbounded tsquery
