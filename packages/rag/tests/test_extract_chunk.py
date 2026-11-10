import io
import zipfile

import pytest
from sharepoint_rag.chunk import chunk_sections
from sharepoint_rag.extract import Section, UnsupportedFileType, extract

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


def test_pptx_slides_in_numeric_order():
    def slide(t):
        return f"<p:sld {A} xmlns:p='p'><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>"
    data = make_zip({f"ppt/slides/slide{n}.xml": slide(f"s{n}") for n in (10, 2, 1)})
    assert [(s.heading_path, s.text) for s in extract("d.pptx", data)] == [("Slide 1", "s1"), ("Slide 2", "s2"), ("Slide 10", "s10")]


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
    assert [len(c.text) for c in chunks] == [500, 500, 251]  # last unit (200) plus 50 chars of overlap + newline
    with pytest.raises(ValueError):
        chunk_sections([], max_chars=100, overlap=100)
