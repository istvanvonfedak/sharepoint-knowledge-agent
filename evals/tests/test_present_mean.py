"""The log's own metrics skip checks that don't apply (None), as run.py's report does: Inspect's default epoch reducer
turned every None into 0.0, even with one epoch, so `inspect view` showed fact_recall 0.68 for a run at 0.89."""
from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import TaskState

from sharepoint_evals.agent_evals import epochs_of, present_mean
from sharepoint_evals.common import MOCK

VALUES = {  # sample -> per epoch: an answered item, a not_found item (nothing to grade), a grading failure
    "answered": [{"ok": 1.0, "graded": 1.0}, {"ok": 1.0, "graded": 0.0}],
    "not_found": [{"ok": 0.0, "graded": None}, {"ok": 1.0, "graded": None}],
    "failed_grading": [{"ok": 1.0, "graded": None}, {"ok": 1.0, "graded": 1.0}],
}


@scorer(metrics={k: [present_mean()] for k in ("ok", "graded")}, name="toy")
def toy_scores():
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=VALUES[str(state.sample_id)][state.epoch - 1])

    return score


def _metrics(tmp_path, epochs: int) -> dict[str, float]:
    task = Task(dataset=MemoryDataset([Sample(id=i, input="q") for i in VALUES]), scorer=toy_scores(), model=MOCK,
                epochs=epochs_of(epochs))
    [log] = inspect_eval(task, log_dir=str(tmp_path), display="none")
    return {s.name: next(iter(s.metrics.values())).value for s in log.results.scores}  # one metric each: present_mean


def test_one_epoch_means_only_the_samples_that_have_a_value(tmp_path):
    metrics = _metrics(tmp_path, 1)
    assert metrics["ok"] == 2 / 3
    assert metrics["graded"] == 1.0  # one graded sample, not 1 of 3


def test_epochs_are_averaged_per_sample_over_the_runs_that_have_a_value(tmp_path):
    metrics = _metrics(tmp_path, 2)
    # per sample: answered 0.5, not_found nothing, failed_grading 1.0 (one graded run of two)
    assert metrics["graded"] == 0.75
    assert metrics["ok"] == (1.0 + 0.5 + 1.0) / 3
