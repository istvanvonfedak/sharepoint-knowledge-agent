"""The golden set (docs/evals.md#golden-set): hand-made questions about documents like ours, each with a reviewed
answer, the sources it must cite and what the asker must not see.

    evals/golden/questions.jsonl     one item per line
    evals/golden/docs/               the documents, at their site/library/path
    evals/golden/permissions.json    who may read what (the visibility the seeder sets up), and the personas

EVAL_GOLDEN_DIR points runs at another set in the same format (one about a real tenant's content stays outside the
repo). `validate()` checks every item, and that every path it names exists, before anything runs.

Today the set runs locally (agent tier): the documents are indexed like any suite and the agent runs through
agent_harness.py. In the live tier the same items will run against the seeded test tenant. Only `reviewed` items
are scored; `--golden-drafts` scores drafts too, labelled as such.

Scoring, deterministic checks first:
- permissions (GATE): nothing in `must_not_see` among the persona's search results or sources, nothing in
  `forbidden` anywhere in the output.
- deterministic: the status matches `expect`; every path in `sources` is cited; no citation outside `sources` and
  `also_acceptable`.
- grader (rubrics/golden.v1.md): fact recall, groundedness, answer in the question's language.
Reported per kind, persona and language.
"""
import json
import os
import re
from datetime import date
from functools import cache
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import TaskState

from sharepoint_evals import agent_evals, grader
from sharepoint_evals.agent_evals import present_mean, run_agent, try_grade
from sharepoint_evals.common import MOCK
from sharepoint_evals.corpus import ROOT, Corpus, Doc, principals

DIR = Path(os.environ.get("EVAL_GOLDEN_DIR") or ROOT / "evals" / "golden")
STATUSES = {"draft", "reviewed", "retired"}
LANGUAGES = {"en", "es"}
KINDS = {"fact", "multi_doc", "table", "cross_lingual", "restricted", "unanswerable", "injection"}
EXPECTS = {"answered", "partial", "not_found", "refused"}
REQUIRED = ["id", "status", "question", "language", "persona", "kind", "expect", "origin", "author", "reviewer", "added"]
KNOWN = set(REQUIRED) | {"answer", "facts", "sources", "also_acceptable", "must_not_see", "forbidden", "notes"}


class InvalidGoldenSet(ValueError):
    pass


def _permissions() -> dict:
    return json.loads((DIR / "permissions.json").read_text("utf-8"))


def _acl(path: str, rules: dict[str, list[str]]) -> tuple[str, ...]:
    match = max((p for p in rules if path == p or (p.endswith("/") and path.startswith(p))), key=len, default=None)
    return tuple(rules[match]) if match else ()


@cache
def corpus() -> Corpus:
    perms = _permissions()
    docs_dir = DIR / "docs"
    docs = []
    for path in sorted(p for p in docs_dir.rglob("*") if p.is_file()):
        name = path.relative_to(docs_dir).as_posix()
        docs.append(Doc(name, _acl(name, perms["paths"]), name not in perms.get("unresolved", []), path))
    return Corpus("golden", docs, {p: principals(v) for p, v in perms["personas"].items()}, items())


def items() -> list[dict]:
    path = DIR / "questions.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()] if path.exists() else []


def validate(rows: list[dict] | None = None, docs: Corpus | None = None) -> list[str]:
    """Every problem with the set; an empty list means it may run."""
    rows = items() if rows is None else rows
    docs = docs or corpus()
    paths = set(docs.by_name)
    problems, seen = [], set()
    for n, item in enumerate(rows, 1):
        where = f"line {n} ({item.get('id', '?')})"
        problems += [f"{where}: missing {f}" for f in REQUIRED if f not in item]
        problems += [f"{where}: unknown field {f}" for f in sorted(set(item) - KNOWN)]
        if not re.fullmatch(r"g-\d{4,}", str(item.get("id", ""))):
            problems.append(f"{where}: id must look like g-0042")
        if item.get("id") in seen:
            problems.append(f"{where}: duplicate id (ids are never reused)")
        seen.add(item.get("id"))
        for field, allowed in (("status", STATUSES), ("language", LANGUAGES), ("kind", KINDS), ("expect", EXPECTS),
                               ("persona", set(docs.personas))):
            if field in item and item[field] not in allowed:
                problems.append(f"{where}: {field} {item[field]!r} is not one of {sorted(allowed)}")
        if item.get("expect") in agent_evals.ANSWERED:
            problems += [f"{where}: an {item['expect']} item needs {f}" for f in ("answer", "facts", "sources") if not item.get(f)]
        for field in ("sources", "also_acceptable", "must_not_see"):
            problems += [f"{where}: {field} path {p} is not in the seeded documents" for p in item.get(field, []) if p not in paths]
        if item.get("persona") in docs.personas:
            readable = docs.readable(docs.personas[item["persona"]])
            # A source must be one search can find (resolved), and a must_not_see one SharePoint itself denies: an
            # item must not depend on UNRESOLVED_ACL_POLICY.
            findable = docs.visible(docs.personas[item["persona"]])
            problems += [f"{where}: {item['persona']} can't find source {p}" for p in item.get("sources", []) if p in paths and p not in findable]
            problems += [f"{where}: {item['persona']} can read must_not_see {p}" for p in item.get("must_not_see", []) if p in readable]
        if item.get("status") == "reviewed" and (not item.get("reviewer") or item.get("reviewer") == item.get("author")):
            problems.append(f"{where}: a reviewed item needs a reviewer other than its author")
        origin = str(item.get("origin", ""))
        if origin != "written" and not origin.startswith("failure:"):
            problems.append(f"{where}: origin must be 'written' or 'failure:<where it was seen>'")
        try:
            date.fromisoformat(str(item.get("added")))
        except ValueError:
            problems.append(f"{where}: added must be a date (YYYY-MM-DD)")
    return problems


