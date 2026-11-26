"""Download the large files the golden set cites but doesn't commit (evals/golden/external.json).

    .venv/bin/python scripts/fetch_golden_files.py            # only the files golden items name, if missing
    .venv/bin/python scripts/fetch_golden_files.py --all      # every file in the manifest
    .venv/bin/python scripts/fetch_golden_files.py --list     # what would be fetched, and how big

Each file is fetched from its source's `download` URL plus its own name, written to a temporary name, checked against
the manifest's sha256 and only then moved into data/. A file that doesn't match is deleted, never kept.
"""
import argparse
import hashlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from sharepoint_evals import evidence, golden  # noqa: E402


def download(url: str, target: Path, sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(partial, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError("the downloaded file does not match the manifest's sha256")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--all", action="store_true", help="every file in the manifest, not only those items name")
    parser.add_argument("--list", action="store_true", help="show what would be fetched and exit")
    args = parser.parse_args()
    external = golden.external()
    wanted = set(external) if args.all else {n for i in golden.items() if i["metadata"]["status"] != "retired" for n in evidence.named_docs(i)} & set(external)
    todo = sorted(n for n in wanted if not evidence.present(external[n]))
    if not todo:
        print("nothing to fetch: every file the golden items name is already under data/")
        return 0
    total = sum(external[n].meta["bytes"] for n in todo) / 1e6
    print(f"{len(todo)} files, {total:,.0f} MB")
    failed = 0
    for n, name in enumerate(todo, 1):
        doc = external[name]
        spec = evidence.manifest(golden.DIR)["sources"][doc.meta["source"]]
        url = spec["download"] + urllib.parse.quote(Path(name).name)
        print(f"[{n}/{len(todo)}] {name} ({doc.meta['bytes'] / 1e6:,.0f} MB)" + (f"  <- {url}" if args.list else ""), flush=True)
        if args.list:
            continue
        try:
            download(url, doc.path, doc.meta["sha256"])
        except Exception as e:
            failed += 1
            print(f"  failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
