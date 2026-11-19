"""extraction_robustness: which outcomes pass the gate, what counts as a regression, and the isolated worker."""
import asyncio
import zipfile

from sharepoint_evals import extraction


def test_expected_outcomes():
    assert extraction.expected(".pdf", {"outcome": "chunks"})
    assert extraction.expected(".pdf", {"outcome": "no_text"})
    assert extraction.expected(".pdf", {"outcome": "ExtractionError", "message": "PdfReadError: x"})
    assert extraction.expected(".pdf", {"outcome": "UnsupportedFileType", "message": "password-protected pdf"})
    assert not extraction.expected(".docx", {"outcome": "UnsupportedFileType", "message": ".docx"})
    assert extraction.expected(".doc", {"outcome": "UnsupportedFileType", "message": ".doc"})
    assert not extraction.expected(".doc", {"outcome": "ExtractionError", "message": "x"})  # exact class
    for bad in ("killed", "crashed"):
        assert not extraction.expected(".pdf", {"outcome": bad})


def test_regressions_against_the_snapshot():
    sha = "abc"
    chunks, none = {"sha256": sha, "outcome": "chunks"}, {"sha256": sha, "outcome": "no_text"}
    err = {"sha256": sha, "outcome": "ExtractionError", "message": "KeyError: 'x'"}
    assert extraction.regression(chunks, {"outcome": "ExtractionError", "message": "y"}, sha)
    assert extraction.regression(none, {"outcome": "ExtractionError", "message": "y"}, sha)
    assert extraction.regression(err, {"outcome": "ExtractionError", "message": "TypeError: new crash"}, sha)
    assert extraction.regression(err, {"outcome": "ExtractionError", "message": "KeyError: 'x'"}, sha) is None
    assert extraction.regression(err, {"outcome": "chunks"}, sha) is None  # an improvement
    assert extraction.regression(None, {"outcome": "killed"}, sha) is None  # new file: the gate judges it
    assert "changed" in extraction.regression(chunks, {"outcome": "chunks"}, "other")


def test_worker_runs_each_file_in_its_own_process(tmp_path):
    good = tmp_path / "a.md"
    good.write_text("# T\n\nHello world.\n")
    broken = tmp_path / "b.docx"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("word/document.xml", "<not xml")
    assert asyncio.run(extraction.run_isolated(good, "a.md"))["outcome"] == "chunks"
    assert asyncio.run(extraction.run_isolated(broken, "b.docx"))["outcome"] == "ExtractionError"
    assert asyncio.run(extraction.run_isolated(good, "a.exe"))["outcome"] == "UnsupportedFileType"


def test_worker_over_the_time_limit_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, "TIME_LIMIT", 0.0)
    path = tmp_path / "a.md"
    path.write_text("x")
    result = asyncio.run(extraction.run_isolated(path, "a.md"))
    assert result["outcome"] == "killed" and "time" in result["message"]


def test_isolated_worker_outcomes(tmp_path):
    import asyncio

    from sharepoint_evals.extraction import run_isolated

    good = tmp_path / "note.md"
    good.write_text("# Title\n\nSome text worth indexing.\n")
    result = asyncio.run(run_isolated(good, "note.md", rows=True))
    assert result["outcome"] == "chunks" and result["rows"][0][1].startswith("Some text")
    # A file that vanished makes the worker die with a traceback: reported as a crash, not raised.
    assert asyncio.run(run_isolated(tmp_path / "gone.pdf", "gone.pdf"))["outcome"] == "crashed"
