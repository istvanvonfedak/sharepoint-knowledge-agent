"""Extracts and chunks one file and prints the outcome as one line of JSON. extraction.py runs it in its own
process per file, so a parser that hangs, eats memory or crashes the interpreter takes only itself down.

    python -m sharepoint_evals.extract_worker <path> <name> [--rows] [--title=<title>]

With --rows the outcome carries the chunk rows themselves, for store.py to index. The chunks are what the
indexer stores (`index_store.chunk_rows`). Anything other than ExtractionError escaping extract() is a bug (extract
promises one except clause), so it is not caught here: the process exits non-zero with the traceback, and the
eval reports a crash.
"""
import json
import os
import sys
from dataclasses import astuple
from pathlib import Path

from sharepoint_rag.extract import ExtractionError
from sharepoint_rag.index_store import chunk_rows


def outcome(name: str, data: bytes, rows: bool = False, title: str | None = None) -> dict:
    try:
        chunks = chunk_rows(name, data, title)
    except ExtractionError as e:  # UnsupportedFileType included; the class name tells them apart
        return {"outcome": type(e).__name__, "message": str(e)[:300]}
    if not chunks:
        return {"outcome": "no_text"}
    return {"outcome": "chunks", "chunks": len(chunks), "chars": sum(len(c.text) for c in chunks),
            **({"rows": [astuple(c) for c in chunks]} if rows else {})}


if __name__ == "__main__":
    if sys.platform == "linux" and (limit := int(os.environ.get("EVAL_EXTRACT_MEMORY_BYTES", "0"))):
        import resource  # RLIMIT_AS is honoured on Linux only; virtual > resident, so twice the limit as headroom
        resource.setrlimit(resource.RLIMIT_AS, (limit * 2, limit * 2))
    title = next((a.split("=", 1)[1] for a in sys.argv[3:] if a.startswith("--title=")), None)
    print(json.dumps(outcome(sys.argv[2], Path(sys.argv[1]).read_bytes(), "--rows" in sys.argv[3:], title)))
