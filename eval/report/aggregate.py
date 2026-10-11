"""Aggregate the trajectories of a run into metrics (CLAUDE.md section 6).

    uv run python eval/report/aggregate.py --run-id M0 [--baseline A1]

Reads results/runs/<run_id>/trajectories/*.jsonl (and patches/ when present) and writes
results/runs/<run_id>/metrics.json. Nothing here calls an LLM: every number is computed from
what the run recorded, so a metric can be re-derived by anyone with the trajectories.

Resolved or not comes from the official scorer, not from the trajectories. Until score.py
exists, pass the instance ids that passed in `<run_dir>/resolved.json` (a JSON list) or with
--resolved; without it the resolve fields are null.

The compaction metrics answer "what happens after a summary":

    steps_after_compaction     steps left after the first compaction (the Complexity Trap
                               paper found summaries make runs longer)
    post_compaction_repeat_rate  share of tool calls after the first compaction that repeat a
                               call made before it. A SIGNAL, not a verdict: reading a file
                               again after editing it is fine. Compare with the same measure on
                               a run without compaction, cut at the same step (`compare`)
    summary_file_recall        of the files the agent had edited before a compaction, the share
                               the summary text mentions. Uses the summarizer's own text, not
                               the generated state block (which lists the patch by design)
    compaction_cost_share      compaction cost / task cost
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---- statistics ------------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - half) / denom, (centre + half) / denom


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks; None for no values."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


# ---- one trajectory --------------------------------------------------------------------------
@dataclass
class Task:
    instance_id: str
    run_end: dict[str, Any] | None
    compactions: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.run_end is not None


def load_task(path: Path) -> Task:
    spans = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        by_type.setdefault(s["type"], []).append(s)
    end = by_type.get("run_end", [None])[-1]
    return Task(
        instance_id=path.stem,
        run_end=end,
        compactions=by_type.get("compaction", []),
        tool_calls=by_type.get("tool_call", []),
        messages=by_type.get("message", []),
    )


def load_run(run_dir: Path) -> dict[str, Task]:
    return {
        p.stem: load_task(p) for p in sorted((run_dir / "trajectories").glob("*.jsonl"))
    }


# ---- compaction metrics ------------------------------------------------------------------------
def first_compaction_step(t: Task) -> int | None:
    return min((c["step"] for c in t.compactions), default=None)


def steps_after_compaction(t: Task) -> int | None:
    s = first_compaction_step(t)
    return None if s is None or t.run_end is None else max(t.run_end["steps"] - s, 0)


def repeat_rate(t: Task, boundary_step: int) -> float | None:
    """Share of the tool calls after `boundary_step` that repeat a (tool, arguments) pair seen
    at or before it. None when nothing was called after the boundary."""
    before = {
        (c["tool"], c["args_hash"]) for c in t.tool_calls if c["step"] <= boundary_step
    }
    after = [
        (c["tool"], c["args_hash"]) for c in t.tool_calls if c["step"] > boundary_step
    ]
    if not after:
        return None
    return sum(1 for c in after if c in before) / len(after)


def mentions(text: str, path: str) -> bool:
    """Whether `text` names the file: its path or, failing that, its file name."""
    low = text.lower()
    return path.lower() in low or Path(path).name.lower() in low


def edited_before(t: Task, step: int) -> list[str]:
    """Files the agent changed with str_replace up to `step` (edits made through bash are not
    visible here, a known blind spot)."""
    files: dict[str, None] = {}
    for m in t.messages:
        if m.get("role") != "assistant" or m.get("step", 0) > step:
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "str_replace":
                continue
            try:
                path = json.loads(fn.get("arguments", "")).get("path")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(path, str):
                files[path] = None
    return list(files)


def summary_file_recall(t: Task) -> float | None:
    """Mean over the task's summaries of: files edited before it that it mentions."""
    recalls = []
    for c in t.compactions:
        text = c.get("summary")
        if not text:  # truncate-only compaction, or a fallback
            continue
        files = edited_before(t, c["step"])
        if files:
            recalls.append(sum(mentions(text, f) for f in files) / len(files))
    return mean(recalls)


# ---- per task, per run --------------------------------------------------------------------------
def task_metrics(t: Task) -> dict[str, Any] | None:
    e = t.run_end
    if e is None:
        return None
    boundary = first_compaction_step(t)
    cost = e["cost_usd"]
    return {
        "instance_id": t.instance_id,
        "steps": e["steps"],
        "cost_usd": cost,
        "tokens_in": e["tokens_in"],
        "tokens_cached": e["tokens_cached"],
        "tokens_out": e["tokens_out"],
        "wall_seconds": e["wall_seconds"],
        "fail_reason": e["fail_reason"],
        "submitted": e["submitted"],
        "compactions": len(t.compactions),
        "compaction_cost_usd": e.get("compaction_cost_usd", 0.0),
        "compaction_cost_share": (e.get("compaction_cost_usd", 0.0) / cost)
        if cost and t.compactions
        else None,
        "steps_after_compaction": steps_after_compaction(t),
        "post_compaction_repeat_rate": None
        if boundary is None
        else repeat_rate(t, boundary),
        "summary_file_recall": summary_file_recall(t),
    }


