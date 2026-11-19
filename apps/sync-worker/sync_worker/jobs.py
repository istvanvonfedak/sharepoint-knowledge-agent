"""Job handlers. Each takes the job payload and returns when its work is committed.

| Job                  | Queued by                     |
|----------------------|-------------------------------|
| discover_sites       | schedule (hourly)             |
| crawl_drive          | schedule (every few minutes), discover_sites for new drives |
| index_item           | crawl_drive for changed files |
| delete_item          | index_item / sync_item_acl when Graph says the file is gone |
| purge_drive          | discover_sites when a site or drive is removed |
| sync_item_acl        | sweep_acls                    |
| sweep_acls           | schedule (daily)              |
| backfill_generation  | `python -m sharepoint_rag.generations create` |
"""
import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from psycopg_pool import AsyncConnectionPool
from sharepoint_rag import embeddings, generations, index_store, inference, queue
from sharepoint_rag.acl import AclRead
from sharepoint_rag.chunk import Chunk
from sharepoint_rag.embeddings import RateLimited
from sharepoint_rag.extract import EXTENSIONS, ExtractionError, truncation_note
from sharepoint_rag.index_store import Item

from . import config, permissions
from .directory import Directory
from .graph import AppGraph, FileTooLarge, GraphError, seg

log = logging.getLogger(__name__)

ITEM_SELECT = "id,name,eTag,cTag,size,webUrl,lastModifiedDateTime,parentReference,file,folder,root,package,deleted"
ACL_BATCH = 20
SWEEP_PAGE = 5000
BACKFILL_BATCH = 50
BACKFILL_MAX_BATCHES = 200  # per job run; the job re-queues itself to continue


class Permanent(Exception):
    """A failure retrying won't fix (the job is marked failed, not retried)."""


class LeaseLost(Exception):
    """The job's lease expired and another worker may have taken it over: stop without writing anything."""


@dataclass
class Context:
    pool: AsyncConnectionPool
    graph: AppGraph
    heartbeat: Callable[[], Awaitable[None]]
    final_attempt: bool = False  # the queue won't retry this job if it fails
    # Site group members; the worker shares one across jobs so its cache spans a whole crawl.
    directory: Directory | None = None

    def __post_init__(self) -> None:
        if self.directory is None:
            self.directory = Directory(self.graph)


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _item(drive_id: str, site_id: str, raw: dict) -> Item:
    return Item(
        drive_id=drive_id, item_id=raw["id"], site_id=site_id, name=raw.get("name") or raw["id"],
        # eTag, not cTag: delta omits cTag on SharePoint, and the crawl compares delta's value with the stored one.
        # (A rename also changes eTag, so it costs a re-embed.)
        web_url=raw.get("webUrl") or "", etag=raw.get("eTag"),
        modified_at=_parse_time(raw.get("lastModifiedDateTime")),
        parent_id=(raw.get("parentReference") or {}).get("id"),
        is_folder="folder" in raw or "root" in raw or "package" in raw,
    )


# ---- sites and drives -------------------------------------------------------------------------------

async def _resolve_site(ctx: Context, ref: str) -> dict:
    """A site id (`host,guid,guid`) or `hostname:/sites/path`."""
    return await ctx.graph.get(f"/sites/{ref if ':' in ref else seg(ref)}", params={"$select": "id,displayName,webUrl"})


async def add_site(ctx: Context, ref: str, *, enable: bool = True) -> dict:
    """Records a site. `enable=False` (sites from SYNC_SITES) keeps an admin's `sites disable` in force."""
    site = await _resolve_site(ctx, ref)
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO sites (id, name, web_url, enabled) VALUES (%s, %s, %s, true) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, web_url = EXCLUDED.web_url, "
            "enabled = sites.enabled OR %s",
            (site["id"], site.get("displayName") or site["id"], site.get("webUrl") or "", enable),
        )
    return site


