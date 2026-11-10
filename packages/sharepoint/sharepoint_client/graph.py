"""Thin Microsoft Graph client. All calls run as the signed-in user."""
import asyncio
import io
import logging
import re
import zipfile
from urllib.parse import quote
import defusedxml.ElementTree as ElementTree
import httpx

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".json", ".xml", ".htm", ".html", ".log")


def _seg(value: str) -> str:
    """Percent-encode an id for use as a single URL path segment."""
    return quote(value, safe="")


MAX_DOCX_XML_BYTES = 50_000_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(p) -> str:
    """Text of one paragraph; nested paragraphs (text boxes) are emitted separately, not duplicated."""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node:
            if child.tag == f"{_W}p":
                continue
            if child.tag == f"{_W}t":
                parts.append(child.text or "")
            elif child.tag == f"{_W}tab":
                parts.append("\t")
            elif child.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
            walk(child)

    walk(p)
    return "".join(parts)


def _docx_text(data: bytes) -> str:
    """Plain text of a .docx, paragraphs in document order."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if z.getinfo("word/document.xml").file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("Word document body is too large to read.")
        root = ElementTree.fromstring(z.read("word/document.xml"))
    return "\n".join(_paragraph_text(p) for p in root.iter(f"{_W}p"))


async def _get(token: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        res = await client.get(path if path.startswith("http") else f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if res.is_error:
        raise RuntimeError(f"Graph {res.status_code}: {res.text}")
    return res


MAX_PAGES = 10


async def _get_all(token: str, path: str, **kwargs) -> list[dict]:
    """Follows @odata.nextLink, capped at MAX_PAGES pages."""
    items: list[dict] = []
    for _ in range(MAX_PAGES):
        body = (await _get(token, path, **kwargs)).json()
        items += body["value"]
        path, kwargs = body.get("@odata.nextLink"), {}
        if not path:
            break
    return items


async def me(token: str) -> dict:
    return (await _get(token, "/me", params={"$select": "displayName,userPrincipalName"})).json()


async def find_sites(token: str, query: str) -> list[dict]:
    return await _get_all(token, "/sites", params={"search": query, "$select": "id,displayName,webUrl"})


async def list_drives(token: str, site_id: str) -> list[dict]:
    return await _get_all(token, f"/sites/{_seg(site_id)}/drives", params={"$select": "id,name,webUrl"})


async def list_children(token: str, drive_id: str, item_id: str | None) -> list[dict]:
    base = f"/drives/{_seg(drive_id)}/items/{_seg(item_id)}" if item_id else f"/drives/{_seg(drive_id)}/root"
    return await _get_all(
        token,
        f"{base}/children",
        params={"$select": "id,name,size,webUrl,file,folder,lastModifiedDateTime"},
    )


async def search_content(token: str, query: str, size: int) -> list[dict]:
    """Microsoft Search results are security-trimmed to what the signed-in user can access."""
    body = {
        "requests": [
            {"entityTypes": ["driveItem", "listItem"], "query": {"queryString": query}, "from": 0, "size": size}
        ]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(f"{GRAPH}/search/query", json=body, headers={"Authorization": f"Bearer {token}"})
    if res.is_error:
        raise RuntimeError(f"Graph {res.status_code}: {res.text}")
    hits = [
        hit
        for response in res.json().get("value", [])
        for container in response.get("hitsContainers", [])
        for hit in container.get("hits", [])
    ]
    return [
        {
            "name": h["resource"].get("name"),
            "webUrl": h["resource"].get("webUrl"),
            "driveId": h["resource"].get("parentReference", {}).get("driveId"),
            # Only driveItems have an id usable with sharepoint_read_file; list items are link-only.
            "itemId": h["resource"].get("id") if "file" in h["resource"] or "folder" in h["resource"] else None,
            "summary": h.get("summary"),
        }
        for h in hits
    ]


async def read_file_text(token: str, drive_id: str, item_id: str, max_bytes: int) -> dict:
    meta = (await _get(token, f"/drives/{_seg(drive_id)}/items/{_seg(item_id)}", params={"$select": "name,size,file"})).json()
    if "file" not in meta:
        raise ValueError("Item is not a file")
    mime = meta["file"].get("mimeType", "")
    is_text = mime.startswith("text/") or mime in ("application/json", "application/xml") or mime.endswith(("+json", "+xml"))
    is_docx = meta["name"].lower().endswith(".docx")
    if not (is_text or is_docx or meta["name"].lower().endswith(TEXT_EXTENSIONS)):
        raise ValueError(f"Unsupported file type ({mime}); only text-based files and .docx can be read inline.")
    if is_docx:
        # A .docx is a zip, so it can't be range-truncated; limit by the size reported by Graph instead.
        if meta.get("size", 0) > 20_000_000:
            raise ValueError("Word document is too large to read (over 20 MB).")
        res = await _get(token, f"/drives/{_seg(drive_id)}/items/{_seg(item_id)}/content")
        text = re.sub(r"\n{3,}", "\n\n", _docx_text(res.content))
        return {"name": meta["name"], "truncated": len(text) > max_bytes, "text": text[:max_bytes]}
    if meta.get("size", 0) == 0:
        return {"name": meta["name"], "truncated": False, "text": ""}
    # Range avoids buffering huge files; Graph's content redirect honours it.
    res = await _get(
        token, f"/drives/{_seg(drive_id)}/items/{_seg(item_id)}/content", headers={"Range": f"bytes=0-{max_bytes}"}
    )
    data = res.content
    return {
        "name": meta["name"],
        "truncated": len(data) > max_bytes or meta.get("size", 0) > max_bytes,
        "text": data[:max_bytes].decode("utf-8", errors="ignore"),  # ignore: a cut multi-byte tail is dropped
    }


BATCH_SIZE = 20  # Graph JSON batching limit
MAX_RETRY_WAIT = 10.0  # cap on honouring Retry-After
_DENIED = {403, 404}  # forbidden, or gone/hidden: either way the user cannot open it
_TRANSIENT = {0, 429, 500, 502, 503, 504}  # 0 = Graph returned no response for the item


def _retry_wait(retry_after: str | None, default: float = 1.0) -> float:
    return min(float(retry_after), MAX_RETRY_WAIT) if retry_after and retry_after.isdigit() else default


async def _post_batch(client: httpx.AsyncClient, token: str, body: dict) -> dict:
    """POST /$batch, retrying throttling/5xx on the batch call itself. A 401 means the user's token is bad."""
    for attempt in range(3):
        res = await client.post(f"{GRAPH}/$batch", json=body, headers={"Authorization": f"Bearer {token}"})
        if res.status_code == 401:
            raise PermissionError("Microsoft Graph rejected the user's token; sign in again")
        if res.status_code in _TRANSIENT and attempt < 2:
            await asyncio.sleep(_retry_wait(res.headers.get("Retry-After"), 2**attempt))
            continue
        if res.is_error:
            raise RuntimeError(f"Graph {res.status_code}: {res.text}")
        return res.json()
    raise AssertionError("unreachable")


