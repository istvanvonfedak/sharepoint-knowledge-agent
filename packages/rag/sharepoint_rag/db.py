from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")


def migrate() -> None:
    """Applies schema.sql (idempotent). The vector extension must exist before register_vector."""
    sql = SCHEMA.read_text().replace("{{DIMENSIONS}}", str(config.EMBEDDING_DIMENSIONS))
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql)
        # CREATE TABLE IF NOT EXISTS won't change an existing column, so detect config/table drift here.
        row = conn.execute(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        ).fetchone()
        if row and row[0] != config.EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"chunks.embedding is vector({row[0]}) but EMBEDDING_DIMENSIONS={config.EMBEDDING_DIMENSIONS}. "
                "Re-embedding is required: drop the chunks table (and reset sources) or restore the old setting."
            )


def create_pool() -> AsyncConnectionPool:
    """Call migrate() first (it creates the vector extension), then `await pool.open()`."""
    return AsyncConnectionPool(config.DATABASE_URL, configure=register_vector_async, open=False)


if __name__ == "__main__":
    migrate()
    print(f"Schema applied to {config.DATABASE_URL.rsplit('@', 1)[-1]}")