async def discover_sites(ctx: Context, payload: dict) -> None:
    for ref in config.SITES:
        try:
            await add_site(ctx, ref, enable=False)
        except GraphError as e:
            # Lost access or deleted: the site's row stays, and the drive listing below purges its index (403/404).
            log.warning("SYNC_SITES entry %s: %s", ref, e)
    if config.DISCOVER_ALL_SITES:
        for site in await ctx.graph.get_all("/sites/getAllSites", params={"$select": "id,displayName,webUrl"}):
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO sites (id, name, web_url) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE "
                    "SET name = EXCLUDED.name, web_url = EXCLUDED.web_url",
                    (site["id"], site.get("displayName") or site["id"], site.get("webUrl") or ""),
                )
    async with ctx.pool.connection() as conn:
        sites = await (await conn.execute("SELECT id FROM sites WHERE enabled")).fetchall()
    for (site_id,) in sites:
        try:
            drives = await ctx.graph.get_all(f"/sites/{seg(site_id)}/drives", params={"$select": "id,name"})
        except GraphError as e:
            if e.status not in (403, 404):
                log.warning("site %s: listing drives failed: %s", site_id, e)
                continue  # keep its drives; a transient failure must not purge a site's index
            # The site is gone or the app lost access: we can no longer keep its permissions current, so its
            # index is purged (fail closed). If access returns, the next discovery re-adds and re-crawls it.
            log.warning("site %s: %s; purging its index", site_id, e)
            drives = []
        async with ctx.pool.connection() as conn, conn.transaction():
            known = {r[0] for r in await (await conn.execute(
                "SELECT id FROM drives WHERE site_id = %s AND enabled", (site_id,)
            )).fetchall()}
            for d in drives:
                await conn.execute(
                    "INSERT INTO drives (id, site_id, name) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET site_id = EXCLUDED.site_id, name = EXCLUDED.name, enabled = true",
                    (d["id"], site_id, d.get("name") or d["id"]),
                )
                if d["id"] not in known:
                    await queue.enqueue(conn, "crawl_drive", {"drive_id": d["id"]}, dedupe_key=f"crawl:{d['id']}")
            gone = known - {d["id"] for d in drives}
            for drive_id in gone:
                await conn.execute("UPDATE drives SET enabled = false WHERE id = %s", (drive_id,))
                await queue.enqueue(conn, "purge_drive", {"drive_id": drive_id}, dedupe_key=f"purge:{drive_id}")
    # Drives of disabled sites, and disabled drives that still have data, are purged.
    async with ctx.pool.connection() as conn, conn.transaction():
        rows = await (await conn.execute(
            "SELECT d.id FROM drives d JOIN sites s ON s.id = d.site_id WHERE NOT (s.enabled AND d.enabled) "
            "AND EXISTS (SELECT 1 FROM sources WHERE drive_id = d.id)"
        )).fetchall()
        for (drive_id,) in rows:
            await queue.enqueue(conn, "purge_drive", {"drive_id": drive_id}, dedupe_key=f"purge:{drive_id}")


async def purge_drive(ctx: Context, payload: dict) -> None:
    drive_id = payload["drive_id"]
    async with ctx.pool.connection() as conn, conn.transaction():
        # Locks the drive: a crawl page checks it (FOR SHARE) in its own transaction, so none can interleave.
        row = await (await conn.execute(
            "SELECT d.enabled AND s.enabled FROM drives d JOIN sites s ON s.id = d.site_id WHERE d.id = %s FOR UPDATE OF d",
            (drive_id,),
        )).fetchone()
        if row and row[0]:
            return  # re-enabled since the purge was queued
        deleted = await index_store.purge_drive(conn, drive_id)
    log.info("purged drive %s (%d files)", drive_id, deleted)


# ---- crawl ------------------------------------------------------------------------------------------

async def crawl_drive(ctx: Context, payload: dict) -> None:
    """Follows Graph delta for one drive. Each page is applied in one transaction together with the link to
    the next page, so a crash re-reads at most one page. A full crawl (first run, after the cursor expired, or
    when the last one ran under an older index_store.INDEX_VERSION) stamps every file it sees; when it reaches the
    last page, files without the stamp are deleted."""
    drive_id = payload["drive_id"]
    async with ctx.pool.connection() as conn:
        drive = await (await conn.execute(
            "SELECT d.site_id FROM drives d JOIN sites s ON s.id = d.site_id WHERE d.id = %s AND d.enabled AND s.enabled",
            (drive_id,),
        )).fetchone()
        if drive is None:
            return
        state = await (await conn.execute(
            "SELECT delta_link, next_link, crawl_id, index_version FROM sync_state WHERE drive_id = %s", (drive_id,)
        )).fetchone()
    site_id = drive[0]
    delta_link, next_link, crawl_id, version = state or (None, None, None, None)
    initial = f"/drives/{seg(drive_id)}/root/delta?$select={ITEM_SELECT}"
    if next_link:
        url = next_link
    elif delta_link and version == index_store.INDEX_VERSION:
        url = delta_link
    else:  # delta only reports changed files: files indexed by an older INDEX_VERSION need a full crawl
        url, crawl_id = initial, await _start_full_crawl(ctx, drive_id)

    while True:
        try:
            page = await ctx.graph.get(url)
        except GraphError as e:
            if e.status != 410:  # 410 Gone: the delta cursor expired
                async with ctx.pool.connection() as conn:
                    await conn.execute("UPDATE sync_state SET last_error = %s WHERE drive_id = %s", (str(e)[:1000], drive_id))
                raise
            log.info("drive %s: delta cursor expired, re-crawling", drive_id)
            url, crawl_id = initial, await _start_full_crawl(ctx, drive_id)
            continue
        await _apply_page(ctx, drive_id, site_id, page, crawl_id)
        await ctx.heartbeat()
        if not (url := page.get("@odata.nextLink")):
            return


