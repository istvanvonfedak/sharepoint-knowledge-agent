"""Fixtures shared by every test suite, loaded as a plugin by pytest.ini (a conftest.py here would only reach
tests under tests/). Database tests need Postgres with pgvector: set TEST_DATABASE_URL (a server the tests may
create and drop a `sharepoint_test_<pid>` database on), e.g.

    docker run -d --name sk-testdb -e POSTGRES_PASSWORD=test -p 127.0.0.1:5433:5432 pgvector/pgvector:pg17
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres .venv/bin/python -m pytest

Without it those tests are skipped.
"""
import os

# Tests never send traces to LangSmith: importing mcp_server loads .env.mcp, which turns tracing on for local runs
# of the server. Set before any test module is imported, and load_dotenv never overrides a variable already set.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from tests.testing_support import TEST_DIMENSIONS, fake_vector  # noqa: E402


@pytest.fixture(scope="session")
def _test_db():
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL not set")
    name = f"sharepoint_test_{os.getpid()}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield make_conninfo(base, dbname=name)
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db_url(_test_db, monkeypatch):
    """A migrated, empty database with one active generation of TEST_DIMENSIONS-sized fake vectors."""
    from sharepoint_rag import config, db, inference

    monkeypatch.setattr(config, "EMBEDDING_MODEL", "gemini:test-model")
    monkeypatch.setattr(config, "EMBEDDING_DIMENSIONS", TEST_DIMENSIONS)
    monkeypatch.setattr(config, "DATABASE_URL", _test_db)
    with psycopg.connect(_test_db, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.migrate(_test_db)

    async def embed(model, texts, task, dimensions):
        return [fake_vector(f"{model}:{t}", dimensions) for t in texts]

    monkeypatch.setattr(inference, "embed", embed)
    return _test_db


@pytest.fixture
async def pool(db_url):
    from sharepoint_rag import db

    p = db.create_pool(db_url, min_size=1, max_size=8)
    await p.open()
    yield p
    await p.close()
