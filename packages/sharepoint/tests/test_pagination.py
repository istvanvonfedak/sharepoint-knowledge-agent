import httpx
from sharepoint_client import graph


def _serve(monkeypatch, pages: int):
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", 1))
        body = {"value": [{"id": n}]}
        if n < pages:
            body["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0/x?page={n + 1}"
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(graph.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_all_pages_returned_without_marker_when_under_the_cap(monkeypatch):
    _serve(monkeypatch, pages=graph.MAX_PAGES)
    items = await graph._get_all("tok", "/x")
    assert [i["id"] for i in items] == list(range(1, graph.MAX_PAGES + 1))


async def test_truncation_is_reported_when_more_pages_exist(monkeypatch):
    _serve(monkeypatch, pages=graph.MAX_PAGES + 1)
    items = await graph._get_all("tok", "/x")
    assert len(items) == graph.MAX_PAGES + 1
    assert items[-1]["truncated"] is True