async def _batch_status(
    client: httpx.AsyncClient, token: str, items: list[tuple[str, str]]
) -> tuple[dict[tuple[str, str], int], float]:
    """Per-item HTTP status (0 if Graph omitted it) and the longest Retry-After seen in the sub-responses."""
    body = {
        "requests": [
            {"id": str(i), "method": "GET", "url": f"/drives/{_seg(d)}/items/{_seg(it)}?$select=id"}
            for i, (d, it) in enumerate(items)
        ]
    }
    responses = {r["id"]: r for r in (await _post_batch(client, token, body))["responses"]}
    statuses = {item: responses.get(str(i), {}).get("status", 0) for i, item in enumerate(items)}
    wait = max((_retry_wait(r.get("headers", {}).get("Retry-After")) for r in responses.values() if r.get("status") in _TRANSIENT), default=1.0)
    return statuses, wait


async def _check_all(client: httpx.AsyncClient, token: str, items: list[tuple[str, str]]) -> tuple[dict[tuple[str, str], int], float]:
    chunks = [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    results = await asyncio.gather(*(_batch_status(client, token, c) for c in chunks))
    statuses: dict[tuple[str, str], int] = {}
    for part, _ in results:
        statuses.update(part)
    return statuses, max((w for _, w in results), default=1.0)


async def verify_access(token: str, items: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which (drive_id, item_id) pairs can the token's user open *right now*?

    Layer 2 of permission enforcement: the index is a snapshot, so every hit is re-checked against
    SharePoint as the user. Fails closed: only a 200 counts as access. 403/404 deny; throttled, failed
    or missing sub-responses are retried once (honouring Retry-After), then deny and are logged rather
    than risk showing a file. A 401 raises PermissionError since no answer can be trusted.
    """
    unique = list(dict.fromkeys(items))
    async with httpx.AsyncClient(timeout=30) as client:
        statuses, wait = await _check_all(client, token, unique)
        retry = [i for i in unique if statuses[i] in _TRANSIENT]
        if retry:
            await asyncio.sleep(wait)
            retried, _ = await _check_all(client, token, retry)
            statuses.update(retried)
    if any(s == 401 for s in statuses.values()):
        raise PermissionError("Microsoft Graph rejected the user's token; sign in again")
    if unknown := [i for i in unique if statuses[i] != 200 and statuses[i] not in _DENIED]:
        log.warning("access check failed closed for %d item(s): statuses %s", len(unknown), {statuses[i] for i in unknown})
    return {i for i in unique if statuses[i] == 200}
