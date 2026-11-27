"""The judge set (docs/evals.md#judge-set): hand-labelled cases for the grader, so the grader is measured too.

    evals/golden/judge.jsonl     one case per line, in three buckets:
                                   inputs            what the grader gets: question, language, reference, facts, answer, sources
                                   expected_outputs  what a careful person says its verdicts are: labels
                                   metadata          about the case: id, status, rubric, kind, origin, author, reviewer, added, notes

Every golden-set score that says "fact recall" or "grounded" is the grader's opinion. Each case here fixes the inputs the
grader would get (question, reference, facts, an answer, the source text) and what a careful person says the verdicts
should be (`labels`: one bool per fact, `grounded`, `language_ok`). The task grades each case and counts disagreements.
It never runs the agent, needs no database and no documents: only the grader model (EVAL_GRADER_MODEL).

Reported, per label (facts, grounded, language_ok):
- agree: the grader gave the label's verdict.
- false_accept: the label says no and the grader said yes. These inflate every agent score, so they matter most.
- false_reject: the label says yes and the grader said no. These deflate scores, as the grounded score's page and
  heading problem does.
Reported per case kind, language and origin too: `synthetic` cases are answers written to fail one way; `run:<date>`
cases are real agent answers, with the source text the grader got, labelled before its verdicts were seen. Like the golden set, a case is `draft` until two people have reviewed its
labels; `--golden-drafts` scores drafts, labelled as such. A rubric change or a new grader model is only trusted once
this set has been run against it.
"""
import json
from itertools import zip_longest

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import Generate, TaskState, solver

from sharepoint_evals import golden, grader
from sharepoint_evals.agent_evals import epochs_of, present_mean, try_grade
from sharepoint_evals.common import MOCK, set_output

STATUSES = {"draft", "reviewed", "retired"}
REQUIRED = {"inputs": ["question", "language", "reference", "facts", "answer", "sources"], "expected_outputs": ["labels"],
            "metadata": ["id", "status", "rubric", "kind", "origin", "author", "reviewer", "added"]}
KNOWN = {bucket: set(fields) for bucket, fields in REQUIRED.items()}
KNOWN["metadata"] |= {"notes"}
RUBRICS = {"golden"}  # the rubrics this set has labels for
LABELS = ("facts", "grounded", "language_ok")
JUDGE_METRICS = ["all_agree", "facts_agree", "fact_false_accept", "fact_false_reject", "grounded_agree",
                 "grounded_false_accept", "grounded_false_reject", "language_agree", "language_false_accept",
                 "language_false_reject"]


def items() -> list[dict]:
    path = golden.DIR / "judge.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()] if path.exists() else []


def validate(rows: list[dict] | None = None) -> list[str]:
    """Every problem with the set; an empty list means it may run."""
    rows = items() if rows is None else rows
    problems, seen = [], set()
    for n, row in enumerate(rows, 1):
        buckets = {b: row.get(b) for b in REQUIRED}
        meta = buckets["metadata"] if isinstance(buckets["metadata"], dict) else {}
        name = meta.get("id") or f"line {n}"
        if bad := [b for b, v in buckets.items() if not isinstance(v, dict)]:
            problems.append(f"{name}: {bad} must be objects")
            continue
        if extra := sorted(set(row) - set(REQUIRED)):
            problems.append(f"{name}: unknown buckets {extra}")
        inputs, expected = buckets["inputs"], buckets["expected_outputs"]
        if missing := [f"{b}.{k}" for b, fields in REQUIRED.items() for k in fields if k not in buckets[b]]:
            problems.append(f"{name}: missing {missing}")
            continue
        if unknown := sorted(f"{b}.{k}" for b, v in buckets.items() for k in set(v) - KNOWN[b]):
            problems.append(f"{name}: unknown fields {unknown}")
        if meta["id"] in seen:
            problems.append(f"{name}: duplicate id")
        seen.add(meta["id"])
        if meta["status"] not in STATUSES:
            problems.append(f"{name}: status {meta['status']!r}")
        if meta["rubric"] not in RUBRICS:
            problems.append(f"{name}: rubric {meta['rubric']!r} has no labels here (known: {sorted(RUBRICS)})")
        if inputs["language"] not in golden.LANGUAGES:
            problems.append(f"{name}: language {inputs['language']!r}")
        labels = expected["labels"]
        if not isinstance(labels, dict) or set(labels) != set(LABELS):
            problems.append(f"{name}: labels must be exactly {list(LABELS)}")
            continue
        if not isinstance(inputs["facts"], list) or not inputs["facts"] or not all(isinstance(f, str) and f for f in inputs["facts"]):
            problems.append(f"{name}: facts must be a non-empty list of strings")
        elif not isinstance(labels["facts"], list):
            problems.append(f"{name}: labels.facts must be a list with one true or false per fact")
        elif len(labels["facts"]) != len(inputs["facts"]):
            problems.append(f"{name}: {len(inputs['facts'])} facts but {len(labels['facts'])} fact labels")
        singles = [labels["grounded"], labels["language_ok"]]
        if not all(isinstance(v, bool) for v in [*(labels["facts"] if isinstance(labels["facts"], list) else []), *singles]):
            problems.append(f"{name}: every label must be true or false")
        for key, source in (("question", inputs), ("reference", inputs), ("answer", inputs), ("sources", inputs), ("kind", meta)):
            if not isinstance(source[key], str) or not source[key].strip():
                problems.append(f"{name}: {key} is empty")
        if meta["origin"] != "synthetic" and not str(meta["origin"]).startswith("run:"):
            problems.append(f"{name}: origin {meta['origin']!r} (synthetic, or run:<date> for a real agent answer)")
        if meta["status"] == "reviewed" and (not meta["reviewer"] or meta["reviewer"] == meta["author"]):
            problems.append(f"{name}: a reviewed case needs a reviewer other than its author")
    return problems


