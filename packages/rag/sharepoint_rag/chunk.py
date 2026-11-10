"""Splits extracted sections into overlapping, size-bounded chunks that never cross a section."""
import re
from dataclasses import dataclass

from .extract import Section

MAX_CHARS = 2000  # roughly 500 tokens
OVERLAP_CHARS = 200


@dataclass(frozen=True)
class Chunk:
    heading_path: str
    text: str

    @property
    def embedding_text(self) -> str:
        """Heading path is prepended so the vector carries the chunk's place in the document."""
        return f"{self.heading_path}\n{self.text}" if self.heading_path else self.text


def _units(text: str, max_chars: int) -> list[str]:
    """Paragraphs, with any paragraph longer than max_chars split at sentence/word boundaries."""
    units = []
    for para in re.split(r"\n+", text):
        para = para.strip()
        while len(para) > max_chars:
            cut = max(para.rfind(". ", 0, max_chars), para.rfind(" ", 0, max_chars))
            cut = cut + 1 if cut > 0 else max_chars
            units.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            units.append(para)
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
        for unit in _units(section.text, max_chars):
            if current and len(current) + 1 + len(unit) > max_chars:
                chunks.append(Chunk(section.heading_path, current))
                tail = _overlap_tail(current, overlap)
                # Drop the overlap if it would push the new chunk over the limit.
                current = f"{tail}\n{unit}" if tail and len(tail) + 1 + len(unit) <= max_chars else unit
            else:
                current = f"{current}\n{unit}" if current else unit
        if current.strip():
            chunks.append(Chunk(section.heading_path, current))
    return chunks
