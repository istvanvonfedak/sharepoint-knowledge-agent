"""Runs `sharepoint_ask` as production does (`agent.ask`, the configured ASK_MODEL and budgets), with its tools bound
to a suite's eval index instead of a tenant.

- search and read_around: `PostgresRetriever` over the suite's eval database, with the persona's principals and a
  live check that answers from the dataset's permission lists (what server.search_index and server.nearby_index
  do, minus the Graph lookups).
- with ASK_SANDBOX=local, search also gives the agent's sandbox the files of its top candidates, and read_files
  their whole text, from the same index and permissions (recorded as a "sandbox" call).
- read_file and list_items: production's own `graph.read_file_text` and `graph.list_children`, with Graph's HTTP
  layer (`graph._get`) answered from the suite's files. So the same rules apply as live: text-based files and
  .docx only, cut to MAX_FILE_CHARS, and any other type, PDFs included, fails as it does live. The library is
  flat (every file the persona can open sits in one folder), and a file the persona can't open is a 404.

Every tool call is recorded (`Run.calls`), with the text each source returned, so scorers can check what the
agent looked at, and a grader can see the cited sources' text.
"""
import asyncio
import json
import mimetypes
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote

import httpx
from sharepoint_client import graph

from sharepoint_evals import store
from sharepoint_evals.corpus import Corpus
from sharepoint_evals.retrieval import allow, pool
from sharepoint_rag.retriever import PostgresRetriever

FOLDER = "eval-folder"
PAGE = 200  # Graph's page size for children; more files come back over several pages, as live


class LocalGraph:
    """Stands in for Graph's HTTP endpoints that read_file_text and list_children call. The bearer token is the
    persona's name; the drive id is the suite's name (as store.py indexes it)."""

    def __init__(self) -> None:
        self.corpora: dict[str, Corpus] = {}

    def register(self, corpus: Corpus) -> None:
        """Serves `corpus` from now on. Graph's HTTP layer is replaced for the whole process, which only runs evals."""
        self.corpora[corpus.name] = corpus
        graph._get = self.get

    def _response(self, status: int, *, json_body=None, content: bytes | None = None) -> httpx.Response:
        request = httpx.Request("GET", "https://graph.invalid/")
        if json_body is not None:
            return httpx.Response(status, json=json_body, request=request)
        return httpx.Response(status, content=content or b"", request=request)

    async def get(self, token: str, path: str, **kwargs) -> httpx.Response:
        path, _, query = path.partition("?")
        parts = path.strip("/").split("/")
        if parts[0] != "drives" or unquote(parts[1]) not in self.corpora:
            raise RuntimeError(f"LocalGraph: unexpected request {path}")
        corpus, persona = self.corpora[unquote(parts[1])], token
        readable = corpus.readable(corpus.personas[persona])
        item = unquote(parts[3]) if len(parts) > 3 else None
        if parts[2:] == ["root", "children"] or (item == FOLDER and parts[4:] == ["children"]):
            start = int(parse_qs(query).get("skip", ["0"])[0])  # our own nextLink, followed by graph._get_all
            names = sorted(readable)
            page = [self._meta(corpus, n) for n in names[start:start + PAGE]]
            body = {"value": page}
            if start + PAGE < len(names):
                body["@odata.nextLink"] = f"/drives/{parts[1]}/items/{FOLDER}/children?skip={start + PAGE}"
            return self._response(200, json_body=body)
        if item is None or item not in readable:
            raise RuntimeError("Graph 404: itemNotFound")  # graph._get raises RuntimeError for any error status
        if len(parts) == 4:
            return self._response(200, json_body=self._meta(corpus, item))
        if parts[4:] == ["content"]:
            data = corpus.by_name[item].read()
            if rng := kwargs.get("headers", {}).get("Range"):
                end = int(rng.split("-", 1)[1])
                data = data[:end + 1]
            return self._response(200, content=data)
        raise RuntimeError(f"LocalGraph: unexpected request {path}")

    def _meta(self, corpus: Corpus, name: str) -> dict:
        doc = corpus.by_name[name]
        mime = mimetypes.guess_type(doc.title)[0] or "application/octet-stream"
        return {"id": name, "name": doc.title, "size": doc.path.stat().st_size, "webUrl": store.web_url(corpus.name, name),
                "file": {"mimeType": mime}, "parentReference": {"id": FOLDER}}


local_graph = LocalGraph()


@dataclass
class Run:
    """One agent run: what it returned, every tool call, and the text each source showed it (by link)."""

    question: str
    persona: str
    result: object = None  # agent.AskResult
    calls: list[dict] = field(default_factory=list)
    texts: dict[str, list[str]] = field(default_factory=dict)  # link -> texts the tools returned for it
    seconds: float = 0.0
    error: str | None = None

    def seen(self, tools=("search", "read_around", "read_file", "sandbox")) -> set[str]:
        """Item ids a search returned or the agent tried to read (a folder listing only shows names), including the
        files written to its sandbox, which it can read whole."""
        return {i for c in self.calls if c["tool"] in tools for i in c.get("items", [])}

    @property
    def output_texts(self) -> list[str]:
        """Everything the caller gets back: the answer and every source's title, link and quote."""
        if self.result is None:
            return []
        return [self.result.answer] + [str(s.get(k, "")) for s in self.result.sources for k in ("title", "link", "quote")]

    def cited(self, corpus: Corpus) -> list[str]:
        """Item ids of the returned sources (final or partial), by link."""
        by_link = {store.web_url(corpus.name, d.name): d.name for d in corpus.docs}
        return [by_link[s["link"]] for s in (self.result.sources if self.result else []) if s.get("link") in by_link]

    def cited_pages(self, corpus: Corpus) -> dict[str, set[int]]:
        """Document -> the pages of it the returned sources were found on (a section label ends in 'Page N')."""
        by_link = {store.web_url(corpus.name, d.name): d.name for d in corpus.docs}
        pages: dict[str, set[int]] = {}
        for s in self.result.sources if self.result else []:
            if (doc := by_link.get(s.get("link"))):
                pages.setdefault(doc, set()).update(int(n) for sec in s.get("sections", []) for n in re.findall(r"Page (\d+)\s*$", sec))
        return pages

    def summary(self) -> dict:
        r = self.result
        return {"status": getattr(r, "status", "error"), "answer": getattr(r, "answer", ""), "sources": getattr(r, "sources", []),
                "steps": getattr(r, "steps", 0), "tokens": getattr(r, "tokens", 0), "seconds": self.seconds,
                "calls": self.calls, "error": self.error}


