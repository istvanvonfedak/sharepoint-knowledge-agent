"""acl_scale: the permission gates at scale, on ~2,000 Enron emails with assigned permissions (data/acl_scale).

- acl_scale (regression, must score 1.0): every question asked by every persona, in every search mode.
  Layer 1 at k=50 must return only files the index ACL lets that persona see (a leak at rank 40 is still a leak),
  and vector/hybrid must return something. Then the production path with a live check that revokes 10% of each
  persona's files: results stay inside what SharePoint allows and, in vector/hybrid, fill min(k, permitted chunks).
  A failing live check returns nothing. Run once per UNRESOLVED_ACL_POLICY; `live_check` must be its own process
  (the policy is read at import), and `run.py` starts one.
- acl_scale_controls (regression): positive controls. Each persona finds files it may read when it searches for
  them by their own text, for every way it was granted access (owner, group, org:members, org:everyone); the guest
  for every file it may read. Without these, a filter that hides everything would pass the leak checks.
- acl_scale_recall (capability): owner recall split by how much the persona can see, against an admin persona that
  holds every principal in the dataset (set directly, so never `acl:unresolved`). The gap is the recall the
  permission filter costs; `l2_hit@10` is recall after the live check revoked 10% of the persona's other files.
"""
import asyncio
import hashlib

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver
from sharepoint_rag import acl, config

from sharepoint_evals import store
from sharepoint_evals.common import MOCK, MODES, ranking, set_output
from sharepoint_evals.corpus import Corpus, Doc, load, revoked
from sharepoint_evals.retrieval import SimulatedOutage, allow, index_url, ranked_docs, retrieve

SUITE = "acl_scale"
K_LEAK = 50
K = 10


def index_acl(doc: Doc) -> set[str]:
    """What the index should hold for this file, written out here rather than taken from acl.effective, so a bug
    there can't hide itself: its own permissions if they resolved, otherwise only the unresolved marker."""
    return set(doc.acl) if doc.resolved else {acl.UNRESOLVED}


def layer1(corpus: Corpus, principals: list[str]) -> set[str]:
    return {d.name for d in corpus.docs if not index_acl(d).isdisjoint(principals)}


def after_layer2(corpus: Corpus, persona: str, principals: list[str]) -> set[str]:
    """What the production path may return: layer 1 allows it, SharePoint allows it, and it wasn't revoked."""
    readable = corpus.readable(principals)
    return {d for d in layer1(corpus, principals) if d in readable and not revoked(persona, d)}


def admin_principals(corpus: Corpus) -> list[str]:
    held = {p for d in corpus.docs for p in d.acl if p.startswith(("user:", "group:"))}
    return sorted(held | {acl.ORG_MEMBERS, acl.ORG_EVERYONE})


UNDERFILLED = "live check left"  # marks a backfill problem; every other problem is a leak or a broken search
_chunks: dict[str, dict[str, int]] = {}
_oracles: dict[tuple[str, str], dict[str, set[str]]] = {}


def _oracle(corpus: Corpus, persona: str) -> dict[str, set[str]]:
    key = (corpus.name, persona)
    if key not in _oracles:
        principals = corpus.personas[persona]
        _oracles[key] = {
            "layer1": layer1(corpus, principals),
            "sharepoint": {d for d in corpus.readable(principals) if not revoked(persona, d)},  # the live check's answer
            "final": after_layer2(corpus, persona, principals),
        }
    return _oracles[key]


async def _chunk_counts(corpus: Corpus) -> dict[str, int]:
    if corpus.name not in _chunks:
        _chunks[corpus.name] = await store.chunk_counts(await index_url(corpus), corpus.name)
    return _chunks[corpus.name]


async def check_persona(corpus: Corpus, persona: str, question: str, mode: str) -> list[str]:
    principals = corpus.personas[persona]
    sets = _oracle(corpus, persona)
    problems = []
    returned = ranked_docs(await retrieve(corpus, question, principals, mode, k=K_LEAK))
    if leaked := [d for d in returned if d not in sets["layer1"]]:
        problems.append(f"{persona}: LEAKED at layer 1 {leaked}")
    if not returned and mode != "keyword":  # keyword legitimately finds nothing when no visible file has the words
        problems.append(f"{persona}: search returned nothing")
    final = sets["final"]
    hits = await retrieve(corpus, question, principals, mode, k=K, verify=allow(sets["sharepoint"]))
    if leaked := sorted({h.item_id for h in hits} - final):
        problems.append(f"{persona}: LEAKED after the live check {leaked}")
    if mode != "keyword":
        counts = await _chunk_counts(corpus)
        want = min(K, sum(counts.get(d, 0) for d in final))
        if len(hits) < want:
            problems.append(f"{persona}: {UNDERFILLED} {len(hits)} of {want} results")
    return problems


