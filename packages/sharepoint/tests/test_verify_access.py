import httpx
import pytest
from sharepoint_client import graph


def install_fake_graph(monkeypatch, status_for):
    """Replaces the HTTP client so POST /$batch answers with status_for(drive, item, attempt)."""
    calls = {"batches": 0, "attempts": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        calls["batches"] += 1
        responses = []
        for r in json.loads(request.content)["requests"]:
            drive, item = r["url"].split("/")[2], r["url"].split("/")[4].split("?")[0]
            n = calls["attempts"][(drive, item)] = calls["attempts"].get((drive, item), 0) + 1
            responses.append({"id": r["id"], "status": status_for(drive, item, n)})
        return httpx.Response(200, json={"responses": responses})

    real = httpx.AsyncClient
    monkeypatch.setattr(graph.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(graph.asyncio, "sleep", lambda _: _noop())
    return calls


async def _noop():
    return None


async def test_only_200_is_access(monkeypatch):
    statuses = {"ok": 200, "gone": 404, "forbidden": 403}
    install_fake_graph(monkeypatch, lambda d, i, n: statuses[i])
    allowed = await graph.verify_access("t", [("d", "ok"), ("d", "gone"), ("d", "forbidden")])
    assert allowed == {("d", "ok")}


async def test_batches_of_20_and_dedupes(monkeypatch):
    calls = install_fake_graph(monkeypatch, lambda d, i, n: 200)
    items = [("d", f"i{n}") for n in range(45)] + [("d", "i0")]
    assert len(await graph.verify_access("t", items)) == 45
    assert calls["batches"] == 3  # 20 + 20 + 5


async def test_transient_errors_retry_once_then_fail_closed(monkeypatch):
    calls = install_fake_graph(monkeypatch, lambda d, i, n: {"flaky": 200 if n > 1 else 429, "down": 503}[i])
    allowed = await graph.verify_access("t", [("d", "flaky"), ("d", "down")])
    assert allowed == {("d", "flaky")}  # recovered on retry; persistent 503 denied
    assert calls["attempts"][("d", "down")] == 2


async def test_bad_token_raises(monkeypatch):
    install_fake_graph(monkeypatch, lambda d, i, n: 401)
    with pytest.raises(PermissionError):
        await graph.verify_access("t", [("d", "x")])


async def test_empty_input(monkeypatch):
    calls = install_fake_graph(monkeypatch, lambda d, i, n: 200)
    assert await graph.verify_access("t", []) == set() and calls["batches"] == 0


def install_handler(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(graph.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(graph.asyncio, "sleep", lambda _: _noop())


async def test_batch_401_is_sign_in_again(monkeypatch):
    install_handler(monkeypatch, lambda req: httpx.Response(401, json={"error": "expired"}))
    with pytest.raises(PermissionError):
        await graph.verify_access("t", [("d", "x")])


async def test_throttled_batch_call_is_retried(monkeypatch):
    import json
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"responses": [{"id": r["id"], "status": 200} for r in json.loads(req.content)["requests"]]})

    install_handler(monkeypatch, handler)
    assert await graph.verify_access("t", [("d", "x")]) == {("d", "x")} and seen["n"] == 2


async def test_missing_sub_response_fails_closed(monkeypatch):
    import json

    def handler(req):  # Graph only ever answers for item "a", even when asked about "b"
        asked = json.loads(req.content)["requests"]
        return httpx.Response(200, json={"responses": [{"id": r["id"], "status": 200} for r in asked if "/items/a?" in r["url"]]})

    install_handler(monkeypatch, handler)
    assert await graph.verify_access("t", [("d", "a"), ("d", "b")]) == {("d", "a")}
