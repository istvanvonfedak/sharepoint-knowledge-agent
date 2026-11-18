"""Builds (once) a separate eval database and indexes the dataset into it with the real pipeline."""
import hashlib
import os
from datetime import datetime, timezone

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import sharepoint_rag.chunk as chunk_module
import sharepoint_rag.extract as extract_module
import sharepoint_rag.index_store as index_store_module
import sharepoint_rag.language as language_module
from sharepoint_rag import config, db, generations, index_store, inference, language
from sharepoint_rag.acl import AclRead
from sharepoint_rag.chunk import chunk_sections
from sharepoint_rag.extract import extract
from sharepoint_rag.index_store import ChunkRow, Item

from sharepoint_evals.dataset import DOCS


def eval_database_url() -> str:
    if url := os.environ.get("EVAL_DATABASE_URL"):
        return url
    return make_conninfo(config.require_database_url(), dbname="sharepoint_rag_eval")


def _index_hash() -> str:
    h = hashlib.sha256(f"{config.EMBEDDING_MODEL}:{config.EMBEDDING_DIMENSIONS}".encode())
    # Chunking/extraction code is part of the index: changing it must trigger a rebuild, otherwise the
    # capability eval would keep scoring a stale index and could not show the effect of the change.
    for module in (chunk_module, extract_module, index_store_module, language_module):
        h.update(open(module.__file__, "rb").read())
    for doc in DOCS:
        h.update(doc.name.encode() + b"\0" + doc.read() + b"\0" + ",".join(doc.acl).encode() + str(doc.resolved).encode())
    return h.hexdigest()


def _create_database_if_missing(url: str) -> None:
    params = conninfo_to_dict(url)
    name = params["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


async def ensure_index() -> str:
    """Idempotent: reindexes only if the dataset, model or dimensions changed. Returns the DB url.
    Writes go through the indexer's own write path (index_store.write_index), permissions included."""
    url = eval_database_url()
    _create_database_if_missing(url)
    db.migrate(url)
    index_hash = _index_hash()
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await register_vector_async(conn)
        gen = await generations.active(conn)
        if (gen.model, gen.dimensions) != (generations._model_id(config.EMBEDDING_MODEL), config.EMBEDDING_DIMENSIONS):
            # The eval DB was built for another model: start it over (it holds nothing but the dataset).
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            db.migrate(url)
            await register_vector_async(conn)
            gen = await generations.active(conn)
        # Serialise concurrent eval runs (another process, CI) so they don't truncate each other's rows.
        await conn.execute("SELECT pg_advisory_lock(7355608)")
        try:
            row = await (await conn.execute("SELECT count(*), count(*) FILTER (WHERE etag = %s) FROM sources", (index_hash,))).fetchone()
            if row == (len(DOCS), len(DOCS)):
                return url
            per_doc = []
            for doc in DOCS:
                chunks = chunk_sections(extract(doc.name, doc.read()))
                per_doc.append((doc, [ChunkRow(n, c.text, c.heading_path or None, language.detect(c.text)) for n, c in enumerate(chunks)], chunks))
            texts = [c.embedding_text for _, _, chunks in per_doc for c in chunks]
            vectors = iter(await inference.embed(gen.model, texts, "document", gen.dimensions))
            await conn.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
            read_at = datetime.now(timezone.utc)
            for doc, rows, _ in per_doc:
                item = Item("eval", doc.name, "eval", doc.name, f"https://eval.invalid/{doc.name}", index_hash, read_at)
                doc_vectors = [next(vectors) for _ in rows]
                await index_store.upsert_seen(conn, [item], None)
                await index_store.write_index(conn, item, AclRead(doc.acl, doc.resolved, read_at), rows, {gen.id: doc_vectors})
        finally:
            await conn.execute("SELECT pg_advisory_unlock(7355608)")
    return url
