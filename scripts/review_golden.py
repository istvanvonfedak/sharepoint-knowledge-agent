"""A small web app for reviewing the golden set: read each question, browse the documents its evidence names, and
jump straight to the page, the quoted text and any bounding box.

    python3 scripts/review_golden.py              # http://127.0.0.1:8765
    python3 scripts/review_golden.py --port 9000

It reads questions.jsonl, evals/golden/docs/ and the files evals/golden/external.json keeps under data/, and writes
back to questions.jsonl only when you save an item: that item's line is replaced, every other line is left as it is.
Standard library only; the page loads pdf.js from a CDN.
"""
import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evals" / "golden"
DATA = ROOT / "data"
STATUSES = {"draft", "reviewed", "retired"}
BUCKETS = ("inputs", "expected_outputs", "metadata")
PAGE = Path(__file__).with_name("review_golden.html")


def read_items() -> list[dict]:
    return [json.loads(line) for line in (GOLDEN / "questions.jsonl").read_text("utf-8").splitlines() if line.strip()]


def locate(name: str) -> Path | None:
    """The file behind a document name items use: in golden/docs, or `<source>/<path>` from the external manifest."""
    candidates = [GOLDEN / "docs" / name]
    manifest = GOLDEN / "external.json"
    if manifest.exists():
        for source, spec in json.loads(manifest.read_text("utf-8"))["sources"].items():
            if name.startswith(source + "/"):
                candidates.append(DATA / spec["dir"] / name[len(source) + 1:])
    for path in candidates:
        resolved = path.resolve()
        if resolved.is_file() and any(resolved.is_relative_to(base.resolve()) for base in (GOLDEN / "docs", DATA)):
            return resolved
    return None


def payload() -> dict:
    items = read_items()
    names = {n for i in items for n in doc_names(i)}
    return {"items": items, "docs": {n: locate(n) is not None for n in sorted(names)}, "me": git_user()}


def git_user() -> str:
    """Who is reviewing, to prefill the reviewer: the repo's git user.name."""
    try:
        return subprocess.run(["git", "config", "user.name"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def check(item_id: str, item) -> str | None:
    """Why an edited item can't be saved, or None. Not the full validation (golden.validate): the shape, and the
    review rule that a reviewed item needs a reviewer other than its author."""
    if not isinstance(item, dict) or set(item) != set(BUCKETS) or not all(isinstance(item[b], dict) for b in BUCKETS):
        return f"an item is an object with exactly {', '.join(BUCKETS)}, each an object"
    meta = item["metadata"]
    if meta.get("id") != item_id:
        return "the id can't be changed"
    if not isinstance(item["inputs"].get("question"), str) or not item["inputs"]["question"].strip():
        return "inputs.question must be a non-empty string"
    if meta.get("status") not in STATUSES:
        return f"status must be one of {sorted(STATUSES)}"
    if meta["status"] == "reviewed" and (not meta.get("reviewer") or meta["reviewer"] == meta.get("author")):
        return f"a reviewed item needs a reviewer other than its author ({meta.get('author') or 'unknown'}): type your name in the reviewer box"
    return None


class Changed(Exception):
    """The item on disk is no longer the one the page was showing."""


def save(item_id: str, item: dict, base: dict) -> None:
    """Replace the item's line, leaving the others byte for byte, through a temporary file so a crash can't truncate.
    `base` is the item as the page loaded it: if the line has changed since (an edit made elsewhere), nothing is
    written, so a stale page can't overwrite it."""
    path = GOLDEN / "questions.jsonl"
    lines = path.read_text("utf-8").split("\n")
    for n, line in enumerate(lines):
        if line.strip() and json.loads(line)["metadata"]["id"] == item_id:
            if json.loads(line) != base:
                raise Changed(item_id)
            lines[n] = json.dumps(item, ensure_ascii=False)
            break
    else:
        raise KeyError(item_id)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write("\n".join(lines))
    os.replace(temp, path)


def doc_names(item: dict) -> list[str]:
    out = item["expected_outputs"]
    names = [e.get("doc") for e in out.get("evidence") or []]
    for field in ("sources", "also_acceptable", "must_not_see"):
        names += out.get(field, [])
    return [n for n in dict.fromkeys(names) if isinstance(n, str)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status: int, body: bytes, kind: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif url.path == "/api/items":
                self.send(200, json.dumps(payload()).encode(), "application/json", {"Cache-Control": "no-store"})
            elif url.path == "/file":
                self.file(parse_qs(url.query).get("doc", [""])[0])
            else:
                self.send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        port = self.server.server_address[1]
        # Only this page may write: a page on another site can't reach a local server by guessing its port.
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"} or self.headers.get("Origin") not in {None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return self.send(403, b"forbidden", "text/plain")
        if urlparse(self.path).path != "/api/item":
            return self.send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            problem = check(body["id"], body["item"])
            if problem:
                return self.send(400, problem.encode(), "text/plain; charset=utf-8")
            save(body["id"], body["item"], body["base"])
        except Changed:
            return self.send(409, b"this item was changed on disk after the page loaded it; the page has been refreshed, so check it and save again", "text/plain; charset=utf-8")
        except (ValueError, KeyError, TypeError) as err:
            return self.send(400, f"could not save: {err!r}".encode(), "text/plain; charset=utf-8")
        self.send(200, json.dumps(payload()).encode(), "application/json")

    def file(self, name: str):
        path = locate(name)
        if path is None:
            return self.send(404, b"that document is not on disk (see scripts/fetch_golden_files.py)", "text/plain")
        data = path.read_bytes()
        kind = "text/plain; charset=utf-8" if path.suffix in {".md", ".txt"} else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and (match[1] or match[2]):
            start = int(match[1]) if match[1] else max(len(data) - int(match[2]), 0)
            end = min(int(match[2]), len(data) - 1) if match[1] and match[2] else len(data) - 1
            return self.send(206, data[start:end + 1], kind, {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
        self.send(200, data, kind)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"reviewing {len(read_items())} golden items at http://127.0.0.1:{args.port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
