"""Scan the files built by build_eval_data.py for anything risky, without opening or running them.

    .venv/bin/python scripts/scan_eval_data.py [--clamdb DIR] [--oletools DIR]

Three layers, each reported per file:
- ClamAV signatures (clamscan on PATH; --clamdb points at a freshclam database directory).
- oletools (--oletools: the bin/ directory of a venv with `pip install oletools`, kept out of the project venv):
  mraptor flags Office macros that run on open and write or execute something; rtfobj lists objects embedded
  in RTF files (the usual carrier for Equation Editor exploits).
- Our own byte-level checks: executables and shortcuts by magic number or a leftover DOS stub, whatever the
  extension; extensions Windows runs as code (.js, .vbs, .bat, .ps1, .hta, ...); obfuscated or hidden script in
  HTML and any script in Markdown; active PDF features (JavaScript, launch actions, embedded files, ...); and
  archives that decompress far beyond their size.

Exit code 1 if anything is flagged. The JSON report goes to data/scan-report.json.
"""
import argparse
import gzip
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import zipfile
import zlib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

MAGIC = [
    (b"MZ", "windows-executable"),
    (b"\x7fELF", "elf-executable"),
    (b"\xcf\xfa\xed\xfe", "macho-executable"),
    (b"\xce\xfa\xed\xfe", "macho-executable"),
    (b"\xfe\xed\xfa\xcf", "macho-executable"),  # big-endian
    (b"\xfe\xed\xfa\xce", "macho-executable"),
    (b"\xca\xfe\xba\xbe", "macho-universal-or-java-class"),  # the same magic for both; code either way
    (b"#!", "script"),
    (b"L\x00\x00\x00\x01\x14\x02\x00", "windows-shortcut"),  # .lnk: runs whatever it points at
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
    (b"PK\x03\x04", "zip"),
    (b"\x1f\x8b", "gzip"),
    (b"{\\rt", "rtf"),
    (b"FWS", "swf"),
    (b"CWS", "swf"),
    (b"ZWS", "swf"),
]
OFFICE_EXTENSIONS = {".doc", ".dot", ".xls", ".xlt", ".ppt", ".pot", ".pps", ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm"}
EXECUTABLE = {"windows-executable", "elf-executable", "macho-executable", "macho-universal-or-java-class", "script",
              "windows-shortcut"}
# Extensions Windows executes or scripts on a double-click, whatever the file holds.
RISKY_EXTENSIONS = {".exe", ".dll", ".scr", ".com", ".pif", ".cpl", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".vbe",
                    ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".jar", ".reg", ".inf"}
_DOS_STUB = b"This program cannot be run in DOS mode"  # survives in a PE whose "MZ" was stripped or renamed
HTML_EXTENSIONS = {".html", ".htm", ".xhtml", ".svg"}
# Patterns that legitimate page scripts rarely need but droppers and drive-by pages do.
_HTML_SUSPICIOUS = re.compile(
    rb"eval\s*\(|unescape\s*\(|fromCharCode|atob\s*\(|document\.write\s*\(\s*unescape"
    rb"|<iframe[^>]*(?:width|height)\s*=\s*[\"']?0\b|ActiveXObject|WScript\.Shell", re.I)

# pdfid's keywords. Names may hide letters as #xx escapes, so they're decoded first.
PDF_RISKY = ["/JavaScript", "/JS", "/Launch", "/EmbeddedFile", "/RichMedia", "/XFA", "/SubmitForm", "/ImportData"]
PDF_INFO = ["/OpenAction", "/AA", "/AcroForm", "/ObjStm", "/Encrypt", "/URI"]
_PDF_NAME = re.compile(rb"/[A-Za-z0-9#]+")
_PDF_STREAM = re.compile(rb"stream(?:\r\n|\r|\n).*?endstream", re.S)  # binary bodies: "/JS" turns up in them by chance
MAX_RATIO = 100  # uncompressed / compressed, for archives


def kind(head: bytes) -> str:
    return next((k for magic, k in MAGIC if head.startswith(magic)), "other")


def pdf_keywords(path: Path, data: bytes) -> Counter:
    """pdfid-style keyword counts outside stream bodies, plus the keys of every object the parsed document
    reaches, which also covers objects packed inside compressed object streams. Of the two, the higher count."""
    raw_counts = Counter()
    for raw in _PDF_NAME.findall(_PDF_STREAM.sub(b"", data)):
        name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw).decode("latin-1")
        if name in PDF_RISKY or name in PDF_INFO:
            raw_counts[name] += 1
    parsed = Counter()
    try:
        from pypdf import PdfReader
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

        seen: set[int] = set()
        stack = [PdfReader(io.BytesIO(data), strict=False).trailer]
        while stack:
            o = stack.pop()
            if isinstance(o, IndirectObject):
                if o.idnum in seen:
                    continue
                seen.add(o.idnum)
                o = o.get_object()
            if isinstance(o, DictionaryObject):
                for k, v in o.items():
                    if k in PDF_RISKY or k in PDF_INFO or k == "/EmbeddedFiles":
                        parsed["/EmbeddedFile" if k == "/EmbeddedFiles" else k] += 1
                    stack.append(v)
                if o.get("/S") == "/JavaScript":
                    parsed["/JavaScript"] += 1
            elif isinstance(o, ArrayObject):
                stack.extend(o)
    except Exception as e:  # a broken PDF: the byte scan still counts
        parsed["unparsed"] = 1
        print(f"  pypdf could not parse {path.name}: {type(e).__name__}", file=sys.stderr)
    return Counter({k: max(raw_counts[k], parsed[k]) for k in raw_counts | parsed})


