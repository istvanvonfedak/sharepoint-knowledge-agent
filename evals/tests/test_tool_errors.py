"""A run whose retriever fails (the database, the embedding provider) is scored as an error, not as the agent's
answer, and an eval stops once runs keep failing: an outage would otherwise be paid for and graded case by case."""
from types import SimpleNamespace

import pytest

from sharepoint_evals import agent_evals, agent_harness, run


class Down:
    def __init__(self, pool, mode):
        pass

    async def search(self, *a, **k):
        raise RuntimeError("Gemini 402: Your prepayment credits are depleted.")


async def test_a_failing_search_is_recorded_and_marks_the_run(tiny, monkeypatch):
    async def no_pool(corpus):
        return None

    monkeypatch.setattr(agent_harness, "pool", no_pool)
    monkeypatch.setattr(agent_harness, "PostgresRetriever", Down)
    r = agent_harness.Run("q", "ann")
    tools = agent_harness.tools(tiny, "ann", r)
    with pytest.raises(RuntimeError):  # the agent still gets the error, as in production
        await tools.search("annual leave", 5)
    assert r.calls == [{"tool": "search", "query": "annual leave", "items": [],
                        "error": "RuntimeError: Gemini 402: Your prepayment credits are depleted."}]
    assert r.tool_error.startswith("search: RuntimeError: Gemini 402")
    assert r.summary()["tool_error"] == r.tool_error


def test_a_run_whose_retriever_failed_counts_as_an_error_and_keeps_only_what_it_showed_the_user():
    result = SimpleNamespace(status="not_found", answer="I couldn't retrieve any documents.", sources=[], steps=2, tokens=400)
    r = agent_harness.Run("q", "ann", result=result, tool_error="search: RuntimeError: 402")
    values = {**agent_evals.common(r), "permissions_ok": 1.0, "must_not_state": 0.0, "status_ok": 0.0, "fact_recall": 0.0}
    scored = agent_evals.measured(r, values)
    assert scored["error"] == 1.0  # a failure: --rerun-failed picks it up, and the summary shows it
    assert scored["status_ok"] is None and scored["fact_recall"] is None  # not the agent's: left out of the means
    assert scored["permissions_ok"] == 1.0 and scored["must_not_state"] == 0.0  # what the user saw still counts
    ok = agent_harness.Run("q", "ann", result=result)
    assert agent_evals.measured(ok, {**agent_evals.common(ok), "status_ok": 0.0})["status_ok"] == 0.0


async def test_the_eval_stops_after_runs_keep_failing(monkeypatch):
    failing = agent_harness.Run("q", "ann", tool_error="search: RuntimeError: 402")

    async def ask(*a, **k):
        return failing

    monkeypatch.setattr(agent_harness, "ask", ask)
    monkeypatch.setattr(agent_evals, "load", lambda suite, allow_quick: None)
    monkeypatch.setattr(agent_evals, "_failing", 0)
    solve = agent_evals.run_agent("golden")

    def state(i):
        return SimpleNamespace(metadata={"allow_quick": False, "persona": "ann"}, input_text="q", sample_id=f"g-{i}",
                               epoch=1, output=None)

    monkeypatch.setattr(agent_evals, "set_output", lambda s, text: None)
    for i in range(agent_evals.TRIP - 1):
        await solve(state(i), None)
    with pytest.raises(RuntimeError, match="--rerun-failed"):
        await solve(state(agent_evals.TRIP), None)


def test_a_stopped_runs_unfinished_cases_are_rerun():
    def scored(id, epoch):
        return SimpleNamespace(id=id, epoch=epoch, scores={"golden": SimpleNamespace(value={"error": 0.0})})

    log = SimpleNamespace(
        eval=SimpleNamespace(config=SimpleNamespace(sample_id=["g-0001", "g-0002", "g-0003"], epochs=2),
                             dataset=SimpleNamespace(sample_ids=None)),
        samples=[scored("g-0001", 1), scored("g-0001", 2), scored("g-0002", 1),  # g-0002 has one epoch of two
                 SimpleNamespace(id="g-0003", epoch=1, scores=None)])  # errored: no score
    assert run.unfinished_cases(log) == {"g-0002", "g-0003"}
