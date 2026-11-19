"""extraction_robustness (regression): the extractor against ~400 real files (Govdocs1, Common Crawl PDFs).

Each file runs in its own process (extract_worker.py) with a time limit and a memory limit. The memory limit is
enforced by watching the worker's resident memory and killing it, because RLIMIT_AS isn't enforced on macOS; on
Linux RLIMIT_AS is set as well. Two scores per file:

- gate (must be 1.0): it finished inside the limits with an expected outcome. A supported type returns chunks,
  returns no text (scanned or image-only; tracked, not a failure) or raises ExtractionError; a password-protected
  PDF raises UnsupportedFileType. Any other type raises UnsupportedFileType. The class is compared exactly, since
  UnsupportedFileType subclasses ExtractionError.
- snapshot (no regressions): the committed per-file outcomes in evals/snapshots/extraction_robustness.json. A file
  that gave chunks still does, one that gave no text doesn't now fail, and a failing file fails with the same
  message. extract() turns every parser crash into ExtractionError, so a new message is how a new crash shows.
  `run.py regression --update-snapshot` rewrites it after a reviewed change.

Needs no embeddings, so it is the fastest public suite.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import psutil
from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver
from sharepoint_rag.extract import EXTENSIONS

from sharepoint_evals.common import MOCK, set_output
from sharepoint_evals.corpus import load

SUITE = "extraction_robustness"
SNAPSHOT = Path(__file__).resolve().parents[1] / "snapshots" / f"{SUITE}.json"
TIME_LIMIT = float(os.environ.get("EVAL_EXTRACT_SECONDS", "60"))
MEMORY_LIMIT = int(os.environ.get("EVAL_EXTRACT_MEMORY_MB", "1024")) * 2**20
POLL = 0.05


async def run_isolated(path: Path, name: str, *, rows: bool = False, time_limit: float | None = None,
                       title: str | None = None) -> dict:
    """The worker's outcome for one file, or `killed` (over a limit) or `crashed` (died without an outcome)."""
    time_limit = TIME_LIMIT if time_limit is None else time_limit
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "sharepoint_evals.extract_worker", str(path), name, *(["--rows"] if rows else []),
        *([f"--title={title}"] if title is not None else []),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "EVAL_EXTRACT_MEMORY_BYTES": str(MEMORY_LIMIT)},  # the worker sets RLIMIT_AS from it
    )
    started, peak, killed = time.monotonic(), 0, None
    try:
        watched = psutil.Process(proc.pid)
    except psutil.Error:  # already exited
        watched = None
    communicate = asyncio.ensure_future(proc.communicate())
    while not communicate.done():
        try:
            peak = max(peak, watched.memory_info().rss) if watched else peak
        except psutil.Error:  # exited between checks
            pass
        if peak > MEMORY_LIMIT:
            killed = "memory"
        elif time.monotonic() - started > time_limit:
            killed = "time"
        if killed:
            proc.kill()
            break
        await asyncio.wait([communicate], timeout=POLL)
    stdout, stderr = await communicate
    seconds = round(time.monotonic() - started, 2)
    stats = {"seconds": seconds, "peak_mb": round(peak / 2**20)}
    if killed:
        return {"outcome": "killed", "message": f"over the {killed} limit", **stats}
    if proc.returncode != 0:
        last = (stderr.decode(errors="replace").strip().splitlines() or [f"exit code {proc.returncode}"])[-1]
        return {"outcome": "crashed", "message": last[:300], **stats}
    try:  # the outcome is the last line: a parser may print to stdout before it
        return {**json.loads(stdout.decode(errors="replace").strip().splitlines()[-1]), **stats}
    except (ValueError, IndexError):
        return {"outcome": "crashed", "message": f"no outcome on stdout: {stdout[-200:]!r}", **stats}


def expected(ext: str, result: dict) -> bool:
    if ext not in EXTENSIONS:
        return result["outcome"] == "UnsupportedFileType"
    if result["outcome"] == "UnsupportedFileType":
        return result.get("message") == "password-protected pdf"
    return result["outcome"] in ("chunks", "no_text", "ExtractionError")


def regression(before: dict | None, now: dict, sha256: str) -> str | None:
    """Why `now` is worse than the snapshot, or None."""
    if before is None:
        return None
    if before.get("sha256") != sha256:
        return "the file changed since the snapshot was taken (rebuild of data/? refresh it with --update-snapshot)"
    if before["outcome"] == "chunks" and now["outcome"] != "chunks":
        return f"extracted before, now {now['outcome']}: {now.get('message', '')}"
    if before["outcome"] == "no_text" and now["outcome"] not in ("chunks", "no_text"):
        return f"gave no text before, now {now['outcome']}: {now.get('message', '')}"
    if before["outcome"] not in ("chunks", "no_text") and now["outcome"] not in ("chunks", "no_text") and \
            (before["outcome"], before.get("message")) != (now["outcome"], now.get("message")):
        return f"failed differently: was {before['outcome']}: {before.get('message')}; now {now['outcome']}: {now.get('message')}"
    return None


def snapshot_entry(sha256: str, result: dict) -> dict:
    return {"sha256": sha256, "outcome": result["outcome"],
            **({"message": result["message"]} if result["outcome"] not in ("chunks", "no_text") else {}),
            **({"chunks": result["chunks"]} if result["outcome"] == "chunks" else {})}


def read_snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text("utf-8")) if SNAPSHOT.exists() else {}


@solver
def extract_isolated():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        m = state.metadata
        doc = load(SUITE, allow_quick=m["allow_quick"]).by_name[m["doc"]]
        m["result"] = await run_isolated(doc.path, doc.name)
        set_output(state, json.dumps(m["result"]))
        return state

    return solve


@scorer(metrics=[accuracy()])
def gate():
    async def score(state: TaskState, target: Target) -> Score:
        m = state.metadata
        ok = expected(m["extension"], m["result"])
        want = "chunks, no text or ExtractionError" if m["extension"] in EXTENSIONS else "UnsupportedFileType"
        return Score(value=CORRECT if ok else INCORRECT,
                     explanation=f"{m['result']['outcome']}: {m['result'].get('message', '')}" + ("" if ok else f" (expected {want})"))

    return score


@scorer(metrics=[accuracy()])
def snapshot():
    async def score(state: TaskState, target: Target) -> Score:
        m = state.metadata
        why = regression(m["before"], m["result"], m["sha256"])
        return Score(value=INCORRECT if why else CORRECT, explanation=why or ("new file" if m["before"] is None else "unchanged or better"))

    return score


@task
def extraction_robustness(allow_quick: bool = False) -> Task:
    corpus = load(SUITE, allow_quick=allow_quick)
    before = read_snapshot()
    samples = [
        Sample(id=d.name, input=d.name, target=d.meta["expect"],
               metadata={"doc": d.name, "extension": d.meta["extension"], "sha256": d.meta["sha256"],
                         "before": before.get(d.name), "allow_quick": allow_quick})
        for d in corpus.docs
    ]
    return Task(dataset=MemoryDataset(samples), solver=extract_isolated(), scorer=[gate(), snapshot()], model=MOCK,
                metadata={"suite": SUITE, "quick": corpus.quick})
