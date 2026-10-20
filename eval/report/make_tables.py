"""Generate the result tables from results/runs (CLAUDE.md section 2.3: the only source of numbers).

    uv run python eval/report/make_tables.py [--main M0 --baseline B0] [--check]

Every run is re-aggregated from its trajectories and the scorer's output (resolved.json,
score.json), not read from a metrics.json that may be older than the trajectories. Writes, for
each table T in main, ablation, cost, security:

    results/summary/T.json      every number in the table, with where it came from (run ids,
                                commits, config hashes, models, worker counts)
    results/summary/T.md        the table
    README.md                   the same table between <!-- RESULTS:T:START --> and END

main      M0 against B0 on the same tasks.
ablation  each ablation run against BASE_ABLATION (M0-r2: same commit and worker count as the
          ablations) on the ablation's own tasks, plus a run-to-run row (M0 against M0-r2, same
          config) as the yardstick for noise, plus A6 against A6xA1 (context pressure). A run in
          ABLATIONS that does not exist is left out and named in a note; one that exists but is
          unscored or unfinished is an error.
cost      what each run cost and where M0's calls go next to B0's (quality.call_mix).
security  the false-block rate (CLAUDE.md section 6) from M0's guard_decision spans and the
          human review of each block (docs/failure_analysis.md, REVIEW_HEADING); a block
          without a review row is counted as unreviewed, not as either verdict.

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
from failure_taxonomy import DEFAULT_LABELS, classify, load_labels

ROOT = Path(__file__).resolve().parents[2]
SUBSET_DIR = ROOT / "configs" / "subsets"
MARK = "<!-- RESULTS:{name}:{edge} -->"
NAMES = {"main": "harness-lab", "baseline": "mini-SWE-agent"}
BASE_ABLATION = "M0-r2"
# id, run id, the change against M0 (one factor; A6xA1 is the deliberate pair)
ABLATIONS = (
    ("A1", "A1", "context = none"),
    ("A2", "A2", "action space = bash only"),
    ("A3", "A3", "context = truncate"),
    ("A4", "A4", "context = summarize"),
    ("A5", "A5", "planning = off"),
    ("A6", "A6", "context budget = 32k (hybrid)"),
    ("A6xA1", "A6xA1", "context budget = 32k, context = none"),
)
REVIEW_HEADING = "## 权限层拦截的人工复核"
SUBSETS_META = SUBSET_DIR / "subsets_meta.json"
HARNESS_SRC = ROOT / "src" / "harness"
REVIEW_PATH = ROOT / "docs" / "failure_analysis.md"


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
        "tasks": tasks,
        "ids": {i for i, t in tasks.items() if t.complete},
        "rows": [m for t in tasks.values() if (m := task_metrics(t))],
        "resolved": resolved,
        "score": score,
        "metrics": metrics,
        "provenance": provenance(run_dir, tasks),
    }


def load_if_present(run_id: str, results_dir: Path) -> dict[str, Any] | None:
    return load(run_id, results_dir) if (results_dir / run_id).is_dir() else None


def pair_on(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """`other` against `base` on `other`'s tasks, which must all be in `base`."""
    missing = other["ids"] - base["ids"]
    if missing:
        raise SystemExit(
            f"{other['run_id']} has tasks {sorted(missing)[:5]} that {base['run_id']} lacks"
        )
    # aggregate.paired keeps the tasks both sides have, so `other`'s tasks
    return {
        "base": base["run_id"],
        "other": other["run_id"],
        **paired(base["rows"], other["rows"], base["resolved"], other["resolved"]),
    }


def _overflow(run: dict[str, Any]) -> float:
    return run["metrics"]["overflow_rate"]


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