@solver
def every_persona():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        m = state.metadata
        corpus = load(SUITE, allow_quick=m["allow_quick"])
        if m.get("verifier_raises"):
            persona = next(iter(corpus.personas))
            try:
                hits = await retrieve(corpus, state.input_text, corpus.personas[persona], m["mode"], k=K,
                                      verify=allow(set(), raises=True))
                # No hits at layer 1 means the live check was never needed: nothing returned is failing closed too.
                m["problems"] = [f"live check failed but {len(hits)} results came back"] if hits else []
            except SimulatedOutage:
                m["problems"] = []  # failed closed (any other exception fails the sample: the check didn't run)
        else:
            results = await asyncio.gather(*(check_persona(corpus, p, state.input_text, m["mode"]) for p in corpus.personas))
            m["problems"] = [p for r in results for p in r]
        set_output(state, "; ".join(m["problems"]) or "ok")
        return state

    return solve


def _problem_scorer(keep):
    async def score(state: TaskState, target: Target) -> Score:
        problems = [p for p in state.metadata["problems"] if keep(p)]
        return Score(value=INCORRECT if problems else CORRECT, explanation="; ".join(problems[:20]) or "ok")

    return score


@scorer(metrics=[accuracy()])
def no_problems():
    return _problem_scorer(lambda p: True)


@scorer(metrics=[accuracy()])
def no_leaks():
    """Nothing outside what the caller may see, searches that return something, and failing closed."""
    return _problem_scorer(lambda p: UNDERFILLED not in p)


@scorer(metrics=[accuracy()])
def backfill():
    """In vector and hybrid mode, the live check's drops are replaced: min(k, permitted chunks) results."""
    return _problem_scorer(lambda p: UNDERFILLED in p)


def _policy_guard(policy: str) -> None:
    if config.UNRESOLVED_ACL_POLICY != policy:
        raise RuntimeError(f"acl_scale(policy={policy!r}) must run in a process started with "
                           f"UNRESOLVED_ACL_POLICY={policy} (it is read once, at import); run.py does this")


@task
def acl_scale(policy: str = "hide", allow_quick: bool = False) -> Task:
    _policy_guard(policy)
    corpus = load(SUITE, allow_quick=allow_quick)
    samples = [
        Sample(id=f"{mode}:{i}", input=q["question"], target="no leaks",
               metadata={"mode": mode, "category": q["category"], "allow_quick": allow_quick})
        for i, q in enumerate(corpus.questions) for mode in MODES
    ] + [
        Sample(id=f"{mode}:raises", input=corpus.questions[0]["question"], target="fail closed",
               metadata={"mode": mode, "verifier_raises": True, "category": "raises", "allow_quick": allow_quick})
        for mode in MODES
    ]
    return Task(dataset=MemoryDataset(samples), solver=every_persona(), scorer=[no_leaks(), backfill()], model=MOCK,
                name=f"acl_scale_{policy}", metadata={"suite": SUITE, "quick": corpus.quick, "policy": policy})


# ---- positive controls ----------------------------------------------------------------------------------------

def _grant(doc: Doc, principals: list[str]) -> str:
    held = set(doc.acl) & set(principals)
    for kind in (acl.ORG_EVERYONE, acl.ORG_MEMBERS):
        if kind in held:
            return kind
    return "group" if any(p.startswith("group:") for p in held) else "user"


def controls(corpus: Corpus, per_grant: int = 3) -> list[tuple[str, str, str]]:
    """(persona, doc, grant): up to `per_grant` files per persona and kind of grant; every file for the guest."""
    picked = []
    for persona, principals in corpus.personas.items():
        by_grant: dict[str, list[str]] = {}
        for name in sorted(corpus.visible(principals), key=lambda n: hashlib.sha256(f"{persona}:{n}".encode()).digest()):
            by_grant.setdefault(_grant(corpus.by_name[name], principals), []).append(name)
        for grant, names in sorted(by_grant.items()):
            picked += [(persona, n, grant) for n in (names if persona == "guest" else names[:per_grant])]
    return picked


