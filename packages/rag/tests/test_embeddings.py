"""Gemini rate limits: the shared limiter, Gemini's retry hint, and the typed error callers act on."""
import httpx
import pytest
from sharepoint_rag import config, embeddings, inference
from sharepoint_rag.embeddings import Limiter, RateLimited


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_documents_leave_the_reserve_for_queries():
    clock = Clock()
    limiter = Limiter(600, 0.1, clock=clock)  # 10 texts a second, 60 kept for queries
    assert limiter.wait_for(540, query=False) == 0
    assert limiter.wait_for(1, query=False) > 0  # only the reserve is left
    assert limiter.wait_for(60, query=True) == 0  # ...and queries may use it
    assert limiter.wait_for(1, query=True) == pytest.approx(0.1)


def test_tokens_refill_with_time():
    clock = Clock()
    limiter = Limiter(600, 0.0, clock=clock)
    assert limiter.wait_for(600, query=False) == 0
    assert limiter.wait_for(100, query=False) == pytest.approx(10.0)
    clock.now = 10.0
    assert limiter.wait_for(100, query=False) == 0


def test_a_batch_bigger_than_the_budget_waits_for_a_full_bucket_not_forever():
    clock = Clock()
    limiter = Limiter(60, 0.0, clock=clock)
    assert limiter.wait_for(100, query=False) == 0  # capped at the bucket's size
    assert limiter.wait_for(100, query=False) == pytest.approx(60.0)


@pytest.mark.parametrize("per_minute", [98, 101, 111, 2700])
def test_a_full_bucket_always_fits_the_largest_document_batch(per_minute):
    """Rounding must not leave a capped batch a few ulps short of a full bucket, waiting forever."""
    limiter = Limiter(per_minute, 0.1, clock=Clock())
    assert limiter.wait_for(100, query=False) == 0


def test_a_pause_does_not_credit_back_the_time_before_it():
    clock = Clock()
    limiter = Limiter(600, 0.0, clock=clock)
    assert limiter.wait_for(600, query=True) == 0  # bucket emptied at t=0
    clock.now = 5.0
    limiter.pause(1.0)  # the 429 arrives 5 s later
    clock.now = 6.0
    assert limiter.wait_for(20, query=True) == pytest.approx(1.0)  # 10 tokens from 1 s since the pause; 6 s would be 60


async def test_a_small_budget_waits_for_refills_instead_of_deferring(monkeypatch):
    clock = Clock()
    limiter = Limiter(200, 0.1, clock=clock)  # a 100-text batch from an empty bucket waits over 30 s

    async def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(embeddings.asyncio, "sleep", sleep)
    for _ in range(3):
        await limiter.acquire(100, query=False)  # waits, never raises RateLimited
    assert clock.now > 30


def test_a_429_holds_documents_back_but_not_queries():
    clock = Clock()
    limiter = Limiter(600, 0.1, clock=clock)
    limiter.pause(5.0)
    assert limiter.wait_for(1, query=False) == pytest.approx(5.0)
    assert limiter.wait_for(1, query=True) == pytest.approx(0.1)  # the bucket was emptied, but no pause


def response(status=429, body=None, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, headers=headers or {})


def test_retry_delay_reads_what_gemini_says():
    retry_info = {"error": {"message": "Quota exceeded", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "4.027s"}]}}
    assert embeddings.retry_delay(response(body=retry_info)) == pytest.approx(4.027)
    in_message = {"error": {"message": "Quota exceeded for metric ... Please retry in 12.5s."}}
    assert embeddings.retry_delay(response(body=in_message)) == pytest.approx(12.5)
    assert embeddings.retry_delay(response(headers={"Retry-After": "7"})) == 7.0
    assert embeddings.retry_delay(response()) is None
    # The inference-server's: its header (Gemini's delay plus a margin) wins over Gemini's figure in the message.
    ours = httpx.Response(429, json={"error": "Gemini 429: Please retry in 45.3s."}, headers={"Retry-After": "47"})
    assert embeddings.retry_delay(ours) == 47.0


def gemini(handler, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(embeddings.asyncio, "sleep", sleep)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(embeddings, "LIMITER", Limiter(10_000, 0.1))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), waits


QUOTA = {"error": {"message": "Quota exceeded. Please retry in 45s."}}


async def test_a_document_429_is_deferred_at_once_with_geminis_delay(monkeypatch):
    """No waiting inside the request: the sync-worker defers the job and resumes it from its cached batches."""
    client, waits = gemini(lambda r: httpx.Response(429, json=QUOTA), monkeypatch)
    async with client:
        with pytest.raises(RateLimited) as e:
            await embeddings._post(client, "https://gemini/embed", {})
    assert waits == [] and e.value.retry_after == 46.0
    assert embeddings.LIMITER.wait_for(1, query=False) > 40  # other document batches are held back too


