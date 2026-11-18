"""Picks the Postgres text-search config for a chunk (English or Spanish stemming), by stopword counts.

Keyword search always matches the unstemmed `simple` form too, so a wrong guess costs recall of word
forms only, never exact-term matches.
"""
import re

_STOPWORDS = {
    "english": set("the and of to in is that for it with as on are be this by or from at an not have you your will".split()),
    "spanish": set("el la de que y en los las del se por un una para con no es al lo como más su sus le o este".split()),
}
MIN_HITS = 3


def detect(text: str) -> str:
    """'english', 'spanish', or 'simple' when the text is too short or unclear to tell."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    counts = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    best = max(counts, key=counts.__getitem__)
    others = max(v for lang, v in counts.items() if lang != best)
    return best if counts[best] >= MIN_HITS and counts[best] > others * 1.5 else "simple"
