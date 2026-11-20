"""sharepoint_ask's code interpreter: the files the user may read, in a sandbox, for the agent's `run` tool
(docs/code-interpreter.md).

The mcp-server never runs model-written commands itself: they go to a `Sandbox` backend. `LocalSandbox` is the
sandbox service (apps/sandbox); an Azure Container Apps dynamic sessions backend can take its place, as its three
operations are the same. A session's id is as good as its files, so it stays here: it is never shown to the model or
the MCP client, and no error raised here contains it (an HTTP error's message would, through the URL).
"""
import bisect
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sharepoint_rag.chunk import rejoin_spans

FOLDER = "/work"  # where the files are, as commands see them
UPLOAD_FILES, UPLOAD_BYTES = 100, 32 * 2**20  # per upload request; more go in several


class SandboxError(Exception):
    """The sandbox failed or refused; the message is safe to show the model."""


@dataclass
class Execution:
    output: str  # stdout and stderr, interleaved, as far as the sandbox kept it
    omitted_bytes: int  # output past what the sandbox kept
    exit_code: int | None
    timed_out: bool


class Sandbox(Protocol):
    async def upload(self, session_id: str, files: dict[str, str]) -> None: ...

    async def execute(self, session_id: str, command: str, timeout: float) -> Execution: ...

    async def delete(self, session_id: str) -> None: ...


class LocalSandbox:
    """The sandbox service (apps/sandbox), over HTTP with a shared key."""

    def __init__(self, url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60,
                                         transport=transport)

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise SandboxError(f"The sandbox could not be reached ({type(e).__name__})") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):  # a proxy's error page can be any JSON
            data = {}
        if response.status_code >= 400:
            raise SandboxError(f"The sandbox refused the request ({response.status_code}): {str(data.get('error', ''))[:300]}")
        return data

    async def upload(self, session_id: str, files: dict[str, str]) -> None:
        batch, size = [], 0
        for name, content in files.items():
            n = len(content.encode())
            if batch and (len(batch) == UPLOAD_FILES or size + n > UPLOAD_BYTES):
                await self._call("POST", f"/sessions/{session_id}/files", {"files": batch})
                batch, size = [], 0
            batch.append({"name": name, "content": content})
            size += n
        if batch:
            await self._call("POST", f"/sessions/{session_id}/files", {"files": batch})

    async def execute(self, session_id: str, command: str, timeout: float) -> Execution:
        data = await self._call("POST", f"/sessions/{session_id}/execute", {"command": command, "timeout": timeout})
        return Execution(str(data.get("output", "")), int(data.get("omitted_bytes", 0)), data.get("exit_code"),
                         bool(data.get("timed_out")))

    async def delete(self, session_id: str) -> None:
        await self._call("DELETE", f"/sessions/{session_id}")


@dataclass
class WorkFile:
    """One file as the sandbox holds it: its sections in file order, each under a marker line."""

    sections: list[tuple[str | None, str]]  # (heading_path, text), the chunks' repeated overlap removed
    lines: dict[int, tuple[int, int]] = field(default_factory=dict)  # chunk ordinal -> its first and last line

    @classmethod
    def from_chunks(cls, hits: Iterable) -> "WorkFile":
        hits = list(hits)
        sections, spans = rejoin_spans([(h.heading_path, h.text) for h in hits])
        # The line each section's text starts on, in `text` (1-based, as sed and rg number them).
        first, line = [], 1
        for heading, text in sections:
            line += 1 if heading else 0
            first.append(line)
            line += text.count("\n") + 1
        breaks = [[m.start() for m in re.finditer("\n", text)] for _, text in sections]
        lines = {}
        for h, (i, start, end) in zip(hits, spans):
            lines[h.ordinal] = (first[i] + bisect.bisect_left(breaks[i], start),
                                first[i] + bisect.bisect_left(breaks[i], max(start, end - 1)))
        return cls(sections, lines)

    @property
    def text(self) -> str:
        return "".join((f"{marker(h)}\n" if h else "") + f"{t}\n" for h, t in self.sections)


def marker(heading: str) -> str:
    return f"=== {heading} ==="


def manifest(rows: Iterable[tuple[str, str]]) -> str:
    """manifest.tsv: source_id and title per file, one line each. No links: the model can't use them, and every
    character it reads is paid for again on each later step."""
    clean = lambda s: " ".join(s.split())  # a title can't break the table
    return "source_id\ttitle\n" + "".join(f"{clean(i)}\t{clean(t)}\n" for i, t in rows)