async def _start_full_crawl(ctx: Context, drive_id: str) -> str:
    crawl_id = uuid.uuid4().hex
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO sync_state (drive_id, crawl_id) VALUES (%s, %s) ON CONFLICT (drive_id) DO UPDATE "
            "SET crawl_id = EXCLUDED.crawl_id, delta_link = NULL, next_link = NULL",
            (drive_id, crawl_id),
        )
    return crawl_id


async def _apply_page(ctx: Context, drive_id: str, site_id: str, page: dict, crawl_id: str | None) -> None:
    seen, deleted = [], []
    for raw in page.get("value", []):
        (deleted if "deleted" in raw else seen).append(raw)
    items = [_item(drive_id, site_id, r) for r in seen]
    files = [i for i in items if not i.is_folder]
    async with ctx.pool.connection() as conn, conn.transaction():
        if not await _drive_enabled(conn, drive_id):
            raise Permanent(f"drive {drive_id} was disabled during the crawl")  # purge_drive has its data
        # Moves first, so a file moved out of a folder deleted on the same page isn't deleted with it.
        await index_store.upsert_seen(conn, items, crawl_id)
        await index_store.delete_items(conn, drive_id, [r["id"] for r in deleted])
        changed = await index_store.not_current(conn, drive_id, [(f.item_id, f.etag) for f in files])
        await queue.enqueue_many(conn, [
            ("index_item", {"drive_id": drive_id, "item_id": item_id}, f"index:{drive_id}:{item_id}") for item_id in changed
        ])
        if next_link := page.get("@odata.nextLink"):
            await conn.execute("UPDATE sync_state SET next_link = %s WHERE drive_id = %s", (next_link, drive_id))
            return
        removed = await index_store.finish_full_crawl(conn, drive_id, crawl_id) if crawl_id else 0
        await conn.execute(
            "UPDATE sync_state SET delta_link = %s, next_link = NULL, crawl_id = NULL, synced_at = now(), last_error = NULL "
            "WHERE drive_id = %s",
            (page.get("@odata.deltaLink"), drive_id),
        )
    if removed:
        log.info("drive %s: full crawl removed %d files no longer in SharePoint", drive_id, removed)


async def _drive_enabled(conn, drive_id: str) -> bool:
    """Under FOR SHARE, so a concurrent purge_drive (FOR UPDATE) waits for this transaction, or we for it."""
    row = await (await conn.execute(
        "SELECT d.enabled AND s.enabled FROM drives d JOIN sites s ON s.id = d.site_id WHERE d.id = %s FOR SHARE OF d, s",
        (drive_id,),
    )).fetchone()
    return bool(row and row[0])


# ---- one file ---------------------------------------------------------------------------------------

async def _to_acl(ctx: Context, site_url: str, perms: list[dict], read_at: datetime) -> AclRead:
    return permissions.to_acl(perms, read_at, await ctx.directory.lookups(site_url, perms))


async def _read_acl(ctx: Context, site_url: str, drive_id: str, item_id: str) -> AclRead:
    perms, read_at = await ctx.graph.get_all_timed(f"/drives/{seg(drive_id)}/items/{seg(item_id)}/permissions")
    return await _to_acl(ctx, site_url, perms, read_at)


async def _site_url(ctx: Context, drive_id: str) -> str:
    async with ctx.pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT s.web_url FROM drives d JOIN sites s ON s.id = d.site_id WHERE d.id = %s", (drive_id,),
        )).fetchone()
    return row[0] if row else ""


