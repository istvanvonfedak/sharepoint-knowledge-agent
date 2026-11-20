import io
import random
import zipfile

import pytest
from sharepoint_rag.chunk import chunk_sections, rejoin, rejoin_spans
from sharepoint_rag.extract import ExtractionError, Section, UnsupportedFileType, extract
from tests.testing_support import text_pdf

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def make_zip(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


def para(text, style=None):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def test_docx_headings_become_paths():
    body = para("Policies", "Heading1") + para("Intro text") + para("Leave", "Heading2") + para("20 days") \
        + para("Expenses", "Heading1") + para("Submit monthly")
    sections = extract("a.docx", make_zip({"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"}))
    assert [(s.heading_path, s.text) for s in sections] == [
        ("Policies", "Intro text"), ("Policies > Leave", "20 days"), ("Expenses", "Submit monthly"),
    ]


def test_docx_text_box_not_duplicated():
    xml = f"<w:document {W}><w:body><w:p><w:r><w:t>Outer</w:t><w:txbxContent>{para('Boxed')}</w:txbxContent></w:r></w:p></w:body></w:document>"
    text = extract("a.docx", make_zip({"word/document.xml": xml}))[0].text
    assert text.count("Boxed") == 1 and "Outer" in text


def slide(t):
    return f"<p:sld {A} xmlns:p='p'><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>"


def test_pptx_fallback_numeric_order_without_presentation_xml():
    data = make_zip({f"ppt/slides/slide{n}.xml": slide(f"s{n}") for n in (10, 2, 1)})
    assert [s.text for s in extract("d.pptx", data)] == ["s1", "s2", "s10"]


def test_pptx_follows_presentation_order():
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pres = (f'<p:presentation xmlns:p="p" xmlns:r="{R}"><p:sldIdLst>'
            '<p:sldId id="1" r:id="rB"/><p:sldId id="2" r:id="rA"/></p:sldIdLst></p:presentation>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rA" Target="slides/slide1.xml"/><Relationship Id="rB" Target="slides/slide7.xml"/></Relationships>')
    data = make_zip({"ppt/presentation.xml": pres, "ppt/_rels/presentation.xml.rels": rels,
                     "ppt/slides/slide1.xml": slide("was-first"), "ppt/slides/slide7.xml": slide("shown-first")})
    assert [(s.heading_path, s.text) for s in extract("d.pptx", data)] == [("Slide 1", "shown-first"), ("Slide 2", "was-first")]


def test_robustness_nul_utf16_fences_and_corrupt_files():
    assert extract("a.txt", "hi there".encode("utf-16"))[0].text == "hi there"
    assert extract("a.txt", b"a\x00b")[0].text == "ab"
    md = extract("a.md", b"# T\n```\n# comment\n```\ntext")
    assert [s.heading_path for s in md] == ["T"] and "# comment" in md[0].text
    with pytest.raises(ExtractionError):
        extract("a.docx", b"not a zip")
    with pytest.raises(ExtractionError):
        extract("a.docx", make_zip({"other.xml": "<x/>"}))


def test_docx_alternate_content_and_localized_styles():
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    box = para("Boxed")
    body = (para("Titre", "Titre1") + para("corps")
            + f'<w:p><w:r><mc:AlternateContent xmlns:mc="{mc}"><mc:Choice>{box}</mc:Choice><mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p>')
    styles = f'<w:styles {W}><w:style w:styleId="Titre1"><w:name w:val="heading 1"/></w:style></w:styles>'
    data = make_zip({"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>", "word/styles.xml": styles})
    sections = extract("a.docx", data)
    assert sections[0].heading_path == "Titre" and sections[0].text.count("Boxed") == 1


def test_markdown_and_html_and_text():
    md = extract("a.md", b"# Title\nintro\n## Sub\nbody")
    assert [(s.heading_path, s.text) for s in md] == [("Title", "intro"), ("Title > Sub", "body")]
    assert extract("a.html", b"<html><head><style>x{}</style></head><body><p>Hi</p><script>no()</script><p>there</p></body></html>")[0].text == "Hi\n\nthere"
    assert extract("a.txt", b"\xef\xbb\xbfhello")[0].text == "hello"


def test_xlsx_and_pdf():
    from openpyxl import Workbook
    wb = Workbook(); wb.active.title = "Data"; wb.active.append(["a", 1]); wb.active.append([None, None]); wb.active.append(["b", 2])
    buf = io.BytesIO(); wb.save(buf)
    assert extract("x.xlsx", buf.getvalue()) == [Section("Sheet Data", "a\t1\nb\t2")]

    stream = b"BT /F1 12 Tf 10 100 Td (Hello PDF) Tj ET"
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
           b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream endobj\n"
           b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R/Size 6>>\nstartxref\n0\n%%EOF")
    assert "Hello PDF" in extract("a.pdf", pdf)[0].text


def test_a_pdf_past_the_page_limit_says_how_much_was_read(monkeypatch):
    from sharepoint_rag import extract as extract_module
    from sharepoint_rag.extract import truncation_note

    monkeypatch.setattr(extract_module, "MAX_PDF_PAGES", 2)
    sections = extract("long.pdf", text_pdf(3))
    assert [s.heading_path for s in sections] == ["Page 1", "Page 2", ""]
    assert truncation_note(s.text for s in sections) == "partly indexed: only the first 2 of 3 pages were indexed"
    assert truncation_note(s.text for s in extract("short.pdf", text_pdf(2))) is None


def test_unsupported_and_blank():
    with pytest.raises(UnsupportedFileType):
        extract("a.exe", b"x")
    assert extract("a.txt", b"  \n ") == []


def test_chunks_respect_limits_and_sections():
    text = "\n".join(f"Sentence number {i} is here. " * 3 for i in range(60))
    chunks = chunk_sections([Section("A", text), Section("B", "short")], max_chars=500, overlap=80)
    assert all(len(c.text) <= 500 for c in chunks)
    assert [c.heading_path for c in chunks][-1] == "B" and chunks[-1].text == "short"
    a = [c for c in chunks if c.heading_path == "A"]
    assert len(a) > 3
    # consecutive chunks overlap
    assert a[1].text.split("\n")[0] in a[0].text
    assert chunks[0].embedding_text.startswith("A\n")


def test_chunk_oversized_word_and_bad_overlap():
    chunks = chunk_sections([Section("", "x" * 1200)], max_chars=500, overlap=50)
    assert [len(c.text) for c in chunks] == [500, 500, 300]  # pieces overlap by 50 chars inside the split; no second tail is added
    with pytest.raises(ValueError):
        chunk_sections([], max_chars=100, overlap=100)


def test_chunk_rows_start_every_heading_with_the_document_title():
    from sharepoint_rag.index_store import chunk_rows

    rows = chunk_rows("Ulta Beauty 10-K 2023.md", b"# Results\n\nRevenue grew.\n")
    assert [r.heading_path for r in rows] == ["Ulta Beauty 10-K 2023 > Results"]
    assert chunk_rows("notes.md", b"no heading here", title="")[0].heading_path is None


def _words(rng, n):
    return " ".join(rng.choice(["oil", "crankcase", "capacity", "7", "quarts", "with", "filter.", "CUCV", "M1008", "see",
                                "table", "1-2", "Page", "........", "NSN", "2520-01-149-1221"]) for _ in range(n))


@pytest.mark.parametrize("seed", range(40))
def test_rejoin_gives_back_each_sections_text_once(seed):
    """Chunks overlap by up to OVERLAP_CHARS; the file text rebuilt from them must hold every word exactly once."""
    rng = random.Random(seed)
    sections = []
    for page in range(rng.randint(1, 4)):
        # Short paragraphs (overlap carried as a tail) and long ones (cut inside, with the overlap built in).
        paras = [_words(rng, rng.choice([3, 30, 120, 400, 900])) for _ in range(rng.randint(1, 8))]
        sections.append(Section(f"Manual > Page {page % 3}", "\n".join(paras)))  # a heading can come back later
    chunks = chunk_sections(sections)
    rebuilt = rejoin([(c.heading_path, c.text) for c in chunks])

    expected: list[tuple[str, str]] = []
    for s in sections:  # consecutive sections with one heading come back as one
        if expected and expected[-1][0] == s.heading_path:
            expected[-1] = (s.heading_path, expected[-1][1] + "\n" + s.text)
        else:
            expected.append((s.heading_path, s.text))
    assert [h for h, _ in rebuilt] == [h for h, _ in expected]
    for (_, got), (_, want) in zip(rebuilt, expected):
        assert got.split() == want.split()


def test_rejoin_keeps_text_that_only_looks_like_an_overlap():
    chunks = [("P1", "first part ends with the same words"), ("P1", "same words start the next chunk"),
              ("P1", "words\nare repeated but were not carried over"), ("P2", "a new section never loses its start")]
    assert rejoin(chunks) == [
        ("P1", "first part ends with the same words\nsame words start the next chunk\nwords\nare repeated but were not carried over"),
        ("P2", "a new section never loses its start"),
    ]


def test_rejoin_spans_say_where_each_chunk_is():
    rng = random.Random(7)
    words = "oil the capacity quarts engine filter\nnote".split(" ")
    sections = [Section(f"Page {i}", " ".join(rng.choice(words) for _ in range(rng.randint(50, 700)))) for i in range(4)]
    chunks = [(c.heading_path, c.text) for c in chunk_sections(sections)]
    joined, spans = rejoin_spans(chunks)
    assert len(spans) == len(chunks)
    for (heading, text), (i, start, end) in zip(chunks, spans):
        assert joined[i][0] == heading and joined[i][1][start:end] == text


def test_rejoin_drops_a_short_chunk_repeated_whole():
    # chunk_sections repeats a chunk shorter than the overlap whole at the start of the next one.
    chunks = [("H", "x" * 300), ("H", "short"), ("H", "short\nnext part")]
    assert rejoin(chunks, overlap=200) == [("H", "x" * 300 + "\nshort\nnext part")]
