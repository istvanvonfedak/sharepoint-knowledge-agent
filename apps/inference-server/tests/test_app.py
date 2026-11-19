import os

from starlette.testclient import TestClient

os.environ.setdefault("INFERENCE_ALLOW_ANONYMOUS", "true")
from inference_server import app as server  # noqa: E402


def client(monkeypatch, key=""):
    monkeypatch.setattr(server, "API_KEY", key)
    return TestClient(server.app)


def test_embed_calls_the_backend(monkeypatch):
    async def embed(model, texts, task, dimensions):
        return [[float(len(t))] * dimensions for t in texts]

    monkeypatch.setattr(server.models, "embed", embed)
    res = client(monkeypatch).post("/embed", json={"model": "gemini:m", "texts": ["ab"], "task": "query", "dimensions": 2})
    assert res.status_code == 200 and res.json() == {"vectors": [[2.0, 2.0]]}


def test_api_key_is_required_when_set(monkeypatch):
    c = client(monkeypatch, "s3cret")
    body = {"model": "m", "texts": ["x"], "task": "query", "dimensions": 2}
    assert c.post("/embed", json=body).status_code == 401
    assert c.post("/embed", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_invalid_requests_are_rejected(monkeypatch):
    c = client(monkeypatch)
    assert c.post("/embed", json={"model": "m", "texts": ["x"], "task": "summarize", "dimensions": 2}).status_code == 400
    assert c.post("/embed", json={"model": "m", "texts": ["x" * 30_000], "task": "query", "dimensions": 2}).status_code == 400


def test_health(monkeypatch):
    assert client(monkeypatch).get("/health").json() == {"ok": True}


def test_a_rate_limit_is_a_429_with_retry_after(monkeypatch):
    from sharepoint_rag.embeddings import RateLimited

    async def embed(model, texts, task, dimensions):
        raise RateLimited("Gemini 429: quota", retry_after=4.2)

    monkeypatch.setattr(server.models, "embed", embed)
    res = client(monkeypatch).post("/embed", json={"model": "m", "texts": ["x"], "task": "document", "dimensions": 2})
    assert res.status_code == 429 and res.headers["Retry-After"] == "5"
