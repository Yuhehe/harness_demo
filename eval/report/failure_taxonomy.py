"""Classify the unresolved tasks of a run (CLAUDE.md section 6, `fail_taxonomy`).

    uv run python eval/report/failure_taxonomy.py --run-id M0

Reads results/runs/<run_id>/ (trajectories, resolved.json and score.json from score.py) and
writes results/runs/<run_id>/failure_taxonomy.json. Nothing here calls an LLM. Every task gets
three labels, from three sources that are kept apart:

    end      how the run ended: a FailReason, `submitted`, or for a submitted run the scorer did
             not resolve `submitted_but_failed` / `scoring_error` / `submitted_empty_patch`
             (aggregate.end_category, the same function the `fail_taxonomy` metric uses)
    tests    for an unresolved task the scorer judged, what its tests say (score.json `tests`):
             `regression` (a PASS_TO_PASS test fails now; wins over the others), `partial_fix`
             (some FAIL_TO_PASS tests pass), `target_fail` (none does), `unknown` (not judged)
    owner    whose problem the failure is, from a person reading the trajectory: `harness`,
             `model` or `task` (the table in docs/failure_analysis.md, section TABLE_HEADING);
             `unlabelled` until someone has looked

Loop signal (docs/notes.md section 23.2, item 4): a run that issues the same tool call with the
same arguments (`tool`, `args_hash`) LOOP_RUN times in a row, with nothing else in between, is
flagged `loop_suspect`. OpenHands stops a conversation on 4 identical action-observation pairs;
here the observation is not compared, so the flag is a SIGNAL for a person to look at, not a
verdict. The largest count of one call inside any LOOP_WINDOW consecutive calls is reported
next to it, unthresholded. B0 has no per-call spans (mini-swe-agent's trajectory is converted
to run_start / run_end only), so its loop fields are null.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any

from aggregate import Task, end_category, load_run

LOOP_RUN = 4
LOOP_WINDOW = 20
OWNERS = ("harness", "model", "task")
TABLE_HEADING = "## 归属标注"
DEFAULT_LABELS = Path("docs/failure_analysis.md")


def outcome_class(t: dict[str, int] | None) -> str:
    """What the scorer's per-test outcome says about an unresolved task."""
    if not t:
        return "unknown"
    if t["p2p_fail"] > 0:
        return "regression"
    if 0 < t["f2p_pass"] < t["f2p_total"]:
        return "partial_fix"
    if t["f2p_pass"] == 0:
        return "target_fail"
    return "unknown"  # every target test passes and nothing regressed, yet not resolved


def loop_signal(t: Task) -> dict[str, Any] | None:
    """Longest run of identical consecutive calls; most repeats of one call in a window."""
    calls = [(c["tool"], c.get("args_hash")) for c in t.tool_calls]
    if not calls:
        return None
    longest = run = 1
    for prev, cur in itertools.pairwise(calls):
        run = run + 1 if cur == prev else 1
        longest = max(longest, run)
    in_window = 0
    for i in range(max(len(calls) - LOOP_WINDOW, 0) + 1):
        window = calls[i : i + LOOP_WINDOW]
        in_window = max(in_window, max(window.count(c) for c in set(window)))
    return {
        "max_consecutive_identical": longest,
        "max_same_in_window": in_window,
        "loop_suspect": longest >= LOOP_RUN,
    }


def guard_denials(t: Task) -> int:
    return sum(
        1
        for s in t.spans
        if s["type"] == "guard_decision" and s.get("action") == "deny"
    )


def load_labels(path: Path, run_id: str) -> dict[str, dict[str, str]]:
    """Rows `| run_id | instance_id | owner | basis |` of the table under TABLE_HEADING."""
    if not path.exists():
        return {}
    labels: dict[str, dict[str, str]] = {}
    inside = False
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            inside = line.startswith(TABLE_HEADING)
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] != run_id:
            continue
        owner = cells[2]
        if owner not in OWNERS:
            raise ValueError(
                f"{path}: owner {owner!r} for {cells[1]} is not one of {OWNERS}"
            )
        labels[cells[1]] = {"owner": owner, "basis": cells[3] if len(cells) > 3 else ""}
    return labels


def classify(
    tasks: dict[str, Task],
    resolved: set[str],
    score: dict[str, Any] | None,
    labels: dict[str, dict[str, str]],
) -> dict[str, Any]:
    tests = (score or {}).get("tests") or {}
    per_task = []
    for iid, t in sorted(tasks.items()):
        if t.run_end is None:
            continue
        ok = iid in resolved
        row: dict[str, Any] = {
            "instance_id": iid,
            "resolved": ok,
            "end": end_category(iid, t.run_end["fail_reason"], resolved, score),
            "fail_detail": t.run_end.get("fail_detail"),
            "steps": t.run_end["steps"],
            "guard_denials": guard_denials(t),
            "loop": loop_signal(t),
        }
        if not ok:
            row["tests"] = outcome_class(tests.get(iid))
            label = labels.get(iid)
            row["owner"] = label["owner"] if label else "unlabelled"
            row["owner_basis"] = label["basis"] if label else None
        per_task.append(row)

    failures = [r for r in per_task if not r["resolved"]]

    def count(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in failures:
            out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items()))

    with_loop = [r for r in per_task if r["loop"]]
    return {
        "tasks": len(per_task),
        "incomplete": len(tasks) - len(per_task),
        "resolved": len(per_task) - len(failures),
        "failures": {
            "n": len(failures),
            "by_end": count("end"),
            "by_tests": count("tests"),
            "by_owner": count("owner"),
        },
        "loop_signal": {
            "threshold_consecutive": LOOP_RUN,
            "window": LOOP_WINDOW,
            "tasks_with_calls": len(with_loop),
            "suspect": [
                r["instance_id"] for r in with_loop if r["loop"]["loop_suspect"]
            ],
            "suspect_unresolved": [
                r["instance_id"]
                for r in with_loop
                if r["loop"]["loop_suspect"] and not r["resolved"]
            ],
        },
        "unknown_labels": sorted(set(labels) - {r["instance_id"] for r in failures}),
        "per_task": per_task,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    args = ap.parse_args(argv)

    run_dir = args.results_dir / args.run_id
    tasks = load_run(run_dir)
    if not tasks:
        raise SystemExit(f"no trajectories in {run_dir / 'trajectories'}")
    resolved_path = run_dir / "resolved.json"
    if not resolved_path.exists():
        raise SystemExit(f"{resolved_path} is missing: score the run first (score.py)")
    resolved = set(json.loads(resolved_path.read_text()))
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text()) if score_path.exists() else None
    result = {
        "run_id": args.run_id,
        **classify(tasks, resolved, score, load_labels(args.labels, args.run_id)),
    }
    out = run_dir / "failure_taxonomy.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True))
    f = result["failures"]
    print(f"{args.run_id}: {result['tasks']} tasks, {f['n']} unresolved")
    for key in ("by_end", "by_tests", "by_owner"):
        print(f"  {key}: {f[key]}")
    loop = result["loop_signal"]
    print(
        f"  loop suspects: {loop['suspect'] or 'none'} (of {loop['tasks_with_calls']})"
    )
    if result["unknown_labels"]:
        print(
            f"  labels for tasks that are not failures here: {result['unknown_labels']}"
        )
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
