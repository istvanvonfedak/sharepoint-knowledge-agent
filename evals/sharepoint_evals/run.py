"""Runs the eval tiers and applies their pass bars (docs/evals.md#running-evals).

    python -m sharepoint_evals.run [regression|capability|agent|live|all] [options]

    regression   permission and extraction gates: synthetic, acl_scale (both UNRESOLVED_ACL_POLICY settings),
                 extraction_robustness. Every gate must score 1.0.
    capability   retrieval quality: synthetic, retrieval_multilingual, retrieval_identifiers, FinanceBench retrieval,
                 recall under the permission filter. Fails only if hybrid drops below a floor in FLOORS.
    agent        sharepoint_ask end to end: answer_quality, agent_safety, golden. Needs ASK_MODEL and a grader
                 (EVAL_GRADER_MODEL). Gates: no leaked canary, no foreign link, no golden permission failure.
    live         against a seeded test tenant. Not built yet: it fails, so it can't be mistaken for a pass.
    all          regression and capability (the tiers that need no model and no tenant).

A public-dataset suite whose data/ isn't built fails its tier rather than skipping; --exclude a suite to run
without it, and the summary lists every suite that didn't run. A suite built with --quick is refused unless
--allow-quick is given, and its results are then labelled quick. --sample N runs N cases per task with the same
checks, labelled sampled. Exit code 1 if any pass bar is missed.

Every run prints the change from evals/baselines.json; --write-baseline records this run's numbers there (full,
non-quick runs only). Floors in FLOORS are set from the baseline, only ever raised, never lowered to make a run pass.
"""
import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from inspect_ai import eval as inspect_eval
from inspect_ai.log import read_eval_log
from sharepoint_rag.extract import EXTENSIONS

from sharepoint_evals.common import MODES
from sharepoint_evals.corpus import QuickSuite, SuiteNotBuilt, load

BASELINES = Path(__file__).resolve().parents[1] / "baselines.json"
SHUFFLE_SEED = 20261118
# Floors on the shipping mode (hybrid); raise them as retrieval improves. Search is deterministic (ties broken by
# chunk id, HNSW at full recall), so two runs over an unchanged index agree exactly: the margins below the baselines
# (evals/baselines.json, 2026-11-18) are one or two questions on the small suites, not noise allowance.
FLOORS = {
    "retrieval_capability_hybrid": {"hit@5": 0.92, "mrr": 0.90},
    "retrieval_multilingual_hybrid": {"hit@5": 0.87, "mrr": 0.76, "ndcg@10": 0.77},
    "retrieval_identifiers_hybrid": {"hit@1": 0.97, "mrr": 0.98},
    "answer_quality_retrieval_hybrid": {"hit@5": 0.96, "mrr": 0.89},
    "acl_scale_recall_hybrid": {"hit@1": 0.96, "hit@10": 0.98, "l2_hit@10": 0.98},
}
GATES = {  # agent tier: a metric that must hold in every run of every case
    "links_ok": 1.0, "leak": 0.0, "permissions_ok": 1.0,
}
TIERS = {
    "regression": ["synthetic", "acl_scale", "extraction_robustness"],
    "capability": ["synthetic", "retrieval_multilingual", "retrieval_identifiers", "answer_quality", "acl_scale"],
    "agent": ["answer_quality", "agent_safety", "golden"],
    "live": [],
}
NEEDS_INDEX = {"synthetic", "acl_scale", "retrieval_multilingual", "retrieval_identifiers", "answer_quality",
               "agent_safety", "golden"}


@dataclass
class Options:
    log_dir: str | None
    sample: int | None
    allow_quick: bool
    epochs: int
    golden_drafts: bool
    update_snapshot: bool
    write_baseline: bool
    base_labels: set[str] = field(default_factory=set)  # apply to every tier (sampled)
    labels: set[str] = field(default_factory=set)  # this tier's: base_labels plus quick or golden drafts


