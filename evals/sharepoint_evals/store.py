"""One eval database per suite, filled with the indexer's real write path (`index_store.write_index`).

    sharepoint_rag_eval_<suite>      on the DATABASE_URL server, or EVAL_DATABASE_URL's server and name prefix

Suites rebuild independently and can't change each other's scores. Indexing is incremental: each document's
version stamp covers its bytes, its permissions, the model and the extraction/chunking code, so only what changed
is re-extracted, and the embedding cache means only changed chunk text is re-embedded. A document that fails to
extract is recorded as `skipped` (as the indexer would), and listed by `skipped()`.

Plain-text formats are extracted in a process pool. Every other file (PDF, Office) runs in its own process with a
time and memory limit (extraction.run_isolated), so a parser that hangs or crashes skips that file instead of
stalling or aborting the whole suite.
"""
import asyncio
import hashlib
import inspect
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import sharepoint_rag.chunk as chunk_module
import sharepoint_rag.extract as extract_module
import sharepoint_rag.index_store as index_store_module
import sharepoint_rag.language as language_module
from sharepoint_rag import config, db, generations, index_store, inference
from sharepoint_rag.acl import AclRead
from sharepoint_rag.chunk import Chunk
from sharepoint_rag.extract import TEXT_EXTENSIONS, ExtractionError
from sharepoint_rag.index_store import ChunkRow, Item

from sharepoint_evals import embed_cache, extract_worker
from sharepoint_evals.corpus import Corpus, Doc

WORKERS = min(8, os.cpu_count() or 2)
LOCK = 7355608  # advisory lock: concurrent eval runs on one suite database take turns
BATCH_DOCS = 200
IN_PROCESS = TEXT_EXTENSIONS | {".md", ".html", ".htm"}  # plain-text parsers; everything else is isolated
EXTRACT_SECONDS = float(os.environ.get("EVAL_INDEX_EXTRACT_SECONDS", "300"))  # whole 500-page filings take a while


def database_url(suite: str) -> str:
    base = os.environ.get("EVAL_DATABASE_URL") or make_conninfo(config.require_database_url(), dbname="sharepoint_rag_eval")
    return make_conninfo(base, dbname=f"{conninfo_to_dict(base)['dbname']}_{suite}")


def web_url(suite: str, name: str) -> str:
    return f"https://eval.invalid/{suite}/{name}"


def _code_hash() -> str:
    h = hashlib.sha256(f"{config.EMBEDDING_MODEL}:{config.EMBEDDING_DIMENSIONS}".encode())
    # Chunking/extraction code is part of the index: changing it must re-extract every document, otherwise the
    # evals would keep scoring a stale index and could not show the effect of the change.
    for module in (chunk_module, extract_module, index_store_module, language_module, extract_worker):
        h.update(Path(module.__file__).read_bytes())
    h.update(inspect.getsource(index_title).encode() + repr(GENERATED_NAMES).encode())  # what titles the chunks get
    return h.hexdigest()


def version(doc: Doc, code_hash: str) -> str:
    h = hashlib.sha256(code_hash.encode())
    for part in (doc.name.encode(), hashlib.sha256(doc.read()).digest(), "\n".join(doc.acl).encode(), str(doc.resolved).encode()):
        h.update(part + b"\0")
    return h.hexdigest()


