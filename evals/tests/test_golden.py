"""The golden set's validator: the committed set is valid, and each rule catches what it should."""
import copy

from sharepoint_evals import golden

GOOD = {"id": "g-9001", "status": "reviewed", "question": "How many days of annual leave do we get?", "language": "en",
        "persona": "alice", "kind": "fact", "expect": "answered", "answer": "22 working days.", "facts": ["22 working days"],
        "sources": ["eval-public/Documents/leave-policy.md"], "origin": "written", "author": "a", "reviewer": "b",
        "added": "2026-11-18"}


def problems(**changes):
    item = copy.deepcopy(GOOD)
    for k, v in changes.items():
        if v is None:
            item.pop(k)
        else:
            item[k] = v
    return golden.validate([item])


def test_the_committed_set_is_valid_and_covers_every_kind_and_persona():
    assert golden.validate() == []
    items = golden.items()
    assert {i["kind"] for i in items} == golden.KINDS
    assert {i["persona"] for i in items} == set(golden.corpus().personas)
    assert sum(i["language"] == "es" for i in items) * 4 >= len(items)
    assert sum(i["kind"] == "restricted" for i in items) >= 10 and sum(i["kind"] == "unanswerable" for i in items) >= 10


def test_a_good_item_passes():
    assert problems() == []


def test_rules():
    assert problems(id="42")
    assert problems(status="done")
    assert problems(persona="mallory")
    assert problems(facts=[])  # an answered item needs facts
    assert problems(sources=["eval-public/Documents/missing.md"])  # not in the seeded documents
    assert problems(sources=["eval-restricted/HR/salaries-2025.md"])  # alice can't read it
    assert problems(must_not_see=["eval-public/Documents/expenses.md"])  # alice can read it
    assert problems(reviewer="a")  # reviewed by its own author
    assert problems(origin="bug")
    assert problems(added="yesterday")
    assert problems(answer=None)
    assert problems(extra="field")
    assert golden.validate([GOOD, GOOD])  # duplicate id


def test_unresolved_files_cannot_make_an_item_depend_on_the_policy():
    # HR really can read the reorg plan, but search hides it (unresolved): neither a source nor a must_not_see.
    assert problems(persona="hr", sources=["eval-restricted/HR/reorg-plan.md"])
    assert problems(persona="hr", expect="not_found", kind="restricted", answer=None, facts=None, sources=None,
                    must_not_see=["eval-restricted/HR/reorg-plan.md"])
