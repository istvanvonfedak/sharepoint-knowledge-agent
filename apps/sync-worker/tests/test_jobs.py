"""Job handlers against a real Postgres and a fake Graph (see tests/fixtures.py for the database)."""
import json
from datetime import datetime, timezone
from urllib.parse import unquote

import pytest
from sharepoint_rag import index_store, queue
from sync_worker import jobs
from sync_worker.graph import BatchResult, FileTooLarge, GraphError


class FakeGraph:
    """Delta pages, item metadata, permissions and file bytes for one drive `d1`."""

    def __init__(self) -> None:
        self.delta_pages: list[dict] = []
        self.expired_links: set[str] = set()
        self.items: dict[str, dict] = {}
        self.perms: dict[str, list[dict]] = {}
        self.content: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.denied: set[str] = set()  # items whose permissions the app may not read
        self.site_groups: dict[str, list[str]] = {}
        self.users: dict[str, str] = {}  # UPN -> Entra object id  # SharePoint site group id -> member login names

    async def sharepoint_get_all(self, url: str) -> list[dict]:
        assert url.startswith("https://sp/_api/web/sitegroups/getbyid(")
        return [{"LoginName": login} for login in self.site_groups[url.split("(")[1].split(")")[0]]]

    async def get(self, path: str, **kwargs) -> dict:
        self.calls.append(path)
        if path in self.expired_links:
            raise GraphError(410, "resyncRequired")
        if path.startswith("delta:"):  # a saved delta link: nothing changed since
            return {"value": [], "@odata.deltaLink": path}
        if "/delta" in path:
            return self.delta_pages[0]
        if path.startswith("page:"):
            return self.delta_pages[int(path.split(":")[1])]
        if path.startswith("/users/"):
            upn = unquote(path.removeprefix("/users/"))
            if upn not in self.users:
                raise GraphError(404, "notFound")
            return {"id": self.users[upn]}
        item_id = path.rsplit("/", 1)[-1]
        if item_id not in self.items:
            raise GraphError(404, "itemNotFound")
        return self.items[item_id]

    async def get_all(self, path: str, **kwargs) -> list[dict]:
        item_id = path.split("/items/")[1].split("/")[0]
        if item_id in self.denied:
            raise GraphError(403, "accessDenied")
        return self.perms[item_id]

    async def get_all_timed(self, path: str, **kwargs):
        return await self.get_all(path), datetime.now(timezone.utc)

    async def download(self, drive_id: str, item_id: str, max_bytes: int) -> bytes:
        return self.content[item_id]

    async def batch_get(self, paths: list[str]):
        out, now = [], datetime.now(timezone.utc)
        for p in paths:
            item_id = p.split("/items/")[1].split("/")[0]
            if item_id in self.denied:
                out.append(BatchResult(403, {}, now))
            elif item_id in self.items:
                out.append(BatchResult(200, {"value": self.perms[item_id]}, now))
            else:
                out.append(BatchResult(404, {}, now))
        return out


def raw(item_id: str, etag="e1", name=None, parent="root") -> dict:
    return {
        "id": item_id, "name": name or f"{item_id}.md", "eTag": etag, "webUrl": f"https://sp/{item_id}", "size": 10,
        "lastModifiedDateTime": "2026-01-01T00:00:00Z", "parentReference": {"id": parent}, "file": {},
    }


@pytest.fixture
async def ctx(pool):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO sites (id, name, web_url) VALUES ('s1', 'Site', 'https://sp')")
        await conn.execute("INSERT INTO drives (id, site_id, name) VALUES ('d1', 's1', 'Docs')")

    async def heartbeat():
        pass

    return jobs.Context(pool=pool, graph=FakeGraph(), heartbeat=heartbeat)


