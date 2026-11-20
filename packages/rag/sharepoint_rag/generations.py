"""Index generations: one chunk table per embedding model, so a model change never mixes vectors.

A generation is live while `building` or `active`. Writes go to every live generation; search reads the
active one. A new generation is filled by the sync-worker's `backfill_generation` job and switched to with
`activate`, which refuses until the generation's permissions and coverage match `sources`.

    python -m sharepoint_rag.generations list
    python -m sharepoint_rag.generations create gemini:gemini-embedding-001 768
    python -m sharepoint_rag.generations check 2
    python -m sharepoint_rag.generations activate 2
    python -m sharepoint_rag.generations drop 1      # a retired generation's table
"""
import argparse
from dataclasses import dataclass

import psycopg
from psycopg import sql

from . import acl, config, models

MAX_HNSW_DIMENSIONS = 2000  # pgvector's HNSW limit for the `vector` type
LIVE = ("building", "active")
# The chunk ACL a source should have; chunks must match it exactly (see acl.effective).
EFFECTIVE_ACL_SQL = f"CASE WHEN s.acl_status = 'resolved' THEN s.acl ELSE ARRAY['{acl.UNRESOLVED}']::text[] END"


@dataclass(frozen=True)
class Generation:
    id: int
    model: str
    dimensions: int
    status: str

    @property
    def table(self) -> sql.Identifier:
        return table(self.id)


def table(generation_id: int) -> sql.Identifier:
    return sql.Identifier(f"chunks_g{int(generation_id)}")


# What keyword search matches: the chunk's heading path and text, as both the exact (simple) tokens and the stemmed
# ones for the chunk's language, so "contratos" matches "contrato" in Spanish text while exact identifiers still match
# as typed. The heading path is included because it holds a document's title, an email's subject or a section name,
# often the only place an identifier ("Ticket INC-48213") appears; the embedding text includes it too.
_TSV_TEXT = "coalesce(heading_path, '') || ' ' || text"
# Digit groups joined by hyphens are also indexed without them, so a number matches however it is written:
# "2520-01-149-1221" and "2520011491221" (a stock number as an index page and a parts list print it), or
# "INV-2024-0173" and "INV-20240173". Only tokens with a digit-hyphen-digit are kept for this; search.compact_ids
# turns the query's into the same form.
_DIGIT_GROUPS = rf"""regexp_replace({_TSV_TEXT}, '(^|\s)(?!\S*[0-9]-[0-9])\S+', ' ', 'g')"""
TSV = (f"to_tsvector('simple', {_TSV_TEXT}) || to_tsvector(language, {_TSV_TEXT}) || "
       rf"""to_tsvector('simple', regexp_replace({_DIGIT_GROUPS}, '([0-9])-(?=[0-9])', '\1', 'g'))""")


def _ddl(generation_id: int, dimensions: int) -> sql.Composed:
    t = table(generation_id)
    name = f"chunks_g{int(generation_id)}"
    return sql.SQL("""
        CREATE TABLE IF NOT EXISTS {t} (
            id           bigserial PRIMARY KEY,
            source_id    bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            ordinal      int NOT NULL,
            text         text NOT NULL,
            heading_path text,
            language     regconfig NOT NULL DEFAULT 'simple',
            embedding    vector({dims}) NOT NULL,
            tsv          tsvector GENERATED ALWAYS AS ({tsv_expr}) STORED,
            acl          text[] NOT NULL DEFAULT '{{}}',
            metadata     jsonb NOT NULL DEFAULT '{{}}',
            UNIQUE (source_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS {emb} ON {t} USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS {tsv} ON {t} USING gin (tsv);
        CREATE INDEX IF NOT EXISTS {acl} ON {t} USING gin (acl);
    """).format(
        t=t, dims=sql.Literal(int(dimensions)), tsv_expr=sql.SQL(TSV),
        emb=sql.Identifier(f"{name}_embedding_idx"), tsv=sql.Identifier(f"{name}_tsv_idx"), acl=sql.Identifier(f"{name}_acl_idx"),
    )


def _validate(model: str, dimensions: int) -> None:
    models.parse(model)
    if not 1 <= dimensions <= MAX_HNSW_DIMENSIONS:
        raise ValueError(f"dimensions must be 1..{MAX_HNSW_DIMENSIONS} (pgvector HNSW limit)")


def _model_id(model: str) -> str:
    backend, name = models.parse(model)
    return f"{backend}:{name}"


