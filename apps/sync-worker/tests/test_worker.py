import asyncio

from sharepoint_rag import queue
from sync_worker import jobs, worker


async def test_a_lost_lease_stops_the_running_handler(pool, monkeypatch):
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    stopped, wrote = asyncio.Event(), []

    async def slow_handler(ctx, payload):  # never calls ctx.heartbeat itself, like sync_item_acl
        try:
            await asyncio.sleep(5)
            wrote.append(True)
        except asyncio.CancelledError:
            stopped.set()
            raise

    monkeypatch.setitem(jobs.HANDLERS, "slow", slow_handler)
    async with pool.connection() as conn:
        await queue.enqueue(conn, "slow", {})
        job = await queue.claim(conn)
        # The lease expired and another worker reclaimed the job.
        await conn.execute("UPDATE jobs SET locked_by = 'other-worker' WHERE id = %s", (job.id,))
    await asyncio.wait_for(worker.run_job(pool, None, None, job), 2)
    assert stopped.is_set() and not wrote
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT state, locked_by FROM jobs WHERE id = %s", (job.id,))).fetchone()
    assert row == ("running", "other-worker")  # the new owner's state is untouched


async def test_embedding_jobs_wait_for_the_quota_without_running(pool, monkeypatch):
    """After a rate limit, queued index jobs are deferred before they start, not run to re-download their file
    and meet the same limit; and it costs them no attempt."""
    ran = []

    async def handler(ctx, payload):
        ran.append(payload)

    monkeypatch.setitem(jobs.HANDLERS, "index_item", handler)
    monkeypatch.setattr(worker, "_quota_resets_at", worker.time.monotonic() + 30)
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {"item_id": "a"})
        await queue.enqueue(conn, "sweep_acls", {})
        index_job = await queue.claim(conn, ["index_item"])
        other_job = await queue.claim(conn, ["sweep_acls"])
    monkeypatch.setitem(jobs.HANDLERS, "sweep_acls", handler)
    await worker.run_job(pool, None, None, index_job)
    await worker.run_job(pool, None, None, other_job)
    assert ran == [{}]  # only the job that doesn't embed ran
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT state, attempts, run_at > now() + interval '20 seconds' FROM jobs WHERE id = %s", (index_job.id,)
        )).fetchone()
    assert row == ("queued", 0, True)