def tools(corpus: Corpus, persona: str, run: Run):
    """AskTools bound to the persona, recording every call into `run`."""
    from mcp_server import agent

    local_graph.register(corpus)
    principals = corpus.personas[persona]
    readable = corpus.readable(principals)

    def note(link: str | None, text: str | None) -> None:
        if link and text:
            run.texts.setdefault(link, []).append(text)

    async def search(query: str, k: int):
        retriever = PostgresRetriever(await pool(corpus), mode="hybrid")
        hits = await retriever.search(query, principals=principals, verify=allow(readable), k=k)
        run.calls.append({"tool": "search", "query": query, "items": [h.item_id for h in hits]})
        for h in hits:
            note(h.web_url, h.text)
        return hits

    async def read_file(drive_id: str, item_id: str) -> dict:
        entry = {"tool": "read_file", "items": [item_id]}
        run.calls.append(entry)
        try:
            res = await graph.read_file_text(persona, drive_id, item_id, agent.MAX_FILE_CHARS)
        except Exception as e:
            entry["error"] = str(e)[:200]
            raise
        note(res.get("webUrl"), res.get("text"))
        return res

    async def list_items(drive_id: str, item_id: str | None) -> list[dict]:
        children = await graph.list_children(persona, drive_id, item_id)
        run.calls.append({"tool": "list_items", "items": [c["id"] for c in children if "id" in c]})
        return children

    async def nearby(hit, before: int, after: int):
        retriever = PostgresRetriever(await pool(corpus), mode="hybrid")
        hits = await retriever.nearby(hit, principals=principals, verify=allow(readable), before=before, after=after)
        run.calls.append({"tool": "read_around", "items": [h.item_id for h in hits]})
        for h in hits:
            note(h.web_url, h.text)
        return hits

    async def search_files(query: str, k: int):
        retriever = PostgresRetriever(await pool(corpus), mode="hybrid")
        hits, files = await retriever.search_with_files(query, principals=principals, verify=allow(readable), k=k)
        run.calls.append({"tool": "search", "query": query, "items": [h.item_id for h in hits]})
        for h in hits:
            note(h.web_url, h.text)
        return hits, files

    async def read_files(files: list):
        """The whole files written to the sandbox: the agent can read any of them with run."""
        retriever = PostgresRetriever(await pool(corpus), mode="hybrid")
        chunks = await retriever.read_files(files, principals=principals, verify=allow(readable))
        run.calls.append({"tool": "sandbox", "items": [hs[0].item_id for hs in chunks.values()]})
        for hs in chunks.values():
            note(hs[0].web_url, "\n".join(h.text for h in hs))
        return chunks

    return agent.AskTools(search=search, read_file=read_file, list_items=list_items, nearby=nearby,
                          search_files=search_files, read_files=read_files)


_model = None
_sandboxes: dict = {}  # per event loop: its HTTP client belongs to the loop that made it, and evals run several


def sandbox():
    """The sandbox service when ASK_SANDBOX=local (SANDBOX_URL must reach it from where the evals run), else None."""
    from mcp_server import interpreter, settings

    if settings.ASK_SANDBOX != "local":
        return None
    loop = asyncio.get_running_loop()
    if loop not in _sandboxes:
        _sandboxes[loop] = interpreter.LocalSandbox(settings.SANDBOX_URL, settings.SANDBOX_API_KEY)
    return _sandboxes[loop]


def ask_model():
    """The configured ASK_MODEL, created once, as server.py does."""
    global _model
    from mcp_server import settings

    if not settings.ASK_MODEL:
        raise RuntimeError("The agent tier needs ASK_MODEL (or OPENAI_API_KEY for the default model); see .env.mcp.example")
    if _model is None:
        from langchain.chat_models import init_chat_model
        _model = init_chat_model(settings.ASK_MODEL, **settings.ASK_MODEL_KWARGS)
    return _model


async def ask(corpus: Corpus, persona: str, question: str, model=None) -> Run:
    from mcp_server import agent, settings

    run = Run(question, persona)
    started = time.monotonic()
    try:
        run.result = await agent.ask(model or ask_model(), question, tools(corpus, persona, run),
                                     max_steps=settings.ASK_MAX_STEPS, max_tokens=settings.ASK_MAX_TOKENS,
                                     timeout_seconds=settings.ASK_TIMEOUT_SECONDS, sandbox=sandbox(),
                                     region=corpus.regions.get(persona))
    except Exception as e:  # the server would surface this as a tool error; score it as a failed run
        run.error = f"{type(e).__name__}: {e}"[:500]
    run.seconds = round(time.monotonic() - started, 2)
    return run


def dump(run: Run) -> str:
    return json.dumps(run.summary(), ensure_ascii=False, default=str)
