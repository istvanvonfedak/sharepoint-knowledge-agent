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
