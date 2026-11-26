"""Evidence for golden items: where in which document an answer lives, checkable without a model.

An item's `evidence` is a list of entries, each pointing at one place in one document:

    {"doc": "manuals/army/TM 9-2320-280-10.pdf", "pages": [212], "printed_page": "2-14", "section": "Fuel filter",
     "quote": {"exact": "...", "prefix": "...", "suffix": "..."},
     "boxes": [{"page": 212, "bbox": [0.08, 0.31, 0.92, 0.44], "modality": "table"}],
     "role": "required", "group": "same-fact-in-two-editions"}

- `pages` (PDF only) are physical pages counted from 1, as the indexer's `Page N` sections are. `printed_page` is
  the number printed on the page, for people only.
- `section` is the indexer's heading path (`Travel Rates 2026 > City allowances`, `Slide 3`, `Sheet Costs`) for every
  file but a PDF, whose location is `pages`; on a PDF it is a free-text label for people.
- `quote` is text copied from what the indexer extracts, normalised for whitespace. `prefix` and `suffix` tell apart
  a phrase that occurs more than once. It is the anchor the validator checks.
- `boxes` are optional and for reviewers only, never scored: x0, y0, x1, y1 as fractions of the page as rendered
  (after rotation), measured from the top left. Human boxes agree only moderately (ViDoRe V3: IoU 0.50), so they
  can't be a pass/fail criterion.
- `role` is `required` (the default) or `also_acceptable`. Required entries sharing a `group` are alternatives (the
  same fact in two editions of a manual): citing any one of them satisfies the group.

Documents come from `evals/golden/docs/` or, for files too large to commit, `evals/golden/external.json`: a manifest
of files kept under `data/` with their size, sha256 and page count. The manifest lets the structure be checked
without the files; when a file is there its hash, pages and quotes are checked too.
"""
import hashlib
import json
import re
from functools import cache, lru_cache
from pathlib import Path

from sharepoint_rag.extract import MAX_FILE_BYTES, extract

from sharepoint_evals.corpus import DATA, Doc

ROLES = {"required", "also_acceptable"}
MODALITIES = {"text", "table", "figure", "equation", "other"}
ENTRY_FIELDS = {"doc", "role", "group", "section", "pages", "printed_page", "quote", "boxes"}
QUOTE_FIELDS = {"exact", "prefix", "suffix"}
BOX_FIELDS = {"page", "bbox", "modality"}


@cache
def manifest(directory: Path) -> dict:
    path = directory / "external.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {"sources": {}}


def external_docs(directory: Path) -> dict[str, Doc]:
    """Files kept outside the repo, by the name items use: `<source>/<path in the manifest>`."""
    docs = {}
    for source, spec in manifest(directory)["sources"].items():
        for rel, info in spec["files"].items():
            name = f"{source}/{rel}"
            docs[name] = Doc(name, tuple(spec["acl"]), True, DATA / spec["dir"] / rel, {**info, "source": source})
    return docs


def is_external(doc: Doc) -> bool:
    return "source" in doc.meta


def present(doc: Doc) -> bool:
    return doc.path.is_file()


# ---- what an item names -----------------------------------------------------------------------------------

def _entries(item: dict) -> list[dict]:
    return [e for e in item["expected_outputs"].get("evidence") or [] if isinstance(e, dict)]


def required_docs(item: dict) -> list[str]:
    """Every document the item must cite, in order."""
    docs = [e.get("doc") for e in _entries(item) if e.get("role", "required") == "required"] + list(item["expected_outputs"].get("sources", []))
    return list(dict.fromkeys(d for d in docs if isinstance(d, str)))


def named_docs(item: dict) -> list[str]:
    """Every document the item mentions in any field."""
    docs = [e.get("doc") for e in _entries(item)] + [d for f in ("sources", "also_acceptable", "must_not_see") for d in item["expected_outputs"].get(f, [])]
    return list(dict.fromkeys(d for d in docs if isinstance(d, str)))


def groups(item: dict) -> list[frozenset[str]]:
    """What must be cited: every group needs at least one of its documents. Without evidence, one group per source."""
    alternatives: dict[str, set[str]] = {}
    singles = [frozenset([s]) for s in item["expected_outputs"].get("sources", [])]
    for e in _entries(item):
        if e.get("role", "required") != "required" or not isinstance(e.get("doc"), str):
            continue
        if e.get("group"):
            alternatives.setdefault(e["group"], set()).add(e["doc"])
        else:
            singles.append(frozenset([e["doc"]]))
    return list(dict.fromkeys(singles + [frozenset(v) for v in alternatives.values()]))


def page_groups(item: dict) -> list[list[tuple[str, frozenset[int]]]]:
    """The same groups as `groups`, as (document, pages) for the required entries that name pages."""
    alternatives: dict[object, list[tuple[str, frozenset[int]]]] = {}
    for n, e in enumerate(_entries(item)):
        if e.get("role", "required") != "required" or not isinstance(e.get("doc"), str):
            continue
        if isinstance(e.get("pages"), list) and e["pages"]:
            alternatives.setdefault(e.get("group") or ("single", n), []).append((e["doc"], frozenset(e["pages"])))
    return list(alternatives.values())