async def run_queued(ctx, kinds=None) -> list[str]:
    ran = []
    while True:
        async with ctx.pool.connection() as conn:
            job = await queue.claim(conn, kinds)
        if job is None:
            return ran
        await jobs.HANDLERS[job.kind](ctx, job.payload)
        async with ctx.pool.connection() as conn:
            await queue.complete(conn, job)
        ran.append(job.kind)


async def seed(ctx, *item_ids):
    """Record files as a crawl would before queueing index_item (index_item never creates rows)."""
    async with ctx.pool.connection() as conn:
        await index_store.upsert_seen(conn, [jobs._item("d1", "s1", ctx.graph.items[i]) for i in item_ids], None)


async def rows(ctx, q: str):
    async with ctx.pool.connection() as conn:
        return await (await conn.execute(q)).fetchall()


async def test_crawl_index_and_incremental_delete(ctx):
    g = ctx.graph
    g.items = {"a": raw("a"), "b": raw("b")}
    g.perms = {"a": [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}}}], "b": [{"roles": ["read"], "link": {"scope": "organization"}}]}
    g.content = {"a": b"# Alpha\nalpha text", "b": b"# Beta\nbeta text"}
    g.delta_pages = [{"value": [{"id": "root", "root": {}, "folder": {}}, raw("a")], "@odata.nextLink": "page:1"},
                     {"value": [raw("b")], "@odata.deltaLink": "delta:1"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    assert sorted(await run_queued(ctx)) == ["index_item", "index_item"]
    assert await rows(ctx, "SELECT s.item_id, c.acl FROM chunks_g1 c JOIN sources s ON s.id = c.source_id ORDER BY 1") == [
        ("a", ["user:u1"]), ("b", ["org:members"]),
    ]
    assert await rows(ctx, "SELECT delta_link, next_link, crawl_id FROM sync_state") == [("delta:1", None, None)]

    # Next run: the delta link reports `a` deleted.
    g.delta_pages = [{"value": [{"id": "a", "deleted": {}}], "@odata.deltaLink": "delta:2"}]
    g.get = _with_page(g, "delta:1", {"value": [{"id": "a", "deleted": {}}], "@odata.deltaLink": "delta:2"})
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    assert await rows(ctx, "SELECT item_id FROM sources WHERE status <> 'folder'") == [("b",)]


def _with_page(g: FakeGraph, link: str, page: dict):
    original = g.get

    async def get(path, **kwargs):
        if path == link:
            return page
        return await original(path, **kwargs)

    return get


async def test_unchanged_files_are_not_requeued(ctx):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"text"}
    g.delta_pages = [{"value": [raw("a")], "@odata.deltaLink": "delta:1"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    await run_queued(ctx)
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})  # resync from scratch would also see `a` unchanged
    assert await rows(ctx, "SELECT count(*) FROM jobs WHERE state = 'queued'") == [(0,)]


async def test_a_new_index_version_reindexes_unchanged_files(ctx):
    """Bumping index_store.INDEX_VERSION (chunking changed) must reach files nobody edits: delta reports none of
    them, so the next sync is a full crawl and every file indexed by the older version is queued again."""
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"text"}
    g.delta_pages = [{"value": [raw("a")], "@odata.deltaLink": "delta:1"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    await run_queued(ctx)
    assert await rows(ctx, "SELECT index_version FROM sources") == [(index_store.INDEX_VERSION,)]
    assert await rows(ctx, "SELECT index_version FROM sync_state") == [(index_store.INDEX_VERSION,)]

    async with ctx.pool.connection() as conn:  # as if indexed and crawled by the previous version
        await conn.execute("UPDATE sources SET index_version = %s", (index_store.INDEX_VERSION - 1,))
        await conn.execute("UPDATE sync_state SET index_version = %s", (index_store.INDEX_VERSION - 1,))
    g.calls.clear()
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    assert not any(c.startswith("delta:") for c in g.calls)  # a full crawl, not the saved delta link
    assert await run_queued(ctx) != []
    assert await rows(ctx, "SELECT index_version FROM sources") == [(index_store.INDEX_VERSION,)]
    assert await rows(ctx, "SELECT index_version FROM sync_state") == [(index_store.INDEX_VERSION,)]


async def test_expired_cursor_recrawls_and_deletes_files_missed_in_the_gap(ctx):
    g = ctx.graph
    g.items = {"a": raw("a"), "b": raw("b")}
    g.perms, g.content = {"a": [], "b": []}, {"a": b"a", "b": b"b"}
    g.delta_pages = [{"value": [raw("a"), raw("b")], "@odata.deltaLink": "delta:1"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    await run_queued(ctx)
    # `b` was deleted while the cursor was expired: the re-crawl never mentions it.
    g.expired_links = {"delta:1"}
    del g.items["b"]
    g.delta_pages = [{"value": [raw("a")], "@odata.deltaLink": "delta:2"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    assert await rows(ctx, "SELECT item_id FROM sources ORDER BY 1") == [("a",)]


async def test_partial_recrawl_deletes_nothing(ctx):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a"), "b": raw("b")}, {"a": [], "b": []}, {"a": b"a", "b": b"b"}
    g.delta_pages = [{"value": [raw("a"), raw("b")], "@odata.deltaLink": "delta:1"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    g.expired_links = {"delta:1"}
    g.delta_pages = [{"value": [raw("a")], "@odata.nextLink": "page:1"}, None]
    with pytest.raises(AttributeError):  # the crawl dies on its second page
        await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    assert await rows(ctx, "SELECT item_id FROM sources ORDER BY 1") == [("a",), ("b",)]
    assert (await rows(ctx, "SELECT next_link FROM sync_state"))[0][0] == "page:1"  # resumes there


async def test_unsupported_and_vanished_files(ctx):
    g = ctx.graph
    g.items = {"x": raw("x", name="tool.exe")}
    await seed(ctx, "x")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "x"})
    assert await rows(ctx, "SELECT status FROM sources") == [("skipped",)]
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "gone"})  # 404: nothing to keep
    assert await rows(ctx, "SELECT count(*) FROM sources WHERE item_id = 'gone'") == [(0,)]


async def test_corrupt_file_is_marked_error_but_its_permissions_still_update(ctx):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}}}]}, {"a": b"ok"}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    g.items["a"] = raw("a", etag="e2", name="a.docx")
    g.content["a"] = b"not a zip"
    g.perms["a"] = []  # u1 lost access at the same time
    with pytest.raises(jobs.Permanent):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT status FROM sources") == [("error",)]
    assert await rows(ctx, "SELECT acl FROM chunks_g1") == [([],)]  # old text kept, but nobody matches it now


