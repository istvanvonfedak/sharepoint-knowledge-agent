"""Turns SharePoint file bytes into text sections (with a heading path for context)."""
import html.parser
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import defusedxml.ElementTree as ElementTree

MAX_FILE_BYTES = 50_000_000  # whole-file cap; openpyxl/pypdf decompress without limits of their own
MAX_XML_BYTES = 50_000_000  # decompressed size cap per XML part (zip-bomb guard)
MAX_PDF_PAGES = 500
MAX_XLSX_ROWS = 5000
TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".xml", ".log"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class ExtractionError(Exception):
    """The file could not be turned into text (corrupt, too large, or an unsupported type)."""


class UnsupportedFileType(ExtractionError):
    pass


@dataclass(frozen=True)
class Section:
    heading_path: str  # e.g. "Policies > Leave" or "Slide 3"
    text: str


def extract(name: str, data: bytes) -> list[Section]:
    """Sections with non-blank text, in document order.

    Raises ExtractionError (UnsupportedFileType for unknown formats) so callers need one except clause.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ExtractionError(f"file is larger than {MAX_FILE_BYTES} bytes")
    try:
        sections = _extract(name, data)
    except ExtractionError:
        raise
    except Exception as e:  # corrupt zips, malformed XML/PDF, missing parts: one bad file must not kill a crawl
        raise ExtractionError(f"{type(e).__name__}: {e}") from e
    # Postgres rejects NUL in text, and PDF/UTF-16 extraction can produce them.
    return [Section(s.heading_path.replace("\x00", ""), s.text.replace("\x00", "")) for s in sections if s.text.strip("\x00 \t\r\n")]


def _extract(name: str, data: bytes) -> list[Section]:
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
    return sections


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 BOM, common for Windows/Excel exports
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _markdown(text: str) -> list[Section]:
    sections, stack, body = [], [], []
    in_fence = False
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)  # YAML front matter

    def flush() -> None:
        sections.append(Section(" > ".join(h for _, h in stack), "\n".join(body).strip()))
        body.clear()

    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
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


_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _drop_fallbacks(root) -> None:
    """Word stores shapes/text boxes twice (mc:Choice + mc:Fallback); keep only the Choice copy."""
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag == f"{_MC}Fallback":
                parent.remove(child)


def _heading_styles(z: zipfile.ZipFile) -> dict[str, int]:
    """styleId -> heading level. Canonical style names stay English ('heading 1') even in localized Word."""
    if "word/styles.xml" not in z.namelist():
        return {}
    levels = {}
    for style in _read_xml_part(z, "word/styles.xml").iter(f"{_W}style"):
        name = style.find(f"{_W}name")
        m = re.fullmatch(r"(?i)heading ([1-9])|(title)", (name.get(f"{_W}val") if name is not None else "") or "")
        if m:
            levels[style.get(f"{_W}styleId", "")] = int(m.group(1)) if m.group(1) else 1
    return levels


def _docx(data: bytes) -> list[Section]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = _read_xml_part(z, "word/document.xml")
        heading_levels = _heading_styles(z)
    _drop_fallbacks(root)
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
        level = heading_levels.get(style_id) or ((int(m.group(1)) if m.group(1) else 1) if m else None)
        if level and text:
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
        else:
            body.append(text)
    flush()
    return sections


_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _slide_parts(z: zipfile.ZipFile) -> list[str]:
    """Slide parts in presentation order (slideN.xml numbering goes stale after reorder/delete)."""
    try:
        rels = {
            r.get("Id"): "ppt/" + r.get("Target").lstrip("/").removeprefix("ppt/")
            for r in _read_xml_part(z, "ppt/_rels/presentation.xml.rels").iter(f"{_REL}Relationship")
        }
        pres = _read_xml_part(z, "ppt/presentation.xml")
        ordered = [rels[s.get(f"{_R}id")] for s in pres.iter() if s.tag.endswith("}sldId")]
        if ordered and all(n in z.namelist() for n in ordered):
            return ordered
    except (KeyError, AttributeError):
        pass
    return sorted(
        (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
    )


def _pptx(data: bytes) -> list[Section]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sections = []
        for i, part in enumerate(_slide_parts(z), 1):
            root = _read_xml_part(z, part)
            paragraphs = (_paragraph_text(p, f"{_A}t", _A).strip() for p in root.iter(f"{_A}p"))
            sections.append(Section(f"Slide {i}", "\n".join(t for t in paragraphs if t)))
    return sections


def _pdf(data: bytes) -> list[Section]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    # Many PDFs are "encrypted" only to restrict printing/copying and open with an empty user password.
    if reader.is_encrypted and not reader.decrypt(""):
        raise UnsupportedFileType("password-protected pdf")
    sections = [
        Section(f"Page {i}", page.extract_text() or "")
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1)
    ]
    if len(reader.pages) > MAX_PDF_PAGES:
        sections.append(Section("", f"[Truncated: only the first {MAX_PDF_PAGES} of {len(reader.pages)} pages were indexed]"))
    return sections


def _xlsx(data: bytes) -> list[Section]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sections = []
    try:
        for ws in wb.worksheets:
            rows = []
            for n, row in enumerate(ws.iter_rows(values_only=True), 1):
                if n > MAX_XLSX_ROWS:
                    rows.append(f"[Truncated: only the first {MAX_XLSX_ROWS} rows of this sheet were indexed]")
                    break
                if any(c is not None for c in row):
                    rows.append("\t".join("" if c is None else str(c) for c in row))
            sections.append(Section(f"Sheet {ws.title}", "\n".join(rows)))
    finally:
        wb.close()
    return sections