async def index_item(ctx: Context, payload: dict) -> None:
    drive_id, item_id = payload["drive_id"], payload["item_id"]
    async with ctx.pool.connection() as conn:
        drive = await (await conn.execute(
            "SELECT d.site_id, s.web_url FROM drives d JOIN sites s ON s.id = d.site_id WHERE d.id = %s AND d.enabled AND s.enabled",
            (drive_id,),
        )).fetchone()
    if drive is None:
        return
    try:
        raw = await ctx.graph.get(f"/drives/{seg(drive_id)}/items/{seg(item_id)}", params={"$select": ITEM_SELECT})
    except GraphError as e:
        if e.status == 404:
            return await delete_item(ctx, payload)
        if e.status in (401, 403):  # the app can't see the file any more: hide it rather than keep its old list
            await _hide(ctx, drive_id, [item_id])
        raise
    item = _item(drive_id, drive[0], raw)
    if "deleted" in raw:
        return await delete_item(ctx, payload)
    if item.is_folder:
        return  # folders are recorded by the crawl; nothing to index
    async with ctx.pool.connection() as conn:
        if await index_store.is_current(conn, drive_id, item_id, item.etag):
            return
        gens = await generations.live(conn)
        source = await index_store.source_id(conn, drive_id, item_id)

    ext = PurePosixPath(item.name.lower()).suffix
    size = raw.get("size") or 0
    skip = None if ext in EXTENSIONS else f"unsupported file type {ext or '(none)'}"
    skip = skip or (f"file is larger than {config.MAX_FILE_BYTES} bytes" if size > config.MAX_FILE_BYTES else None)
    if skip:
        # No chunks, so permissions don't matter; any chunks from an earlier version are removed.
        async with ctx.pool.connection() as conn:
            await index_store.write_index(conn, item, None, [], {g.id: [] for g in gens}, status="skipped", note=skip)
        return

    acl_read = None
    try:
        acl_read = await _read_acl(ctx, drive[1], drive_id, item_id)  # before the download: see "Newest permissions win"
        data = await ctx.graph.download(drive_id, item_id, config.MAX_FILE_BYTES)
        chunks = await asyncio.to_thread(index_store.chunk_rows, item.name, data)
        del data  # no file copies are kept
        vectors = {}
        texts = [Chunk(c.heading_path or "", c.text).embedding_text for c in chunks]
        for g in gens:
            try:
                vectors[g.id] = (await _embed(ctx, g, [(source, texts)], reuse=True))[0]
            except Exception as e:
                if g.status == "active" or isinstance(e, (RateLimited, LeaseLost)):
                    raise
                # A broken backend for a generation still being built must not stop updates to the live index:
                # write_index leaves this one out and queues its backfill. A rate limit is different: it is the
                # same quota for every generation, so the job waits for it rather than leave work to the backfill.
                log.warning("generation %d: embedding failed (%s); its backfill will catch up", g.id, e)
    except (ExtractionError, FileTooLarge) as e:  # corrupt or oversized: retrying gives the same result
        await _record_failure(ctx, item, acl_read, str(e))
        raise Permanent(str(e)) from e
    except LeaseLost:
        raise  # the worker that took the job over owns the file's state now
    except RateLimited:
        # Deferred, not failed: the file keeps its status and old chunks until the job runs again. The permission
        # read is applied now all the same: a narrowed share must not wait for the quota.
        if acl_read is not None:
            async with ctx.pool.connection() as conn:
                await index_store.write_acl(conn, drive_id, item_id, acl_read)
        raise
    except Exception as e:
        if acl_read is None and isinstance(e, GraphError) and e.status in (401, 403):
            # The app can't read this file's permissions: hide it rather than keep its last known list.
            acl_read = AclRead((), False, datetime.now(timezone.utc))
        await _record_failure(ctx, item, acl_read, f"{type(e).__name__}: {e}")
        raise
    note = truncation_note(c.text for c in chunks)  # a file cut off at a limit says so, rather than look whole
    async with ctx.pool.connection() as conn:
        result = await index_store.write_index(conn, item, acl_read, chunks, vectors, note=note)
        if source is not None:
            await index_store.clear_progress(conn, list(vectors), [source])
    if result == "gone":
        log.info("%s/%s was deleted while it was being indexed; not written", drive_id, item_id)
    elif result == "stale":
        log.info("%s/%s: a newer version was indexed meanwhile; applied permissions only", drive_id, item_id)


