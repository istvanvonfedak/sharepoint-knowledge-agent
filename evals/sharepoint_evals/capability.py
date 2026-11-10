"""Capability eval: how well does retrieval find the right document? Scores are a quality gradient
(not pass/fail), tracked over time and compared across search modes and embedding/chunking changes."""
from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver

from sharepoint_evals.questions import QUESTIONS
from sharepoint_evals.retrieval import ranked_docs, retrieve

MOCK = "mockllm/model"


@solver
def retrieve_docs(mode: str):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        hits = await retrieve(state.input_text, state.metadata["persona"], mode, k=20)
        state.metadata["returned"] = ranked_docs(hits)
        state.output = ModelOutput.from_content(MOCK, ", ".join(state.metadata["returned"]))
        return state

    return solve


@scorer(metrics={"hit@1": [mean()], "hit@3": [mean()], "hit@5": [mean()], "mrr": [mean()]})
def rank_of_expected():
    async def score(state: TaskState, target: Target) -> Score:
        returned = state.metadata["returned"]
        rank = returned.index(target.text) + 1 if target.text in returned else None
        return Score(
            value={
                "hit@1": float(rank == 1),
                "hit@3": float(rank is not None and rank <= 3),
                "hit@5": float(rank is not None and rank <= 5),
                "mrr": 1.0 / rank if rank else 0.0,
            },
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