def pages_cited(item: dict, cited: dict[str, set[int]], indexed) -> float | None:
    """1.0 if every group with pages is satisfied by a cited source on one of its pages, else 0.0; None when no
    group has pages in a file the run could search (`indexed(doc)`). A group counts the alternatives it could find."""
    groups = [[(d, p) for d, p in g if indexed(d)] for g in page_groups(item)]
    groups = [g for g in groups if g]
    if not groups:
        return None
    return float(all(any(pages & cited.get(doc, set()) for doc, pages in g) for g in groups))


def acceptable(item: dict) -> set[str]:
    """What the answer may cite: all of the above, any evidence, and the older copies the item allows."""
    return set(required_docs(item)) | set(item["expected_outputs"].get("also_acceptable", [])) | {e["doc"] for e in _entries(item) if isinstance(e.get("doc"), str)}


# ---- validation -------------------------------------------------------------------------------------------

def problems(where: str, evidence, docs: dict[str, Doc], deferred: list | None = None) -> list[str]:
    """Every problem with an item's evidence. Structure is checked from the manifest alone; hashes, pages and
    quotes only for the files that are present. With `deferred`, those checks are queued there for `content_problems`
    instead, so a caller validating many items can run them file by file."""
    if not isinstance(evidence, list) or not evidence:
        return [f"{where}: evidence must be a non-empty list"]
    out = []
    for n, e in enumerate(evidence, 1):
        out += _entry(f"{where} evidence[{n}]", e, docs, deferred)
    if not any(isinstance(e, dict) and e.get("role", "required") == "required" for e in evidence):
        out.append(f"{where}: evidence needs at least one required entry")
    return out


def _entry(where: str, e, docs: dict[str, Doc], deferred: list | None = None) -> list[str]:
    if not isinstance(e, dict):
        return [f"{where}: must be an object"]
    out = [f"{where}: unknown field {f}" for f in sorted(set(e) - ENTRY_FIELDS)]
    role = e.get("role", "required")
    if role not in ROLES:
        out.append(f"{where}: role {role!r} is not one of {sorted(ROLES)}")
    doc = docs.get(e["doc"]) if isinstance(e.get("doc"), str) else None
    if doc is None:
        return out + [f"{where}: doc {e.get('doc')!r} is not in the seeded documents or the external manifest"]
    needed = role == "required"
    pdf = doc.name.lower().endswith(".pdf")
    if "group" in e and not (isinstance(e["group"], str) and e["group"] and needed):
        out.append(f"{where}: group must be a non-empty string, on a required entry")
    if doc.meta.get("bytes", 0) > MAX_FILE_BYTES:
        out.append(f"{where}: {doc.name} is over {MAX_FILE_BYTES} bytes, so the indexer skips it and search can never find it")

    pages = e.get("pages")
    if pdf:
        if needed and pages is None:
            out.append(f"{where}: an entry on a PDF needs pages")
        if pages is not None:
            out += _pages(where, pages, doc)
        boxes_ok = isinstance(pages, list)
    else:
        out += [f"{where}: {f} only applies to a PDF" for f in ("pages", "printed_page", "boxes") if f in e]
        if needed and not isinstance(e.get("section"), str):
            out.append(f"{where}: needs section, the heading path the indexer gives it ('' for a file without headings)")
        boxes_ok = False
    if "section" in e and not isinstance(e["section"], str):
        out.append(f"{where}: section must be a string")
    if "printed_page" in e and not isinstance(e["printed_page"], str):
        out.append(f"{where}: printed_page must be a string, as printed ('2-14')")
    out += _boxes(where, e["boxes"], pages if boxes_ok else []) if "boxes" in e and pdf else []

    quote = e.get("quote")
    if quote is None:
        if needed:
            out.append(f"{where}: a required entry needs a quote")
    else:
        out += _quote_shape(where, quote)
    if out or not present(doc):
        return out
    if deferred is not None:
        deferred.append((where, e, doc, pdf))
        return out
    return out + _content(where, e, doc, pdf)


def content_problems(deferred: list) -> list[str]:
    """The queued checks against the files, one file at a time: each large manual is opened once, however many
    items cite it, so the readers kept in memory can stay few."""
    return [p for where, e, doc, pdf in sorted(deferred, key=lambda d: str(d[2].path)) for p in _content(where, e, doc, pdf)]


def _pages(where: str, pages, doc: Doc) -> list[str]:
    if not (isinstance(pages, list) and pages and all(type(p) is int and p >= 1 for p in pages) and len(set(pages)) == len(pages)):
        return [f"{where}: pages must be a list of distinct page numbers counted from 1"]
    last = doc.meta.get("pages")
    return [f"{where}: page {p} is past the end of {doc.name} ({last} pages)" for p in pages if last and p > last]


