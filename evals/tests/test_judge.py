"""The judge set: the committed cases are valid, each rule catches what it should, and verdicts are compared right."""
import sys

import pytest
from golden_items import change, judge_bucket

from sharepoint_evals import judge

GOOD = {"inputs": {"question": "q?", "language": "en", "reference": "r", "facts": ["a", "b"], "answer": "ans", "sources": "src"},
        "expected_outputs": {"labels": {"facts": [True, False], "grounded": True, "language_ok": True}},
        "metadata": {"id": "j-9999", "status": "draft", "rubric": "golden", "kind": "faithful", "origin": "synthetic",
                     "author": "claude", "reviewer": "", "added": "2026-11-25"}}


def problems(**changes) -> list[str]:
    return judge.validate([change(GOOD, judge_bucket, **changes)])


def test_the_committed_set_is_valid_and_covers_what_the_grader_gets_wrong():
    assert judge.validate() == []
    rows = judge.items()
    assert len(rows) >= 90
    assert {r["metadata"]["origin"].split(":")[0] for r in rows} == {"synthetic", "run"}  # made-up failures and real agent answers
    for key in ("grounded", "language_ok"):
        assert {r["expected_outputs"]["labels"][key] for r in rows} == {True, False}
    assert {v for r in rows for v in r["expected_outputs"]["labels"]["facts"]} == {True, False}
    assert {r["inputs"]["language"] for r in rows} == {"en", "es"}
    # the cases that probe the grader's known weak spots
    kinds = {r["metadata"]["kind"] for r in rows}
    assert {"label_not_in_source", "no_sources", "injection_answer", "injection_source", "unsupported_claim"} <= kinds


def test_a_good_case_passes_and_each_rule_catches_its_problem():
    assert judge.validate([GOOD]) == []
    assert judge.validate([GOOD, GOOD])  # duplicate id
    assert problems(status="done")
    assert problems(rubric="correctness")
    assert problems(language="fr")
    assert problems(labels={"facts": [True], "grounded": True, "language_ok": True})  # 2 facts, 1 label
    assert problems(labels={"facts": [True, False], "grounded": "yes", "language_ok": True})
    assert problems(labels={"facts": [True, False], "grounded": True})  # language_ok missing
    assert problems(answer=" ")
    assert problems(status="reviewed")  # no reviewer
    assert problems(extra="x")
    assert problems(origin="invented")
    assert not problems(origin="run:2026-11-25")
    assert not problems(status="reviewed", reviewer="someone")


def verdict(facts, grounded=True, language_ok=True):
    return {"facts": facts, "grounded": grounded, "language_ok": language_ok, "reason": ""}


def test_compare_counts_false_accepts_and_false_rejects():
    labels = {"facts": [True, False], "grounded": False, "language_ok": True}
    right = judge.compare(labels, verdict([True, False], grounded=False))
    assert right["all_agree"] == 1 and right["facts_agree"] == 1 and right["grounded_agree"] == 1
    assert right["fact_false_accept"] == 0 and right["fact_false_reject"] == 0
    assert right["grounded_false_accept"] == 0 and right["grounded_false_reject"] is None
    lenient = judge.compare(labels, verdict([True, True], grounded=True))
    assert lenient["all_agree"] == 0 and lenient["facts_agree"] == 0.5
    assert lenient["fact_false_accept"] == 1 and lenient["fact_false_reject"] == 0
    assert lenient["grounded_false_accept"] == 1 and lenient["grounded_agree"] == 0
    strict = judge.compare({"facts": [True], "grounded": True, "language_ok": True}, verdict([False], False, False))
    assert strict["fact_false_reject"] == 1 and strict["fact_false_accept"] is None
    assert strict["grounded_false_reject"] == 1 and strict["language_false_reject"] == 1


def test_a_verdict_with_too_few_facts_disagrees():
    out = judge.compare({"facts": [True, True], "grounded": True, "language_ok": True}, verdict([True]))
    assert out["facts_agree"] == 0.5 and out["all_agree"] == 0 and out["fact_false_reject"] == 0.5


def test_draft_cases_need_the_flag():
    assert len(judge.golden_judge(include_drafts=True).dataset) == len(judge.items())
    assert judge.selected(False) == []


def test_a_rubric_version_can_be_chosen_by_env(monkeypatch):
    import importlib

    from sharepoint_evals import grader
    try:
        monkeypatch.setenv("EVAL_RUBRIC_VERSIONS", "golden=1")
        importlib.reload(grader)
        template, label = grader.rubric("golden")
        assert label == "golden.v1" and "{sources}" in template and "{facts}" in template
        monkeypatch.setenv("EVAL_RUBRIC_VERSIONS", "golden")
        with pytest.raises(ValueError):
            importlib.reload(grader)
    finally:
        monkeypatch.delenv("EVAL_RUBRIC_VERSIONS", raising=False)
        importlib.reload(grader)
    assert grader.rubric("golden")[1] == "golden.v4"  # the default


