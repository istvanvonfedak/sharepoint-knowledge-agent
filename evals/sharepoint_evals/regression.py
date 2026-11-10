"""Regression evals: deterministic, no LLM. Any score below 1.0 means a behaviour we promised broke.

- extraction_regression: key facts survive extract+chunk for every dataset doc in md/html/docx form.
- acl_regression: restricted chunks never appear for callers without access, in any search mode.
"""
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver
from sharepoint_rag.chunk import MAX_CHARS, chunk_sections
from sharepoint_rag.extract import extract

from sharepoint_evals.dataset import DOCS
from sharepoint_evals.retrieval import ranked_docs, retrieve

MOCK = "mockllm/model"

KEY_FACTS = {
    "leave-policy.md": ["22 working days", "carried over", "HR portal", "medical certificate"],
    "expenses.md": ["30 days", "45 EUR", "EXP-2041", "10 working days"],
    "onboarding.md": ["onboarding buddy", "10:00", "30-60-90"],
    "security-incident.md": ["security@example.com", "within 1 hour", "P1", "blameless"],
    "it-helpdesk.md": ["error 809", "UDP ports 500 and 4500", "180 days"],
    "engineering-oncall.md": ["every Monday", "15 minutes", "stipend"],
    "politica-ferias-portugal.md": ["25 dias úteis", "subsídio de férias", "15 dias"],
    "guia-teletrabalho.md": ["40 euros", "eletricidade", "portátil"],
    "salaries-2025.md": ["62,000 to 78,000", "L6", "15 percent"],
    "board-minutes.md": ["Project Falcon", "18 to 24 million", "due-diligence"],
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _md_to_html(md: str) -> bytes:
    out = []
    for line in md.splitlines():
        if m := re.match(r"^(#{1,6})\s+(.*)", line):
            out.append(f"<h{len(m.group(1))}>{escape(m.group(2))}</h{len(m.group(1))}>")
        elif line.strip():
            out.append(f"<p>{escape(line.removeprefix('- '))}</p>")
    return f"<html><head><style>p{{}}</style></head><body>{''.join(out)}</body></html>".encode()


def _md_to_docx(md: str) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paras = []
    for line in md.splitlines():
        if m := re.match(r"^(#{1,6})\s+(.*)", line):
            paras.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{len(m.group(1))}"/></w:pPr><w:r><w:t>{escape(m.group(2))}</w:t></w:r></w:p>')
        elif line.strip():
            paras.append(f"<w:p><w:r><w:t>{escape(line.removeprefix('- '))}</w:t></w:r></w:p>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {ns}><w:body>{''.join(paras)}</w:body></w:document>")
    return buf.getvalue()


def _variants(doc_name: str) -> dict[str, tuple[str, bytes]]:
    md_bytes = next(d for d in DOCS if d.name == doc_name).read()
    md = md_bytes.decode()
    stem = doc_name.removesuffix(".md")
    return {"md": (doc_name, md_bytes), "html": (f"{stem}.html", _md_to_html(md)), "docx": (f"{stem}.docx", _md_to_docx(md))}


@solver
def extract_and_chunk():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        filename, data = _variants(state.metadata["doc"])[state.metadata["format"]]
        sections = extract(filename, data)
        chunks = chunk_sections(sections)
        state.metadata["chunk_texts"] = [c.text for c in chunks]
        state.metadata["heading_paths"] = sorted({c.heading_path for c in chunks if c.heading_path})
        state.output = ModelOutput.from_content(MOCK, f"{len(chunks)} chunks")
        return state

    return solve


@scorer(metrics=[accuracy()])
def facts_survive():
    async def score(state: TaskState, target: Target) -> Score:
        joined = _norm(" ".join(state.metadata["chunk_texts"]))
        missing = [f for f in json.loads(target.text) if _norm(f) not in joined]
        oversized = [len(t) for t in state.metadata["chunk_texts"] if len(t) > MAX_CHARS]
        empty = any(not t.strip() for t in state.metadata["chunk_texts"])
        # Structured formats must keep the document title as a heading; HTML in this fixture has none to check.
        no_title = state.metadata["format"] != "html" and not any(
            state.metadata["title"] in h for h in state.metadata["heading_paths"]
        )
        problems = [p for p in (
            f"missing facts: {missing}" if missing else "",
            f"oversized chunks: {oversized}" if oversized else "",
            "empty chunk" if empty else "",
            "title missing from heading paths" if no_title else "",
        ) if p]
        return Score(value=INCORRECT if problems else CORRECT, explanation="; ".join(problems) or "ok")

    return score


@task
def extraction_regression() -> Task:
    samples = []
    for doc in DOCS:
        title = doc.read().decode().splitlines()[0].removeprefix("# ").split(" (")[0]
        for fmt in ("md", "html", "docx"):
            samples.append(Sample(
                id=f"{doc.name}:{fmt}", input=f"{doc.name} as {fmt}", target=json.dumps(KEY_FACTS[doc.name]),
                metadata={"doc": doc.name, "format": fmt, "title": title},
            ))
    return Task(dataset=MemoryDataset(samples), solver=extract_and_chunk(), scorer=facts_survive(), model=MOCK)


# ---- ACL regression --------------------------------------------------------------------------
RESTRICTED = ["salaries-2025.md", "board-minutes.md"]
ACL_CASES = [
    # (persona, query, docs that must NOT appear, doc that MUST appear (positive control or None))
    ("alice", "base salary band for level L5", RESTRICTED, None),
    ("alice", "62,000 to 78,000 EUR L4 compensation band", RESTRICTED, None),
    ("alice", "Project Falcon acquisition logistics start-up valuation", RESTRICTED, None),
    ("alice", "board approved exploratory talks due-diligence executive session", RESTRICTED, None),
    ("exec", "base salary band for level L5", ["salaries-2025.md"], None),  # execs are not HR
    ("hr", "Project Falcon acquisition valuation", ["board-minutes.md"], None),  # HR is not execs
    ("hr", "base salary band for level L5", [], "salaries-2025.md"),
    ("exec", "Project Falcon acquisition valuation", [], "board-minutes.md"),
]


@solver
def acl_probe():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        m = state.metadata
        hits = await retrieve(state.input_text, m["persona"], m["mode"], k=20)
        state.metadata["returned"] = ranked_docs(hits)
        state.output = ModelOutput.from_content(MOCK, ", ".join(state.metadata["returned"]))
        return state

    return solve


@scorer(metrics=[accuracy()])
def acl_respected():
    async def score(state: TaskState, target: Target) -> Score:
        returned = state.metadata["returned"]
        leaked = [d for d in state.metadata["forbidden"] if d in returned]
        must = state.metadata["must_include"]
        missing = [must] if must and must not in returned else []
        # Empty results would trivially "leak nothing": a broken search must not pass. Only vector/hybrid are
        # held to this: keyword search legitimately finds nothing when the only matching docs are restricted.
        problems = [
            f"LEAKED {leaked}" if leaked else "",
            f"expected {missing} not returned" if missing else "",
            "search returned nothing" if not returned and state.metadata["mode"] != "keyword" else "",
        ]
        problems = [p for p in problems if p]
        return Score(value=INCORRECT if problems else CORRECT, explanation="; ".join(problems) or f"ok: {returned}")

    return score


@task
def acl_regression() -> Task:
    samples = [
        Sample(
            id=f"{persona}:{mode}:{i}", input=query, target="no leaks",
            metadata={"persona": persona, "mode": mode, "forbidden": forbidden, "must_include": must},
        )
        for i, (persona, query, forbidden, must) in enumerate(ACL_CASES)
        for mode in ("vector", "keyword", "hybrid")
    ]
    return Task(dataset=MemoryDataset(samples), solver=acl_probe(), scorer=acl_respected(), model=MOCK)