async def test_sweep_retries_files_left_in_error(ctx, monkeypatch):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"hello"}
    await seed(ctx, "a")

    async def down(*args, **kwargs):
        raise RuntimeError("inference-server unreachable")

    with monkeypatch.context() as m:
        m.setattr(jobs.inference, "embed", down)
        with pytest.raises(RuntimeError):
            await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT status FROM sources") == [("error",)]
    await jobs.sweep_acls(ctx, {})  # the outage is over; delta won't report the unchanged file again
    assert sorted(await run_queued(ctx)) == ["index_item", "sync_item_acl"]
    assert await rows(ctx, "SELECT status FROM sources") == [("indexed",)]


async def test_malformed_responses_are_retried_not_permanent(ctx, monkeypatch):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"hello"}
    await seed(ctx, "a")

    async def html_page(*args, **kwargs):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)  # a ValueError, but not the file's fault

    monkeypatch.setattr(jobs.inference, "embed", html_page)
    with pytest.raises(ValueError) as e:
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert not isinstance(e.value, jobs.Permanent)


async def test_a_rate_limit_resumes_where_it_stopped_and_keeps_the_file_status(ctx, monkeypatch):
    """Each embedded batch is kept: after a rate limit the retry embeds only the rest, and the file is not
    marked `error` (it is waiting, not broken)."""
    from sharepoint_rag.embeddings import RateLimited
    from tests.testing_support import fake_vector

    g = ctx.graph
    text = "\n\n".join(f"Paragraph {n}: " + "torque the lug nuts to spec. " * 40 for n in range(12))
    g.items, g.perms, g.content = {"a": raw("a", name="a.txt")}, {"a": []}, {"a": text.encode()}
    await seed(ctx, "a")
    monkeypatch.setattr(jobs.embeddings, "BATCH_SIZE", 2)
    embedded, budget = [], [2]

    async def embed(model, texts, task, dimensions):
        if budget[0] == 0:
            raise RateLimited("quota", retry_after=3)
        budget[0] -= 1
        embedded.extend(texts)
        return [fake_vector(f"{model}:{t}", dimensions) for t in texts]

    monkeypatch.setattr(jobs.inference, "embed", embed)
    with pytest.raises(RateLimited):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert len(embedded) == 4
    assert await rows(ctx, "SELECT status FROM sources WHERE item_id = 'a'") == [("pending",)]

    budget[0] = 100
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    chunks = (await rows(ctx, "SELECT count(*) FROM chunks_g1"))[0][0]
    assert len(embedded) == chunks > 4  # every chunk embedded exactly once over both attempts
    assert len(set(embedded)) == len(embedded)
    assert await rows(ctx, "SELECT status FROM sources WHERE item_id = 'a'") == [("indexed",)]
    assert await rows(ctx, "SELECT count(*) FROM embedding_progress") == [(0,)]  # written chunks hold the vectors now