def ensure_initial(conn: psycopg.Connection) -> None:
    """Creates generation 1 from EMBEDDING_MODEL/EMBEDDING_DIMENSIONS on a fresh database. A pre-generation
    `chunks` table is dropped: its rows carry no permission record (`sources.acl`), so they must be re-indexed."""
    if conn.execute("SELECT 1 FROM index_generations LIMIT 1").fetchone():
        return
    _validate(config.EMBEDDING_MODEL, config.EMBEDDING_DIMENSIONS)
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute("UPDATE sources SET etag = NULL, status = 'pending' WHERE status <> 'folder'")
        conn.execute(
            "INSERT INTO index_generations (id, model, dimensions, status) VALUES (1, %s, %s, 'active')",
            (_model_id(config.EMBEDDING_MODEL), config.EMBEDDING_DIMENSIONS),
        )
        conn.execute(_ddl(1, config.EMBEDDING_DIMENSIONS))


def ensure_tables(conn: psycopg.Connection) -> None:
    """Re-creates any live generation's table that is missing (e.g. after a partial restore), and rebuilds the
    keyword column of one whose expression is out of date. Only those: the DDL's CREATE INDEX IF NOT EXISTS would
    lock an existing table on every start, while reading the column's expression from the catalog takes no lock."""
    for gen_id, dims in conn.execute(
        "SELECT id, dimensions FROM index_generations WHERE status = ANY(%s)", (list(LIVE),)
    ).fetchall():
        name = f"chunks_g{int(gen_id)}"
        if not conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]:
            conn.execute(_ddl(gen_id, dims))
        elif _tsv_outdated(conn, name):
            _rebuild_tsv(conn, gen_id)


def _tsv_expression(conn: psycopg.Connection, name: str) -> str:
    return conn.execute(
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
        "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum WHERE d.adrelid = %s::regclass AND a.attname = 'tsv'",
        (name,),
    ).fetchone()[0]


def _tsv_outdated(conn: psycopg.Connection, name: str) -> bool:
    """Postgres stores the expression in its own normal form, so compare with what it makes of TSV."""
    with conn.transaction():
        conn.execute(f"CREATE TEMP TABLE _tsv_probe (heading_path text, text text, language regconfig, "
                     f"tsv tsvector GENERATED ALWAYS AS ({TSV}) STORED) ON COMMIT DROP")
        wanted = _tsv_expression(conn, "_tsv_probe")
    return _tsv_expression(conn, name) != wanted


def _rebuild_tsv(conn: psycopg.Connection, generation_id: int) -> None:
    """Recomputes the keyword column (and its index) for every row: one table rewrite, under an exclusive lock.
    Fine while the project runs locally. On a deployed index of millions of chunks it would block search for the
    rewrite and run on whichever service starts first: change the expression through a new generation instead
    (`generations create`, backfill, activate), which builds the new table beside the live one."""
    t, name = table(generation_id), f"chunks_g{int(generation_id)}"
    with conn.transaction():
        conn.execute(sql.SQL("ALTER TABLE {t} DROP COLUMN tsv").format(t=t))
        conn.execute(sql.SQL("ALTER TABLE {t} ADD COLUMN tsv tsvector GENERATED ALWAYS AS ({e}) STORED").format(t=t, e=sql.SQL(TSV)))
        conn.execute(sql.SQL("CREATE INDEX {i} ON {t} USING gin (tsv)").format(i=sql.Identifier(f"{name}_tsv_idx"), t=t))


def _row(r) -> Generation:
    return Generation(id=r[0], model=r[1], dimensions=r[2], status=r[3])


async def live(conn: psycopg.AsyncConnection, *, lock: bool = False) -> list[Generation]:
    """Live generations. With `lock`, they can't change status until this transaction ends, so a
    write sees the same set that `activate` and `create` see."""
    q = "SELECT id, model, dimensions, status FROM index_generations WHERE status = ANY(%s) ORDER BY id"
    rows = await (await conn.execute(q + (" FOR SHARE" if lock else ""), (list(LIVE),))).fetchall()
    return [_row(r) for r in rows]


async def active(conn: psycopg.AsyncConnection) -> Generation:
    row = await (await conn.execute(
        "SELECT id, model, dimensions, status FROM index_generations WHERE status = 'active'"
    )).fetchone()
    if row is None:
        raise RuntimeError("No active index generation; run `python -m sharepoint_rag.db` to set up the database")
    return _row(row)


def create(conn: psycopg.Connection, model: str, dimensions: int) -> int:
    """New `building` generation plus its backfill job. Writes from now on also go to it."""
    _validate(model, dimensions)
    with conn.transaction():
        conn.execute("LOCK TABLE index_generations IN EXCLUSIVE MODE")
        gen_id = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM index_generations").fetchone()[0]
        conn.execute(
            "INSERT INTO index_generations (id, model, dimensions, status) VALUES (%s, %s, %s, 'building')",
            (gen_id, _model_id(model), dimensions),
        )
        conn.execute(_ddl(gen_id, dimensions))
        conn.execute(
            "INSERT INTO jobs (kind, payload, dedupe_key) VALUES ('backfill_generation', %s, %s) "
            "ON CONFLICT (dedupe_key) WHERE state = 'queued' DO NOTHING",
            (psycopg.types.json.Jsonb({"generation_id": gen_id}), f"backfill:{gen_id}"),
        )
    return gen_id


