"""Writes to the index. Each function is one transaction, so a file's chunks and permissions change together.

Rules (see docs/architecture.md):
- Newest permissions win: a permission read is written only if it was taken after the stored one
  (`sources.acl_synced_at`), whichever job it comes from.
- Every chunk write and permission write goes to all live generations.
- Lock order is index_generations, then sources rows; `generations.activate` takes them in the same order.
"""
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

import psycopg
from psycopg import sql

from . import acl, generations, language, queue
from .acl import AclRead
from .chunk import chunk_sections
from .extract import extract
from .generations import Generation


@dataclass(frozen=True)
class Item:
    """A file or folder as Graph reports it."""

    drive_id: str
    item_id: str
    site_id: str
    name: str
    web_url: str
    etag: str | None = None
    modified_at: datetime | None = None
    parent_id: str | None = None
    is_folder: bool = False


@dataclass(frozen=True)
class ChunkRow:
    ordinal: int
    text: str
    heading_path: str | None
    language: str  # Postgres text search config: english | spanish | simple


# The version of what chunk_rows makes of a file. Bump it whenever that changes (extraction, chunking, headings,
# language detection): files indexed by an older version are then no longer current, and each drive's next sync
# is a full crawl, so unchanged files are re-indexed too. 2: the document title starts every heading path.
INDEX_VERSION = 2


def chunk_rows(name: str, data: bytes, title: str | None = None) -> list[ChunkRow]:
    """What the index stores for a file: its non-blank chunks, numbered in order, each with its text-search
    language. Raises ExtractionError. Shared by the sync-worker and the evals, so they index files the same way.

    Every chunk's heading path starts with the document's title (by default its file name without the extension):
    a PDF page's own heading is just "Page 52", so without it neither the embedding nor keyword search knows which
    company's filing, or which year's policy, the page belongs to."""
    title = PurePosixPath(name).stem if title is None else title
    chunks = [c for c in chunk_sections(extract(name, data)) if c.text.strip()]
    return [ChunkRow(n, c.text, " > ".join(p for p in (title, c.heading_path) if p) or None, language.detect(c.text))
            for n, c in enumerate(chunks)]


def acl_hash(principals: list[str]) -> str:
    return hashlib.sha256("\n".join(principals).encode()).hexdigest()


async def upsert_seen(conn: psycopg.AsyncConnection, items: list[Item], crawl_id: str | None) -> None:
    """Records files/folders a crawl saw: new ones as `pending`, existing ones get their current name, link and
    parent (renames, moves) and the crawl's stamp. Indexed content and permissions are left alone."""
    if not items:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO sources (drive_id, item_id, site_id, web_url, name, parent_id, status, last_crawl_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (drive_id, item_id) DO UPDATE SET site_id = EXCLUDED.site_id, web_url = EXCLUDED.web_url, "
            "name = EXCLUDED.name, parent_id = EXCLUDED.parent_id, "
            "status = CASE WHEN EXCLUDED.status = 'folder' THEN 'folder' "
            "              WHEN sources.status = 'folder' THEN 'pending' ELSE sources.status END, "
            "last_crawl_id = coalesce(EXCLUDED.last_crawl_id, sources.last_crawl_id)",
            [
                (i.drive_id, i.item_id, i.site_id, i.web_url, i.name, i.parent_id, "folder" if i.is_folder else "pending", crawl_id)
                for i in items
            ],
        )


async def is_current(conn: psycopg.AsyncConnection, drive_id: str, item_id: str, etag: str | None) -> bool:
    """True if this version of the file is already indexed, by this INDEX_VERSION, in every live generation."""
    if etag is None:
        return False
    row = await (await conn.execute(
        "SELECT s.etag = %s AND s.status IN ('indexed', 'skipped') AND s.index_version IS NOT DISTINCT FROM %s AND NOT EXISTS ("
        "  SELECT 1 FROM index_generations g WHERE g.status = ANY(%s) AND NOT EXISTS ("
        "    SELECT 1 FROM source_generations sg WHERE sg.source_id = s.id AND sg.generation_id = g.id AND sg.etag = %s)) "
        "FROM sources s WHERE s.drive_id = %s AND s.item_id = %s",
        (etag, INDEX_VERSION, list(generations.LIVE), etag, drive_id, item_id),
    )).fetchone()
    return bool(row and row[0])