async def test_a_daily_cap_defers_for_as_long_as_it_says(monkeypatch):
    """Sleeps are capped at a minute; the delay handed to the caller is not, so jobs aren't retried every minute."""
    daily = {"error": {"message": "Quota exceeded for metric ... per day. Please retry in 3600s."}}
    client, waits = gemini(lambda r: httpx.Response(429, json=daily), monkeypatch)
    async with client:
        with pytest.raises(RateLimited) as e:
            await embeddings._post(client, "https://gemini/embed", {})
    assert e.value.retry_after == 3601.0


async def test_a_body_that_is_not_an_object_is_still_retried(monkeypatch):
    responses = iter([httpx.Response(503, json=[{"error": "busy"}]), httpx.Response(200, json={"ok": 1})])
    client, waits = gemini(lambda r: next(responses), monkeypatch)
    async with client:
        assert await embeddings._post(client, "https://gemini/embed", {}) == {"ok": 1}
    assert waits == [1]


async def test_a_short_429_is_waited_out_inside_the_request(monkeypatch):
    """The minute window turning in a few seconds: cheaper to wait than to defer the job and redo its download."""
    responses = iter([httpx.Response(429, json={"error": {"message": "Please retry in 2s."}}), httpx.Response(200, json={"ok": 1})])
    client, waits = gemini(lambda r: next(responses), monkeypatch)
    async with client:
        assert await embeddings._post(client, "https://gemini/embed", {}) == {"ok": 1}
    assert waits == [3.0]


async def test_a_zero_hint_is_a_hint(monkeypatch):
    zero = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}]}}
    responses = iter([httpx.Response(429, json=zero), httpx.Response(200, json={"ok": 1})])
    client, waits = gemini(lambda r: next(responses), monkeypatch)
    async with client:
        await embeddings._post(client, "https://gemini/embed", {})
    assert waits == [1.0]  # not the 10 s used when there is no hint


def test_a_malformed_delay_is_ignored_not_raised():
    assert embeddings.retry_delay(response(body={"error": {"message": "Please retry in 1.2.3s."}})) is None
    assert embeddings.retry_delay(response(body={"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "..s"}]}})) is None


async def test_a_query_429_retries_briefly_not_for_minutes(monkeypatch):
    client, waits = gemini(lambda r: httpx.Response(429, json=QUOTA), monkeypatch)
    async with client:
        with pytest.raises(RateLimited) as e:
            await embeddings._post(client, "https://gemini/embed", {}, query=True)
    assert waits == [embeddings.QUERY_MAX_WAIT] * (embeddings.MAX_ATTEMPTS - 1)
    assert e.value.retry_after == 46.0


async def test_a_5xx_waits_as_long_as_retry_after_says(monkeypatch):
    responses = iter([httpx.Response(503, headers={"Retry-After": "30"}), httpx.Response(200, json={"ok": 1})])
    client, waits = gemini(lambda r: next(responses), monkeypatch)
    async with client:
        assert await embeddings._post(client, "https://gemini/embed", {}) == {"ok": 1}
    assert waits == [30.0]


async def test_a_document_batch_facing_a_long_wait_is_deferred_not_held(monkeypatch):
    limiter = Limiter(600, 0.1)
    limiter.pause(45)
    with pytest.raises(RateLimited):
        await limiter.acquire(10, query=False)
    await limiter.acquire(10, query=True)  # a query goes through


def test_the_limiter_works_on_every_event_loop():
    """Evals run each suite on its own event loop; a lock bound to the first loop would raise on the next."""
    import asyncio

    limiter = Limiter(60, 0.0)

    async def contend():
        await asyncio.gather(*(limiter.acquire(1, query=True) for _ in range(3)))

    asyncio.run(contend())
    asyncio.run(contend())


async def test_other_errors_are_not_rate_limits(monkeypatch):
    async def sleep(seconds):
        pass

    monkeypatch.setattr(embeddings.asyncio, "sleep", sleep)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={}))) as client:
        with pytest.raises(RuntimeError) as e:
            await embeddings._post(client, "https://gemini/embed", {})
    assert not isinstance(e.value, RateLimited)


async def test_the_inference_client_turns_a_429_into_rate_limited(monkeypatch):
    real = httpx.AsyncClient

    def client(**kwargs):
        transport = httpx.MockTransport(lambda r: httpx.Response(429, text="quota", headers={"Retry-After": "9"}))
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(inference.httpx, "AsyncClient", client)
    monkeypatch.setattr(config, "INFERENCE_URL", "http://inference")
    with pytest.raises(RateLimited) as e:
        await inference._call("/embed", {})
    assert e.value.retry_after == 9.0
