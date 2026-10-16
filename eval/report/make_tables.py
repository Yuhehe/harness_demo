"""Generate the result tables from results/runs (CLAUDE.md section 2.3: the only source of numbers).

    uv run python eval/report/make_tables.py [--main M0 --baseline B0] [--check]

Minimal version (plan day 3): the main comparison only. Both runs are re-aggregated from their
trajectories and the scorer's output (resolved.json, score.json), not read from a metrics.json
that may be older than the trajectories. Writes

    results/summary/main.json   every number in the table, with where it came from (run ids,
                                commits, config hashes, models, worker counts)
    results/summary/main.md     the table
    README.md                   the same table between <!-- RESULTS:MAIN:START --> and END

results/runs/ is not committed; results/summary/ is, so the README can be traced to it.
`--check` writes nothing and exits 1 if any of the three would change (a hand edit, or results
that moved on without the tables being regenerated).

Refuses to build a table when the two runs did not finish the same tasks, or a run has not been
scored: a comparison on different tasks is not a comparison.

Worker counts: a run's status.jsonl rows carry `workers` since v0.1.1; rows without it come from
the runner before that, which ran one task at a time (1). B0's status rows never carry it, so
B0's count comes from its run_meta.json, which records how the run was started.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aggregate import aggregate, load_run, paired, task_metrics

ROOT = Path(__file__).resolve().parents[2]
SUBSET_DIR = ROOT / "configs" / "subsets"
MARK = "<!-- RESULTS:{name}:{edge} -->"
NAMES = {"main": "harness-lab", "baseline": "mini-SWE-agent"}


# ---- inputs -----------------------------------------------------------------------------------
def subset_name(ids: set[str], subset_dir: Path = SUBSET_DIR) -> str:
    """The subset file whose ids are exactly `ids`, or `custom`."""
    for path in sorted(subset_dir.glob("*.txt")):
        if {x.strip() for x in path.read_text().splitlines() if x.strip()} == ids:
            return path.stem
    return "custom"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def provenance(run_dir: Path, tasks: dict[str, Any]) -> dict[str, Any]:
    """Commits, config hashes, models and worker counts the run's tasks were made with."""
    meta = json.loads((run_dir / "run_meta.json").read_text())
    starts = [
        s for t in tasks.values() for s in t.spans[:1] if s.get("type") == "run_start"
    ]
    commits = {s["git_commit"] for s in starts if s.get("git_commit")} or {
        meta.get("git_commit")
    }
    hashes = {s["config_hash"] for s in starts if s.get("config_hash")} or {
        meta.get("config_hash") or meta.get("harness_config_hash")
    }
    status = _jsonl(run_dir / "status.jsonl")
    if any("workers" in r for r in status) or "workers" not in meta:
        workers = {r.get("workers", 1) for r in status} or {1}
    else:
        workers = {meta["workers"]}
    return {
        "git_commits": sorted(c for c in commits if c),
        "config_hashes": sorted(h for h in hashes if h),
        "models": sorted({s["model"] for s in starts if s.get("model")}),
        "workers": sorted(workers),
    }


def load(run_id: str, results_dir: Path) -> dict[str, Any]:
    run_dir = results_dir / run_id
    tasks = load_run(run_dir)
    if not tasks:
        raise SystemExit(f"{run_id}: no trajectories in {run_dir / 'trajectories'}")
    resolved_path = run_dir / "resolved.json"
    if not resolved_path.exists():
        raise SystemExit(f"{run_id}: {resolved_path} is missing, score the run first")
    resolved = set(json.loads(resolved_path.read_text()))
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text()) if score_path.exists() else None
    metrics = aggregate(tasks, resolved, score)
    if metrics["incomplete"]:
        raise SystemExit(f"{run_id}: {metrics['incomplete']} task(s) have no run_end")
    return {
        "run_id": run_id,
        "ids": {i for i, t in tasks.items() if t.complete},
        "rows": [m for t in tasks.values() if (m := task_metrics(t))],
        "resolved": resolved,
        "metrics": metrics,
        "provenance": provenance(run_dir, tasks),
    }