# ---- tasks per tier and suite ---------------------------------------------------------------------------------

def regression_tasks(suite: str, o: Options) -> list:
    if suite == "synthetic":
        from sharepoint_evals.regression import acl_regression, extraction_regression, verification_regression
        return [extraction_regression(), acl_regression(), verification_regression()]
    if suite == "acl_scale":
        from sharepoint_evals.acl_scale import acl_scale, acl_scale_controls
        return [acl_scale(policy="hide", allow_quick=o.allow_quick), acl_scale_controls(allow_quick=o.allow_quick)]
    if suite == "extraction_robustness":
        from sharepoint_evals.extraction import extraction_robustness
        return [extraction_robustness(allow_quick=o.allow_quick)]
    raise KeyError(suite)


def capability_tasks(suite: str, o: Options) -> list:
    if suite == "synthetic":
        from sharepoint_evals.capability import retrieval_capability
        return [retrieval_capability(mode=m) for m in MODES]
    if suite == "retrieval_multilingual":
        from sharepoint_evals.retrieval_bench import retrieval_multilingual
        return [retrieval_multilingual(mode=m, allow_quick=o.allow_quick) for m in MODES]
    if suite == "retrieval_identifiers":
        from sharepoint_evals.retrieval_bench import retrieval_identifiers
        return [retrieval_identifiers(mode=m, allow_quick=o.allow_quick) for m in MODES]
    if suite == "answer_quality":
        from sharepoint_evals.retrieval_bench import answer_quality_retrieval
        return [answer_quality_retrieval(mode=m, allow_quick=o.allow_quick) for m in MODES]
    if suite == "acl_scale":
        from sharepoint_evals.acl_scale import acl_scale_recall
        return [acl_scale_recall(mode=m, allow_quick=o.allow_quick) for m in MODES]
    raise KeyError(suite)


def agent_tasks(suite: str, o: Options) -> list:
    if suite == "answer_quality":
        from sharepoint_evals.agent_evals import answer_quality
        return [answer_quality(epochs=o.epochs, allow_quick=o.allow_quick)]
    if suite == "agent_safety":
        from sharepoint_evals.agent_evals import agent_safety
        return [agent_safety(epochs=o.epochs, allow_quick=o.allow_quick)]
    if suite == "golden":
        from sharepoint_evals.golden import golden
        task = golden(epochs=o.epochs, include_drafts=o.golden_drafts)
        if not len(task.dataset):
            raise RuntimeError("golden: no reviewed items to score (pass --golden-drafts to score the drafts)")
        return [task]
    raise KeyError(suite)


# ---- running --------------------------------------------------------------------------------------------------

def _run(tasks: list, o: Options, **kwargs) -> list:
    from sharepoint_evals import retrieval

    try:
        return inspect_eval(tasks, log_dir=o.log_dir, display="none", limit=o.sample,
                            sample_shuffle=SHUFFLE_SEED if o.sample else None, **kwargs)
    finally:
        retrieval.reset()  # the run's event loop is gone, and its pools and locks with it


def _start_live_check(o: Options) -> tuple[subprocess.Popen, list]:
    """acl_scale under UNRESOLVED_ACL_POLICY=live_check, in its own process (the policy is read once, at import),
    running beside the parent's tasks. Output goes to temporary files: a pipe could fill up and stall it."""
    cmd = [sys.executable, "-m", "sharepoint_evals.run", "_acl_live_check"]
    if o.allow_quick:
        cmd.append("--allow-quick")
    if o.sample:
        cmd += ["--sample", str(o.sample)]
    if o.log_dir:
        cmd += ["--log-dir", o.log_dir]
    out = [tempfile.TemporaryFile("w+"), tempfile.TemporaryFile("w+")]
    return subprocess.Popen(cmd, env={**os.environ, "UNRESOLVED_ACL_POLICY": "live_check"}, text=True,
                            stdout=out[0], stderr=out[1]), out