def archive_ratio(path: Path, k: str) -> float | None:
    size = max(path.stat().st_size, 1)
    try:
        if k == "zip":
            with zipfile.ZipFile(path) as z:
                return sum(i.file_size for i in z.infolist()) / size
        if k == "gzip":
            total = 0
            with gzip.open(path) as f:
                while chunk := f.read(1 << 20):
                    total += len(chunk)
                    if total > size * MAX_RATIO:  # enough to flag; a bomb isn't inflated any further
                        break
            return total / size
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error):
        return None
    return None


def static_checks(path: Path) -> tuple[str, list[str], list[str]]:
    data = path.read_bytes()
    k = kind(data[:16])
    ext = path.suffix.lower()
    flags, notes = [], []
    if k in EXECUTABLE:
        flags.append(f"is a {k}")
    elif _DOS_STUB in data[:4096]:
        flags.append("holds a Windows executable header (DOS stub) under another name")
    if ext in RISKY_EXTENSIONS:
        flags.append(f"{ext} files run as code on Windows")
    if k == "swf":
        notes.append("Flash file: inert unless opened in a Flash player; the extractor skips it")
    if k == "pdf":
        counts = pdf_keywords(path, data)
        if risky := {n: c for n, c in counts.items() if n in PDF_RISKY}:
            flags.append(f"active PDF content {risky}")
        if counts.pop("unparsed", 0):
            if counts.get("/ObjStm"):  # objects packed in compressed streams: the byte scan can't see them
                flags.append("pypdf could not parse it, and its compressed object streams hide objects from the "
                             "byte scan: look at it by hand")
            else:
                notes.append("pypdf could not parse it (no object streams, so the byte scan saw every object)")
        if info := {n: c for n, c in counts.items() if n not in PDF_RISKY}:
            notes.append(f"pdf {info}")
    if k in ("zip", "gzip"):
        ratio = archive_ratio(path, k)
        if ratio is None:
            notes.append(f"{k} could not be read")
        elif ratio > MAX_RATIO:
            flags.append(f"{k} expands {ratio:,.0f}x (decompression bomb?)")
        if k == "zip" and path.suffix.lower() not in (".docx", ".xlsx", ".pptx"):
            notes.append("zip archive")
    if ext == ".md" and re.search(rb"<script|javascript:", data, re.I):
        flags.append("script markup in Markdown")
    if ext in HTML_EXTENSIONS:
        if suspicious := sorted({m.group(0).decode("latin-1")[:30] for m in _HTML_SUSPICIOUS.finditer(data)}):
            flags.append(f"obfuscated or hidden script in HTML {suspicious}")
        elif scripts := len(re.findall(rb"<script", data, re.I)):
            notes.append(f"HTML with {scripts} script block(s); the extractor reads it as text and never runs it")
    return k, flags, notes


def clamscan(clamdb: Path | None) -> dict[str, str]:
    exe = shutil.which("clamscan")
    if not exe:
        sys.exit("clamscan not found: brew install clamav, then freshclam (or pass --skip-clamav)")
    args = [exe, "-r", "--infected", "--no-summary", "--alert-macros=yes", "--alert-encrypted=yes",
            "--alert-exceeds-max=yes", f"--exclude-dir=^{re.escape(str(DATA / '.cache'))}", str(DATA)]  # a regex
    if clamdb:
        args.insert(1, f"--database={clamdb}")
    print("clamscan: scanning data/ (a few minutes)", file=sys.stderr)
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode == 2:
        sys.exit(f"clamscan failed: {out.stderr.strip()}")
    hits = {}
    for line in out.stdout.splitlines():  # "<path>: <signature> FOUND"
        if line.endswith(" FOUND"):
            path, _, sig = line[:-6].rpartition(": ")
            hits[path] = sig
    return hits