def selected(include_drafts: bool) -> list[dict]:
    if problems := validate():
        raise InvalidGoldenSet("the golden set is invalid, nothing was run:\n  " + "\n  ".join(problems))
    wanted = {"reviewed", "draft"} if include_drafts else {"reviewed"}
    return [i for i in items() if i["status"] in wanted]


GOLDEN_METRICS = ["permissions_ok", "status_ok", "sources_cited", "citations_ok", "fact_recall", "grounded",
                  "language_ok", "links_ok", "budget_exhausted", "error", "steps", "tokens", "seconds"]


@scorer(metrics={k: [present_mean()] for k in GOLDEN_METRICS}, name="golden")
def golden_scores():
    async def score(state: TaskState, target: Target) -> Score:
        m = state.metadata
        docs, run, item = corpus(), agent_evals.run_of(state, "golden"), m["q"]
        status = run.result.status if run.result else "error"
        cited = run.cited(docs)
        problems = []
        # "sandbox": files written to the agent's sandbox, which it can read whole with run
        exposed = (run.seen(("search", "read_around", "sandbox")) | set(cited)) & set(item.get("must_not_see", []))
        output = " ".join(run.output_texts).lower()
        shown = [f for f in item.get("forbidden", []) if f.lower() in output]
        if exposed:
            problems.append(f"MUST NOT SEE {sorted(exposed)}")
        if shown:
            problems.append(f"FORBIDDEN in output {shown}")
        values = {**agent_evals.common(run), "permissions_ok": float(not problems), "status_ok": float(status == item["expect"]),
                  "sources_cited": None, "citations_ok": None, "fact_recall": None, "grounded": None, "language_ok": None}
        if item["expect"] in agent_evals.ANSWERED:
            values["sources_cited"] = float(set(item["sources"]) <= set(cited))
            values["citations_ok"] = float(set(cited) <= set(item["sources"]) | set(item.get("also_acceptable", [])))
            if status in agent_evals.ANSWERED:
                links = [s["link"] for s in run.result.sources]
                if v := await try_grade(m, "golden", question=item["question"], language=item["language"],
                                        reference=item["answer"], facts="\n".join(item["facts"]), answer=run.result.answer,
                                        sources=grader.sources_text(run.texts, links)):
                    values["fact_recall"] = sum(v["facts"][:len(item["facts"])]) / len(item["facts"])
                    values["grounded"], values["language_ok"] = float(v["grounded"]), float(v["language_ok"])
            else:
                values["fact_recall"] = 0.0
        if not values["links_ok"]:
            problems.append("FOREIGN LINK in the output")
        return Score(value=values, answer=run.result.answer if run.result else "",
                     explanation="; ".join(problems + [f"status {status} (expected {item['expect']})", f"cited {cited}"]))

    return score


@task
def golden(epochs: int = 3, include_drafts: bool = False) -> Task:
    rows = selected(include_drafts)
    samples = [Sample(id=i["id"], input=i["question"], target=i.get("answer", ""),
                      metadata={"persona": i["persona"], "kind": i["kind"], "language": i["language"], "status": i["status"],
                                "q": i, "allow_quick": False})
               for i in rows]
    return Task(dataset=MemoryDataset(samples), solver=run_agent("golden"), scorer=golden_scores(), model=MOCK,
                epochs=epochs, metadata={"suite": "golden", "drafts": include_drafts, "grader": grader.MODEL})