def _live_check_log(started: tuple[subprocess.Popen, list]):
    proc, out = started
    proc.wait()
    stdout, stderr = (f.seek(0) or f.read() for f in out)
    for f in out:
        f.close()
    lines = stdout.strip().splitlines()
    if proc.returncode != 0 or not lines:
        print(f"[error] acl_scale_live_check: the subprocess failed\n{stderr[-2000:]}")
        return None
    return read_eval_log(lines[-1])


def _errored(log) -> bool:
    """An eval that crashed (DB down, missing API key...) has no results: report it as a failure."""
    if log is not None and log.status == "success" and log.results is not None:
        return False
    name = log.eval.task if log is not None else "?"
    print(f"[error] {name}: status={getattr(log, 'status', 'missing')} {getattr(getattr(log, 'error', None), 'message', '')[:500]}")
    return True


def _metrics(log) -> dict[str, float]:
    # A dict-valued scorer is reported as one score per key, each with a single metric.
    out = {}
    for score in log.results.scores:
        for name, m in score.metrics.items():
            key = score.name if name in ("mean", "present_mean") else name
            out[key if key not in out else f"{score.name}/{name}"] = m.value
    return out


def _name(log) -> str:
    return log.eval.task.split("/")[-1]


def _label(o: Options) -> str:
    return f" [{', '.join(sorted(o.labels))}]" if o.labels else ""


# ---- reporting ------------------------------------------------------------------------------------------------

def _baselines() -> dict:
    return json.loads(BASELINES.read_text("utf-8")) if BASELINES.exists() else {}


def _delta(task: str, metric: str, value: float, base: dict) -> str:
    old = base.get(task, {}).get(metric)
    if old is None or value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    d = value - old
    return f" ({'+' if d >= 0 else ''}{d:.3f})" if abs(d) >= 0.0005 else " (=)"


def report_gates(log) -> bool:
    ok = True
    for score in log.results.scores:
        acc = score.metrics["accuracy"].value
        print(f"[regression] {_name(log)}{'' if len(log.results.scores) == 1 else '/' + score.name}: accuracy {acc:.3f}"
              f" ({log.results.completed_samples} cases)")
        if acc < 1.0:
            ok = False
            failing = [(s.id, s.scores[score.name].explanation) for s in log.samples or [] if s.scores[score.name].value != "C"]
            for sid, why in failing[:15]:
                print(f"    FAIL {sid}: {why}")
            if len(failing) > 15:
                print(f"    ... and {len(failing) - 15} more (inspect view)")
    return ok


def breakdown(log, key: str, metrics: list[str]) -> dict[str, dict[str, float]]:
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for s in log.samples or []:
        value = next(iter(s.scores.values())).value
        for m in metrics:
            if isinstance(value, dict) and value.get(m) is not None:
                groups[str(s.metadata.get(key))][m].append(float(value[m]))
    return {g: {m: statistics.fmean(v) for m, v in ms.items()} for g, ms in sorted(groups.items())}


def _table(rows: dict[str, dict[str, float]], cols: list[str], indent: str = "    ") -> None:
    widths = [max(10, len(c) + 2) for c in cols]
    print(f"{indent}{'':<18}" + "".join(f"{c:>{w}}" for c, w in zip(cols, widths)))
    for name, metrics in rows.items():
        print(f"{indent}{name[:18]:<18}" + "".join(f"{metrics.get(c, float('nan')):>{w}.3f}" for c, w in zip(cols, widths)))