def _create_database_if_missing(url: str) -> None:
    name = conninfo_to_dict(url)["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


# Suites whose builder names files by a hash ("enron-0a43d18ae365.md"), with the pattern of those names. A real file
# has a real name; these have none, so they get no title. Every other file, here and in production, is titled by
# its name.
GENERATED_NAMES = {
    "acl_scale": r"enron-[0-9a-f]{12}",
    "retrieval_multilingual": r"(mlqa-[a-z]{2}|miracl)-.+",
    "agent_safety": r"squad-[0-9a-f]{12}",
}


def index_title(doc: Doc, suite: str) -> str:
    """The title the indexer gives a document's chunks: its file name without the extension, as in production."""
    stem = PurePosixPath(doc.name).stem
    pattern = GENERATED_NAMES.get(suite)
    return "" if pattern and re.fullmatch(pattern, stem) else stem


def chunk_file(path: str, name: str, title: str | None = None) -> list[ChunkRow] | str:
    """What the indexer stores for a file (index_store.chunk_rows), or the extraction error. Module-level so a
    process pool can run it."""
    try:
        return index_store.chunk_rows(name, Path(path).read_bytes(), title)
    except ExtractionError as e:
        return f"{type(e).__name__}: {e}"


async def _chunks(doc: Doc, suite: str, pool: ProcessPoolExecutor, isolated: asyncio.Semaphore) -> list[ChunkRow] | str:
    if PurePosixPath(doc.name.lower()).suffix in IN_PROCESS:
        return await asyncio.get_running_loop().run_in_executor(pool, chunk_file, str(doc.path), doc.name,
                                                                index_title(doc, suite))
    from sharepoint_evals.extraction import run_isolated

    async with isolated:
        result = await run_isolated(doc.path, doc.name, rows=True, time_limit=EXTRACT_SECONDS, title=index_title(doc, suite))
    if result["outcome"] == "chunks":
        return [ChunkRow(*row) for row in result["rows"]]
    return [] if result["outcome"] == "no_text" else f"{result['outcome']}: {result.get('message', '')}"


RETRY = "retry:"  # stamp prefix of a file killed or crashed in extraction: never equal to a version, so it's redone


async def _write(conn, corpus: Corpus, gen, docs: list[Doc], stamps: dict[str, str], pool: ProcessPoolExecutor) -> None:
    isolated = asyncio.Semaphore(WORKERS)
    results = await asyncio.gather(*(_chunks(d, corpus.name, pool, isolated) for d in docs))
    texts = [Chunk(r.heading_path or "", r.text).embedding_text for rows in results if isinstance(rows, list) for r in rows]
    vectors = iter(await inference.embed(gen.model, texts, "document", gen.dimensions))
    read_at = datetime.now(timezone.utc)
    for doc, rows in zip(docs, results):
        # Killed (over a limit) or crashed depends on the machine and the limits, not only on the file: try again next run.
        stamp = RETRY + stamps[doc.name] if isinstance(rows, str) and rows.startswith(("killed", "crashed")) else stamps[doc.name]
        item = Item(corpus.name, doc.name, corpus.name, doc.title, web_url(corpus.name, doc.name), stamp, read_at)
        await index_store.upsert_seen(conn, [item], None)
        if isinstance(rows, str) or not rows:
            note = rows if isinstance(rows, str) else "no text"
            await index_store.write_index(conn, item, AclRead(doc.acl, doc.resolved, read_at), [], {gen.id: []},
                                          status="skipped", note=note)
            continue
        await index_store.write_index(conn, item, AclRead(doc.acl, doc.resolved, read_at), rows,
                                      {gen.id: [next(vectors) for _ in rows]})


async def ensure_index(corpus: Corpus) -> str:
    """Brings the suite's database up to date with the corpus and returns its URL. Idempotent."""
    embed_cache.install()
    url = database_url(corpus.name)
    _create_database_if_missing(url)
    db.migrate(url)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await register_vector_async(conn)
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
        try:
            gen = await generations.active(conn)
            if (gen.model, gen.dimensions) != (generations._model_id(config.EMBEDDING_MODEL), config.EMBEDDING_DIMENSIONS):
                # Built for another model: start over (the database holds nothing but this suite).
                await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
                db.migrate(url)
                await register_vector_async(conn)
                gen = await generations.active(conn)
            code = _code_hash()
            stamps = {d.name: version(d, code) for d in corpus.docs}
            # A row left `pending` by an interrupted run has no etag here, so it is redone.
            stored = dict(await (await conn.execute(
                "SELECT item_id, CASE WHEN status IN ('indexed', 'skipped') THEN etag END FROM sources WHERE drive_id = %s",
                (corpus.name,),
            )).fetchall())
            if gone := sorted(set(stored) - set(stamps)):
                await index_store.delete_items(conn, corpus.name, gone)
            todo = [d for d in corpus.docs if stored.get(d.name) != stamps[d.name]]
            if todo:
                print(f"[index] {corpus.name}: {len(todo)} of {len(corpus.docs)} documents to (re)index", file=sys.stderr)
            with ProcessPoolExecutor(max_workers=WORKERS) as pool:
                for i in range(0, len(todo), BATCH_DOCS):
                    await _write(conn, corpus, gen, todo[i:i + BATCH_DOCS], stamps, pool)
                    print(f"[index] {corpus.name}: {min(i + BATCH_DOCS, len(todo))}/{len(todo)}", file=sys.stderr)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
    return url


async def skipped(url: str, suite: str) -> dict[str, str]:
    """Documents the index holds no chunks for, with the reason (extraction error or no text)."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        rows = await (await conn.execute(
            "SELECT item_id, status_note FROM sources WHERE drive_id = %s AND status = 'skipped'", (suite,))).fetchall()
    return dict(rows)


async def chunk_counts(url: str, suite: str) -> dict[str, int]:
    """Chunks per document in the active generation."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        gen = await generations.active(conn)
        rows = await (await conn.execute(sql.SQL(
            "SELECT s.item_id, count(*) FROM {t} c JOIN sources s ON s.id = c.source_id WHERE s.drive_id = %s GROUP BY 1"
        ).format(t=gen.table), (suite,))).fetchall()
    return dict(rows)
