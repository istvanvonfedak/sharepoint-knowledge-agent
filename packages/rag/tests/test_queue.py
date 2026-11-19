import psycopg
from sharepoint_rag import queue


async def test_dedupe_keeps_one_queued_job_per_key(pool):
    async with pool.connection() as conn:
        assert await queue.enqueue(conn, "crawl_drive", {"drive_id": "d"}, dedupe_key="crawl:d")
        assert await queue.enqueue(conn, "crawl_drive", {"drive_id": "d"}, dedupe_key="crawl:d") is None


async def test_follow_up_is_queued_while_running_but_not_claimed_until_done(pool):
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {"n": 1}, dedupe_key="index:x")
        first = await queue.claim(conn)
        # A change arriving mid-run queues a follow-up rather than being dropped...
        assert await queue.enqueue(conn, "index_item", {"n": 2}, dedupe_key="index:x")
        # ...which isn't claimed while the first still runs, so the two never overlap.
        assert await queue.claim(conn) is None
        await queue.complete(conn, first)
        second = await queue.claim(conn)
        assert second.payload == {"n": 2}


async def test_failure_backs_off_then_gives_up(pool, monkeypatch):
    monkeypatch.setattr(queue, "MAX_ATTEMPTS", 2)
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {})
        job = await queue.claim(conn)
        assert await queue.fail(conn, job, "boom") == "queued"
        assert await queue.claim(conn) is None  # backing off
        await conn.execute("UPDATE jobs SET run_at = now()")
        job = await queue.claim(conn)
        assert await queue.fail(conn, job, "boom again") == "failed"


async def test_a_deferred_job_waits_but_keeps_its_attempts(pool, monkeypatch):
    """A rate limit is not the job's fault: deferring it any number of times never uses up MAX_ATTEMPTS."""
    monkeypatch.setattr(queue, "MAX_ATTEMPTS", 2)
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {})
        for _ in range(5):
            job = await queue.claim(conn)
            assert job.attempts == 1
            assert await queue.defer(conn, job, "rate limited", 0) == "queued"
        assert (await queue.claim(conn)).attempts == 1
        await queue.enqueue(conn, "index_item", {"n": 2})
        job = await queue.claim(conn)
        assert await queue.defer(conn, job, "rate limited", 60) == "queued"
        assert await queue.claim(conn) is None  # waits for its delay


async def test_claim_can_leave_kinds_out(pool):
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {})
        await queue.enqueue(conn, "sweep_acls", {})
        assert (await queue.claim(conn, exclude=["index_item", "backfill_generation"])).kind == "sweep_acls"
        assert await queue.claim(conn, exclude=["index_item"]) is None


async def test_expired_lease_is_reclaimed(pool):
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {}, dedupe_key="k")
        job = await queue.claim(conn)
        await conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = %s", (job.id,))
        assert await queue.reap(conn) == 1
        assert not await queue.heartbeat(conn, job)
        assert (await queue.claim(conn)).id == job.id


async def test_fail_survives_a_follow_up_queued_concurrently(pool, monkeypatch):
    """fail() decides 'queued', but a follow-up with the same key commits first: the job must end, not stick."""
    async with pool.connection() as conn:
        await queue.enqueue(conn, "index_item", {"n": 1}, dedupe_key="k")
        job = await queue.claim(conn)
        real = queue._requeue

        async def racing_requeue(*args):
            async with pool.connection() as other:
                await queue.enqueue(other, "index_item", {"n": 2}, dedupe_key="k")
            # Pretend the EXISTS check ran before that commit, as in the real race.
            raise psycopg.errors.UniqueViolation("simulated")

        monkeypatch.setattr(queue, "_requeue", racing_requeue)
        assert await queue.fail(conn, job, "boom") == "done"
        monkeypatch.setattr(queue, "_requeue", real)
        states = await (await conn.execute("SELECT payload->>'n', state FROM jobs ORDER BY id")).fetchall()
        assert states == [("1", "done"), ("2", "queued")]
        assert (await queue.claim(conn)).payload == {"n": 2}


async def test_bulk_jobs_yield_to_everything_else(pool):
    async with pool.connection() as conn:
        for n in range(3):
            await queue.enqueue(conn, "index_item", {"n": n})  # a big crawl's backlog, queued first
        await queue.enqueue_many(conn, [("backfill_generation", {}, None)])
        await queue.enqueue(conn, "purge_drive", {"drive_id": "d"})
        await queue.enqueue_many(conn, [("sync_item_acl", {}, None)])
        kinds = []
        while job := await queue.claim(conn):
            kinds.append(job.kind)
            await queue.complete(conn, job)
    assert kinds == ["purge_drive", "sync_item_acl", "index_item", "index_item", "index_item", "backfill_generation"]