async def not_current(conn: psycopg.AsyncConnection, drive_id: str, versions: list[tuple[str, str | None]]) -> list[str]:
    """Of (item_id, etag) pairs, the item ids that `is_current` would say need indexing, in one query."""
    if not versions:
        return []
    rows = await (await conn.execute(
        "SELECT v.item_id FROM unnest(%s::text[], %s::text[]) AS v(item_id, etag) "
        "LEFT JOIN sources s ON s.drive_id = %s AND s.item_id = v.item_id "
        "WHERE v.etag IS NULL OR s.id IS NULL OR s.etag IS DISTINCT FROM v.etag OR s.status NOT IN ('indexed', 'skipped') "
        "OR s.index_version IS DISTINCT FROM %s "
        "OR EXISTS (SELECT 1 FROM index_generations g WHERE g.status = ANY(%s) AND NOT EXISTS ("
        "  SELECT 1 FROM source_generations sg WHERE sg.source_id = s.id AND sg.generation_id = g.id AND sg.etag = v.etag))",
        ([i for i, _ in versions], [e for _, e in versions], drive_id, INDEX_VERSION, list(generations.LIVE)),
    )).fetchall()
    return [r[0] for r in rows]


async def _lock_source(conn: psycopg.AsyncConnection, drive_id: str, item_id: str):
    return await (await conn.execute(
        "SELECT id, modified_at, acl, acl_status, acl_synced_at FROM sources WHERE drive_id = %s AND item_id = %s FOR UPDATE",
        (drive_id, item_id),
    )).fetchone()


def _is_newer(read: AclRead | None, synced_at: datetime | None) -> bool:
    return read is not None and (synced_at is None or read.read_at > synced_at)


async def _set_acl(conn, source_id: int, read: AclRead) -> list[str]:
    principals = sorted(set(read.principals))
    effective = acl.effective(principals, read.status)
    await conn.execute(
        "UPDATE sources SET acl = %s, acl_status = %s, acl_synced_at = %s, acl_hash = %s WHERE id = %s",
        (principals, read.status, read.read_at, acl_hash(effective), source_id),
    )
    return effective


async def _set_chunk_acl(conn, gens: list[Generation], source_id: int, effective: list[str]) -> None:
    for g in gens:
        await conn.execute(
            sql.SQL("UPDATE {t} SET acl = %s WHERE source_id = %s AND acl IS DISTINCT FROM %s").format(t=g.table),
            (effective, source_id, effective),
        )


class MissingVectors(RuntimeError):
    """write_index got no vectors for the active generation: retry, re-reading the live generations."""


