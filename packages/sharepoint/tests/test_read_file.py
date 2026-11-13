import httpx
from sharepoint_client import graph


async def test_text_file_read_sends_range_and_auth_headers(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):
            seen["range"], seen["auth"] = request.headers.get("range"), request.headers.get("authorization")
            return httpx.Response(200, content=b"hello world")
        return httpx.Response(200, json={"name": "a.txt", "size": 11, "file": {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(graph.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    result = await graph.read_file_text("tok", "d", "i", 100)

    assert result == {"name": "a.txt", "truncated": False, "text": "hello world"}
    assert seen == {"range": "bytes=0-100", "auth": "Bearer tok"}