def summarize_ablations(
    runs: dict[str, dict[str, Any]], base: dict[str, Any], m0: dict[str, Any] | None
) -> dict[str, Any]:
    rows, not_run = [], []
    for aid, run_id, change in ABLATIONS:
        run = runs.get(run_id)
        if run is None:
            not_run.append(aid)
            continue
        m = run["metrics"]
        rows.append(
            {
                "id": aid,
                "run_id": run_id,
                "change": change,
                "subset": subset_name(run["ids"]),
                "tasks": len(run["ids"]),
                "resolve_rate": m["resolve_rate"],
                "cost_per_task": m["cost_per_task"],
                "overflow_rate": m["overflow_rate"],
                "paired": pair_on(base, run),
                "provenance": run["provenance"],
            }
        )
    out: dict[str, Any] = {
        "base": base["run_id"],
        "base_provenance": base["provenance"],
        "rows": rows,
        "not_run": not_run,
        "base_compactions": base["metrics"]["compaction"]["tasks_with_compaction"],
    }
    if m0 is not None:  # the yardstick: the same config twice, in both directions
        m0_on_base = {
            **m0,
            "ids": m0["ids"] & base["ids"],
            "rows": [r for r in m0["rows"] if r["instance_id"] in base["ids"]],
        }
        out["run_to_run"] = pair_on(base, m0_on_base)
        out["run_to_run_reverse"] = pair_on(m0_on_base, base)
        out["calibration"] = calibration(out)
    if "A6" in runs and "A6xA1" in runs:
        out["pressure"] = {
            "with": "A6",
            "without": "A6xA1",
            "overflow_with": _overflow(runs["A6"]),
            "overflow_without": _overflow(runs["A6xA1"]),
            "paired": pair_on(runs["A6xA1"], runs["A6"]),
        }
    return out


def summarize_cost(
    runs: dict[str, dict[str, Any]], order: list[str], main_id: str, base_id: str
) -> dict[str, Any]:
    rows = []
    for run_id in order:
        run = runs.get(run_id)
        if run is None:
            continue
        m, q = run["metrics"], run["metrics"]["quality"]
        rows.append(
            {
                "run_id": run_id,
                "subset": subset_name(run["ids"]),
                "tasks": len(run["ids"]),
                "total_usd": sum(r["cost_usd"] for r in run["rows"]),
                "cost_per_task": m["cost_per_task"],
                "cost_per_resolved": q["cost_per_resolved"],
                "tokens_in_mean": m["tokens_in_mean"],
                "tokens_out_mean": m["tokens_out_mean"],
                "cache_hit_rate": m["cache_hit_rate"],
                "compaction_cost_share": m["compaction"]["compaction_cost_share"],
                "steps_mean": m["steps_mean"],
                "call_mix": q["call_mix"],
            }
        )
    return {"rows": rows, "main": main_id, "baseline": base_id}


def load_reviews(path: Path, run_id: str) -> dict[str, list[str]]:
    """Verdicts per instance from the review table: `| run | instance | rule | verdict | ... |`."""
    if not path.exists():
        return {}
    out: dict[str, list[str]] = {}
    inside = False
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            inside = line.startswith(REVIEW_HEADING)
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if inside and line.startswith("|") and len(cells) >= 4 and cells[0] == run_id:
            if cells[3] not in ("false_block", "true_block"):
                raise SystemExit(f"{path}: verdict {cells[3]!r} for {cells[1]}")
            out.setdefault(cells[1], []).append(cells[3])
    return out


def summarize_security(
    run: dict[str, Any], tasks: dict[str, Any], reviews: dict[str, list[str]]
) -> dict[str, Any]:
    decisions = [
        s for t in tasks.values() for s in t.spans if s["type"] == "guard_decision"
    ]
    bash = [s for s in decisions if s.get("tool") == "bash"]
    by_rule: dict[str, int] = {}
    for s in decisions:
        if s.get("action") == "deny":
            rule = s.get("rule_id") or "?"
            by_rule[rule] = by_rule.get(rule, 0) + 1
    denies: dict[str, int] = {}
    for t in tasks.values():
        n = sum(
            1
            for s in t.spans
            if s["type"] == "guard_decision" and s.get("action") == "deny"
        )
        if n:
            denies[t.instance_id] = n
    false = true = unreviewed = 0
    for iid, n in denies.items():  # one review row per block
        verdicts = reviews.get(iid, [])
        f = min(n, verdicts.count("false_block"))
        t = min(n - f, verdicts.count("true_block"))
        false, true, unreviewed = false + f, true + t, unreviewed + n - f - t
    return {
        "run_id": run["run_id"],
        "tasks": len(run["ids"]),
        "decisions": len(decisions),
        "bash_decisions": len(bash),
        "denies": sum(denies.values()),
        "denies_by_rule": dict(sorted(by_rule.items())),
        "false_blocks": false,
        "true_blocks": true,
        "unreviewed": unreviewed,
        "false_block_rate": false / len(decisions) if decisions else None,
        "false_block_rate_bash": false / len(bash) if bash else None,
        "injection_suite": "not run (optional, plan.md)",
    }