def selected(include_drafts: bool) -> list[dict]:
    if problems := validate():
        raise golden.InvalidGoldenSet("the judge set is invalid, nothing was run:\n  " + "\n  ".join(problems))
    wanted = {"reviewed", "draft"} if include_drafts else {"reviewed"}
    return [i for i in items() if i["metadata"]["status"] in wanted]


def compare(labels: dict, verdict: dict) -> dict[str, float | None]:
    """How one verdict differs from the labels. A rate is None where the label gives it nothing to measure
    (no fact labelled false, say)."""
    # A verdict with more or fewer facts than the labels disagrees on each one that has no counterpart.
    facts = list(zip_longest(labels["facts"], verdict["facts"]))
    out: dict[str, float | None] = {}
    out["facts_agree"] = sum(want == got for want, got in facts) / len(facts)
    wrong = [got for want, got in facts if want is False]
    right = [got for want, got in facts if want is True]
    out["fact_false_accept"] = sum(g is True for g in wrong) / len(wrong) if wrong else None
    out["fact_false_reject"] = sum(g is not True for g in right) / len(right) if right else None
    for key, name in (("grounded", "grounded"), ("language_ok", "language")):
        want, got = labels[key], bool(verdict[key])
        out[f"{name}_agree"] = float(want == got)
        out[f"{name}_false_accept"] = float(got) if not want else None
        out[f"{name}_false_reject"] = float(not got) if want else None
    out["all_agree"] = float(out["facts_agree"] == 1 and out["grounded_agree"] == 1 and out["language_agree"] == 1)
    return out


@solver
def grade_case():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        case = state.metadata["case"]
        given = case["inputs"]
        await try_grade(state.metadata, case["metadata"]["rubric"], question=given["question"], language=given["language"],
                        reference=given["reference"], facts="\n".join(given["facts"]), answer=given["answer"],
                        sources=given["sources"])
        set_output(state, given["answer"])
        return state

    return solve


@scorer(metrics={k: [present_mean()] for k in JUDGE_METRICS}, name="judge")
def judge_scores():
    async def score(state: TaskState, target: Target) -> Score:
        case = state.metadata["case"]
        labels = case["expected_outputs"]["labels"]
        verdict = state.metadata.get("grades", {}).get(case["metadata"]["rubric"])
        values = compare(labels, verdict) if verdict else {k: None for k in JUDGE_METRICS}
        explanation = "grading failed" if not verdict else "; ".join(
            f"{k}: labelled {want}, graded {got}" for k, want, got in
            [("facts", labels["facts"], verdict["facts"]),
             ("grounded", labels["grounded"], verdict["grounded"]),
             ("language_ok", labels["language_ok"], verdict["language_ok"])] if want != got)
        return Score(value=values, answer=case["inputs"]["answer"],
                     explanation=(explanation or "agrees") + (f" | grader: {verdict['reason']}" if verdict else ""))

    return score


@task
def golden_judge(epochs: int = 3, include_drafts: bool = False) -> Task:
    rows = selected(include_drafts)
    samples = [Sample(id=c["metadata"]["id"], input=c["inputs"]["answer"], target="",
                      metadata={"kind": c["metadata"]["kind"], "language": c["inputs"]["language"], "status": c["metadata"]["status"],
                                "origin": c["metadata"]["origin"].split(":")[0], "case": c})
               for c in rows]
    return Task(dataset=MemoryDataset(samples), solver=grade_case(), scorer=judge_scores(), model=MOCK, epochs=epochs_of(epochs),
                metadata={"suite": "golden_judge", "drafts": include_drafts, "grader": grader.MODEL,
                          "rubric": grader.rubric("golden")[1]})