async def test_a_lost_lease_while_embedding_for_a_building_generation_stops_the_job(ctx, monkeypatch):
    """A building generation's embedding errors are logged and left to its backfill, but a lost lease is not an
    embedding error: the job must stop, or two workers write the file."""
    import dataclasses

    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"hello"}
    await seed(ctx, "a")
    real_live, real_embed = jobs.generations.live, jobs._embed

    async def live(conn, **kwargs):
        active = await real_live(conn, **kwargs)
        return active + [dataclasses.replace(active[0], id=99, status="building")]

    async def embed(ctx, gen, files, *, reuse):
        if gen.status == "building":
            raise jobs.LeaseLost("job lease lost")
        return await real_embed(ctx, gen, files, reuse=reuse)

    monkeypatch.setattr(jobs.generations, "live", live)
    monkeypatch.setattr(jobs, "_embed", embed)
    with pytest.raises(jobs.LeaseLost):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT count(*) FROM chunks_g1") == [(0,)]


async def test_a_rate_limited_index_still_applies_the_new_permissions(ctx, monkeypatch):
    """The job waits for the quota, but a narrowed share must not: the permission read it made is written."""
    from sharepoint_rag.embeddings import RateLimited

    g = ctx.graph
    g.items, g.content = {"a": raw("a")}, {"a": b"budget plan"}
    g.perms = {"a": [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}}}, {"roles": ["read"], "grantedToV2": {"user": {"id": "u2"}}}]}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    g.items["a"], g.content["a"] = raw("a", etag="e2"), b"budget plan, revised"  # edited, and u2 lost access
    g.perms["a"] = [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}}}]

    async def quota(*args):
        raise RateLimited("quota", retry_after=30)

    monkeypatch.setattr(jobs.inference, "embed", quota)
    with pytest.raises(RateLimited):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT acl FROM chunks_g1") == [(["user:u1"],)]  # the old chunk, new permissions
    assert await rows(ctx, "SELECT status FROM sources") == [("indexed",)]