def _git(*args: str) -> str:
    import subprocess  # trusted host command (git metadata), not model output

    r = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=ROOT
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def summarize_setup(main_run: dict[str, Any], cost: dict[str, Any]) -> dict[str, Any]:
    start = next(
        (
            s
            for t in main_run["tasks"].values()
            for s in t.spans[:1]
            if s.get("type") == "run_start"
        ),
        {},
    )
    cfg = start.get("config", {})
    repos: dict[str, int] = {}
    for iid in main_run["ids"]:
        repo = iid.rsplit("-", 1)[0].split("__")[0]
        repos[repo] = repos.get(repo, 0) + 1
    meta = json.loads(SUBSETS_META.read_text()) if SUBSETS_META.exists() else {}
    loc = sum(
        1
        for f in sorted(HARNESS_SRC.rglob("*.py"))
        for line in f.read_text().splitlines()
        if line.strip()
    )
    return {
        "subset": subset_name(main_run["ids"]),
        "tasks": len(main_run["ids"]),
        "repos": dict(sorted(repos.items())),
        "seed": meta.get("seed"),
        "dataset_revision": meta.get("revision"),
        "models": main_run["provenance"]["models"],
        "summary_model": start.get("summary_model"),
        "thinking": start.get("thinking"),
        "reasoning_effort": start.get("reasoning_effort"),
        "limits": cfg.get("limits"),
        "budget_tokens": (cfg.get("context") or {}).get("budget_tokens"),
        "config_hashes": main_run["provenance"]["config_hashes"],
        "commits": [
            {"commit": c, "tags": _git("tag", "--points-at", c).split()}
            for c in main_run["provenance"]["git_commits"]
        ],
        "harness_lines": loc,
        "cost_of_tables": sum(r["total_usd"] for r in cost["rows"]),
        "cost_runs": [r["run_id"] for r in cost["rows"]],
    }


def summarize_failures(run: dict[str, Any], labels_path: Path) -> dict[str, Any]:
    out = classify(
        run["tasks"],
        run["resolved"],
        run["score"],
        load_labels(labels_path, run["run_id"]),
    )
    by: dict[str, list[str]] = {}
    for r in out["per_task"]:
        if not r["resolved"]:
            by.setdefault(f"{r['end']} / {r['tests']}", []).append(r["instance_id"])
    return {
        "run_id": run["run_id"],
        "tasks": out["tasks"],
        "failures": out["failures"],
        "by_class": dict(sorted(by.items())),
        "loop_suspects": out["loop_signal"]["suspect"],
    }


def summarize_tldr(t: dict[str, Any]) -> dict[str, Any]:
    main = t["main"]
    m, b = main["systems"]
    sec = t["security"]
    out: dict[str, Any] = {
        "subset": main["subset"],
        "tasks": main["tasks"],
        "main": {
            k: m[k]
            for k in ("run_id", "resolve_rate", "cost_per_task", "cache_hit_rate")
        },
        "baseline": {k: b[k] for k in ("run_id", "resolve_rate", "cost_per_task")},
        "resolved_pairs": main["paired"].get("resolved"),
        "cost_ratio": main["paired"].get("cost_usd"),
        "steps_ratio": main["paired"].get("steps"),
        "false_block_rate": sec["false_block_rate"],
        "false_blocks": sec["false_blocks"],
        "denies": sec["denies"],
        "unreviewed": sec["unreviewed"],
        "deny_rules": list(sec["denies_by_rule"]),
    }
    pr = t["ablation"].get("pressure")
    if pr:
        out["pressure"] = {
            "overflow_with": pr["overflow_with"],
            "overflow_without": pr["overflow_without"],
            "resolved": pr["paired"].get("resolved"),
            "tasks": pr["paired"]["tasks"],
        }
    rr = t["ablation"].get("run_to_run")
    if rr and rr.get("resolved"):
        r = rr["resolved"]
        out["rerun"] = {
            "a": rr["other"],
            "b": rr["base"],
            "resolved_a": r["both"] + r["only_other"],
            "resolved_b": r["both"] + r["only_base"],
            "flipped": r["only_other"] + r["only_base"],
            "tasks": rr["tasks"],
        }
    return out


