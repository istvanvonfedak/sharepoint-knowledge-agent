"""Thin Microsoft Graph client. All calls run as the signed-in user."""
import io
import re
import zipfile
from urllib.parse import quote
import defusedxml.ElementTree as ElementTree
import httpx

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