async def test_a_backfill_embeds_small_files_together_and_resumes_after_a_rate_limit(ctx, db_url, monkeypatch):
    import psycopg
    from sharepoint_rag import generations
    from sharepoint_rag.embeddings import RateLimited
    from tests.testing_support import TEST_DIMENSIONS, fake_vector

    g = ctx.graph
    names = [f"f{n}" for n in range(5)]
    g.items = {n: raw(n) for n in names}
    g.perms, g.content = {n: [] for n in names}, {n: f"note {n} about the winch".encode() for n in names}
    await seed(ctx, *names)
    for n in names:
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": n})
    with psycopg.connect(db_url, autocommit=True) as sconn:
        gen_id = generations.create(sconn, "gemini:new-model", TEST_DIMENSIONS)
    monkeypatch.setattr(jobs.embeddings, "BATCH_SIZE", 3)
    calls, budget = [], [1]

    async def embed(model, texts, task, dimensions):
        if budget[0] == 0:
            raise RateLimited("quota", retry_after=3)
        budget[0] -= 1
        calls.append(list(texts))
        return [fake_vector(f"{model}:{t}", dimensions) for t in texts]

    monkeypatch.setattr(jobs.inference, "embed", embed)
    with pytest.raises(RateLimited):
        await jobs.backfill_generation(ctx, {"generation_id": gen_id})
    budget[0] = 100
    await jobs.backfill_generation(ctx, {"generation_id": gen_id})
    assert [len(c) for c in calls] == [3, 2]  # five one-chunk files in two calls, the first batch kept across the limit
    assert await rows(ctx, f"SELECT count(*) FROM chunks_g{gen_id}") == [(5,)]
    assert await rows(ctx, "SELECT count(*) FROM embedding_progress") == [(0,)]


async def test_a_reindex_keeps_the_vectors_of_unchanged_chunks(ctx, monkeypatch):
    g = ctx.graph
    paragraphs = [f"Section {n}. " + "check the hydraulic fluid level. " * 40 for n in range(6)]
    g.items, g.perms, g.content = {"a": raw("a", name="a.txt")}, {"a": []}, {"a": "\n\n".join(paragraphs).encode()}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    embedded = []
    real = jobs.inference.embed

    async def counting(model, texts, task, dimensions):
        embedded.extend(texts)
        return await real(model, texts, task, dimensions)

    monkeypatch.setattr(jobs.inference, "embed", counting)
    g.items["a"] = raw("a", etag="e2", name="a.txt")  # one paragraph edited
    g.content["a"] = "\n\n".join(paragraphs[:-1] + ["Section 5. replaced text."]).encode()
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert 0 < len(embedded) < (await rows(ctx, "SELECT count(*) FROM chunks_g1"))[0][0]


async def test_a_cut_off_file_is_marked_partly_indexed(ctx, monkeypatch):
    """A file past an extraction limit is indexed as far as it goes, and its status says it isn't whole."""
    from sharepoint_rag import extract as extract_module
    from tests.testing_support import text_pdf

    monkeypatch.setattr(extract_module, "MAX_PDF_PAGES", 2)
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a", name="manual.pdf")}, {"a": []}, {"a": text_pdf(3)}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT status, status_note FROM sources") == [
        ("indexed", "partly indexed: only the first 2 of 3 pages were indexed")]


async def test_oversized_download_is_permanent(ctx):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"x"}
    await seed(ctx, "a")

    async def too_big(*args):
        raise FileTooLarge("file is larger than 10 bytes")

    g.download = too_big
    with pytest.raises(jobs.Permanent):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})


async def test_a_lost_lease_writes_nothing(ctx):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"hello"}
    await seed(ctx, "a")

    async def lost():
        raise jobs.LeaseLost("job lease lost")

    ctx.heartbeat = lost
    with pytest.raises(jobs.LeaseLost):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT status FROM sources") != [("error",)]


async def test_direct_grant_to_a_upn_only_user(ctx):
    g = ctx.graph
    g.items, g.content = {"a": raw("a")}, {"a": b"hello"}
    g.perms = {"a": [{"roles": ["read"], "grantedToV2": {"siteUser": {"id": "9", "loginName": "i:0#.f|membership|Bob@contoso.com"}}}]}
    g.users = {"Bob@contoso.com": "b1"}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT acl FROM chunks_g1") == [(["user:b1"],)]