# ---- rendering ------------------------------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def _pct2(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.2f}%"


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
            f"| Tasks where {main['run_id']} is higher | Differs |"
        ),
        "|---|---|---|---|",
    ]
    for key, label in (
        ("cost_usd", "Cost"),
        ("steps", "Steps"),
        ("wall_seconds", "Wall time"),
    ):
        ratio, higher = _ratio(p.get(key))
        verdict = differs(p.get(key)) or "—"
        if key == "wall_seconds" and verdict != "—":
            verdict += " (descriptive only, see notes)"
        lines.append(f"| {label} | {ratio} | {higher} | {verdict} |")
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
    lines += ["", RULE + " " + _calibration_note(s.get("calibration"))]
    lines += [
        "",
        (
            f"Notes: workers per run: {workers}. Wall time is only comparable between runs with "
            "the same worker count made in the same period; B0's wall time runs from its first "
            "to its last model response, so it misses the wait for the first one."
        ),
    ]
    return "\n".join(lines) + "\n"


def _ci(p: dict[str, Any] | None) -> str:
    if not p:
        return "—"
    lo, hi = p["ci95"]
    return f"{p['median_ratio']:.2f} ({lo:.2f}–{hi:.2f})"


def differs(p: dict[str, Any] | None) -> str | None:
    """Rule B (docs/notes.md section 22.9): a ratio differs from 1 when its 95% interval
    excludes 1; "higher", "lower", or "no". None without data."""
    if not p:
        return None
    lo, hi = p["ci95"]
    return "higher" if lo > 1 else "lower" if hi < 1 else "no"


def calibration(a: dict[str, Any]) -> dict[str, Any] | None:
    """The check rule B rests on: the same configuration run twice must not "differ"."""
    pairs = [a[k] for k in ("run_to_run", "run_to_run_reverse") if k in a]
    if not pairs:
        return None
    verdicts = {
        f"{p['other']}/{p['base']} {key}": differs(p.get(key))
        for p in pairs
        for key in ("cost_usd", "steps")
    }
    return {"verdicts": verdicts, "passed": all(v == "no" for v in verdicts.values())}


def _calibration_note(c: dict[str, Any] | None) -> str:
    if c is None:
        return (
            "No rerun of the main configuration is available to check the rule against."
        )
    if c["passed"]:
        return (
            "Check: the main configuration run twice does not differ by this rule (every "
            "noise-row interval contains 1)."
        )
    bad = ", ".join(k for k, v in c["verdicts"].items() if v != "no")
    return (
        f"Check FAILED: the main configuration run twice differs by this rule ({bad}); "
        "intervals here understate the run-to-run noise."
    )


RULE = (
    "Rule: a cost or steps ratio counts as a difference when its 95% interval excludes 1 "
    "(docs/notes.md section 22.9; adopted after the first results, replacing a rule that "
    "compared intervals with the noise interval and depended on the ratio's direction)."
)


def _res(p: dict[str, Any]) -> str:
    r = p.get("resolved")
    if not r:
        return "—"
    return f"{r['only_other']} / {r['only_base']} (p = {r['sign_test_p']:.2g})"


