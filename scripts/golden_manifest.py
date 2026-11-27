"""Write evals/golden/external.json: the large files the golden set cites but doesn't commit.

    .venv/bin/python scripts/golden_manifest.py            # data/manuals (army/...) -> source "manuals"
    .venv/bin/python scripts/golden_manifest.py --dir DIR --source NAME    # DIR is the source's root: file keys are relative to it

For each PDF under the directory: size, sha256 and page count, so the golden validator can check an item's pages and
quotes against the exact file it was written for, and say what to fetch when it isn't there. Re-run after adding
files; a file whose hash changes makes every item that cites it fail validation until it is re-checked.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from sharepoint_evals.corpus import DATA  # noqa: E402  (data/, or EVAL_DATA_DIR, as the evals resolve files)

OUT = ROOT / "evals" / "golden" / "external.json"
MANUALS_ABOUT = ("US Army / Air Force technical manuals (Distribution A: approved for public release), from the Internet "
                 "Archive collection 'U.S. Military Vehicle Manuals'.")
ARCHIVE = "https://archive.org/download/0000-00-00-00-us-military-vehicle-manuals-unclassified-and-or-declassified/"


def describe(path: Path) -> dict:
    from pypdf import PdfReader

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("password-protected pdf")
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest(), "pages": len(reader.pages)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", type=Path, default=DATA / "manuals",
                        help="the source's folder, under data/ or EVAL_DATA_DIR (default: manuals there)")
    parser.add_argument("--source", default="manuals", help="the name items use as the first part of a path")
    parser.add_argument("--download", help="base URL the files are fetched from (+ the file's own name); required for a new "
                        "source other than manuals")
    parser.add_argument("--acl", action="append", metavar="PRINCIPAL",
                        help="who may read the files, e.g. org:members (repeatable); required for a new source other than manuals")
    parser.add_argument("--prune", action="store_true", help="drop the manifest's entries for files that aren't on disk (default: keep them)")
    args = parser.parse_args()
    args.dir = args.dir.resolve()
    if not args.dir.is_relative_to(DATA.resolve()):  # before hashing anything: the manifest stores the folder relative to it
        print(f"{args.dir} is not under {DATA.resolve()} (data/, or EVAL_DATA_DIR)", file=sys.stderr)
        return 1
    files = sorted(p for p in args.dir.rglob("*.pdf") if p.is_file())
    if not files:
        print(f"no PDFs under {args.dir}", file=sys.stderr)
        return 1
    # fetch_golden_files.py asks the download URL for each file by its own name, so two files can't share one
    if clash := sorted(n for n, k in Counter(p.name for p in files).items() if k > 1):
        print(f"files with the same name in different folders can't be fetched by name: {clash[:5]}", file=sys.stderr)
        return 1
    manifest = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {"sources": {}}
    listed = manifest["sources"].get(args.source, {})
    if args.source != "manuals" and not ((args.download or listed.get("download")) and (args.acl or listed.get("acl"))):
        print(f"source {args.source!r} is new: say where its files come from (--download URL) and who may read them "
              f"(--acl PRINCIPAL); the defaults belong to the public-release manuals", file=sys.stderr)
        return 1
    previous = manifest["sources"].get(args.source, {})
    described, unreadable = {}, []
    for n, path in enumerate(files, 1):
        rel = path.relative_to(args.dir).as_posix()
        try:
            # fields written by hand (covers: the vehicle or product the file is for) are kept
            described[rel] = {**previous.get("files", {}).get(rel, {}), **describe(path)}
        except Exception as e:  # one bad file doesn't stop the rest; an entry already in the manifest is kept below
            unreadable.append(rel)
            print(f"[{n}/{len(files)}] {rel}: not described ({type(e).__name__}: {e})", file=sys.stderr, flush=True)
            continue
        print(f"[{n}/{len(files)}] {rel}: {described[rel]['pages']} pages", flush=True)
    here = args.dir.relative_to(DATA.resolve()).as_posix()
    # Merge into what is listed: the files not downloaded here keep their hash and page count, so items citing them
    # are skipped rather than failing validation. --prune drops what isn't on disk.
    kept = {} if args.prune or previous.get("dir", here) != here else {k: v for k, v in previous.get("files", {}).items() if k not in described}
    if previous.get("dir", here) != here:
        print(f"note: the source was listed under {previous['dir']}, now {here}: its old entries are replaced", file=sys.stderr)
    described = {**kept, **described}
    manifest["_comment"] = ("Files the golden set cites but doesn't commit (too large, or not ours to redistribute). "
                            "Written by scripts/golden_manifest.py; see evals/sharepoint_evals/evidence.py.")
    manifest["sources"][args.source] = {
        "dir": here,
        # the defaults describe the manuals: Distribution A, approved for public release
        "acl": args.acl or previous.get("acl", ["org:members"]),
        "download": args.download or previous.get("download", ARCHIVE),  # + the file's own name, from fetch_golden_files.py
        "about": previous.get("about", MANUALS_ABOUT if args.source == "manuals" else args.source),
        "files": described,
    }
    OUT.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(described)} files ({len(files) - len(unreadable)} read now)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