async def test_sweep_resyncs_permissions_and_deletes_vanished_files(ctx):
    g = ctx.graph
    g.items = {"a": raw("a"), "b": raw("b")}
    g.perms = {"a": [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}}}], "b": []}
    g.content = {"a": b"a", "b": b"b"}
    await seed(ctx, "a", "b")
    for i in ("a", "b"):
        await jobs.index_item(ctx, {"drive_id": "d1", "item_id": i})
    g.perms["a"] = [{"roles": ["read"], "grantedToV2": {"group": {"id": "g9"}}}]
    del g.items["b"]
    await jobs.sweep_acls(ctx, {})
    assert await run_queued(ctx) == ["sync_item_acl"]
    assert await rows(ctx, "SELECT s.item_id, c.acl FROM chunks_g1 c JOIN sources s ON s.id = c.source_id") == [("a", ["group:g9"])]


async def test_site_groups_are_expanded_on_index_and_sweep(ctx):
    g = ctx.graph
    g.items, g.content = {"a": raw("a")}, {"a": b"a"}
    g.perms = {"a": [{"roles": ["write"], "grantedToV2": {"siteGroup": {"id": "5", "displayName": "Members"}}}]}
    g.site_groups = {"5": ["c:0o.c|federateddirectoryclaimprovider|m365"]}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    assert await rows(ctx, "SELECT acl FROM chunks_g1") == [(["group:m365"],)]
    g.site_groups["5"].append("c:0t.c|tenant|sg1")  # a membership change: the file itself is untouched
    ctx.directory = jobs.Directory(g)  # what the cache expiring does
    await jobs.sweep_acls(ctx, {})
    await run_queued(ctx)
    assert await rows(ctx, "SELECT acl FROM chunks_g1") == [(["group:m365", "group:sg1"],)]


async def test_unreadable_permissions_hide_the_file(ctx):
    g = ctx.graph
    g.items, g.content = {"a": raw("a")}, {"a": b"a"}
    g.perms = {"a": [{"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}}}]}
    await seed(ctx, "a")
    await jobs.index_item(ctx, {"drive_id": "d1", "item_id": "a"})
    g.denied = {"a"}  # e.g. the Sites.Selected grant was removed
    await jobs.sweep_acls(ctx, {})
    await run_queued(ctx)
    assert await rows(ctx, "SELECT acl FROM chunks_g1") == [(["acl:unresolved"],)]


async def test_purge_beats_an_index_job_in_flight(ctx):
    g = ctx.graph
    g.items, g.perms, g.content = {"a": raw("a")}, {"a": []}, {"a": b"secret"}
    g.delta_pages = [{"value": [raw("a")], "@odata.deltaLink": "delta:1"}]
    await jobs.crawl_drive(ctx, {"drive_id": "d1"})
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE sites SET enabled = false")
    await jobs.purge_drive(ctx, {"drive_id": "d1"})
    await run_queued(ctx, ["index_item"])  # queued before the purge
    assert await rows(ctx, "SELECT count(*) FROM sources") == [(0,)]


async def test_a_drive_that_reappears_is_crawled_again(ctx, monkeypatch):
    listing = {"value": [{"id": "d1", "name": "Docs"}]}

    async def get_all(path, **kwargs):
        return listing["value"]

    monkeypatch.setattr(ctx.graph, "get_all", get_all)
    listing["value"] = []
    await jobs.discover_sites(ctx, {})
    assert await rows(ctx, "SELECT enabled FROM drives") == [(False,)]
    listing["value"] = [{"id": "d1", "name": "Docs"}]
    await jobs.discover_sites(ctx, {})
    assert await rows(ctx, "SELECT enabled FROM drives") == [(True,)]
    assert ("crawl_drive",) in await rows(ctx, "SELECT kind FROM jobs WHERE state = 'queued'")