def render_ablation(a: dict[str, Any]) -> str:
    base = a["base"]
    lines = [
        "### Ablations (one factor changed at a time)",
        "",
        (
            f"Each ablation against `{base}` (the main configuration rerun on the same commit "
            "with the same worker count) on the ablation's own tasks; ratios are medians of "
            'the per-task ratio with a bootstrap 95% interval. "Only abl / only base" counts '
            "the tasks only one side resolved, with an exact sign test."
        ),
        "",
        "| ID | Change vs. M0 | Tasks | Resolved | Only abl / only base | $/task | Cost ratio | Steps ratio | Differs (cost / steps) | Overflow rate |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in a["rows"]:
        rr, p = r["resolve_rate"], r["paired"]
        base_k = p["resolved"]["both"] + p["resolved"]["only_base"]
        lines.append(
            f"| {r['id']} | {r['change']} | {r['tasks']} (`{r['subset']}`) "
            f"| {rr['resolved']}/{rr['n']} (base {base_k}) | {_res(p)} | {_usd(r['cost_per_task']['mean'])} "
            f"| {_ci(p.get('cost_usd'))} | {_ci(p.get('steps'))} "
            f"| {differs(p.get('cost_usd')) or '—'} / {differs(p.get('steps')) or '—'} "
            f"| {_pct(r['overflow_rate'])} |"
        )
    for key in ("run_to_run", "run_to_run_reverse"):
        if key in a:
            p = a[key]
            lines.append(
                f"| (noise) | none: `{p['other']}` against `{p['base']}`, same config "
                f"| {p['tasks']} | — | {_res(p)} | — | {_ci(p.get('cost_usd'))} "
                f"| {_ci(p.get('steps'))} "
                f"| {differs(p.get('cost_usd')) or '—'} / {differs(p.get('steps')) or '—'} | — |"
            )
    notes = []
    if a["not_run"]:
        notes.append(
            f"Not run: {', '.join(a['not_run'])}. `{base}` compacted in "
            f"{a['base_compactions']} of its tasks at the 128k budget, so at that budget the "
            "context strategy never acts."
        )
    if "pressure" in a:
        pr = a["pressure"]
        p = pr["paired"]
        notes.append(
            f"Context pressure (32k): {pr['with']} (compaction on) against {pr['without']} "
            f"(off) on the same tasks: overflow {_pct(pr['overflow_with'])} vs "
            f"{_pct(pr['overflow_without'])}; resolved only with compaction / only without: "
            f"{_res(p)}; cost ratio {_ci(p.get('cost_usd'))}, steps ratio {_ci(p.get('steps'))} "
            "(runs that overflow stop early, so they are cheaper)."
        )
    notes.append(RULE)
    notes.append(
        "The two noise rows are the same pair of runs of the main configuration, each way "
        "round (the direction of a ratio is arbitrary). "
        + _calibration_note(a.get("calibration"))
        + " One run per configuration."
    )
    lines += [""] + notes
    return "\n".join(lines) + "\n"


def render_cost(c: dict[str, Any]) -> str:
    lines = [
        "### Cost",
        "",
        "Standard (peak) price for every token, the same formula for all runs (`llm/cost.py`).",
        "",
        "| Run | Tasks | Total | $/task mean (P50) | $/resolved | Tokens in/out (mean) | Cache hit | Compaction share | Steps |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in c["rows"]:
        lines.append(
            f"| {r['run_id']} | {r['tasks']} (`{r['subset']}`) | {_usd(r['total_usd'])} "
            f"| {_usd(r['cost_per_task']['mean'])} ({_usd(r['cost_per_task']['p50'])}) "
            f"| {_usd(r['cost_per_resolved'])} | {_k(r['tokens_in_mean'])} / {_k(r['tokens_out_mean'])} "
            f"| {_pct(r['cache_hit_rate'])} | {_pct(r['compaction_cost_share'])} | {r['steps_mean']:.1f} |"
        )
    mix = {r["run_id"]: r["call_mix"] for r in c["rows"] if r["call_mix"]}
    if c["main"] in mix and c["baseline"] in mix:
        lines += [
            "",
            "Where the calls go (per task; classes from the command text, `aggregate.py`):",
            "",
            "| Run | Explore | Run scripts | Run tests | Edit source | Todo | Tasks with one source edit | Responses with several calls |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for rid in (c["main"], c["baseline"]):
            m = mix[rid]
            pt = m["per_task"]
            lines.append(
                f"| {rid} | {pt['explore']:.1f} | {pt['script']:.1f} | {pt['test']:.1f} "
                f"| {pt['edit']:.1f} | {pt['todo']:.1f} | {m['single_edit_tasks']}/{m['tasks']} "
                f"| {_pct(m['multi_call_share'])} |"
            )
    return "\n".join(lines) + "\n"


def render_security(s: dict[str, Any]) -> str:
    return (
        "\n".join(
            [
                "### Permission guard",
                "",
                f"Prompt-injection suite: {s['injection_suite']}.",
                "",
                "| Run | Tasks | Guard checks (bash) | Blocks | Reviewed: false / true / unreviewed | False-block rate (all calls) | False-block rate (bash) |",
                "|---|---|---|---|---|---|---|",
                (
                    f"| {s['run_id']} | {s['tasks']} | {s['decisions']} ({s['bash_decisions']}) "
                    f"| {s['denies']} | {s['false_blocks']} / {s['true_blocks']} / {s['unreviewed']} "
                    f"| {_pct2(s['false_block_rate'])} | {_pct2(s['false_block_rate_bash'])} |"
                ),
                "",
                (
                    "Every block was reviewed by hand (docs/failure_analysis.md); the rule matches "
                    "commands by text, so it is defence in depth, not a proof. The sandbox (no "
                    "network, resource caps) is the boundary."
                ),
            ]
        )
        + "\n"
    )


def _rate(r: dict[str, Any]) -> str:
    lo, hi = r["wilson95"]
    return f"{r['resolved']}/{r['n']} ({_pct(r['rate'])}, 95% CI {_pct(lo)}–{_pct(hi)})"


def render_tldr(t: dict[str, Any]) -> str:
    m, b, rp = t["main"], t["baseline"], t["resolved_pairs"] or {}
    p = rp.get("sign_test_p")
    same = (
        f"indistinguishable (exact sign test p = {p:.3g})"
        if p is not None and p >= 0.05
        else f"different (exact sign test p = {p:.3g})"
    )
    cr, sr = t["cost_ratio"], t["steps_ratio"]
    lines = [
        "> **TL;DR** (every number here is generated by `eval/report/make_tables.py`)",
        (
            f"> - On {t['tasks']} SWE-bench Verified tasks (`{t['subset']}`), harness-lab "
            f"resolves **{_rate(m['resolve_rate'])}** and mini-SWE-agent "
            f"**{_rate(b['resolve_rate'])}** with the same model and limits: {same}. "
            f"harness-lab costs **{_usd(m['cost_per_task']['mean'])}** per task against "
            f"**{_usd(b['cost_per_task']['mean'])}** (task-by-task cost ratio {_ci(cr)}, "
            f"steps ratio {_ci(sr)})."
        ),
    ]
    if "pressure" in t:
        pr = t["pressure"]
        r = pr["resolved"] or {}
        lines.append(
            "> - Under a 32K context budget, compaction (hybrid) takes context-overflow "
            f"failures from **{_pct(pr['overflow_without'])}** (no compaction) to "
            f"**{_pct(pr['overflow_with'])}**, and resolves {r.get('only_other', 0)} tasks "
            f"the run without it misses ({r.get('only_base', 0)} the other way; p = "
            f"{r.get('sign_test_p', 1):.2g}; {pr['tasks']} tasks)."
        )
    lines.append(
        f"> - Prompt-cache hit rate on the main run: **{_pct(m['cache_hit_rate'])}** of "
        "input tokens."
    )
    rules = ", ".join(f"`{x}`" for x in t["deny_rules"]) or "none"
    lines.append(
        "> - Permission guard false-block rate on the main run: "
        f"**{_pct2(t['false_block_rate'])}** of tool calls ({t['false_blocks']} of "
        f"{t['denies']} blocks judged false after review, {t['unreviewed']} unreviewed; "
        f"rules that fired: {rules})."
    )
    return "\n".join(lines) + "\n"


def render_setup(s: dict[str, Any]) -> str:
    lim = s["limits"] or {}
    repos = ", ".join(f"{k} {v}" for k, v in s["repos"].items())
    commits = "; ".join(
        f"`{c['commit'][:7]}`" + (f" ({', '.join(c['tags'])})" if c["tags"] else "")
        for c in s["commits"]
    )
    wall = lim.get("max_wall_seconds")
    return "\n".join(
        [
            "**Setup**",
            "",
            (
                f"- Model: `{', '.join(s['models'])}` (thinking {s['thinking']}, effort "
                f"{s['reasoning_effort']}); summarizer: `{s['summary_model']}` (called only "
                "when a context strategy writes a summary)"
            ),
            (
                f"- Tasks: `{s['subset']}`, {s['tasks']} tasks from SWE-bench Verified across "
                f"{len(s['repos'])} repos ({repos}); seed {s['seed']}, dataset revision "
                f"`{(s['dataset_revision'] or '')[:12]}`. The ablations use `verified_25`, "
                "half of these drawn by repo"
            ),
            (
                f"- Limits per task: {lim.get('max_steps')} steps, "
                f"{_usd(lim.get('max_cost_per_task_usd'))}, "
                f"{'—' if wall is None else f'{wall:.0f} s'}; context budget "
                f"{s['budget_tokens']} tokens"
            ),
            f"- Main run: commits {commits}; config hash {', '.join(s['config_hashes'])}",
            f"- Harness size: {s['harness_lines']} non-blank lines of Python in `src/harness`",
            (
                f"- API cost of the runs in these tables ({', '.join(s['cost_runs'])}): "
                f"{_usd(s['cost_of_tables'])} at standard prices"
            ),
            "",
        ]
    )


def render_failures(f: dict[str, Any]) -> str:
    fl = f["failures"]
    lines = [
        (
            f"How the unresolved tasks of `{f['run_id']}` ended ({fl['n']} of {f['tasks']}; "
            "classes from the run and the official test report, owners from a manual "
            "review):"
        ),
        "",
        "| Ending / tests | Count | Tasks |",
        "|---|---|---|",
    ]
    for cls, ids in f["by_class"].items():
        lines.append(f"| {cls} | {len(ids)} | {', '.join(f'`{i}`' for i in ids)} |")
    owners = ", ".join(f"{k} {v}" for k, v in fl["by_owner"].items())
    lines += [
        "",
        (
            f"Owner after manual review: {owners}. Loop suspects (the same call repeated "
            f"several times in a row): {len(f['loop_suspects'])}."
        ),
        "",
    ]
    return "\n".join(lines)


def render_caveat(t: dict[str, Any]) -> str:
    rr = t.get("rerun")
    if not rr:
        return "> **Caveat:** no rerun of the main configuration is available to size the noise.\n"
    return (
        f"> **Caveat:** the main configuration run twice on the same {rr['tasks']} tasks "
        f"(`{rr['a']}`, `{rr['b']}`) resolved {rr['resolved_a']} and {rr['resolved_b']}; "
        f"{rr['flipped']} tasks flipped. Differences of a few tasks are noise at this size; "
        "resolve rates are compared only with the sign test on the tasks one side resolved, "
        "and each configuration was run once.\n"
    )


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
        "--review", type=Path, default=REVIEW_PATH, help="guard block review"
    )
    ap.add_argument(
        "--ablation-base",
        default=BASE_ABLATION,
        help="run id the ablations are paired with",
    )
    ap.add_argument(
        "--labels", type=Path, default=DEFAULT_LABELS, help="failure owner labels"
    )
    ap.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if stale"
    )
    args = ap.parse_args(argv)

    main_run = load(args.main, args.results_dir)
    runs = {args.main: main_run, args.baseline: load(args.baseline, args.results_dir)}
    wanted = {r for _, r, _ in ABLATIONS} | {args.ablation_base}
    for run_id in sorted(wanted - set(runs)):
        if run := load_if_present(run_id, args.results_dir):
            runs[run_id] = run
    if args.ablation_base not in runs:
        raise SystemExit(f"the ablation baseline {args.ablation_base} is missing")
    tables = {
        "main": summarize(main_run, runs[args.baseline]),
        "ablation": summarize_ablations(runs, runs[args.ablation_base], main_run),
        "cost": summarize_cost(
            runs,
            [args.main, args.baseline, args.ablation_base]
            + [r for _, r, _ in ABLATIONS if r in runs],
            args.main,
            args.baseline,
        ),
        "security": summarize_security(
            main_run, main_run["tasks"], load_reviews(args.review, args.main)
        ),
    }
    tables["main"]["calibration"] = tables["ablation"].get("calibration")
    tables["setup"] = summarize_setup(main_run, tables["cost"])
    tables["failures"] = summarize_failures(main_run, args.labels)
    tables["tldr"] = summarize_tldr(tables)
    tables["caveat"] = tables["tldr"]
    renderers = {
        "main": render,
        "ablation": render_ablation,
        "cost": render_cost,
        "security": render_security,
        "setup": render_setup,
        "failures": render_failures,
        "tldr": render_tldr,
        "caveat": render_caveat,
    }
    readme = args.readme.read_text()
    outputs = {}
    for name, summary in tables.items():
        table = renderers[name](summary)
        outputs[args.summary_dir / f"{name}.json"] = (
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        outputs[args.summary_dir / f"{name}.md"] = table
        readme = replace_block(readme, name.upper(), table)
    outputs[args.readme] = readme
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
    print(outputs[args.summary_dir / "main.md"])
    print("-> " + ", ".join(str(p) for p in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
