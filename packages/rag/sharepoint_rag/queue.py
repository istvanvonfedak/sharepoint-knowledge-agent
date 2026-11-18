"""Postgres job queue (FOR UPDATE SKIP LOCKED). No broker: a job commits together with the data it changes.

At most one job per `dedupe_key` is queued, and a queued job is not claimed while another job with the same key
is running. So a change that arrives while a file is being indexed queues one follow-up run rather than being
dropped, and two crawls of one drive never overlap.
"""
import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

LEASE = timedelta(minutes=5)  # renewed by the worker's heartbeat while a job runs
MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
# Claimed only when nothing else is ready. The first crawl of a big library queues hundreds of thousands of
# these; without this, purges (`sites disable`), permission syncs and other drives' crawls would wait hours.
BULK_KINDS = frozenset({"index_item", "backfill_generation"})


def _priority(kind: str) -> int:
    return 1 if kind in BULK_KINDS else 0


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    dedupe_key: str | None
    lease: str  # this claim's lock owner: a reclaimed job's old runner no longer matches it


async def enqueue(
    conn: psycopg.AsyncConnection, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
    delay_seconds: float = 0,
) -> int | None:
    """Queues a job; returns its id, or None if one with the same dedupe_key is already queued."""
    cur = await conn.execute(
        "INSERT INTO jobs (kind, payload, dedupe_key, run_at, priority) "
        "VALUES (%s, %s, %s, now() + %s * interval '1 second', %s) "
        "ON CONFLICT (dedupe_key) WHERE state = 'queued' DO NOTHING RETURNING id",
        (kind, Jsonb(payload or {}), dedupe_key, delay_seconds, _priority(kind)),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def enqueue_many(conn: psycopg.AsyncConnection, jobs: list[tuple[str, dict, str | None]]) -> None:
    """(kind, payload, dedupe_key) per job, in one statement."""
    if not jobs:
        return
    await conn.execute(
        "INSERT INTO jobs (kind, payload, dedupe_key, priority) "
        "SELECT * FROM unnest(%s::text[], %s::jsonb[], %s::text[], %s::smallint[]) "
        "ON CONFLICT (dedupe_key) WHERE state = 'queued' DO NOTHING",
        ([k for k, _, _ in jobs], [json.dumps(p) for _, p, _ in jobs], [d for _, _, d in jobs], [_priority(k) for k, _, _ in jobs]),
    )


async def claim(conn: psycopg.AsyncConnection, kinds: list[str] | None = None) -> Job | None:
    """Takes the next ready job (autocommit connection) and leases it to this worker."""
    lease = f"{WORKER_ID}:{uuid.uuid4().hex[:12]}"
    cur = await conn.execute(
        """
        UPDATE jobs SET state = 'running', locked_by = %(me)s, locked_until = now() + %(lease)s, attempts = attempts + 1
        WHERE id = (
            SELECT j.id FROM jobs j
            WHERE j.state = 'queued' AND j.run_at <= now() AND (%(kinds)s::text[] IS NULL OR j.kind = ANY(%(kinds)s))
              AND (j.dedupe_key IS NULL OR NOT EXISTS (
                  SELECT 1 FROM jobs r WHERE r.state = 'running' AND r.dedupe_key = j.dedupe_key))
            ORDER BY j.priority, j.run_at, j.id
            LIMIT 1 FOR UPDATE SKIP LOCKED
        )
        RETURNING id, kind, payload, attempts, dedupe_key
        """,
        {"me": lease, "lease": LEASE, "kinds": kinds},
    )
    row = await cur.fetchone()
    return Job(*row, lease=lease) if row else None


async def heartbeat(conn: psycopg.AsyncConnection, job: Job) -> bool:
    """Extends the lease; False if the job is no longer ours (lease expired and it was reclaimed)."""
    cur = await conn.execute(
        "UPDATE jobs SET locked_until = now() + %s WHERE id = %s AND state = 'running' AND locked_by = %s",
        (LEASE, job.id, job.lease),
    )
    return cur.rowcount == 1


async def complete(conn: psycopg.AsyncConnection, job: Job) -> None:
    await conn.execute(
        "UPDATE jobs SET state = 'done', finished_at = now(), locked_by = NULL, locked_until = NULL, last_error = NULL "
        "WHERE id = %s AND locked_by = %s",
        (job.id, job.lease),
    )


async def fail(conn: psycopg.AsyncConnection, job: Job, error: str, *, retry: bool = True, delay_seconds: float | None = None) -> str:
    """Re-queues with exponential backoff, or marks the job failed after MAX_ATTEMPTS. Returns the new state.
    If a newer job with the same key is already queued, this one is closed: the newer one redoes the work."""
    exhausted = not retry or job.attempts >= MAX_ATTEMPTS
    delay = delay_seconds if delay_seconds is not None else min(BACKOFF_BASE_SECONDS * 2 ** (job.attempts - 1), BACKOFF_MAX_SECONDS)
    try:
        return await _requeue(conn, job, error, exhausted, delay)
    except psycopg.errors.UniqueViolation:
        # A follow-up with the same key was queued (committed) while we decided: it redoes the work.
        await conn.execute(
            "UPDATE jobs SET state = 'done', finished_at = now(), last_error = %s, locked_by = NULL, locked_until = NULL "
            "WHERE id = %s AND locked_by = %s",
            (error[:2000], job.id, job.lease),
        )
        return "done"


async def _requeue(conn: psycopg.AsyncConnection, job: Job, error: str, exhausted: bool, delay: float) -> str:
    cur = await conn.execute(
        """
        UPDATE jobs SET
            state = CASE WHEN %(exhausted)s THEN 'failed'
                         WHEN dedupe_key IS NOT NULL AND EXISTS (
                             SELECT 1 FROM jobs q WHERE q.state = 'queued' AND q.dedupe_key = jobs.dedupe_key) THEN 'done'
                         ELSE 'queued' END,
            run_at = now() + %(delay)s * interval '1 second',
            finished_at = now(),  -- only read for done/failed jobs (pruning)
            last_error = %(error)s, locked_by = NULL, locked_until = NULL
        WHERE id = %(id)s AND locked_by = %(me)s
        RETURNING state
        """,
        {"exhausted": exhausted, "delay": delay, "error": error[:2000], "id": job.id, "me": job.lease},
    )
    row = await cur.fetchone()
    return row[0] if row else "lost"


async def reap(conn: psycopg.AsyncConnection) -> int:
    """Returns jobs whose worker died (lease expired) to the queue; drops ones a newer queued job supersedes."""
    await conn.execute(
        "UPDATE jobs r SET state = 'done', finished_at = now(), last_error = 'lease expired; superseded', locked_by = NULL "
        "WHERE r.state = 'running' AND r.locked_until < now() AND r.dedupe_key IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM jobs q WHERE q.state = 'queued' AND q.dedupe_key = r.dedupe_key)"
    )
    try:
        cur = await conn.execute(
            "UPDATE jobs SET state = CASE WHEN attempts >= %s THEN 'failed' ELSE 'queued' END, "
            "finished_at = CASE WHEN attempts >= %s THEN now() END, "
            "last_error = 'lease expired (worker died?)', locked_by = NULL, locked_until = NULL "
            "WHERE state = 'running' AND locked_until < now()",
            (MAX_ATTEMPTS, MAX_ATTEMPTS),
        )
    except psycopg.errors.UniqueViolation:
        return 0  # a follow-up was queued between the two statements; the next run's first statement closes it
    return cur.rowcount


async def prune(conn: psycopg.AsyncConnection, keep_days: int = 7) -> None:
    await conn.execute(
        "DELETE FROM jobs WHERE state IN ('done', 'failed') AND finished_at < now() - %s * interval '1 day'", (keep_days,)
    )
