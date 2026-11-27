"""--rerun-failed: which cases count as failed, and the selectors that run them again on the full corpus."""
from types import SimpleNamespace

from sharepoint_evals import run

PASS = {"permissions_ok": 1.0, "status_ok": 1.0, "sources_cited": 1.0, "citations_ok": 1.0, "pages_cited": 0.0,
        "fact_recall": 1.0, "grounded": 1.0, "language_ok": 1.0, "links_ok": 1.0, "budget_exhausted": 0.0, "error": 0.0}


def sample(id, epoch=1, metadata=None, **changes):
    return SimpleNamespace(id=id, epoch=epoch, scores={"golden": SimpleNamespace(value={**PASS, **changes})},
                           metadata=metadata or {})


def test_a_case_fails_on_any_check_in_any_run_but_not_on_pages_or_checks_that_dont_apply():
    samples = [
        sample("g-0001"),  # passes; pages_cited 0 is tracked, not a check
        sample("g-0002", sources_cited=None, fact_recall=None, grounded=None),  # a not_found item: nothing to cite
        sample("g-0003", epoch=1), sample("g-0003", epoch=2, fact_recall=0.5),  # failed one run of two
        sample("g-0004", grounded=None, metadata={"grader_errors": ["golden: RateLimitError"]}),  # not graded
        sample("g-0005", permissions_ok=0.0),
        sample("g-0006", budget_exhausted=1.0),
        SimpleNamespace(id="g-0007", epoch=1, scores={}, metadata={}),  # the scorer crashed
    ]
    assert run.failed_cases("golden", samples) == {"g-0003", "g-0004", "g-0005", "g-0006", "g-0007"}


def test_the_selectors_name_the_current_task_and_skip_cases_no_longer_in_the_set(capsys):
    task = SimpleNamespace(name="golden", dataset=[SimpleNamespace(id="g-0003"), SimpleNamespace(id="g-0005")])
    o = run.Options(None, None, False, None, False, False, False, rerun={"golden": {"g-0003", "g-0005", "g-0099"}})
    assert run._rerun_ids([task], o) == ["golden:g-0003", "golden:g-0005"]
    assert "g-0099" in capsys.readouterr().out


def test_case_says_whether_drafts_are_needed_and_refuses_ids_that_wont_run():
    import pytest

    assert run.chosen_drafts([], "agent") is False
    assert run.chosen_drafts(["g-0001"], "agent") is False  # reviewed
    assert run.chosen_drafts(["g-0001", "g-0120"], "agent") is True  # a draft: drafts are loaded
    with pytest.raises(ValueError, match="retired"):
        run.chosen_drafts(["g-0086"], "agent")
    with pytest.raises(ValueError, match="no golden item"):
        run.chosen_drafts(["g-9999"], "agent")