def check(conn: psycopg.Connection, generation_id: int) -> list[str]:
    """Problems that block activating the generation; empty means it is safe to switch to."""
    t = table(generation_id)
    problems = []
    wrong_acl = conn.execute(sql.SQL(
        "SELECT count(DISTINCT c.source_id) FROM {t} c JOIN sources s ON s.id = c.source_id "
        "WHERE c.acl IS DISTINCT FROM " + EFFECTIVE_ACL_SQL
    ).format(t=t)).fetchone()[0]
    if wrong_acl:
        problems.append(f"{wrong_acl} file(s) have chunks whose permissions differ from sources.acl")
    # Every file indexed in the active generation must be in this one too, at the same version.
    missing = conn.execute(
        "SELECT count(*) FROM sources s "
        "JOIN source_generations a ON a.source_id = s.id "
        "JOIN index_generations g ON g.id = a.generation_id AND g.status = 'active' "
        "LEFT JOIN source_generations n ON n.source_id = s.id AND n.generation_id = %s "
        "WHERE n.source_id IS NULL OR n.etag IS DISTINCT FROM a.etag",
        (generation_id,),
    ).fetchone()[0]
    if missing:
        problems.append(f"{missing} indexed file(s) are missing or out of date in generation {generation_id}")
    orphans = conn.execute(sql.SQL(
        "SELECT count(DISTINCT c.source_id) FROM {t} c "
        "LEFT JOIN source_generations n ON n.source_id = c.source_id AND n.generation_id = %s WHERE n.source_id IS NULL"
    ).format(t=t), (generation_id,)).fetchone()[0]
    if orphans:
        problems.append(f"{orphans} file(s) have chunks but no source_generations record")
    return problems


def activate(conn: psycopg.Connection, generation_id: int) -> None:
    """One transaction: re-check under lock, then the old active generation retires and this one serves."""
    with conn.transaction():
        conn.execute("LOCK TABLE index_generations IN EXCLUSIVE MODE")
        row = conn.execute("SELECT status FROM index_generations WHERE id = %s", (generation_id,)).fetchone()
        if row is None or row[0] != "building":
            raise ValueError(f"Generation {generation_id} is not building (status: {row[0] if row else 'missing'})")
        # Writers lock sources rows; block them so nothing changes between the check and the flip.
        conn.execute("LOCK TABLE sources IN SHARE MODE")
        if problems := check(conn, generation_id):
            raise ValueError("Not activating: " + "; ".join(problems))
        conn.execute("UPDATE index_generations SET status = 'retired' WHERE status = 'active'")
        conn.execute("UPDATE index_generations SET status = 'active' WHERE id = %s", (generation_id,))


def drop(conn: psycopg.Connection, generation_id: int) -> None:
    """Deletes a retired (or abandoned building) generation's chunks for good."""
    with conn.transaction():
        conn.execute("LOCK TABLE index_generations IN EXCLUSIVE MODE")
        row = conn.execute("SELECT status FROM index_generations WHERE id = %s", (generation_id,)).fetchone()
        if row is None:
            raise ValueError(f"No generation {generation_id}")
        if row[0] == "active":
            raise ValueError("Can't drop the active generation; activate another one first")
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {t}").format(t=table(generation_id)))
        conn.execute("DELETE FROM index_generations WHERE id = %s", (generation_id,))


def main() -> None:
    from . import db

    parser = argparse.ArgumentParser(prog="python -m sharepoint_rag.generations")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("create")
    c.add_argument("model", help="e.g. gemini:gemini-embedding-001")
    c.add_argument("dimensions", type=int)
    for name in ("check", "activate", "drop"):
        sub.add_parser(name).add_argument("id", type=int)
    args = parser.parse_args()
    db.migrate()
    with psycopg.connect(config.require_database_url(), autocommit=True) as conn:
        if args.cmd == "list":
            for r in conn.execute(
                "SELECT g.id, g.model, g.dimensions, g.status, count(sg.source_id) FROM index_generations g "
                "LEFT JOIN source_generations sg ON sg.generation_id = g.id GROUP BY g.id ORDER BY g.id"
            ).fetchall():
                print(f"{r[0]:>3}  {r[3]:<9} {r[1]} ({r[2]} dims), {r[4]} files")
        elif args.cmd == "create":
            print(f"Created generation {create(conn, args.model, args.dimensions)}; the sync-worker will backfill it.")
        elif args.cmd == "check":
            problems = check(conn, args.id)
            print("\n".join(problems) or "OK: safe to activate")
        elif args.cmd == "activate":
            activate(conn, args.id)
            print(f"Generation {args.id} is now active.")
        elif args.cmd == "drop":
            drop(conn, args.id)
            print(f"Dropped generation {args.id}.")


if __name__ == "__main__":
    main()
