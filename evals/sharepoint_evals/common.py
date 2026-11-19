"""Helpers shared by the eval tasks: Inspect plumbing for tasks that call no model, and ranking metrics."""
import math

from inspect_ai.model import ModelOutput
from inspect_ai.solver import TaskState

MOCK = "mockllm/model"  # Inspect needs a model; these tasks never generate
MODES = ("vector", "keyword", "hybrid")


def set_output(state: TaskState, text: str) -> None:
    state.output = ModelOutput.from_content(MOCK, text)


def ranking(returned: list[str], relevant: list[str] | set[str], ks=(1, 5, 10)) -> dict[str, float]:
    """hit@k (any relevant doc in the top k), MRR (first relevant) and binary nDCG@10 over a ranked doc list."""
    relevant = set(relevant)
    rank = next((i for i, d in enumerate(returned, 1) if d in relevant), None)
    dcg = sum(1 / math.log2(i + 1) for i, d in enumerate(returned[:10], 1) if d in relevant)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(relevant), 10) + 1))
    return {**{f"hit@{k}": float(rank is not None and rank <= k) for k in ks},
            "mrr": 1 / rank if rank else 0.0, "ndcg@10": dcg / ideal if ideal else 0.0}
