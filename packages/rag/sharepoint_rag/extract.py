"""Turns SharePoint file bytes into text sections (with a heading path for context)."""
import html.parser
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import defusedxml.ElementTree as ElementTree

MAX_XML_BYTES = 50_000_000  # decompressed size cap per XML part (zip-bomb guard)
MAX_PDF_PAGES = 500
MAX_XLSX_ROWS = 5000
TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".xml", ".log"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class UnsupportedFileType(Exception):
    pass


@dataclass(frozen=True)
class Section:
    heading_path: str  # e.g. "Policies > Leave" or "Slide 3"
    text: str


def extract(name: str, data: bytes) -> list[Section]:
    """Sections with non-blank text, in document order. Raises UnsupportedFileType for other formats."""
    ext = PurePosixPath(name.lower()).suffix
    if ext in TEXT_EXTENSIONS:
        sections = [Section("", _decode(data))]
    elif ext == ".md":
        sections = _markdown(_decode(data))
    elif ext in (".html", ".htm"):
        sections = [Section("", _html_text(_decode(data)))]
    elif ext == ".docx":
        sections = _docx(data)
    elif ext == ".pptx":
        sections = _pptx(data)
    elif ext == ".pdf":
        sections = _pdf(data)
    elif ext == ".xlsx":
        sections = _xlsx(data)
    else:
        raise UnsupportedFileType(ext or name)
    return [s for s in sections if s.text.strip()]


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _markdown(text: str) -> list[Section]:
    sections, stack, body = [], [], []

    def flush() -> None:
        sections.append(Section(" > ".join(h for _, h in stack), "\n".join(body).strip()))
        body.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
        else:
            body.append(line)
    flush()
    return sections


class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_text(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def _read_xml_part(z: zipfile.ZipFile, part: str):
    if z.getinfo(part).file_size > MAX_XML_BYTES:
        raise ValueError(f"{part} is too large to read")
    return ElementTree.fromstring(z.read(part))


def _paragraph_text(p, text_tag: str, ns: str) -> str:
    """Text of one paragraph, skipping nested paragraphs (text boxes), which are visited on their own."""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node:
            if child.tag == f"{ns}p":
                continue
            if child.tag == text_tag:
                parts.append(child.text or "")
            elif child.tag in (f"{ns}tab",):
                parts.append("\t")
            elif child.tag in (f"{ns}br", f"{ns}cr"):
                parts.append("\n")
            walk(child)

    walk(p)
    return "".join(parts)


def _docx(data: bytes) -> list[Section]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = _read_xml_part(z, "word/document.xml")
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []

    def flush() -> None:
        sections.append(Section(" > ".join(h for _, h in stack), "\n".join(body).strip()))
        body.clear()

    for p in root.iter(f"{_W}p"):
        text = _paragraph_text(p, f"{_W}t", _W).strip()
        style = p.find(f"{_W}pPr/{_W}pStyle")
        style_id = (style.get(f"{_W}val") if style is not None else "") or ""
        m = re.fullmatch(r"(?i)heading\s*([1-9])|title()", style_id)
        if m and text:
            flush()
            level = int(m.group(1)) if m.group(1) else 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
        else:
            body.append(text)
    flush()
    return sections


def _pptx(data: bytes) -> list[Section]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
        )
        sections = []
        for part in slides:
            number = int(re.search(r"(\d+)\.xml$", part).group(1))  # real slide number, not list position
            root = _read_xml_part(z, part)
            paragraphs = (_paragraph_text(p, f"{_A}t", _A).strip() for p in root.iter(f"{_A}p"))
            sections.append(Section(f"Slide {number}", "\n".join(t for t in paragraphs if t)))
    return sections


def _pdf(data: bytes) -> list[Section]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise UnsupportedFileType("encrypted pdf")
    return [
        Section(f"Page {i}", page.extract_text() or "")
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1)
    ]


def _xlsx(data: bytes) -> list[Section]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sections = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True, max_row=MAX_XLSX_ROWS):
                if any(c is not None for c in row):
                    rows.append("\t".join("" if c is None else str(c) for c in row))
            sections.append(Section(f"Sheet {ws.title}", "\n".join(rows)))
    finally:
        wb.close()
    return sections
