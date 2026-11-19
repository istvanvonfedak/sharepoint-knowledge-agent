"""Retrieval quality on the public datasets (capability: a quality gradient, tracked against baselines).

- retrieval_multilingual: MLQA (en→es, es→en, es→es) and MIRACL Spanish, whose distractors include the
  annotators' hard negatives. The bench for RRF weights, chunk size, BM25, a reranker or `halfvec`. MIRACL labels
  only some passages as relevant, so compare runs rather than reading the absolute numbers.
- retrieval_identifiers: generated tickets, invoices, contracts and product sheets that differ only in their
  identifiers and a few fields. Questions name one by identifier ("INC-48213"), ask about it in a sentence, or
  combine fields without an identifier. Embeddings are weakest here: keyword search has to earn its place in hybrid.
- answer_quality_retrieval: FinanceBench, no LLM. Is the filing a question is about in the top 5? `wrong_filing@1`
  is how often the top result is another filing of the same company, the distractor that matters most.

Each runs once per search mode; `run.py` prints the modes side by side and per category.
"""
from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver

from sharepoint_evals.common import MOCK, ranking, set_output
from sharepoint_evals.corpus import load
from sharepoint_evals.retrieval import ranked_docs, retrieve

METRICS = ["hit@1", "hit@5", "hit@10", "mrr", "ndcg@10"]


@solver
def rank(suite: str, mode: str):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        m = state.metadata
        corpus = load(suite, allow_quick=m["allow_quick"])
        returned = ranked_docs(await retrieve(corpus, state.input_text, corpus.personas[m["persona"]], mode, k=50))
        m["scores"] = ranking(returned, m["expected"])
        if suite == "answer_quality":
            company = corpus.by_name[m["expected"][0]].meta.get("company")
            top = corpus.by_name.get(returned[0]) if returned else None
            m["scores"]["wrong_filing@1"] = float(top is not None and top.name not in m["expected"]
                                                  and top.meta.get("company") == company)
        m["returned"] = returned[:10]
        set_output(state, ", ".join(returned[:5]))
        return state

    return solve


def _scorer(names: list[str]):
    @scorer(metrics={k: [mean()] for k in names}, name="ranking")
    def ranked():
        async def score(state: TaskState, target: Target) -> Score:
            s = state.metadata["scores"]
            return Score(value={k: s[k] for k in names},
                         explanation=f"expected {state.metadata['expected'][:3]}; got {state.metadata['returned'][:5]}")

        return score

    return ranked()


def _task(suite: str, mode: str, allow_quick: bool, extra: list[str] = ()) -> Task:
    corpus = load(suite, allow_quick=allow_quick)
    samples = [
        Sample(id=q.get("id") or f"{q['category']}:{i}", input=q["question"], target=q["expected"][0],
               metadata={"persona": q["persona"], "expected": q["expected"], "category": q["category"],
                         "allow_quick": allow_quick})
        for i, q in enumerate(corpus.questions) if q["expected"]
    ]
    return Task(dataset=MemoryDataset(samples), solver=rank(suite, mode), scorer=_scorer(METRICS + list(extra)),
                model=MOCK, name=f"{suite}_{mode}" if suite != "answer_quality" else f"answer_quality_retrieval_{mode}",
                metadata={"suite": suite, "quick": corpus.quick, "mode": mode})


@task
def retrieval_multilingual(mode: str = "hybrid", allow_quick: bool = False) -> Task:
    return _task("retrieval_multilingual", mode, allow_quick)


@task
def retrieval_identifiers(mode: str = "hybrid", allow_quick: bool = False) -> Task:
    return _task("retrieval_identifiers", mode, allow_quick)


@task
def answer_quality_retrieval(mode: str = "hybrid", allow_quick: bool = False) -> Task:
    return _task("answer_quality", mode, allow_quick, ["wrong_filing@1"])
