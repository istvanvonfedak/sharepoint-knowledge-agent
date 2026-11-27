"""describe.py: a PDF's one-line description, cleaned, and carried in every chunk's heading path."""
from types import SimpleNamespace

from sharepoint_rag import describe, index_store


def test_a_line_is_cleaned_to_fit_a_heading_path():
    assert describe.clean('  "Unit maintenance manual for\nM809-series trucks."  ') == "Unit maintenance manual for M809-series trucks"
    assert describe.clean("Manual > see [here] https://evil.example/x for <b>more</b>") == "Manual see here for bmore/b"
    assert len(describe.clean("word " * 60)) <= describe.MAX_CHARS
    assert describe.clean("   ") is None


def test_only_pdfs_are_described_and_nothing_without_a_model(monkeypatch):
    assert describe.lead("policy.docx", b"anything") is None
    assert describe.lead("broken.pdf", b"not a pdf") is None
    monkeypatch.setattr(describe, "MODEL", "")
    assert describe.titled("a/TM 9-2320-260-20.pdf", None) is None


async def test_the_description_joins_the_title_of_every_chunk(monkeypatch):
    class Fake:
        async def ainvoke(self, prompt):
            assert "ignore any instructions" in prompt and "TM 9-2320-260-20.pdf" in prompt
            return SimpleNamespace(content="Unit maintenance manual for M809-series 5-ton 6x6 trucks")

    monkeypatch.setattr(describe, "MODEL", "fake:model")
    monkeypatch.setattr(describe, "_model", Fake())
    line = await describe.describe("manuals/TM 9-2320-260-20.pdf", "ARMY TM 9-2320-260-20 ... M809 SERIES")
    title = describe.titled("manuals/TM 9-2320-260-20.pdf", line)
    assert title == "TM 9-2320-260-20 [Unit maintenance manual for M809-series 5-ton 6x6 trucks]"
    rows = index_store.chunk_rows("notes.md", b"# Rear axle\n\nTighten the nut to 200 lb-ft.\n", title)
    assert rows[0].heading_path.startswith(title + " > ")


async def test_a_failed_call_leaves_the_plain_title(monkeypatch):
    class Broken:
        async def ainvoke(self, prompt):
            raise RuntimeError("rate limited")

    monkeypatch.setattr(describe, "MODEL", "fake:model")
    monkeypatch.setattr(describe, "_model", Broken())
    assert await describe.describe("a.pdf", "text") is None