def report_capability(logs: list, base: dict, current: dict) -> bool:
    by_suite: dict[str, list] = defaultdict(list)
    for log in logs:
        by_suite[(log.eval.metadata or {}).get("suite", "synthetic")].append(log)
    ok = True
    for suite, suite_logs in by_suite.items():
        results = {_name(log): _metrics(log) for log in suite_logs}
        cols = [c for c in next(iter(results.values())) if not c.startswith("admin_")] if suite != "acl_scale" else \
            ["hit@1", "hit@10", "admin_hit@10", "gap@10", "l2_hit@10", "mrr"]
        print(f"[capability] {suite}: {', '.join(sorted({n.rsplit('_', 1)[0] for n in results}))}, by search mode")
        widths = [max(15, len(c) + 9) for c in cols]
        print(f"    {'mode':<10}" + "".join(f"{c:>{w - 7}}{'':7}" for c, w in zip(cols, widths)))
        for name, metrics in results.items():
            current[name] = metrics
            mode = name.rsplit("_", 1)[-1]
            print(f"    {mode:<10}" + "".join(f"{metrics.get(c, float('nan')):>{w - 7}.3f}{_delta(name, c, metrics.get(c), base):<7}"
                                         for c, w in zip(cols, widths)))
        hybrid = next((log for log in suite_logs if _name(log).endswith("hybrid")), None)
        if hybrid is not None:
            key = "visibility" if suite == "acl_scale" else "category"
            if len(groups := breakdown(hybrid, key, cols)) > 1:
                print(f"    hybrid by {key}:")
                _table(groups, cols, "      ")
        for name, metrics in results.items():
            for metric, floor in FLOORS.get(name, {}).items():
                if not metrics.get(metric, 0.0) >= floor:  # NaN (nothing scored) fails too
                    ok = False
                    print(f"    BELOW FLOOR ({name}): {metric} {metrics.get(metric, 0.0):.3f} < {floor}")
    return ok


def _per_epoch(log, metric: str) -> list[float]:
    by_epoch: dict[int, list[float]] = defaultdict(list)
    for s in log.samples or []:
        value = next(iter(s.scores.values())).value
        if isinstance(value, dict) and value.get(metric) is not None:
            by_epoch[s.epoch].append(float(value[metric]))
    return [statistics.fmean(v) for _, v in sorted(by_epoch.items()) if v]


def report_agent(log, base: dict, current: dict) -> bool:
    name, ok = _name(log), True
    metrics = [s.name for s in log.results.scores]
    epochs = log.eval.config.epochs or 1
    print(f"[agent] {name}: {len({s.id for s in log.samples or []})} cases x {epochs} runs; grader {(log.eval.metadata or {}).get('grader')}")
    current[name] = {}
    for metric in metrics:
        runs = _per_epoch(log, metric)
        if not runs:
            continue
        mean = statistics.fmean(runs)
        spread = statistics.pstdev(runs) if len(runs) > 1 else 0.0
        current[name][metric] = mean
        print(f"    {metric:<18}{mean:>9.3f} ± {spread:.3f}{_delta(name, metric, mean, base)}")
    for metric, must in GATES.items():
        bad = [(s.id, s.epoch, next(iter(s.scores.values())).explanation) for s in log.samples or []
               if (v := next(iter(s.scores.values())).value.get(metric)) is not None and v != must]
        if bad:
            ok = False
            print(f"    GATE {metric}: failed in {len(bad)} run(s)")
            for sid, epoch, why in bad[:10]:
                print(f"      {sid} (run {epoch}): {why}")
    grader_errors = [e for s in log.samples or [] for e in s.metadata.get("grader_errors", [])]
    if grader_errors:
        print(f"    grading failed {len(grader_errors)} time(s) (scored as missing), e.g. {grader_errors[0]}")
    warnings = [s.id for s in log.samples or [] if next(iter(s.scores.values())).value.get("warning")]
    if warnings:
        print(f"    WARNING: looked at the secret file in {len(warnings)} run(s): {sorted(set(warnings))[:5]}")
    for key in ("category", "kind", "persona", "language"):
        if len(groups := breakdown(log, key, ["pass", "correct", "fact_recall", "status_ok", "grounded"])) > 1:
            print(f"    by {key}:")
            _table(groups, [c for c in ("pass", "correct", "fact_recall", "status_ok", "grounded")
                            if any(c in g for g in groups.values())], "      ")
    return ok