async def write_index(
    conn: psycopg.AsyncConnection,
    item: Item,
    acl_read: AclRead | None,
    chunks: list[ChunkRow],
    vectors: dict[int, list[list[float]]],
    *,
    status: str = "indexed",
    note: str | None = None,
) -> str:
    """Replaces the file's chunks in every live generation and applies `acl_read` if it is newer.

    `vectors` maps generation id -> one vector per chunk. A building generation without vectors (created after
    the caller embedded) loses its stale copy of the file and is refilled by its backfill; the active one
    without vectors raises MissingVectors and nothing is written.

    Name, link and parent are the crawl's (`upsert_seen`), not set here: the caller's copy may be older than a
    move the crawl has applied since, and writing it back could put the file under a deleted folder. Returns 'written',
    'stale' if a newer version of the file was already written (only the permissions are then applied), or
    'gone' if the file's row no longer exists: the crawl records every file before indexing it, so a missing
    row means it was deleted (or its drive purged) while the caller worked, and must not be written back.
    """
    async with conn.transaction():
        gens = await generations.live(conn, lock=True)
        row = await _lock_source(conn, item.drive_id, item.item_id)
        if row is None:
            return "gone"
        source_id, stored_modified, stored_acl, stored_status, synced_at = row
        if _is_newer(acl_read, synced_at):
            effective = await _set_acl(conn, source_id, acl_read)
        else:
            effective = acl.effective(stored_acl, stored_status)
        if stored_modified and item.modified_at and item.modified_at < stored_modified:
            await _set_chunk_acl(conn, gens, source_id, effective)
            return "stale"
        for g in gens:
            await conn.execute(sql.SQL("DELETE FROM {t} WHERE source_id = %s").format(t=g.table), (source_id,))
            if g.id not in vectors:
                if g.status == "active":
                    # Activated after the caller embedded (its embedding had failed while it was still building).
                    # Nothing would refill it: fail, so the job retries and embeds for the generation that serves.
                    raise MissingVectors(f"generation {g.id} is active but was not embedded for {item.item_id}")
                await conn.execute(
                    "DELETE FROM source_generations WHERE source_id = %s AND generation_id = %s", (source_id, g.id)
                )
                if g.status == "building":  # make sure a backfill is coming to refill it, even if one just finished
                    await queue.enqueue(conn, "backfill_generation", {"generation_id": g.id}, dedupe_key=f"backfill:{g.id}")
                continue
            if len(vectors[g.id]) != len(chunks):
                raise ValueError(f"generation {g.id}: {len(vectors[g.id])} vectors for {len(chunks)} chunks")
            await _insert_chunks(conn, g, source_id, chunks, vectors[g.id], effective)
            await conn.execute(
                "INSERT INTO source_generations (source_id, generation_id, etag) VALUES (%s, %s, %s) "
                "ON CONFLICT (source_id, generation_id) DO UPDATE SET etag = EXCLUDED.etag",
                (source_id, g.id, item.etag),
            )
        languages = [c.language for c in chunks if c.language != "simple"]
        await conn.execute(
            "UPDATE sources SET etag = %s, modified_at = %s, indexed_at = now(), status = %s, status_note = %s, "
            "language = %s, attempts = 0, index_version = %s WHERE id = %s",
            (item.etag, item.modified_at, status, note, max(set(languages), key=languages.count) if languages else None,
             INDEX_VERSION, source_id),
        )
    return "written"


async def _insert_chunks(conn, g: Generation, source_id: int, chunks: list[ChunkRow], vectors, effective: list[str]) -> None:
    if not chunks:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            sql.SQL(
                "INSERT INTO {t} (source_id, ordinal, text, heading_path, language, embedding, acl) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            ).format(t=g.table),
            [(source_id, c.ordinal, c.text, c.heading_path, c.language, v, effective) for c, v in zip(chunks, vectors)],
        )


async def write_acl(conn: psycopg.AsyncConnection, drive_id: str, item_id: str, acl_read: AclRead) -> bool:
    """Applies a permission read to the file and its chunks in every live generation, if it is the newest."""
    async with conn.transaction():
        gens = await generations.live(conn, lock=True)
        row = await _lock_source(conn, drive_id, item_id)
        if row is None or not _is_newer(acl_read, row[4]):
            return False
        effective = await _set_acl(conn, row[0], acl_read)
        await _set_chunk_acl(conn, gens, row[0], effective)
    return True


async def mark_error(conn: psycopg.AsyncConnection, drive_id: str, item_id: str, note: str) -> None:
    """The file's previous chunks (if any) keep serving until a retry succeeds; see 'Stale text'."""
    await conn.execute(
        "UPDATE sources SET status = 'error', status_note = %s, attempts = attempts + 1 WHERE drive_id = %s AND item_id = %s",
        (note[:1000], drive_id, item_id),
    )


async def delete_items(conn: psycopg.AsyncConnection, drive_id: str, item_ids: list[str]) -> int:
    """Deletes the files, and for folders everything under them; chunks go with them (ON DELETE CASCADE)."""
    if not item_ids:
        return 0
    cur = await conn.execute(
        "WITH RECURSIVE sub AS ("
        "  SELECT id, item_id FROM sources WHERE drive_id = %(d)s AND item_id = ANY(%(ids)s)"
        "  UNION SELECT s.id, s.item_id FROM sources s JOIN sub ON s.drive_id = %(d)s AND s.parent_id = sub.item_id"
        ") DELETE FROM sources WHERE id IN (SELECT id FROM sub)",
        {"d": drive_id, "ids": item_ids},
    )
    return cur.rowcount


