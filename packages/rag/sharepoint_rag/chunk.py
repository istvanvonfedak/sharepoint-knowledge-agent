"""Splits extracted sections into overlapping, size-bounded chunks that never cross a section."""
import re
from dataclasses import dataclass

from .extract import Section

MAX_CHARS = 1500  # leaves headroom under Gemini's 2048-token input limit even at ~1 token/char (CJK, numbers)
MAX_HEADING_CHARS = 200
OVERLAP_CHARS = 200


@dataclass(frozen=True)
class Chunk:
    heading_path: str
    text: str

    @property
    def embedding_text(self) -> str:
        """Heading path is prepended so the vector carries the chunk's place in the document."""
        heading = self.heading_path[-MAX_HEADING_CHARS:]  # keep the most specific end of a long path
        return f"{heading}\n{self.text}" if heading else self.text


def _units(text: str, max_chars: int, overlap: int) -> list[tuple[str, bool]]:
    """(text, continued) per paragraph. Paragraphs over max_chars are cut at a sentence end (or else a
    space) with the overlap built in; `continued` marks pieces that already carry it."""
    units: list[tuple[str, bool]] = []
    for para in re.split(r"\n+", text):
        para = para.strip()
        continued = False
        while len(para) > max_chars:
            sentence = para.rfind(". ", 0, max_chars)
            cut = sentence + 1 if sentence > max_chars // 2 else para.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            units.append((para[:cut].strip(), continued))
            continued = True
            # Step back so consecutive pieces share context, but always make forward progress.
            start = max(cut - overlap, 1) if cut > overlap else cut
            para = para[start:].strip()
        if para:
            units.append((para, continued))
    return units


def _overlap_tail(text: str, overlap: int) -> str:
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def chunk_sections(
    sections: list[Section], max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS
) -> list[Chunk]:
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be >= 0 and smaller than max_chars")
    chunks: list[Chunk] = []
    for section in sections:
        current = ""
        for unit, continued in _units(section.text, max_chars, overlap):
            if current and len(current) + 1 + len(unit) > max_chars:
                chunks.append(Chunk(section.heading_path, current))
                tail = "" if continued else _overlap_tail(current, overlap)
                # Drop the overlap if it would push the new chunk over the limit.
                current = f"{tail}\n{unit}" if tail and len(tail) + 1 + len(unit) <= max_chars else unit
            else:
                current = f"{current}\n{unit}" if current else unit
        if current.strip():
            chunks.append(Chunk(section.heading_path, current))
    return chunks


def rejoin(chunks: list[tuple[str | None, str]], overlap: int = OVERLAP_CHARS) -> list[tuple[str | None, str]]:
    """A file's text back from its chunks, in file order: one (heading_path, text) per run of chunks with the same
    heading, with the text chunk_sections repeated between neighbours dropped. Only text it is known to have repeated
    goes: the tail of the previous chunk followed by a line break (the overlap above), or, where a long paragraph was
    cut, the longest start of the chunk the previous one ends with, if at least half an overlap long. A chunk whose
    overlap was skipped, or that only starts like the previous one ends by chance, keeps all its text."""
    return rejoin_spans(chunks, overlap)[0]


def rejoin_spans(
    chunks: list[tuple[str | None, str]], overlap: int = OVERLAP_CHARS
) -> tuple[list[tuple[str | None, str]], list[tuple[int, int, int]]]:
    """rejoin's sections, and where each chunk is in them: (section index, start, end), its repeated start included."""
    # Each section's pieces are joined once at the end: a whole CSV is one section, and joining as we go is
    # quadratic. The rules compare with the previous chunk alone, as chunk_sections took the overlap from it: a
    # chunk shorter than the overlap is repeated whole, and the joined text's end would span two chunks.
    out: list[tuple[str | None, list[str]]] = []
    spans: list[tuple[int, int, int]] = []
    prev, length = "", 0
    for heading, text in chunks:
        if not out or out[-1][0] != heading:
            out.append((heading, [text]))
            spans.append((len(out) - 1, 0, len(text)))
            prev, length = text, len(text)
            continue
        tail = _overlap_tail(prev, overlap)
        if tail and text.startswith(tail + "\n"):
            piece, start = text[len(tail):], length - len(tail)
        else:
            n = next((n for n in range(min(len(prev), len(text), overlap), overlap // 2 - 1, -1)
                      if prev.endswith(text[:n])), 0)
            piece, start = (text[n:], length - n) if n else (f"\n{text}", length + 1)
        out[-1][1].append(piece)
        spans.append((len(out) - 1, start, start + len(text)))
        prev, length = text, length + len(piece)
    return [(h, "".join(p)) for h, p in out], spans
