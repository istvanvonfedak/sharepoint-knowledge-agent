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