def aggregate(
    tasks: dict[str, Task], resolved: set[str] | None = None
) -> dict[str, Any]:
    rows = [m for t in tasks.values() if (m := task_metrics(t))]
    n = len(rows)
    out: dict[str, Any] = {"tasks": n, "incomplete": len(tasks) - n}
    if not n:
        return out
    costs = [r["cost_usd"] for r in rows]
    tin = sum(r["tokens_in"] for r in rows)
    compacted = [r for r in rows if r["compactions"]]

    out |= {
        "cost_per_task": {"mean": mean(costs), "p50": percentile(costs, 0.5)},
        "tokens_in_mean": mean([r["tokens_in"] for r in rows]),
        "tokens_out_mean": mean([r["tokens_out"] for r in rows]),
        "tokens_cached_mean": mean([r["tokens_cached"] for r in rows]),
        "cache_hit_rate": (sum(r["tokens_cached"] for r in rows) / tin)
        if tin
        else None,
        "latency": {
            "p50": percentile([r["wall_seconds"] for r in rows], 0.5),
            "p95": percentile([r["wall_seconds"] for r in rows], 0.95),
        },
        "steps_mean": mean([r["steps"] for r in rows]),
        "overflow_rate": sum(r["fail_reason"] == "context_overflow" for r in rows) / n,
        "fail_taxonomy": _taxonomy(rows, resolved),
        "compaction": {
            "tasks_with_compaction": len(compacted),
            "compactions_total": sum(r["compactions"] for r in rows),
            "steps_after_compaction_mean": mean(
                [
                    r["steps_after_compaction"]
                    for r in compacted
                    if r["steps_after_compaction"] is not None
                ]
            ),
            "post_compaction_repeat_rate_mean": mean(
                [
                    r["post_compaction_repeat_rate"]
                    for r in compacted
                    if r["post_compaction_repeat_rate"] is not None
                ]
            ),
            "summary_file_recall_mean": mean(
                [
                    r["summary_file_recall"]
                    for r in compacted
                    if r["summary_file_recall"] is not None
                ]
            ),
            "compaction_cost_share": _share(
                sum(r["compaction_cost_usd"] for r in compacted),
                sum(r["cost_usd"] for r in compacted),
            ),
        },
    }
    if resolved is not None:
        k = sum(r["instance_id"] in resolved for r in rows)
        out["resolve_rate"] = {
            "resolved": k,
            "n": n,
            "rate": k / n,
            "wilson95": wilson(k, n),
        }
    else:
        out["resolve_rate"] = None
    return out


def _share(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def _taxonomy(rows: list[dict[str, Any]], resolved: set[str] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        key = r["fail_reason"] or "submitted"
        if (
            key == "submitted"
            and resolved is not None
            and r["instance_id"] not in resolved
        ):
            key = "submitted_but_failed"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---- two runs on the same tasks -------------------------------------------------------------------
def compare(
    base: dict[str, Task],
    other: dict[str, Task],
    base_resolved: set[str] | None = None,
    other_resolved: set[str] | None = None,
) -> dict[str, Any]:
    """`other` (e.g. M0) against `base` (e.g. A1, no compaction) on the tasks of `other` that
    had a compaction. Pairing by instance keeps the comparison from being a comparison of
    easy tasks with hard ones, and restricting it to compacted tasks keeps tasks that never
    reached the budget (identical in both runs) from diluting it."""
    common = [
        i
        for i, t in other.items()
        if t.complete and t.compactions and i in base and base[i].complete
    ]
    out: dict[str, Any] = {"paired_tasks": len(common)}
    if not common:
        return out
    deltas = [other[i].run_end["steps"] - base[i].run_end["steps"] for i in common]
    other_rates, base_rates = [], []
    for i in common:
        boundary = first_compaction_step(other[i])
        r_other, r_base = (
            repeat_rate(other[i], boundary),
            repeat_rate(base[i], boundary),
        )
        if r_other is not None and r_base is not None:
            other_rates.append(r_other)
            base_rates.append(r_base)
    out |= {
        "steps_delta_mean": mean(deltas),
        "repeat_rate_other": mean(other_rates),
        "repeat_rate_base_at_same_step": mean(base_rates),
    }
    if base_resolved is not None and other_resolved is not None:
        a = {i: i in base_resolved for i in common}
        b = {i: i in other_resolved for i in common}
        out["resolved_pairs"] = {
            "both": sum(a[i] and b[i] for i in common),
            "only_base": sum(a[i] and not b[i] for i in common),
            "only_other": sum(b[i] and not a[i] for i in common),
            "neither": sum(not a[i] and not b[i] for i in common),
        }
    return out


# ---- command line -----------------------------------------------------------------------------------
def _resolved(run_dir: Path, explicit: Path | None) -> set[str] | None:
    path = explicit or run_dir / "resolved.json"
    return set(json.loads(path.read_text())) if path.exists() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--baseline", help="a second run id to compare with (paired, compacted tasks)"
    )
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--resolved", type=Path, help="JSON list of resolved instance ids")
    args = ap.parse_args(argv)

    run_dir = args.results_dir / args.run_id
    tasks = load_run(run_dir)
    if not tasks:
        raise SystemExit(f"no trajectories in {run_dir / 'trajectories'}")
    resolved = _resolved(run_dir, args.resolved)
    result = aggregate(tasks, resolved)
    if args.baseline:
        base_dir = args.results_dir / args.baseline
        result["vs_baseline"] = {
            "baseline": args.baseline,
            **compare(load_run(base_dir), tasks, _resolved(base_dir, None), resolved),
        }
    out = run_dir / "metrics.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