def test_the_manifest_script_refuses_a_folder_outside_the_data_dir_before_hashing(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "golden_manifest.py"
    spec = importlib.util.spec_from_file_location("golden_manifest", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(script, "DATA", tmp_path / "data")
    monkeypatch.setattr(sys, "argv", ["golden_manifest.py", "--dir", str(outside)])
    monkeypatch.setattr(script, "describe", lambda p: pytest.fail("hashed a file before checking the folder"))
    assert script.main() == 1
    assert "is not under" in capsys.readouterr().err


def test_a_reviewer_must_be_someone_other_than_the_author():
    assert problems(status="reviewed", reviewer="claude")  # GOOD's author
    assert not problems(status="reviewed", reviewer="someone else")


def test_malformed_labels_are_problems_not_crashes():
    for facts in (True, "ab", None, {"a": 1}):
        assert problems(labels={"facts": facts, "grounded": True, "language_ok": True})
    assert problems(facts="not a list")


def test_only_needs_the_agent_tier_and_leaves_the_other_agent_suites_out(monkeypatch):
    from sharepoint_evals import run

    seen = []
    monkeypatch.setattr(run, "run_tier", lambda tier, excluded, o: seen.append((tier, excluded)) or True)
    monkeypatch.setattr(sys, "argv", ["run.py", "agent", "--only", "g-0001"])
    with pytest.raises(SystemExit) as done:
        run.main()
    assert done.value.code == 0 and seen == [("agent", {"answer_quality", "agent_safety"})]
    monkeypatch.setattr(sys, "argv", ["run.py", "regression", "--only", "g-0001"])
    with pytest.raises(SystemExit) as refused:
        run.main()
    assert refused.value.code == 2 and len(seen) == 1
    for name in ("EVAL_GOLDEN_ONLY", "EVAL_GOLDEN_DRAFTS"):
        monkeypatch.delenv(name, raising=False)


def test_skipped_items_are_named_next_to_the_gates(capsys):
    from types import SimpleNamespace

    from sharepoint_evals import golden, run

    guarded = next(i["metadata"]["id"] for i in golden.items() if i["expected_outputs"].get("must_not_see"))
    log = SimpleNamespace(eval=SimpleNamespace(task="golden", config=SimpleNamespace(epochs=1),
                                               metadata={"grader": "g", "skipped_missing_data": [guarded, "g-0049"]}),
                          results=SimpleNamespace(scores=[]), samples=[])
    assert run.report_agent(log, {}, {}) is False  # permission checks did not run
    out = capsys.readouterr().out
    assert "2 item(s) were skipped" in out and guarded in out and "permission checks" in out and "GATE permissions" in out
    assert run.report_agent(log, {}, {}, allow_missing_data=True) is True
    log.eval.metadata["skipped_missing_data"] = ["g-0049"]  # skipped, but nothing in them is guarded
    assert run.report_agent(log, {}, {}) is True


def test_grader_settings(monkeypatch):
    import importlib

    from sharepoint_evals import grader
    try:
        monkeypatch.setenv("EVAL_GRADER_MODEL", "openai:gpt-6-sol")
        monkeypatch.setenv("EVAL_GRADER_KWARGS", '{"reasoning_effort": "medium"}')
        importlib.reload(grader)
        assert grader.model_kwargs() == {"reasoning_effort": "medium"}  # no temperature: reasoning models reject 0
        monkeypatch.delenv("EVAL_GRADER_KWARGS", raising=False)
        monkeypatch.setenv("EVAL_GRADER_MODEL", "openai:gpt-4o")  # not a reasoning model: it takes temperature 0
        importlib.reload(grader)
        assert grader.model_kwargs() == {"temperature": 0}
        monkeypatch.setenv("EVAL_GRADER_KWARGS", '{"reasoning_effort": "low"}')
        assert grader.model_kwargs() == {"reasoning_effort": "low"}
        monkeypatch.delenv("EVAL_GRADER_KWARGS", raising=False)
        monkeypatch.setenv("EVAL_GRADER_MODEL", "google_genai:gemini-pro-latest")
        importlib.reload(grader)
        monkeypatch.setenv("EVAL_GRADER_MODEL", "google_genai:gemini-pro-latest")
        monkeypatch.delenv("EVAL_GRADER_KWARGS", raising=False)
        importlib.reload(grader)
        assert grader.model_kwargs() == {"temperature": 0}
        monkeypatch.setenv("EVAL_GRADER_KWARGS", "[1]")
        with pytest.raises(ValueError):
            grader.model_kwargs()
    finally:
        monkeypatch.undo()
        importlib.reload(grader)


def test_extra_fact_verdicts_are_disagreements():
    out = judge.compare({"facts": [True], "grounded": True, "language_ok": True}, verdict([True, True]))
    assert out["facts_agree"] == 0.5 and out["all_agree"] == 0
    assert out["fact_false_accept"] is None and out["fact_false_reject"] == 0  # the extra one has no label to be wrong against


def test_the_judge_task_runs_three_epochs_by_default():
    assert judge.golden_judge(include_drafts=True).epochs == 3


def test_a_model_that_refuses_temperature_is_graded_again_without_it(monkeypatch):
    import asyncio

    from sharepoint_evals import grader

    built = []

    class Fake:
        def __init__(self, **kw):
            self.kw = kw
            built.append(kw)

        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, prompt):
            if "temperature" in self.kw:
                raise RuntimeError("400 Unsupported value: 'temperature' does not support 0.0 with this model")
            return grader.SCHEMAS["golden"](facts=[True], grounded=True, language_ok=True, reason="ok")

    monkeypatch.setattr(grader, "MODEL", "openai:some-reasoning-model")
    monkeypatch.setattr(grader, "_model", None)
    monkeypatch.setattr(grader, "_no_temperature", False)
    monkeypatch.setattr("langchain.chat_models.init_chat_model", lambda name, **kw: Fake(**kw))
    verdict = asyncio.run(grader.grade("golden", question="q", language="en", reference="r", facts="f", answer="a", sources="s"))
    assert verdict["grounded"] is True and built == [{"temperature": 0}, {}]
    monkeypatch.setattr(grader, "_no_temperature", False)  # other errors are not swallowed
    monkeypatch.setattr(grader, "_model", None)

    class Broken(Fake):
        async def ainvoke(self, prompt):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr("langchain.chat_models.init_chat_model", lambda name, **kw: Broken(**kw))
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(grader.grade("golden", question="q", language="en", reference="r", facts="f", answer="a", sources="s"))
