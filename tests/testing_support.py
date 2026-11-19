"""Helpers shared by the test suites (importable because pytest.ini puts the repo root on sys.path)."""
import hashlib
import io
import math

TEST_DIMENSIONS = 8


def fake_vector(text: str, dimensions: int = TEST_DIMENSIONS) -> list[float]:
    """Deterministic unit vector per text, so tests never call an embedding API."""
    digest = hashlib.sha256(text.encode()).digest()
    v = [b - 127.5 for b in digest[:dimensions]]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def text_pdf(pages: int) -> bytes:
    """A PDF of `pages` pages, each with the text "Hello PDF"."""
    from pypdf import PdfReader, PdfWriter

    stream = b"BT /F1 12 Tf 10 100 Td (Hello PDF) Tj ET"
    one = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
           b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream endobj\n"
           b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R/Size 6>>\nstartxref\n0\n%%EOF")
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_page(PdfReader(io.BytesIO(one)).pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