def oletools(bin_dir: Path, files: list[Path], kinds: dict[str, str]) -> tuple[dict[str, list[tuple[str, str]]], dict[str, int]]:
    """Per file: ("flags" | "notes", message); and mraptor's exit code per Office file."""
    flags: dict[str, list[tuple[str, str]]] = {}
    codes: dict[str, int] = {}
    office = [f for f in files if kinds[str(f)] in ("ole", "zip")]
    rtf = [f for f in files if kinds[str(f)] == "rtf"]
    print(f"oletools: {len(office)} Office/OLE files, {len(rtf)} RTF files", file=sys.stderr)
    for f in office:
        # mraptor exit codes: 0 no macro, 1 not an Office file, 2 macro OK, 10 error, 20 suspicious
        out = subprocess.run([str(bin_dir / "mraptor"), str(f)], capture_output=True, text=True)
        codes[str(f)] = out.returncode
        unanalysed = ("flags", f"mraptor could not analyse it (exit {out.returncode}): look at it by hand")
        # "Not an Office file" for an OLE container or an Office extension means a format mraptor can't read
        # (e.g. PowerPoint 95), not a clean result.
        office_like = kinds[str(f)] == "ole" or f.suffix.lower() in OFFICE_EXTENSIONS
        result = {
            20: ("flags", "mraptor: suspicious macro (auto-run + write/execute)"),
            2: ("notes", "mraptor: contains macros (not auto-run + write/execute)"),
            1: unanalysed if office_like else ("notes", "mraptor: not an Office file"),
            0: None,
        }.get(out.returncode, unanalysed)
        if result:
            flags.setdefault(str(f), []).append(result)
    for f in rtf:
        out = subprocess.run([str(bin_dir / "rtfobj"), str(f)], capture_output=True, text=True)
        objects = len(re.findall(r"^\s*\d+\s*\|[0-9A-Fa-f]+h\s*\|", out.stdout, re.M))  # one table row per object
        if out.returncode != 0:
            flags.setdefault(str(f), []).append(("flags", f"rtfobj could not analyse it (exit {out.returncode}): look at it by hand"))
        elif "CVE" in out.stdout:
            flags.setdefault(str(f), []).append(("flags", f"rtfobj: {objects} embedded object(s), CVE match"))
        elif objects:
            flags.setdefault(str(f), []).append(("notes", f"rtfobj: {objects} embedded object(s)"))
    return flags, codes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clamdb", type=Path, help="ClamAV database directory (default: clamscan's own)")
    parser.add_argument("--oletools", type=Path, help="bin/ directory holding mraptor and rtfobj")
    parser.add_argument("--skip-clamav", action="store_true")
    args = parser.parse_args()
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # broken real-world PDFs are expected here

    files = sorted(p for p in DATA.glob("*/docs/**/*") if p.is_file() and ".cache" not in p.parts)
    report: dict[str, dict] = {}
    kinds: dict[str, str] = {}  # path -> kind, so no file is read again just for its header
    for f in files:
        k, flags, notes = static_checks(f)
        kinds[str(f)] = k
        if flags or notes:
            report[str(f)] = {"kind": k, "flags": flags, "notes": notes}

    def add(path: str, field: str, message: str) -> None:
        kinds.setdefault(path, kind(Path(path).open("rb").read(16)))
        report.setdefault(path, {"kind": kinds[path], "flags": [], "notes": []})[field].append(message)

    mraptor: dict[str, int] = {}
    if args.oletools:
        results, mraptor = oletools(args.oletools, files, kinds)
        for path, entries in results.items():
            for field, message in entries:
                add(path, field, message)
    if not args.skip_clamav:
        for path, sig in clamscan(args.clamdb).items():
            # ClamAV flags any macro at all. That's only a note when mraptor read the macro and found it isn't
            # auto-run + write/execute (exit 2); if mraptor didn't run, or missed it, a person has to look.
            if sig.startswith("Heuristics.OLE2.ContainsMacros"):
                if mraptor.get(path) == 2:
                    add(path, "notes", f"clamav: {sig}")
                else:
                    why = "mraptor didn't run (--oletools)" if not args.oletools else "mraptor didn't confirm the macro"
                    add(path, "flags", f"clamav: {sig}; {why}: look at it by hand")
            else:
                add(path, "flags", f"clamav: {sig}")

    flagged = {p: r for p, r in report.items() if r["flags"]}
    (DATA / "scan-report.json").write_text(json.dumps(
        {"files": len(files), "kinds": dict(Counter(kinds[str(f)] for f in files)), "flagged": flagged,
         "notes": {p: r["notes"] for p, r in report.items() if r["notes"] and not r["flags"]}}, indent=2) + "\n")
    print(f"{len(files)} files scanned; kinds: {dict(Counter(kinds[str(f)] for f in files))}")
    for path, r in sorted(flagged.items()):
        print(f"FLAGGED {Path(path).relative_to(DATA)}: {'; '.join(r['flags'])}")
    print(f"{len(flagged)} flagged, {sum(1 for r in report.values() if r['notes'] and not r['flags'])} with notes; "
          f"details in {(DATA / 'scan-report.json').relative_to(ROOT)}")
    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    main()
