"""Eval corpora: the documents to index, who may read each one, who asks, and the cases.

Every suite is a `Corpus`: the synthetic dataset in `dataset/`, the golden set in `evals/golden/`, and the public
datasets that `scripts/build_eval_data.py` writes to `data/<suite>/`. A public suite that hasn't been built raises
`SuiteNotBuilt`, and one built with `--quick` raises `QuickSuite` unless the caller allows it: a missing or small
dataset must never pass a permission gate by testing less than it claims to.

Permission oracles (`allowed`, `truly_allowed`) are computed from the dataset's own permission lists, never from
what the eval database happens to contain.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import cache, cached_property
from pathlib import Path, PurePosixPath

from sharepoint_rag import acl

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("EVAL_DATA_DIR") or ROOT / "data")
PUBLIC_SUITES = ("acl_scale", "retrieval_multilingual", "extraction_robustness", "answer_quality", "agent_safety",
                 "retrieval_identifiers")  # retrieval_identifiers is generated, not downloaded, but built the same way


class SuiteNotBuilt(RuntimeError):
    pass


class QuickSuite(RuntimeError):
    pass


@dataclass(frozen=True)
class Doc:
    name: str  # path under the suite's docs/; also the item id in the eval index
    acl: tuple[str, ...]  # who SharePoint lets read it, even when the crawler couldn't resolve that
    resolved: bool
    path: Path
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    def read(self) -> bytes:
        return self.path.read_bytes()

    @property
    def title(self) -> str:
        return PurePosixPath(self.name).name


@dataclass
class Corpus:
    name: str
    docs: list[Doc]
    personas: dict[str, list[str]]  # persona -> principals, as the mcp-server builds them
    questions: list[dict] = field(default_factory=list)
    source: dict = field(default_factory=dict)  # SOURCE.json, for public suites
    regions: dict[str, str] = field(default_factory=dict)  # persona -> where the user is based, for those that have one

    @property
    def quick(self) -> bool:
        return bool(self.source.get("params", {}).get("quick"))

    @cached_property
    def by_name(self) -> dict[str, Doc]:
        return {d.name: d for d in self.docs}

    def visible(self, principals: list[str]) -> set[str]:
        """What layer 1 may return under the 'hide' policy: resolved files whose ACL overlaps the caller's."""
        return {d.name for d in self.docs if allowed(d, principals)}

    def readable(self, principals: list[str]) -> set[str]:
        """What SharePoint itself would let the caller open (the live check's answer)."""
        return {d.name for d in self.docs if truly_allowed(d, principals)}


def allowed(doc: Doc, principals: list[str]) -> bool:
    return doc.resolved and not set(doc.acl).isdisjoint(principals)


def truly_allowed(doc: Doc, principals: list[str]) -> bool:
    return not set(doc.acl).isdisjoint(principals)


def principals(persona: dict) -> list[str]:
    """personas.json entries hold the inputs of acl.query_principals, so the eval asks exactly as the server does
    (including UNRESOLVED_ACL_POLICY, read when this runs)."""
    return acl.query_principals(persona["user"], list(persona.get("groups", [])), bool(persona.get("is_member", True)))


def revoked(persona: str, doc: str, share: float = 0.10) -> bool:
    """A deterministic `share` of each persona's files, "revoked since indexing" for layer-2 tests."""
    digest = hashlib.sha256(f"revoke:{persona}:{doc}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < share


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()] if path.exists() else []


@cache
def _load_public(name: str) -> Corpus:
    folder = DATA / name
    meta = folder / "SOURCE.json"
    if not meta.exists():
        raise SuiteNotBuilt(
            f"{name}: data/{name} is not built. Run `.venv/bin/python scripts/build_eval_data.py {name}`, "
            f"or pass --exclude {name} to run without it.")
    personas = json.loads((folder / "personas.json").read_text("utf-8")) if (folder / "personas.json").exists() else {}
    docs = [
        Doc(row["name"], tuple(row["acl"]), bool(row["resolved"]), folder / "docs" / row["name"],
            {k: v for k, v in row.items() if k not in ("name", "acl", "resolved")})
        for row in _jsonl(folder / "docs.jsonl")
    ]
    return Corpus(name, docs, {p: principals(v) for p, v in personas.items()}, _jsonl(folder / "questions.jsonl"),
                  json.loads(meta.read_text("utf-8")))


def load(name: str, *, allow_quick: bool = False) -> Corpus:
    if name == "synthetic":
        return synthetic()
    if name == "golden":
        from sharepoint_evals import golden
        return golden.corpus()
    if name not in PUBLIC_SUITES:
        raise ValueError(f"unknown suite {name!r}")
    corpus = _load_public(name)
    if corpus.quick and not allow_quick:
        raise QuickSuite(f"{name}: data/{name} was built with --quick (a small sample). Rebuild it in full "
                         f"(`scripts/build_eval_data.py {name} --force`), or pass --allow-quick to label results quick.")
    return corpus


@cache
def synthetic() -> Corpus:
    from sharepoint_evals.dataset import DOCS, PERSONAS
    from sharepoint_evals.questions import QUESTIONS

    docs = [Doc(d.name, d.acl, d.resolved, d.path) for d in DOCS]
    questions = [{"question": q.question, "expected": [q.expected], "category": q.category, "persona": q.persona}
                 for q in QUESTIONS]
    return Corpus("synthetic", docs, dict(PERSONAS), questions)