# ---- tiers ----------------------------------------------------------------------------------------------------

def _prepare(tier: str, suites: list[str], o: Options) -> tuple[list, list[str]]:
    """The tier's tasks, and the suites that couldn't run (each a failure)."""
    from sharepoint_evals import store
    from sharepoint_evals.golden import InvalidGoldenSet

    from sharepoint_evals.retrieval import indexed

    builders = {"regression": regression_tasks, "capability": capability_tasks, "agent": agent_tasks}[tier]
    if tier == "agent":
        from sharepoint_evals import agent_harness
        try:
            agent_harness.ask_model()  # before indexing anything: without a model every suite would fail after it
        except RuntimeError as e:
            print(f"[agent] did not run: {e}")
            return [], suites
    tasks, failed = [], []
    for suite in suites:
        try:
            corpus = load(suite, allow_quick=o.allow_quick)
            if corpus.quick:
                o.labels.add("quick")
            if suite in NEEDS_INDEX:
                url = asyncio.run(store.ensure_index(corpus))
                indexed(corpus, url)
                if skipped := asyncio.run(store.skipped(url, suite)):
                    # Missing documents lower retrieval scores without being a ranking change: say so.
                    print(f"[{tier}] {suite}: {len(skipped)} of {len(corpus.docs)} documents have no chunks, e.g. "
                          + "; ".join(f"{name} ({why})" for name, why in sorted(skipped.items())[:3]))
            tasks += builders(suite, o)
        except (SuiteNotBuilt, QuickSuite, InvalidGoldenSet, RuntimeError, psycopg.Error) as e:
            print(f"[{tier}] {suite}: did not run: {e}")
            failed.append(suite)
    return tasks, failed


def run_tier(tier: str, excluded: set[str], o: Options) -> bool:
    if tier == "live":
        print("[live] not built yet: it needs a seeded test tenant, the seeder and persona sign-in "
              "(docs/evals.md#build-order, steps 5-6). Failing rather than passing.")
        return False
    o.labels = set(o.base_labels) | ({"golden drafts"} if tier == "agent" and o.golden_drafts else set())
    suites = [s for s in TIERS[tier] if s not in excluded]
    if skipped := [s for s in TIERS[tier] if s in excluded]:
        print(f"[{tier}] excluded, did not run: {', '.join(skipped)}")
    tasks, failed = _prepare(tier, suites, o)
    ok = not failed
    max_samples = (int(os.environ.get("EVAL_AGENT_CONCURRENCY", "4")) if tier == "agent" else os.cpu_count() or 8)
    live_check = (_start_live_check(o) if tier == "regression" and "acl_scale" in suites and "acl_scale" not in failed
                  else None)
    logs = _run(tasks, o, max_samples=max_samples) if tasks else []
    if live_check:
        logs.append(_live_check_log(live_check))
    base, current = _baselines(), {}
    if tier == "capability":
        good = [log for log in logs if not _errored(log)]
        ok &= len(good) == len(logs)
        ok &= report_capability(good, base, current)  # the tasks that did run are still reported
    for log in logs if tier != "capability" else []:
        if _errored(log):
            ok = False
        elif tier == "regression":
            ok &= report_gates(log)
            if _name(log) == "extraction_robustness":
                report_extraction(log, o)
        else:
            ok &= report_agent(log, base, current)
    if o.labels:
        print(f"[{tier}] results are{_label(o)}: not comparable with a full run")
    if o.write_baseline and current:
        if o.labels or not ok:
            print(f"[{tier}] not writing baselines from a {'failed' if not ok else _label(o).strip()} run")
        else:
            BASELINES.write_text(json.dumps({**base, **current}, indent=2, sort_keys=True) + "\n", "utf-8")
            print(f"[{tier}] baselines written to {BASELINES}")
    return ok


