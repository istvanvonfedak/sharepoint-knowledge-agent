"""Evidence on golden items: the structure is checked from the manifest, quotes against the files that are there."""
import copy
import json

import pytest
from golden_items import changed

from sharepoint_evals import evidence, golden
from sharepoint_evals.corpus import Doc

MD = "eval-public/Documents/travel-rates-2026.md"
QUOTE = {"exact": "| London | 220 | 60 |", "prefix": "| Lisbon | 130 | 40 |"}
GOOD = {"inputs": {"question": "What is the hotel cap per night in London?", "persona": "alice"},
        "expected_outputs": {"expect": "answered", "answer": "220 EUR per night.", "facts": ["220 EUR"],
                             "evidence": [{"doc": MD, "section": "Travel Rates 2026 > City allowances", "quote": QUOTE}]},
        "metadata": {"id": "g-9001", "status": "reviewed", "kind": "table", "language": "en", "origin": "written",
                     "author": "a", "reviewer": "b", "added": "2026-11-24"}}


def pdf_bytes(*pages: str) -> bytes:
    """A minimal PDF with one line of text per page, its cross-reference table computed so pypdf reads it cleanly."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{' '.join(f'{3 + 2 * n} 0 R' for n in range(len(pages)))}] /Count {len(pages)} >>"]
    for n, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"
        objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * n} 0 R "
                    f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>",
                    f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    return out + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


@pytest.fixture
def manual(tmp_path, monkeypatch):
    """An external 'manuals' source with one two-page PDF; `manual.present` toggles whether the file is there."""
    data = pdf_bytes("Torque the drain plug to 45 lb-ft", "Torque the filter cap to 25 lb-ft")
    path = tmp_path / "manuals" / "tm.pdf"
    path.parent.mkdir()
    path.write_bytes(data)
    import hashlib
    meta = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "pages": 2, "source": "manuals"}
    doc = Doc("manuals/tm.pdf", ("org:members",), True, path, meta)
    monkeypatch.setattr(golden, "external", lambda: {doc.name: doc})
    golden.corpus.cache_clear()
    yield doc
    golden.corpus.cache_clear()


def pdf_item(**entry):
    item = copy.deepcopy(GOOD)
    item["expected_outputs"]["evidence"] = [{"doc": "manuals/tm.pdf", "pages": [1], "quote": {"exact": "drain plug to 45 lb-ft"}, **entry}]
    return item


def check(item):
    return golden.validate([item])


def test_a_markdown_item_with_a_matching_section_and_quote_passes():
    assert check(GOOD) == []


def test_markdown_rules():
    def with_entry(**changes):
        item = copy.deepcopy(GOOD)
        item["expected_outputs"]["evidence"][0].update(changes)
        return check(item)

    assert with_entry(section="Travel Rates 2026 > Nope")  # no such section
    assert with_entry(quote={"exact": "| London | 999 | 60 |"})  # not in the section
    assert with_entry(section="Travel Rates 2026", quote=QUOTE)  # a real section, but the quote is in another one
    assert with_entry(pages=[1])  # a Markdown file has no pages
    assert with_entry(boxes=[{"page": 1, "bbox": [0, 0, 1, 1]}])
    assert with_entry(unknown=1)
    assert with_entry(role="maybe")
    assert with_entry(group="")
    assert with_entry(doc="eval-public/Documents/missing.md")
    assert with_entry(quote={"exact": "   "})
    assert with_entry(quote={"exact": "London", "extra": 1})
    item = copy.deepcopy(GOOD)
    del item["expected_outputs"]["evidence"][0]["section"]
    assert check(item)  # a required entry on a file with headings needs its section
    del item["expected_outputs"]["evidence"][0]["quote"]
    assert any("needs a quote" in p for p in check(item))


def test_a_quote_that_occurs_twice_needs_a_prefix_or_suffix():
    item = copy.deepcopy(GOOD)
    item["expected_outputs"]["evidence"][0]["section"] = "Travel Rates 2026 > City allowances"
    item["expected_outputs"]["evidence"][0]["quote"] = {"exact": "|"}
    assert any("occurs" in p for p in check(item))
    item["expected_outputs"]["evidence"][0]["quote"] = {"exact": "| 220 |", "prefix": "London"}
    assert check(item) == []


def test_whitespace_in_a_quote_does_not_matter():
    assert evidence.quote_matches("a  b\n c", {"exact": "a b c"}) == 1
    assert evidence.quote_matches("x a b y", {"exact": "a b", "prefix": "x", "suffix": "y"}) == 1
    assert evidence.quote_matches("x a b z", {"exact": "a b", "suffix": "y"}) == 0


def test_evidence_replaces_sources_and_only_answered_items_have_it():
    assert check(changed(GOOD, sources=[MD]))
    assert check(changed(GOOD, also_acceptable=[MD]))
    assert check(changed(GOOD, expect="not_found", answer=None, facts=None))
    assert check(changed(GOOD, evidence=[]))
    assert check(changed(GOOD, evidence=[{**GOOD["expected_outputs"]["evidence"][0], "role": "also_acceptable"}]))  # nothing required


def test_a_persona_must_be_able_to_find_what_an_item_requires():
    item = copy.deepcopy(GOOD)
    item["expected_outputs"]["evidence"][0]["doc"] = "eval-restricted/HR/salaries-2025.md"
    assert any("can't find source" in p for p in check(item))


def test_a_pdf_entry_is_checked_against_its_page(manual):
    assert check(pdf_item()) == []
    assert check(pdf_item(pages=[2], quote={"exact": "filter cap to 25 lb-ft"})) == []
    assert any("not in page 2" in p for p in check(pdf_item(pages=[2])))  # right quote, wrong page
    assert any("past the end" in p for p in check(pdf_item(pages=[3])))
    assert check(pdf_item(pages=[0]))
    assert check(pdf_item(pages=[1, 1]))
    assert check(pdf_item(pages=[]))
    item = pdf_item()
    del item["expected_outputs"]["evidence"][0]["pages"]
    assert any("needs pages" in p for p in check(item))
    assert check(pdf_item(section="Drain plug", printed_page="2-14")) == []  # labels for people
    assert check(pdf_item(printed_page=14))  # as printed, so text


def test_boxes_are_optional_and_checked_for_shape(manual):
    box = {"page": 1, "bbox": [0.1, 0.2, 0.9, 0.4], "modality": "table"}
    assert check(pdf_item(boxes=[box])) == []
    assert check(pdf_item(boxes=[{**box, "page": 2}]))  # not one of the entry's pages
    assert check(pdf_item(boxes=[{**box, "bbox": [0.9, 0.2, 0.1, 0.4]}]))  # x0 > x1
    assert check(pdf_item(boxes=[{**box, "bbox": [0, 0, 1, 1.2]}]))  # outside the page
    assert check(pdf_item(boxes=[{**box, "bbox": [0, 0, 1]}]))
    assert check(pdf_item(boxes=[{**box, "modality": "photo"}]))
    assert check(pdf_item(boxes=[]))


def test_a_file_that_is_not_the_one_in_the_manifest_fails(manual):
    manual.path.write_bytes(pdf_bytes("Torque the drain plug to 50 lb-ft"))
    assert any("sha256 differs" in p for p in check(pdf_item()))


def test_a_file_the_indexer_would_skip_cannot_be_a_source(manual, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_FILE_BYTES", 10)
    manual.path.unlink()  # the manifest says how big the file is, so this is known without it
    assert any("indexer skips it" in p for p in check(pdf_item()))


def test_without_the_file_the_structure_is_still_checked_and_the_item_is_skipped(manual):
    manual.path.unlink()
    assert check(pdf_item()) == []  # quotes can't be checked here, but nothing else is wrong
    assert check(pdf_item(pages=[3]))  # the manifest knows it has two pages
    assert golden.missing_data([pdf_item()]) == {"g-9001": ["manuals/tm.pdf"]}
    assert "1 files" in golden.missing_notice(golden.missing_data([pdf_item()]))
    assert golden.missing_data([GOOD]) == {}
    only_optional = pdf_item(role="also_acceptable")
    only_optional["expected_outputs"]["evidence"].append(GOOD["expected_outputs"]["evidence"][0])
    assert golden.missing_data([only_optional]) == {}  # an optional file doesn't block an item


def test_the_corpus_indexes_only_the_external_files_that_are_there_and_named(manual, monkeypatch):
    assert "manuals/tm.pdf" not in golden.corpus().by_name  # no item names it
    golden.corpus.cache_clear()
    monkeypatch.setattr(golden, "items", lambda: [pdf_item()])
    assert golden.corpus().by_name["manuals/tm.pdf"].acl == ("org:members",)
    golden.corpus.cache_clear()
    manual.path.unlink()
    assert "manuals/tm.pdf" not in golden.corpus().by_name  # named, but not there


def test_a_group_is_satisfied_by_any_of_its_alternatives():
    item = {"expected_outputs": {"evidence": [{"doc": "a.pdf", "group": "g"}, {"doc": "b.pdf", "group": "g"}, {"doc": "c.md"},
                                              {"doc": "d.md", "role": "also_acceptable"}], "also_acceptable": ["e.md"]}}
    assert set(evidence.groups(item)) == {frozenset({"a.pdf", "b.pdf"}), frozenset({"c.md"})}
    assert evidence.acceptable(item) == {"a.pdf", "b.pdf", "c.md", "d.md", "e.md"}
    assert evidence.groups({"expected_outputs": {"sources": ["x.md", "y.md"]}}) == [frozenset({"x.md"}), frozenset({"y.md"})]  # items without evidence


def test_the_committed_manifest_is_well_formed():
    for source, spec in evidence.manifest(golden.DIR)["sources"].items():
        assert spec["acl"] and spec["download"].endswith("/")
        for name, info in spec["files"].items():
            assert {"bytes", "sha256", "pages"} <= set(info) <= {"bytes", "sha256", "pages", "covers"}, name
            assert info["pages"] >= 1 and len(info["sha256"]) == 64, name
            assert source != "manuals" or info.get("covers"), name  # wrong_subject needs every manual's vehicle


def test_only_restricts_the_corpus_and_gets_its_own_database(monkeypatch):
    monkeypatch.setenv("EVAL_GOLDEN_ONLY", "g-0001")
    golden.corpus.cache_clear()
    c = golden.corpus()
    assert c.name == "golden_only"
    assert not [d for d in c.docs if golden.evidence.is_external(d)]
    assert [i["metadata"]["id"] for i in golden.selected(include_drafts=True)] == ["g-0001"]
    monkeypatch.setenv("EVAL_GOLDEN_ONLY", "g-9999")
    with pytest.raises(golden.InvalidGoldenSet):
        golden.selected(include_drafts=True)
    monkeypatch.delenv("EVAL_GOLDEN_ONLY")
    golden.corpus.cache_clear()


def test_an_also_acceptable_entry_may_name_just_the_file(manual):
    item = pdf_item()
    item["expected_outputs"]["evidence"].append({"doc": "manuals/tm.pdf", "role": "also_acceptable"})
    assert check(item) == []  # with the file present, there is nothing to look up in it
    md = copy.deepcopy(GOOD)
    md["expected_outputs"]["evidence"].append({"doc": MD, "role": "also_acceptable"})
    assert check(md) == []
    # still a required entry that lacks them is wrong
    bad = pdf_item()
    del bad["expected_outputs"]["evidence"][0]["pages"]
    assert check(bad)


def test_a_doc_that_is_not_a_string_is_a_problem_not_a_crash():
    for doc in (["a.pdf"], {"x": 1}, 7):
        item = copy.deepcopy(GOOD)
        item["expected_outputs"]["evidence"][0]["doc"] = doc
        assert any("is not in the seeded documents" in p for p in check(item))


def test_the_skip_notice_covers_only_the_items_asked_for(manual, monkeypatch):
    manual_item = pdf_item()
    manual_item["metadata"]["id"] = "g-9002"
    monkeypatch.setenv("EVAL_GOLDEN_DRAFTS", "")  # golden() sets it: let the test put it back
    monkeypatch.setattr(golden, "items", lambda: [GOOD, manual_item])
    monkeypatch.setattr(golden, "missing_data", lambda rows: {i["metadata"]["id"]: ["x"] for i in rows if i is manual_item})
    assert [i["metadata"]["id"] for i in golden.chosen(True)] == ["g-9001", "g-9002"]
    monkeypatch.setenv("EVAL_GOLDEN_ONLY", "g-9001")
    assert [i["metadata"]["id"] for i in golden.chosen(True)] == ["g-9001"]
    assert golden.golden(include_drafts=True).metadata["skipped_missing_data"] == []


def test_the_readers_kept_while_validating_are_bounded():
    for fn in (evidence._reader, evidence._sections):
        assert fn.cache_info().maxsize is not None


def test_an_item_with_alternative_editions_runs_when_one_of_them_is_there(manual, monkeypatch):
    other = Doc("manuals/tm-old.pdf", ("org:members",), True, manual.path.with_name("tm-old.pdf"), dict(manual.meta))
    monkeypatch.setattr(golden, "external", lambda: {manual.name: manual, other.name: other})
    item = pdf_item(group="g")
    item["expected_outputs"]["evidence"].append({**item["expected_outputs"]["evidence"][0], "doc": other.name})
    assert golden.missing_data([item]) == {}  # tm.pdf is there, tm-old.pdf is not
    manual.path.rename(manual.path.with_name("gone.pdf"))
    assert golden.missing_data([item]) == {item["metadata"]["id"]: ["manuals/tm-old.pdf", "manuals/tm.pdf"]}
    two = copy.deepcopy(item)  # without a group, the second file is required in its own right
    del two["expected_outputs"]["evidence"][0]["group"], two["expected_outputs"]["evidence"][1]["group"]
    manual.path.with_name("gone.pdf").rename(manual.path)
    assert golden.missing_data([two]) == {two["metadata"]["id"]: ["manuals/tm-old.pdf"]}


def pages_item():
    item = copy.deepcopy(GOOD)
    item["expected_outputs"]["evidence"] = [{"doc": "m/a.pdf", "pages": [3, 4], "group": "x", "quote": {"exact": "q"}},
                        {"doc": "m/b.pdf", "pages": [9], "group": "x", "quote": {"exact": "q"}},
                        {"doc": "m/c.pdf", "pages": [1], "quote": {"exact": "q"}},
                        {"doc": MD, "section": "s", "quote": {"exact": "q"}},
                        {"doc": "m/d.pdf", "role": "also_acceptable"}]
    return item


def test_the_page_a_source_was_found_on_is_scored_apart_from_the_document():
    here = lambda d: True
    both = {"m/b.pdf": {9}, "m/c.pdf": {1, 7}}
    assert evidence.pages_cited(pages_item(), both, here) == 1.0  # b on its page, c on its page
    assert evidence.pages_cited(pages_item(), {"m/a.pdf": {4}, "m/c.pdf": {1}}, here) == 1.0  # either alternative
    assert evidence.pages_cited(pages_item(), {"m/a.pdf": {5}, "m/c.pdf": {1}}, here) == 0.0  # right file, wrong page
    assert evidence.pages_cited(pages_item(), {"m/a.pdf": {3}}, here) == 0.0  # c not cited
    # a file the run could not search is left out; if nothing is left there is nothing to score
    assert evidence.pages_cited(pages_item(), {"m/c.pdf": {1}}, lambda d: d == "m/c.pdf") == 1.0
    assert evidence.pages_cited(pages_item(), {}, lambda d: False) is None
    assert evidence.pages_cited(GOOD, {}, here) is None  # a markdown item has no pages


def test_cited_pages_come_from_the_sections_of_the_returned_sources():
    from sharepoint_evals import agent_harness, store
    from sharepoint_evals.corpus import Corpus

    doc = Doc("m/a.pdf", ("org:members",), True, None)
    corpus = Corpus("golden", [doc], {}, [])
    run = agent_harness.Run("q", "alice")
    link = store.web_url("golden", "m/a.pdf")
    run.result = type("R", (), {"sources": [{"link": link, "sections": ["TM a > Page 145", "TM a > Page 146", "TM a > Intro"]},
                                          {"link": "https://elsewhere/x", "sections": ["x > Page 9"]}]})()
    assert run.cited_pages(corpus) == {"m/a.pdf": {145, 146}}


def test_a_run_indexes_only_the_manuals_of_the_items_it_scores(manual, monkeypatch):
    item = pdf_item()
    item["metadata"].update(id="g-9003", status="draft")
    monkeypatch.setattr(golden, "items", lambda: [item])
    for flag, expected in (("", False), ("1", True)):
        monkeypatch.setenv("EVAL_GOLDEN_DRAFTS", flag)
        golden.corpus.cache_clear()
        assert (manual.name in {d.name for d in golden.corpus().docs}) is expected  # a draft that isn't scored costs no embeddings
    monkeypatch.setattr(golden, "missing_data", lambda rows: {i["metadata"]["id"]: ["x"] for i in rows})
    golden.corpus.cache_clear()
    assert manual.name not in {d.name for d in golden.corpus().docs}  # nor does an item skipped for another file


def test_a_smoke_test_validates_only_its_own_items(monkeypatch):
    asked = []
    monkeypatch.setattr(golden, "validate", lambda rows=None, docs=None: asked.append(rows) or [])
    monkeypatch.setenv("EVAL_GOLDEN_ONLY", "g-0001")
    golden.corpus.cache_clear()
    assert [i["metadata"]["id"] for i in golden.selected(include_drafts=True)] == ["g-0001"]
    assert [[i["metadata"]["id"] for i in rows] for rows in asked if rows] == [["g-0001"]]
    monkeypatch.delenv("EVAL_GOLDEN_ONLY")
    golden.selected(include_drafts=True)
    assert asked[-1] is None  # a full run validates the whole set
    golden.corpus.cache_clear()


def test_the_task_indexes_the_manuals_of_the_items_it_scores_without_run_py(manual, monkeypatch):
    item = pdf_item()
    item["metadata"].update(id="g-9004", status="draft")
    monkeypatch.setenv("EVAL_GOLDEN_DRAFTS", "")
    reviewed = copy.deepcopy(GOOD)  # so that the task is never empty
    monkeypatch.setattr(golden, "items", lambda: [reviewed, item])
    golden.corpus.cache_clear()
    golden.golden(include_drafts=True)
    assert manual.name in {d.name for d in golden.corpus().docs}  # the flag is the argument, not an environment variable
    golden.golden(include_drafts=False)
    assert manual.name not in {d.name for d in golden.corpus().docs}


def test_content_checks_run_file_by_file_so_each_manual_is_opened_once(manual, monkeypatch):
    other = copy.deepcopy(manual)
    other_path = manual.path.with_name("tm2.pdf")
    other_path.write_bytes(manual.path.read_bytes())
    other = Doc("manuals/tm2.pdf", manual.acl, True, other_path, dict(manual.meta))
    monkeypatch.setattr(golden, "external", lambda: {manual.name: manual, other.name: other})
    opened = []
    real = evidence._content
    monkeypatch.setattr(evidence, "_content", lambda where, e, doc, pdf: opened.append(doc.name) or real(where, e, doc, pdf))
    items = []
    for n, name in enumerate(["manuals/tm.pdf", "manuals/tm2.pdf"] * 3):
        item = pdf_item()
        item["metadata"]["id"] = f"g-91{n:02d}"
        item["expected_outputs"]["evidence"][0]["doc"] = name
        items.append(item)
    assert golden.validate(items) == []
    assert opened == sorted(opened) and opened.count("manuals/tm.pdf") == 3  # interleaved items, grouped checks


def test_the_manifest_keeps_entries_for_files_that_are_not_on_disk(tmp_path, monkeypatch):
    import importlib.util
    import json
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("gm", Path(__file__).resolve().parents[2] / "scripts" / "golden_manifest.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    data = tmp_path / "data"
    (data / "manuals").mkdir(parents=True)
    (data / "manuals" / "a.pdf").write_bytes(pdf_bytes("one"))
    (data / "manuals" / "bad.pdf").write_bytes(b"not a pdf")
    out = tmp_path / "external.json"
    out.write_text(json.dumps({"sources": {"manuals": {"dir": "manuals", "acl": ["org:members"], "files": {
        "gone.pdf": {"bytes": 1, "sha256": "x", "pages": 9}, "bad.pdf": {"bytes": 2, "sha256": "y", "pages": 3}}}}}))
    monkeypatch.setattr(script, "DATA", data)
    monkeypatch.setattr(script, "OUT", out)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["gm", "--dir", str(data / "manuals")])
    assert script.main() == 0
    files = json.loads(out.read_text())["sources"]["manuals"]["files"]
    assert files["gone.pdf"]["pages"] == 9 and files["bad.pdf"]["pages"] == 3  # not on disk / unreadable: kept
    assert files["a.pdf"]["pages"] == 1
    monkeypatch.setattr(sys, "argv", ["gm", "--dir", str(data / "manuals"), "--prune"])
    assert script.main() == 0
    assert set(json.loads(out.read_text())["sources"]["manuals"]["files"]) == {"a.pdf"}


def test_quote_occurrences_do_not_overlap():
    assert evidence.quote_matches("aaa", {"exact": "aa"}) == 1
    assert evidence.quote_matches("aa aa", {"exact": "aa"}) == 2
    assert evidence.quote_matches("x aa y", {"exact": "aa", "prefix": "x", "suffix": "y"}) == 1


def manifest_script(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("gm2", Path(__file__).resolve().parents[2] / "scripts" / "golden_manifest.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "DATA", tmp_path / "data")
    monkeypatch.setattr(script, "OUT", tmp_path / "external.json")
    monkeypatch.setattr(script, "ROOT", tmp_path)
    return script


def test_a_new_source_other_than_the_manuals_must_say_where_it_comes_from_and_who_may_read_it(tmp_path, monkeypatch, capsys):
    import json
    import sys

    script = manifest_script(tmp_path, monkeypatch)
    folder = tmp_path / "data" / "other"
    folder.mkdir(parents=True)
    (folder / "a.pdf").write_bytes(pdf_bytes("one"))
    monkeypatch.setattr(sys, "argv", ["gm", "--dir", str(folder), "--source", "other"])
    assert script.main() == 1 and "--download" in capsys.readouterr().err
    assert not (tmp_path / "external.json").exists()
    monkeypatch.setattr(sys, "argv", ["gm", "--dir", str(folder), "--source", "other", "--download", "https://x/", "--acl", "org:hr"])
    assert script.main() == 0
    entry = json.loads((tmp_path / "external.json").read_text())["sources"]["other"]
    assert entry["acl"] == ["org:hr"] and entry["download"] == "https://x/" and "manuals" not in entry["about"]


def test_two_files_with_the_same_name_cannot_be_fetched_by_name(tmp_path, monkeypatch, capsys):
    import sys

    script = manifest_script(tmp_path, monkeypatch)
    for sub in ("a", "b"):
        (tmp_path / "data" / "manuals" / sub).mkdir(parents=True)
        (tmp_path / "data" / "manuals" / sub / "tm.pdf").write_bytes(pdf_bytes("one"))
    monkeypatch.setattr(sys, "argv", ["gm", "--dir", str(tmp_path / "data" / "manuals")])
    assert script.main() == 1 and "same name" in capsys.readouterr().err
