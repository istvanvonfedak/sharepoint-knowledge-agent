"""The worker loop: N job runners per replica plus a scheduler tick. Run as many replicas as Graph's rate
limits allow; they coordinate only through Postgres (job claims, schedules, the shared Graph throttle)."""
import asyncio
import logging
import signal
from contextlib import suppress

from psycopg_pool import AsyncConnectionPool
from sharepoint_rag import db, queue

from . import config, jobs
from .directory import Directory
from .graph import AppGraph, GraphError

log = logging.getLogger(__name__)

IDLE_SECONDS = 2.0
SCHEDULE_TICK_SECONDS = 15.0
HEARTBEAT_SECONDS = 60.0


async def _due(pool: AsyncConnectionPool, name: str, every_seconds: int) -> bool:
    """True for exactly one replica once per interval."""
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "INSERT INTO schedules (name, last_run_at) VALUES (%s, now()) ON CONFLICT (name) DO UPDATE "
            "SET last_run_at = now() WHERE schedules.last_run_at < now() - %s * interval '1 second' RETURNING name",
            (name, every_seconds),
        )).fetchone()
    return row is not None


async def schedule_tick(pool: AsyncConnectionPool) -> None:
    if await _due(pool, "maintenance", 60):
        async with pool.connection() as conn:
            if reaped := await queue.reap(conn):
                log.warning("re-queued %d job(s) whose worker stopped responding", reaped)
            await queue.prune(conn)
            await conn.execute("DELETE FROM graph_throttle WHERE until < now()")
    if await _due(pool, "discover_sites", config.DISCOVER_INTERVAL):
        async with pool.connection() as conn:
            await queue.enqueue(conn, "discover_sites", dedupe_key="discover_sites")
    if await _due(pool, "crawl_drives", config.CRAWL_INTERVAL):
        async with pool.connection() as conn, conn.transaction():
            drives = await (await conn.execute(
                "SELECT d.id FROM drives d JOIN sites s ON s.id = d.site_id WHERE d.enabled AND s.enabled"
            )).fetchall()
            await queue.enqueue_many(conn, [("crawl_drive", {"drive_id": d}, f"crawl:{d}") for (d,) in drives])
    if await _due(pool, "sweep_acls", config.SWEEP_INTERVAL):
        async with pool.connection() as conn:
            await queue.enqueue(conn, "sweep_acls", dedupe_key="sweep_acls")


async def run_job(pool: AsyncConnectionPool, graph: AppGraph, directory: Directory, job: queue.Job) -> None:
    lost = asyncio.Event()

    async def heartbeat() -> None:
        async with pool.connection() as conn:
            if not await queue.heartbeat(conn, job):
                lost.set()
        if lost.is_set():
            raise jobs.LeaseLost("job lease lost")

    async def keep_alive(work: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await heartbeat()
            except jobs.LeaseLost:
                # Reclaimed by another worker: stop the handler mid-flight, so two copies never write at once.
                # Its open transaction (if any) rolls back.
                work.cancel()
                return
            except Exception as e:  # a DB blip must not end the heartbeat for the rest of the job
                log.warning("heartbeat for job %d failed: %s", job.id, e)

    handler = jobs.HANDLERS.get(job.kind)
    if handler is None:
        async with pool.connection() as conn:
            await queue.fail(conn, job, f"unknown job kind {job.kind!r}", retry=False)
        log.error("job %d: unknown job kind %r", job.id, job.kind)
        return
    ctx = jobs.Context(pool=pool, graph=graph, heartbeat=heartbeat, final_attempt=job.attempts >= queue.MAX_ATTEMPTS,
                       directory=directory)
    work = asyncio.create_task(handler(ctx, job.payload))
    ticker = asyncio.create_task(keep_alive(work))
    try:
        await work
    except (asyncio.CancelledError, jobs.LeaseLost):
        if not lost.is_set():
            raise  # shutting down
        # The job is someone else's now: record nothing (complete/fail would no-op on the lease anyway).
        log.warning("job %d %s: lease lost, stopped; another worker has it", job.id, job.kind)
    except Exception as e:
        retry = not isinstance(e, jobs.Permanent) and not (isinstance(e, GraphError) and e.status in (400, 401, 403))
        async with pool.connection() as conn:
            state = await queue.fail(conn, job, f"{type(e).__name__}: {e}", retry=retry)
        log.log(logging.ERROR if state == "failed" else logging.WARNING, "job %d %s %s -> %s: %s", job.id, job.kind, job.payload, state, e)
    else:
        async with pool.connection() as conn:
            await queue.complete(conn, job)
    finally:
        ticker.cancel()


async def runner(pool: AsyncConnectionPool, graph: AppGraph, directory: Directory, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with pool.connection() as conn:
                job = await queue.claim(conn)
        except Exception:
            log.exception("claiming a job failed")
            job = None
        if job is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), IDLE_SECONDS)
            continue
        try:
            await run_job(pool, graph, directory, job)
        except Exception:  # recording the outcome failed (DB down?): the lease expires and the job is retried
            log.exception("job %d: recording its result failed", job.id)


async def scheduler(pool: AsyncConnectionPool, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await schedule_tick(pool)
        except Exception:
            log.exception("scheduler tick failed")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), SCHEDULE_TICK_SECONDS)


async def main() -> None:
    db.migrate()
    pool = db.create_pool(min_size=1, max_size=config.CONCURRENCY + 4)
    graph = AppGraph(pool)  # before the pool opens: bad credentials/tenant must fail fast, not hang on shutdown
    directory = Directory(graph)  # one cache for every runner
    await pool.open(wait=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("sync-worker %s: %d runners", queue.WORKER_ID, config.CONCURRENCY)
    try:
        # A job in flight at shutdown finishes; its lease would otherwise make it wait LEASE to be retried.
        await asyncio.gather(scheduler(pool, stop), *(runner(pool, graph, directory, stop) for _ in range(config.CONCURRENCY)))
    finally:
        await graph.close()
        await pool.close()