async def _embed(ctx: Context, g, files: list[tuple[int | None, list[str]]], *, reuse: bool) -> list[list[list[float]]]:
    """Vectors in generation `g` for each (source id, embedding texts) file, embedding only what isn't known yet.
    `reuse`: a file's unchanged chunks keep their vectors (index_item; a backfill's files have none in `g` yet).
    Each Gemini batch is kept as it arrives (embedding_progress), so a retry after a rate limit resumes instead of
    spending the quota again. Small files share batches. A source id of None (the file's row is gone): nothing kept."""
    known: dict[tuple[int | None, str], list[float]] = {}
    sources = [source for source, _ in files if source is not None]
    async with ctx.pool.connection() as conn:
        progress = await index_store.progress_vectors(conn, g, sources)
        for source, texts in files:
            if source is None:
                continue
            if reuse:
                known |= {(source, t): v for t, v in (await index_store.reusable_vectors(conn, g, source, texts)).items()}
            for t in texts:
                if (source, t) not in known and (v := progress.get((source, index_store.text_digest(t)))) is not None:
                    known[(source, t)] = v
    missing = list(dict.fromkeys((source, t) for source, texts in files for t in texts if (source, t) not in known))
    # One Gemini batch per call: a rate limit then loses nothing that was already paid for.
    for i in range(0, len(missing), embeddings.BATCH_SIZE):
        batch = missing[i : i + embeddings.BATCH_SIZE]
        vectors = await inference.embed(g.model, [t for _, t in batch], "document", g.dimensions)
        async with ctx.pool.connection() as conn:
            await index_store.save_progress(conn, g, [(s, t, v) for (s, t), v in zip(batch, vectors) if s is not None])
        known |= dict(zip(batch, vectors))
        await ctx.heartbeat()
    return [[known[(source, t)] for t in texts] for source, texts in files]


async def _record_failure(ctx: Context, item: Item, acl_read, note: str) -> None:
    """Marks the file `error` (its old chunks keep serving) but still applies a fresh permission read."""
    async with ctx.pool.connection() as conn:
        await index_store.mark_error(conn, item.drive_id, item.item_id, note)
        if acl_read is not None:
            await index_store.write_acl(conn, item.drive_id, item.item_id, acl_read)


async def delete_item(ctx: Context, payload: dict) -> None:
    async with ctx.pool.connection() as conn:
        await index_store.delete_items(conn, payload["drive_id"], [payload["item_id"]])


# ---- permissions ------------------------------------------------------------------------------------

ACL_RETRIES = 5  # then the file is hidden: permissions we can't refresh must not keep serving


async def sync_item_acl(ctx: Context, payload: dict) -> None:
    """Re-reads the permissions of up to 20 files of one drive in one $batch call.

    A file whose permissions can't be read (401/403, or repeated failures) is marked unresolved, which hides
    it: otherwise its last known, possibly too open, list would keep matching searches indefinitely."""
    drive_id, item_ids, attempt = payload["drive_id"], payload["item_ids"], payload.get("attempt", 0)
    paths = [f"/drives/{seg(drive_id)}/items/{seg(i)}/permissions" for i in item_ids]
    retry, hidden = [], []
    site_url = await _site_url(ctx, drive_id)
    try:
        results = await ctx.graph.batch_get(paths)
    except Exception as e:
        if ctx.final_attempt or (isinstance(e, GraphError) and e.status in (401, 403)):
            await _hide(ctx, drive_id, item_ids)
        raise
    for item_id, res in zip(item_ids, results):
        if res.status == 404:
            await delete_item(ctx, {"drive_id": drive_id, "item_id": item_id})
            continue
        if res.status in (401, 403) or (res.status != 200 and attempt + 1 >= ACL_RETRIES):
            hidden.append((item_id, res.status))
            async with ctx.pool.connection() as conn:
                await index_store.write_acl(conn, drive_id, item_id, AclRead((), False, res.sent_at))
            continue
        if res.status != 200:
            retry.append(item_id)
            continue
        perms = res.body.get("value", [])
        if next_link := res.body.get("@odata.nextLink"):
            perms += await ctx.graph.get_all(next_link)
        acl_read = await _to_acl(ctx, site_url, perms, res.sent_at)  # SharePoint/Graph calls: no connection held
        async with ctx.pool.connection() as conn:
            await index_store.write_acl(conn, drive_id, item_id, acl_read)
    if hidden:
        log.warning("drive %s: permissions unreadable for %d file(s), now hidden: %s", drive_id, len(hidden), hidden[:5])
    if retry:  # only the failures; the rest are written already
        async with ctx.pool.connection() as conn:
            await queue.enqueue(conn, "sync_item_acl", {"drive_id": drive_id, "item_ids": retry, "attempt": attempt + 1},
                                dedupe_key=_acl_key(drive_id, retry), delay_seconds=300 * 2**attempt)