def _own_text(doc: Doc) -> str:
    """The file's first chunk, as a query that should find it; empty if it has no text (not indexed, so no control)."""
    rows = store.chunk_file(str(doc.path), doc.name, store.index_title(doc, SUITE))
    return rows[0].text[:1000] if isinstance(rows, list) and rows else ""


@solver
def find_own_file():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        m = state.metadata
        corpus = load(SUITE, allow_quick=m["allow_quick"])
        returned = ranked_docs(await retrieve(corpus, state.input_text, corpus.personas[m["persona"]], m["mode"], k=K_LEAK))
        m["problems"] = [] if m["doc"] in returned else [f"{m['persona']} can read {m['doc']} ({m['grant']}) but didn't find it"]
        set_output(state, ", ".join(returned[:5]))
        return state

    return solve


@task
def acl_scale_controls(allow_quick: bool = False) -> Task:
    corpus = load(SUITE, allow_quick=allow_quick)
    texts = {d: _own_text(corpus.by_name[d]) for d in {doc for _, doc, _ in controls(corpus)}}
    samples = [
        Sample(id=f"{mode}:{persona}:{doc}", input=texts[doc], target=doc,
               metadata={"mode": mode, "persona": persona, "doc": doc, "grant": grant, "allow_quick": allow_quick})
        for persona, doc, grant in controls(corpus) if texts[doc] for mode in ("vector", "hybrid")
    ]
    return Task(dataset=MemoryDataset(samples), solver=find_own_file(), scorer=no_problems(), model=MOCK,
                metadata={"suite": SUITE, "quick": corpus.quick})


# ---- recall under the permission filter -----------------------------------------------------------------------

@solver
def recall_probe(mode: str):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        m = state.metadata
        corpus = load(SUITE, allow_quick=m["allow_quick"])
        principals = corpus.personas[m["persona"]]
        expected = set(m["expected"])
        mine = ranked_docs(await retrieve(corpus, state.input_text, principals, mode, k=K_LEAK))
        admin = ranked_docs(await retrieve(corpus, state.input_text, admin_principals(corpus), mode, k=K_LEAK))
        # The live check revokes 10% of the persona's other files; the target itself stays readable.
        readable = {d for d in corpus.readable(principals) if d in expected or not revoked(m["persona"], d)}
        after = ranked_docs(await retrieve(corpus, state.input_text, principals, mode, k=K, verify=allow(readable)))
        m["scores"] = {**ranking(mine, expected), **{f"admin_{k}": v for k, v in ranking(admin, expected).items()},
                       "l2_hit@10": float(bool(expected & set(after)))}
        m["scores"]["gap@10"] = m["scores"]["hit@10"] - m["scores"]["admin_hit@10"]
        set_output(state, ", ".join(mine[:5]))
        return state

    return solve


RECALL_METRICS = ["hit@1", "hit@5", "hit@10", "mrr", "admin_hit@1", "admin_hit@10", "gap@10", "l2_hit@10"]


@scorer(metrics={k: [mean()] for k in RECALL_METRICS})
def recall_scores():
    async def score(state: TaskState, target: Target) -> Score:
        s = state.metadata["scores"]
        return Score(value={k: s[k] for k in RECALL_METRICS}, explanation=f"hit@10 {s['hit@10']} admin {s['admin_hit@10']}")

    return score


@task
def acl_scale_recall(mode: str = "hybrid", allow_quick: bool = False) -> Task:
    corpus = load(SUITE, allow_quick=allow_quick)
    sizes = {p: len(corpus.visible(pr)) for p, pr in corpus.personas.items()}
    cut = sorted(sizes.values())
    low, high = cut[len(cut) // 3], cut[2 * len(cut) // 3]
    samples = [
        Sample(id=f"{i}", input=q["question"], target=q["expected"][0],
               metadata={"persona": q["persona"], "expected": q["expected"], "allow_quick": allow_quick,
                         "visibility": "low" if sizes[q["persona"]] < low else "high" if sizes[q["persona"]] >= high else "mid",
                         "category": q["category"]})
        for i, q in enumerate(corpus.questions) if q["expected"]
    ]
    return Task(dataset=MemoryDataset(samples), solver=recall_probe(mode), scorer=recall_scores(), model=MOCK,
                name=f"acl_scale_recall_{mode}", metadata={"suite": SUITE, "quick": corpus.quick, "mode": mode})