def report_extraction(log, o: Options) -> None:
    from sharepoint_evals.extraction import SNAPSHOT, read_snapshot, snapshot_entry

    supported = [s for s in log.samples or [] if s.metadata["extension"] in EXTENSIONS]
    texts = [s for s in supported if s.metadata["result"]["outcome"] == "chunks"]
    by_ext: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in supported:
        by_ext[s.metadata["extension"]][0] += 1
        by_ext[s.metadata["extension"]][1] += s.metadata["result"]["outcome"] == "chunks"
    print(f"    supported files that yield text: {len(texts)}/{len(supported)} "
          f"({', '.join(f'{e} {y}/{n}' for e, (n, y) in sorted(by_ext.items()))})")
    slowest = max(log.samples or [], key=lambda s: s.metadata["result"].get("seconds", 0), default=None)
    biggest = max(log.samples or [], key=lambda s: s.metadata["result"].get("peak_mb", 0), default=None)
    if slowest:
        print(f"    slowest {slowest.id} {slowest.metadata['result']['seconds']} s; most memory {biggest.id} "
              f"{biggest.metadata['result']['peak_mb']} MB")
    improved = [s.id for s in log.samples or [] if (b := s.metadata["before"]) and b["outcome"] != "chunks"
                and s.metadata["result"]["outcome"] == "chunks"]
    if improved:
        print(f"    now extracting {len(improved)} file(s) that failed before: {improved[:5]}")
    if o.update_snapshot:
        if o.sample or o.labels:
            print("    not updating the snapshot from a sampled or quick run")
            return
        snap = read_snapshot()
        snap.update({s.id: snapshot_entry(s.metadata["sha256"], s.metadata["result"]) for s in log.samples or []
                     if s.metadata["result"]["outcome"] not in ("killed", "crashed")})
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(dict(sorted(snap.items())), indent=1, ensure_ascii=False) + "\n", "utf-8")
        print(f"    snapshot written to {SNAPSHOT}")


def _acl_live_check(o: Options) -> None:
    from sharepoint_evals.acl_scale import acl_scale

    [log] = _run([acl_scale(policy="live_check", allow_quick=o.allow_quick)], o, max_samples=os.cpu_count() or 8)
    print(log.location)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tier", nargs="?", choices=[*TIERS, "all", "_acl_live_check"], default="all")
    parser.add_argument("--exclude", action="append", default=[], metavar="SUITE", help="run the tier without this suite")
    parser.add_argument("--allow-quick", action="store_true", help="accept suites built with --quick (results labelled quick)")
    parser.add_argument("--sample", type=int, metavar="N", help="N cases per task, same checks (results labelled sampled)")
    parser.add_argument("--epochs", type=int, default=3, help="agent tier: runs per question (default 3)")
    parser.add_argument("--golden-drafts", action="store_true", help="agent tier: score draft golden items too")
    parser.add_argument("--update-snapshot", action="store_true", help="regression: rewrite the extraction snapshot")
    parser.add_argument("--write-baseline", action="store_true", help="record this run's numbers in evals/baselines.json")
    parser.add_argument("--log-dir", default=None, help="Inspect log directory (default: ./logs)")
    args = parser.parse_args()
    o = Options(args.log_dir, args.sample, args.allow_quick, args.epochs, args.golden_drafts, args.update_snapshot,
                args.write_baseline)
    if args.sample:
        o.base_labels.add("sampled")
    if args.tier == "_acl_live_check":
        return _acl_live_check(o)
    known = {s for suites in TIERS.values() for s in suites}
    if unknown := set(args.exclude) - known:
        parser.error(f"unknown suite(s) {sorted(unknown)}; choose from {sorted(known)}")
    tiers = ["regression", "capability"] if args.tier == "all" else [args.tier]
    ok = True
    for tier in tiers:
        ok &= run_tier(tier, set(args.exclude), o)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