def _boxes(where: str, boxes, pages) -> list[str]:
    if not isinstance(boxes, list) or not boxes:
        return [f"{where}: boxes must be a non-empty list"]
    out = []
    for n, b in enumerate(boxes, 1):
        at = f"{where} boxes[{n}]"
        if not isinstance(b, dict):
            out.append(f"{at}: must be an object")
            continue
        out += [f"{at}: unknown field {f}" for f in sorted(set(b) - BOX_FIELDS)]
        if b.get("page") not in pages:
            out.append(f"{at}: page must be one of the entry's pages")
        box = b.get("bbox")
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in box)
                and box[0] < box[2] and box[1] < box[3]):
            out.append(f"{at}: bbox must be [x0, y0, x1, y1] as fractions of the page from the top left, with x0 < x1 and y0 < y1")
        if "modality" in b and b["modality"] not in MODALITIES:
            out.append(f"{at}: modality {b['modality']!r} is not one of {sorted(MODALITIES)}")
    return out


def _quote_shape(where: str, quote) -> list[str]:
    if not isinstance(quote, dict):
        return [f"{where}: quote must be an object with exact, and optionally prefix and suffix"]
    out = [f"{where}: unknown quote field {f}" for f in sorted(set(quote) - QUOTE_FIELDS)]
    if not (isinstance(quote.get("exact"), str) and _norm(quote["exact"])):
        out.append(f"{where}: quote.exact must be non-empty text")
    out += [f"{where}: quote.{f} must be text" for f in ("prefix", "suffix") if f in quote and not isinstance(quote[f], str)]
    return out


# ---- checks against the file ------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def quote_matches(text: str, quote: dict) -> int:
    """How often the quote, with its prefix and suffix, occurs in the text (whitespace-insensitive)."""
    text, exact = _norm(text), _norm(quote["exact"])
    prefix, suffix = _norm(quote.get("prefix", "")), _norm(quote.get("suffix", ""))
    count, start = 0, text.find(exact)
    while start != -1:
        end = start + len(exact)
        if text[:start].rstrip().endswith(prefix) and text[end:].lstrip().startswith(suffix):
            count += 1
        start = text.find(exact, end)  # occurrences don't overlap
    return count


def _stamp(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


@cache
def _sha256(stamp: tuple[str, int, int]) -> str:
    digest = hashlib.sha256()
    with open(stamp[0], "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=8)  # a few of the large files, not all of them: validation holds these for the whole run
def _reader(stamp: tuple[str, int, int]):
    from pypdf import PdfReader

    reader = PdfReader(stamp[0])
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("password-protected pdf")
    return reader


@lru_cache(maxsize=2048)
def _page_text(stamp: tuple[str, int, int], page: int) -> str:
    return (_reader(stamp).pages[page - 1].extract_text() or "").replace("\x00", "")  # as extract._pdf and extract()


@lru_cache(maxsize=8)
def _sections(stamp: tuple[str, int, int], name: str):
    return extract(name, Path(stamp[0]).read_bytes())


def page_texts(path: Path, pages: list[int]) -> list[str]:
    """What the indexer extracts from these pages of a PDF, without reading the rest of a large manual."""
    stamp = _stamp(path)
    return [_page_text(stamp, p) for p in pages]


def _content(where: str, e: dict, doc: Doc, pdf: bool) -> list[str]:
    stamp = _stamp(doc.path)
    if (want := doc.meta.get("sha256")) and _sha256(stamp) != want:
        return [f"{where}: {doc.path.name} is not the file the manifest describes (sha256 differs); a deliberate update means "
                f"re-running scripts/golden_manifest.py and re-checking every item that cites it"]
    if "pages" not in e if pdf else "section" not in e:
        return []  # an also_acceptable entry may name just the file: nothing to look up in it
    quote = e.get("quote")
    try:
        if pdf:
            total = len(_reader(stamp).pages)
            if (late := [p for p in e["pages"] if p > total]):
                return [f"{where}: page {late[0]} is past the end of {doc.name} ({total} pages)"]
            scope, label = page_texts(doc.path, e["pages"]), f"page {', '.join(map(str, e['pages']))}"
        else:
            sections = [s for s in _sections(stamp, doc.name) if s.heading_path == e.get("section", "")]
            if not sections:
                have = [s.heading_path for s in _sections(stamp, doc.name)]
                return [f"{where}: {doc.name} has no section {e.get('section')!r}; it has {have[:8]}{' ...' if len(have) > 8 else ''}"]
            if len(sections) > 1:
                return [f"{where}: {len(sections)} sections of {doc.name} share the heading path {e.get('section')!r}, so it can't anchor an item"]
            scope, label = [sections[0].text], f"section {e.get('section')!r}"
    except Exception as ex:  # a file pypdf or the extractors can't read is a problem with the entry, not a crash
        return [f"{where}: could not read {doc.name}: {type(ex).__name__}: {ex}"]
    if quote is None:
        return []
    found = sum(quote_matches(text, quote) for text in scope)
    if found == 0:
        return [f"{where}: the quote is not in {label} of {doc.name} as the indexer extracts it"]
    if found > 1:
        return [f"{where}: the quote occurs {found} times in {label}; add a prefix or suffix that tells them apart"]
    return []
