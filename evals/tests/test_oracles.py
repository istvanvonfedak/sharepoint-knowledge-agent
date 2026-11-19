"""Permission oracles, computed from the dataset's lists, and the acl_scale case selection."""
import pytest
from sharepoint_rag import acl

from sharepoint_evals import acl_scale, corpus


def test_visible_and_readable(tiny):
    assert tiny.visible(tiny.personas["ann"]) == {"own.md", "org.md", "everyone.md", "report.pdf"}
    assert "unresolved.md" in tiny.readable(tiny.personas["ann"])  # SharePoint would let Ann open it
    guest = tiny.visible(tiny.personas["guest"])
    assert guest == {"group.md", "everyone.md"}  # the group's and org:everyone, never org:members


def test_layer1_under_both_policies(tiny):
    hide = acl_scale.layer1(tiny, tiny.personas["bob"])
    assert "unresolved.md" not in hide
    live = acl_scale.layer1(tiny, tiny.personas["bob"] + [acl.UNRESOLVED])
    assert "unresolved.md" in live  # reaches the live check, which then decides
    assert "unresolved.md" not in acl_scale.after_layer2(tiny, "bob", tiny.personas["bob"] + [acl.UNRESOLVED])


def test_admin_holds_every_principal_but_never_unresolved(tiny):
    admin = acl_scale.admin_principals(tiny)
    assert acl.UNRESOLVED not in admin and {"user:ann", "group:legal", acl.ORG_MEMBERS, acl.ORG_EVERYONE} <= set(admin)


def test_controls_cover_every_grant_and_every_guest_file(tiny):
    picked = acl_scale.controls(tiny)
    assert {g for p, _, g in picked if p == "bob"} == {"user", "group", acl.ORG_MEMBERS, acl.ORG_EVERYONE}
    assert {d for p, d, _ in picked if p == "guest"} == tiny.visible(tiny.personas["guest"])


def test_revocation_is_deterministic_and_about_ten_percent():
    picks = [corpus.revoked("p", f"doc{i}") for i in range(5000)]
    assert picks == [corpus.revoked("p", f"doc{i}") for i in range(5000)]
    assert 0.08 < sum(picks) / len(picks) < 0.12


def test_missing_and_quick_suites_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "DATA", tmp_path)
    corpus._load_public.cache_clear()
    with pytest.raises(corpus.SuiteNotBuilt):
        corpus.load("acl_scale")
    (tmp_path / "acl_scale").mkdir()
    (tmp_path / "acl_scale" / "SOURCE.json").write_text('{"params": {"quick": true}}')
    (tmp_path / "acl_scale" / "docs.jsonl").write_text("")
    corpus._load_public.cache_clear()
    with pytest.raises(corpus.QuickSuite):
        corpus.load("acl_scale")
    assert corpus.load("acl_scale", allow_quick=True).quick
    corpus._load_public.cache_clear()


def test_index_title_skips_generated_names():
    from pathlib import Path

    from sharepoint_evals.corpus import Doc
    from sharepoint_evals.store import index_title

    def title(name, suite):
        return index_title(Doc(name, (), True, Path("x")), suite)

    assert title("3M_2018_10K.pdf", "answer_quality") == "3M_2018_10K"
    assert title("tickets/INC-97718.md", "retrieval_identifiers") == "INC-97718"
    assert title("enron-0a43d18ae365.md", "acl_scale") == ""
    assert title("squad-b820b2516a21.md", "agent_safety") == ""
    assert title("executive-compensation-review.md", "agent_safety") == "executive-compensation-review"
    assert title("build-0a1b2c3d4e5f.md", "golden") == "build-0a1b2c3d4e5f"  # a real name that looks like a hash
