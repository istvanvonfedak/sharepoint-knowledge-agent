"""Capability eval: how well does retrieval find the right document? Scores are a quality gradient
(not pass/fail), tracked over time and compared across search modes and embedding/chunking changes."""
from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver

from sharepoint_evals.common import MOCK, ranking, set_output
from sharepoint_evals.corpus import synthetic
from sharepoint_evals.questions import QUESTIONS
from sharepoint_evals.retrieval import ranked_docs, retrieve



@solver
def retrieve_docs(mode: str):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        corpus = synthetic()
        hits = await retrieve(corpus, state.input_text, corpus.personas[state.metadata["persona"]], mode, k=20)
        state.metadata["returned"] = ranked_docs(hits)
        set_output(state, ", ".join(state.metadata["returned"]))
        return state

    return solve


@scorer(metrics={"hit@1": [mean()], "hit@3": [mean()], "hit@5": [mean()], "mrr": [mean()]})
def rank_of_expected():
    async def score(state: TaskState, target: Target) -> Score:
        returned = state.metadata["returned"]
        scores = ranking(returned, [target.text], ks=(1, 3, 5))
        rank = returned.index(target.text) + 1 if target.text in returned else None
        return Score(
            value={k: scores[k] for k in ("hit@1", "hit@3", "hit@5", "mrr")},
            answer=returned[0] if returned else "",
            explanation=f"expected {target.text} at rank {rank}; got {returned[:5]}",
        )

    return score


@task
def retrieval_capability(mode: str = "hybrid") -> Task:
    samples = [
        Sample(id=f"{q.category}:{i}", input=q.question, target=q.expected, metadata={"persona": q.persona, "category": q.category})
        for i, q in enumerate(QUESTIONS)
    ]
    return Task(dataset=MemoryDataset(samples), solver=retrieve_docs(mode), scorer=rank_of_expected(), model=MOCK, name=f"retrieval_capability_{mode}")
