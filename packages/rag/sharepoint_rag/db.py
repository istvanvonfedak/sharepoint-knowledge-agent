import hashlib
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from . import config, generations

SCHEMA = Path(__file__).with_name("schema.sql")
MIGRATE_LOCK = 7_355_609  # advisory lock id: replicas starting together migrate one at a time


def migrate(url: str | None = None) -> None:
    """Applies schema.sql and makes sure an active index generation exists (idempotent).

    Runs on every service start and CLI command, so an unchanged schema is skipped entirely: even a no-op
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` takes an ACCESS EXCLUSIVE lock, which would queue every search
    behind any long-running reader (a `generations activate` check, say)."""
    schema = SCHEMA.read_text()
    digest = hashlib.sha256(schema.encode()).hexdigest()
    with psycopg.connect(url or config.require_database_url(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK,))
        try:
            applied = conn.execute("SELECT to_regclass('schema_version') IS NOT NULL").fetchone()[0] and conn.execute(
                "SELECT 1 FROM schema_version WHERE digest = %s", (digest,)
            ).fetchone()
            if not applied:
                conn.execute(schema)
                conn.execute("CREATE TABLE IF NOT EXISTS schema_version (digest text NOT NULL)")
                with conn.transaction():
                    conn.execute("DELETE FROM schema_version")
                    conn.execute("INSERT INTO schema_version (digest) VALUES (%s)", (digest,))
            generations.ensure_initial(conn)
            generations.ensure_tables(conn)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK,))


def create_pool(url: str | None = None, **kwargs) -> AsyncConnectionPool:
    """Call migrate() first (it creates the vector extension), then `await pool.open()`.

    Connections are autocommit: each statement commits on its own, and everything that must be atomic says so
    with `async with conn.transaction()`. So a caught error never leaves a connection in an aborted transaction."""
    return AsyncConnectionPool(
        url or config.require_database_url(), configure=register_vector_async, open=False,
        kwargs={"autocommit": True}, **kwargs,
    )


if __name__ == "__main__":
    migrate()
    print(f"Schema applied to {config.require_database_url().rsplit('@', 1)[-1]}")