async def finish_full_crawl(conn: psycopg.AsyncConnection, drive_id: str, crawl_id: str) -> int:
    """After a full crawl reached its last page: files it never saw are gone from SharePoint, and every file it saw
    has been checked against INDEX_VERSION."""
    cur = await conn.execute(
        "DELETE FROM sources WHERE drive_id = %s AND last_crawl_id IS DISTINCT FROM %s", (drive_id, crawl_id)
    )
    await conn.execute("UPDATE sync_state SET index_version = %s WHERE drive_id = %s", (INDEX_VERSION, drive_id))
    return cur.rowcount


async def purge_drive(conn: psycopg.AsyncConnection, drive_id: str) -> int:
    async with conn.transaction():
        cur = await conn.execute("DELETE FROM sources WHERE drive_id = %s", (drive_id,))
        await conn.execute("DELETE FROM sync_state WHERE drive_id = %s", (drive_id,))
    return cur.rowcount


@dataclass(frozen=True)
class BackfillSource:
    source_id: int
    etag: str | None
    chunks: list[ChunkRow]


async def backfill_candidates(conn: psycopg.AsyncConnection, gen: Generation, limit: int) -> list[BackfillSource]:
    """Files indexed in the active generation but missing (or outdated) in `gen`, with their stored chunk text."""
    active = await generations.active(conn)
    rows = await (await conn.execute(
        "SELECT a.source_id, a.etag FROM source_generations a "
        "LEFT JOIN source_generations n ON n.source_id = a.source_id AND n.generation_id = %s "
        "WHERE a.generation_id = %s AND (n.source_id IS NULL OR n.etag IS DISTINCT FROM a.etag) "
        "ORDER BY a.source_id LIMIT %s",
        (gen.id, active.id, limit),
    )).fetchall()
    out = []
    for source_id, etag in rows:
        chunk_rows = await (await conn.execute(
            sql.SQL("SELECT ordinal, text, heading_path, language::text FROM {t} WHERE source_id = %s ORDER BY ordinal").format(t=active.table),
            (source_id,),
        )).fetchall()
        out.append(BackfillSource(source_id, etag, [ChunkRow(*r) for r in chunk_rows]))
    return out


async def write_backfill(conn: psycopg.AsyncConnection, gen_id: int, src: BackfillSource, vectors: list[list[float]]) -> bool:
    """Copies one file into a building generation, with the permissions in `sources` at write time.
    Skipped (False) if the file changed since its text was read: the newer write already covered `gen_id`."""
    async with conn.transaction():
        gens = {g.id: g for g in await generations.live(conn, lock=True)}
        if gen_id not in gens:
            return False
        active = next(g for g in gens.values() if g.status == "active")
        row = await (await conn.execute(
            "SELECT s.acl, s.acl_status, sg.etag FROM sources s "
            "JOIN source_generations sg ON sg.source_id = s.id AND sg.generation_id = %s WHERE s.id = %s FOR UPDATE OF s",
            (active.id, src.source_id),
        )).fetchone()
        if row is None or row[2] != src.etag:
            return False
        effective = acl.effective(row[0], row[1])
        g = gens[gen_id]
        await conn.execute(sql.SQL("DELETE FROM {t} WHERE source_id = %s").format(t=g.table), (src.source_id,))
        await _insert_chunks(conn, g, src.source_id, src.chunks, vectors, effective)
        await conn.execute(
            "INSERT INTO source_generations (source_id, generation_id, etag) VALUES (%s, %s, %s) "
            "ON CONFLICT (source_id, generation_id) DO UPDATE SET etag = EXCLUDED.etag",
            (src.source_id, gen_id, src.etag),
        )
    return True
