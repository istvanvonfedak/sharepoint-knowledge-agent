"""Builds (once) a separate eval database and indexes the dataset into it with the real pipeline."""
import hashlib
import os

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import sharepoint_rag.chunk as chunk_module
import sharepoint_rag.extract as extract_module
from sharepoint_rag import config, db, embeddings
from sharepoint_rag.chunk import chunk_sections
from sharepoint_rag.extract import extract

from sharepoint_evals.dataset import DOCS


def eval_database_url() -> str:
    if url := os.environ.get("EVAL_DATABASE_URL"):
        return url
    return make_conninfo(config.DATABASE_URL, dbname="sharepoint_rag_eval")


def _index_hash() -> str:
    h = hashlib.sha256(f"{config.EMBEDDING_MODEL}:{config.EMBEDDING_DIMENSIONS}".encode())
    # Chunking/extraction code is part of the index: changing it must trigger a rebuild, otherwise the
    # capability eval would keep scoring a stale index and could not show the effect of the change.
    for module in (chunk_module, extract_module):
        h.update(open(module.__file__, "rb").read())
    for doc in DOCS:
        h.update(doc.name.encode() + b"\0" + doc.read() + b"\0" + ",".join(doc.acl).encode())
    return h.hexdigest()


def _create_database_if_missing(url: str) -> None:
    params = conninfo_to_dict(url)
    name = params["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


async def ensure_index() -> str:
    """Idempotent: reindexes only if the dataset, model or dimensions changed. Returns the DB url."""
    url = eval_database_url()
    _create_database_if_missing(url)
    db.migrate(url)
    index_hash = _index_hash()
    async with await psycopg.AsyncConnection.connect(url) as conn:
        await register_vector_async(conn)
        # Serialise concurrent eval runs (another process, CI) so they don't truncate each other's rows.
        await conn.execute("SELECT pg_advisory_xact_lock(7355608)")
        row = await (await conn.execute("SELECT count(*), count(*) FILTER (WHERE etag = %s) FROM sources", (index_hash,))).fetchone()
        if row == (len(DOCS), len(DOCS)):
            return url

        # Embed first so the TRUNCATE (ACCESS EXCLUSIVE) isn't held across network calls.
        per_doc = [(doc, chunk_sections(extract(doc.name, doc.read()))) for doc in DOCS]
        vectors = await embeddings.embed_documents([c.embedding_text for _, chunks in per_doc for c in chunks])
        it = iter(vectors)
        await conn.execute("TRUNCATE sources, chunks RESTART IDENTITY CASCADE")
        for doc, chunks in per_doc:
            source_id = (await (await conn.execute(
                "INSERT INTO sources(drive_id, item_id, site_id, web_url, name, etag, status) "
                "VALUES ('eval', %s, 'eval', %s, %s, %s, 'indexed') RETURNING id",
                (doc.name, f"https://eval.invalid/{doc.name}", doc.name, index_hash),
            )).fetchone())[0]
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO chunks(source_id, ordinal, text, heading_path, embedding, acl) VALUES (%s,%s,%s,%s,%s,%s)",
                    [(source_id, n, c.text, c.heading_path, next(it), list(doc.acl)) for n, c in enumerate(chunks)],
                )
        await conn.commit()
    return url
