"""Runs the eval suites and applies pass/fail gates.

    python -m sharepoint_evals.run [regression|capability|all]

Regression evals must score 1.0. Capability evals are a quality gradient: they print a comparison across
search modes and fail only if the shipping mode (hybrid) drops below the floors in CAPABILITY_FLOORS.
"""
import argparse
import sys

from inspect_ai import eval as inspect_eval

from sharepoint_evals.capability import retrieval_capability
from sharepoint_evals.regression import acl_regression, extraction_regression

MODES = ("vector", "keyword", "hybrid")
# Floors for the hybrid mode; raise them as retrieval improves, never lower them to make a run pass.
CAPABILITY_FLOORS = {"hit@5": 0.85, "mrr": 0.60}


def _metrics(log) -> dict[str, float]:
    # A dict-valued scorer is reported as one score per key, each with a single "mean" metric.
    return {
        (score.name if name == "mean" else name): m.value
        for score in log.results.scores
        for name, m in score.metrics.items()
    }


def _errored(log) -> bool:
    """An eval that crashed (DB down, missing API key...) has no results: report it as a failure."""
    if log.status == "success" and log.results is not None:
        return False
    print(f"[error] {log.eval.task}: status={log.status} {getattr(log.error, 'message', '')[:200]}")
    return True


def run_regression(log_dir: str | None) -> bool:
    ok = True
    for log in inspect_eval([extraction_regression(), acl_regression()], log_dir=log_dir, display="none"):
        if _errored(log):
            ok = False
            continue
        accuracy = _metrics(log)["accuracy"]
        print(f"[regression] {log.eval.task}: accuracy {accuracy:.3f}")
        if accuracy < 1.0:
            ok = False
            for s in log.samples or []:
                for name, score in s.scores.items():
                    if score.value != "C":
                        print(f"    FAIL {s.id} ({name}): {score.explanation}")
    return ok


def run_capability(log_dir: str | None) -> bool:
    logs = inspect_eval([retrieval_capability(mode=m) for m in MODES], log_dir=log_dir, display="none")
    if any([_errored(log) for log in logs]):
        return False
    results = {m: _metrics(log) for m, log in zip(MODES, logs)}
    cols = ["hit@1", "hit@3", "hit@5", "mrr"]
    print("[capability] retrieval quality by search mode")
    print(f"    {'mode':<9}" + "".join(f"{c:>8}" for c in cols))
    for mode, metrics in results.items():
        print(f"    {mode:<9}" + "".join(f"{metrics[c]:>8.3f}" for c in cols))
    failures = [f"{c} {results['hybrid'][c]:.3f} < {floor}" for c, floor in CAPABILITY_FLOORS.items() if results["hybrid"][c] < floor]
    for f in failures:
        print(f"    BELOW FLOOR (hybrid): {f}")
    return not failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", nargs="?", choices=["regression", "capability", "all"], default="all")
    parser.add_argument("--log-dir", default=None, help="Inspect log directory (default: ./logs)")
    args = parser.parse_args()
    ok = True
    if args.suite in ("regression", "all"):
        ok &= run_regression(args.log_dir)
    if args.suite in ("capability", "all"):
        ok &= run_capability(args.log_dir)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