async def _hide(ctx: Context, drive_id: str, item_ids: list[str]) -> None:
    """Marks files unresolved (visible to nobody) when their permissions can't be read any more."""
    now = datetime.now(timezone.utc)
    async with ctx.pool.connection() as conn:
        for item_id in item_ids:
            await index_store.write_acl(conn, drive_id, item_id, AclRead((), False, now))
    log.warning("drive %s: permissions unreadable, hid %d file(s)", drive_id, len(item_ids))


def _acl_key(drive_id: str, item_ids: list[str]) -> str:
    return "acl:" + hashlib.sha256("\n".join([drive_id, *item_ids]).encode()).hexdigest()[:32]


async def sweep_acls(ctx: Context, payload: dict) -> None:
    """Queues sync_item_acl for every file with chunks: delta doesn't reliably report permission-only changes.
    Also retries files left in `error` once their index_item ran out of attempts (an outage longer than its
    backoff, say), since delta only reports them again when they change. Corrupt files are retried too: that's
    one download a day each, and a file a later version of the extractor can read gets picked up."""
    after = payload.get("after", 0)
    async with ctx.pool.connection() as conn, conn.transaction():
        rows = await (await conn.execute(
            "SELECT s.id, s.drive_id, s.item_id, s.status FROM sources s JOIN drives d ON d.id = s.drive_id AND d.enabled "
            "JOIN sites si ON si.id = d.site_id AND si.enabled "
            "WHERE s.status IN ('indexed', 'error') AND s.id > %s ORDER BY s.id LIMIT %s",
            (after, SWEEP_PAGE),
        )).fetchall()
        by_drive: dict[str, list[str]] = {}
        jobs = []
        for _, drive_id, item_id, status in rows:
            by_drive.setdefault(drive_id, []).append(item_id)
            if status == "error":
                jobs.append(("index_item", {"drive_id": drive_id, "item_id": item_id}, f"index:{drive_id}:{item_id}"))
        for drive_id, ids in by_drive.items():
            for i in range(0, len(ids), ACL_BATCH):
                batch = ids[i : i + ACL_BATCH]
                jobs.append(("sync_item_acl", {"drive_id": drive_id, "item_ids": batch}, _acl_key(drive_id, batch)))
        await queue.enqueue_many(conn, jobs)
        if len(rows) == SWEEP_PAGE:  # continue in a fresh job, so one run never holds a huge transaction
            await queue.enqueue(conn, "sweep_acls", {"after": rows[-1][0]}, dedupe_key=f"sweep:{rows[-1][0]}")


# ---- generations ------------------------------------------------------------------------------------

async def backfill_generation(ctx: Context, payload: dict) -> None:
    """Fills a building generation from chunk text already stored (no re-download), with each file's
    permissions taken from `sources` at write time."""
    gen_id = payload["generation_id"]
    for _ in range(BACKFILL_MAX_BATCHES):
        async with ctx.pool.connection() as conn:
            gen = next((g for g in await generations.live(conn) if g.id == gen_id), None)
            if gen is None or gen.status != "building":
                return
            batch = await index_store.backfill_candidates(conn, gen, BACKFILL_BATCH)
        if not batch:
            log.info("generation %d is backfilled; check and activate it with python -m sharepoint_rag.generations", gen_id)
            return
        files = [(src.source_id, [Chunk(c.heading_path or "", c.text).embedding_text for c in src.chunks]) for src in batch]
        vectors = await _embed(ctx, gen, files, reuse=False)  # resumable: a rate limit midway keeps what was embedded
        async with ctx.pool.connection() as conn:
            for src, src_vectors in zip(batch, vectors):
                await index_store.write_backfill(conn, gen_id, src, src_vectors)
            await index_store.clear_progress(conn, [gen_id], [src.source_id for src in batch])
        await ctx.heartbeat()
    async with ctx.pool.connection() as conn:
        await queue.enqueue(conn, "backfill_generation", {"generation_id": gen_id}, dedupe_key=f"backfill:{gen_id}")


HANDLERS: dict[str, Callable[[Context, dict], Awaitable[None]]] = {
    "discover_sites": discover_sites,
    "crawl_drive": crawl_drive,
    "index_item": index_item,
    "delete_item": delete_item,
    "purge_drive": purge_drive,
    "sync_item_acl": sync_item_acl,
    "sweep_acls": sweep_acls,
    "backfill_generation": backfill_generation,
}