# ---- the numbers ---------------------------------------------------------------------------------
def summarize(main: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    if main["ids"] != base["ids"]:
        only = sorted(main["ids"] ^ base["ids"])
        raise SystemExit(
            f"{main['run_id']} and {base['run_id']} finished different tasks "
            f"({len(main['ids'])} vs {len(base['ids'])}; differ on {only[:5]}...)"
        )

    def system(run: dict[str, Any], role: str) -> dict[str, Any]:
        m = run["metrics"]
        return {
            "role": role,
            "name": NAMES[role],
            "run_id": run["run_id"],
            "resolve_rate": m["resolve_rate"],
            "cost_per_task": m["cost_per_task"],
            "cost_per_resolved": m["quality"]["cost_per_resolved"],
            "tokens_in_mean": m["tokens_in_mean"],
            "tokens_out_mean": m["tokens_out_mean"],
            "cache_hit_rate": m["cache_hit_rate"],
            "latency": m["latency"],
            "steps_mean": m["steps_mean"],
            "fail_taxonomy": m["fail_taxonomy"],
            "provenance": run["provenance"],
        }

    return {
        "subset": subset_name(main["ids"]),
        "tasks": len(main["ids"]),
        "systems": [system(main, "main"), system(base, "baseline")],
        # main against baseline, task by task (aggregate.paired: sign test, bootstrap CI)
        "paired": paired(
            base["rows"], main["rows"], base["resolved"], main["resolved"]
        ),
    }


# ---- rendering ------------------------------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:.3f}"


def _k(x: float | None) -> str:
    return (
        "—" if x is None else f"{x / 1000:.0f}k" if x >= 10_000 else f"{x / 1000:.1f}k"
    )


def _ratio(p: dict[str, Any] | None) -> tuple[str, str]:
    if not p:
        return "—", "—"
    lo, hi = p["ci95"]
    return (
        f"{p['median_ratio']:.2f} ({lo:.2f}–{hi:.2f})",
        f"{p['other_higher']}/{p['n']}",
    )


def render(s: dict[str, Any]) -> str:
    main, base = s["systems"]
    pm, pb = main["provenance"], base["provenance"]
    if pm["config_hashes"] == pb["config_hashes"] and pm["models"] == pb["models"]:
        same = (
            f"same model and config for both (config {', '.join(pm['config_hashes'])})"
        )
    else:
        same = (
            f"configs {', '.join(pm['config_hashes'])} vs {', '.join(pb['config_hashes'])}, "
            f"models {', '.join(pm['models'])} vs {', '.join(pb['models'])}"
        )
    lines = [
        "### Main comparison",
        "",
        (
            f"Subset `{s['subset']}`, {s['tasks']} tasks; {same} "
            f"(run ids `{main['run_id']}`, `{base['run_id']}`)."
        ),
        "",
        (
            "| System | Resolve rate (95% CI) | $/task mean (P50) | $/resolved | Tokens in/out (mean)"
            " | Cache hit | P50 / P95 time | Steps (mean) |"
        ),
        "|---|---|---|---|---|---|---|---|",
    ]
    for x in (main, base):
        r = x["resolve_rate"]
        lo, hi = r["wilson95"]
        lines.append(
            f"| {x['name']} ({x['run_id']}) "
            f"| {r['resolved']}/{r['n']} = {_pct(r['rate'])} ({_pct(lo)}–{_pct(hi)}) "
            f"| {_usd(x['cost_per_task']['mean'])} ({_usd(x['cost_per_task']['p50'])}) "
            f"| {_usd(x['cost_per_resolved'])} "
            f"| {_k(x['tokens_in_mean'])} / {_k(x['tokens_out_mean'])} "
            f"| {_pct(x['cache_hit_rate'])} "
            f"| {x['latency']['p50']:.0f} s / {x['latency']['p95']:.0f} s "
            f"| {x['steps_mean']:.1f} |"
        )
    p = s["paired"]
    lines += [
        "",
        (
            f"Task by task, {main['run_id']} against {base['run_id']} ({p['tasks']} tasks; "
            "median of the per-task ratio with a bootstrap 95% interval):"
        ),
        "",
        (
            f"| Measure | Median ratio {main['run_id']}/{base['run_id']} (95% CI) "
            f"| Tasks where {main['run_id']} is higher |"
        ),
        "|---|---|---|",
    ]
    for key, label in (
        ("cost_usd", "Cost"),
        ("steps", "Steps"),
        ("wall_seconds", "Wall time"),
    ):
        ratio, higher = _ratio(p.get(key))
        lines.append(f"| {label} | {ratio} | {higher} |")
    res = p.get("resolved")
    if res:
        lines += [
            "",
            (
                f"Resolved by both: {res['both']}; only {main['run_id']}: {res['only_other']}; "
                f"only {base['run_id']}: {res['only_base']}; exact sign test p = "
                f"{res['sign_test_p']:.3g}."
            ),
        ]
    workers = ", ".join(
        f"{x['run_id']} {'/'.join(map(str, x['provenance']['workers']))}"
        for x in (main, base)
    )
    lines += [
        "",
        (
            f"Notes: workers per run: {workers}. Wall time is only comparable between runs with "
            "the same worker count made in the same period; B0's wall time runs from its first "
            "to its last model response, so it misses the wait for the first one."
        ),
    ]
    return "\n".join(lines) + "\n"


def replace_block(text: str, name: str, body: str) -> str:
    start, end = (
        MARK.format(name=name, edge="START"),
        MARK.format(name=name, edge="END"),
    )
    if text.count(start) != 1 or text.count(end) != 1:
        raise SystemExit(f"README needs exactly one {start} and one {end}")
    head, rest = text.split(start)
    _, tail = rest.split(end)
    return f"{head}{start}\n{body}{end}{tail}"


# ---- command line ----------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--main", default="M0", help="run id of the main configuration")
    ap.add_argument("--baseline", default="B0", help="run id of the baseline")
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--summary-dir", type=Path, default=Path("results/summary"))
    ap.add_argument("--readme", type=Path, default=Path("README.md"))
    ap.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if stale"
    )
    args = ap.parse_args(argv)

    summary = summarize(
        load(args.main, args.results_dir), load(args.baseline, args.results_dir)
    )
    table = render(summary)
    outputs = {
        args.summary_dir / "main.json": json.dumps(summary, indent=2, sort_keys=True)
        + "\n",
        args.summary_dir / "main.md": table,
        args.readme: replace_block(args.readme.read_text(), "MAIN", table),
    }
    stale = [
        p for p, text in outputs.items() if not p.exists() or p.read_text() != text
    ]
    if args.check:
        for p in stale:
            print(f"stale: {p}", file=sys.stderr)
        return 1 if stale else 0
    for p, text in outputs.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    print(table)
    print("-> " + ", ".join(str(p) for p in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
