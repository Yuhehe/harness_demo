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

How large a prompt was after a compaction: the `compaction` span holds an estimate. The exact
size is the provider's prompt_tokens of the request that followed (the compacted view is sent
as is), so that is used; only when no request followed (the run ended with context_overflow,
or every attempt failed) is the estimate used, and the two are counted apart
(`tokens_after_source`). The size before a compaction is always an estimate: that prompt was
never sent.

Also, from data the runs already have (docs/notes.md section 22.7; no trace field is added):
the scorer's per-test outcome (`score.json` -> `tests`: regressions, partial fixes), the patch
in preds.jsonl (files and lines changed, test files touched, new files), tool calls by type
(B0: counted from mini-swe-agent's own trajectory), where M0's wall time went (model calls,
tool runs, the rest), cost per resolved task, and the same numbers by repo and by difficulty.
`--paired RUN` compares two runs task by task: an exact sign test on the tasks only one of them
resolved, and the median of the per-task ratio of cost, steps and wall time with a bootstrap
95% interval (seeded, so a rerun gives the same interval).

Monitoring (not metrics of the experiment, checks on the harness): how the summaries compare
with the size the plan expected (`summary_size`), attempts that changed nothing
(`compaction_skipped` spans), and compactions made with an unusual estimator ratio.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
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
    skipped: list[dict[str, Any]] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(
        default_factory=list
    )  # all of them, in file order
    patch: str | None = None  # the submitted patch (preds.jsonl)
    external_tools: dict[str, int] | None = (
        None  # B0: tool calls from mini's trajectory
    )

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
        skipped=by_type.get("compaction_skipped", []),
        spans=spans,
    )


def load_run(run_dir: Path) -> dict[str, Task]:
    tasks = {
        p.stem: load_task(p) for p in sorted((run_dir / "trajectories").glob("*.jsonl"))
    }
    preds = run_dir / "preds.jsonl"
    if preds.exists():
        for line in preds.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row["instance_id"] in tasks:
                    tasks[row["instance_id"]].patch = row.get("model_patch") or ""
    for iid, t in tasks.items():
        traj = run_dir / "mini" / iid / f"{iid}.traj.json"
        if traj.exists():  # B0: mini-swe-agent's own trajectory has the tool calls
            t.external_tools = mini_tool_calls(json.loads(traj.read_text()))
    return tasks


def mini_tool_calls(traj: dict[str, Any]) -> dict[str, int]:
    """Tool calls in a mini-swe-agent trajectory, by tool name (it only has bash)."""
    counts: dict[str, int] = {}
    for m in traj.get("messages", []):
        if m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls") or []
        names = [c.get("function", {}).get("name", "bash") for c in calls]
        if not calls:
            names = ["bash"] * len((m.get("extra") or {}).get("actions") or [])
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


# ---- what the runs already have: patch, tools, time, tests --------------------------------------
# test files: under a tests/ directory (django/test/ is Django's test framework, not tests),
# test_*.py, *_test.py, conftest.py
_TEST_PATH = re.compile(
    r"(^|/)tests/|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$"
)


def repo_of(instance_id: str) -> str:
    return instance_id.rsplit("-", 1)[0].replace("__", "/")


def patch_stats(patch: str) -> dict[str, Any]:
    """Files and lines a unified diff changes; which of them are tests and which are new."""
    files: list[str] = []
    new: list[str] = []
    added = removed = 0
    current = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            files.append(current)
        elif line.startswith("new file mode") and current:
            new.append(current)
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {
        "files": len(files),
        "test_files": sorted(f for f in files if _TEST_PATH.search(f)),
        "new_files": sorted(new),
        "lines_added": added,
        "lines_removed": removed,
    }


def tool_counts(t: Task) -> dict[str, int]:
    """Tool calls by tool: the run's tool_call spans, or for B0 mini-swe-agent's trajectory."""
    if t.external_tools is not None:
        return dict(t.external_tools)
    counts: dict[str, int] = {}
    for c in t.tool_calls:
        counts[c["tool"]] = counts.get(c["tool"], 0) + 1
    return counts


def time_split(t: Task) -> dict[str, float] | None:
    """Wall time split into model calls (every attempt, agent and summary), tool runs and the
    rest (context building, retry waits, the loop itself). None without per-step spans (B0).
    Not in the wall at all: starting and removing the sandbox (a trace field for a later
    version, docs/notes.md section 22)."""
    llm = [s for s in t.spans if s["type"] == "llm_call"]
    if not llm or t.run_end is None:
        return None
    model = sum(s.get("latency_s") or 0.0 for s in llm)
    tools = sum(c.get("duration_s") or 0.0 for c in t.tool_calls)
    wall = t.run_end["wall_seconds"]
    return {
        "wall": wall,
        "model": model,
        "tools": tools,
        "other": max(wall - model - tools, 0.0),
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


def sizes_after_compaction(t: Task) -> list[dict[str, Any]]:
    """For each compaction: the prompt size after it, from the provider when a request
    followed (the first successful agent call before any new message), else the estimate."""
    out = []
    for i, s in enumerate(t.spans):
        if s["type"] != "compaction":
            continue
        provider = None
        for nxt in t.spans[i + 1 :]:
            if nxt["type"] in ("message", "compaction"):
                break  # the history moved on: no request carried this view
            if (
                nxt["type"] == "llm_call"
                and nxt.get("purpose", "agent") == "agent"
                and nxt.get("outcome", "ok") == "ok"
            ):
                provider = nxt.get("prompt_tokens")
                break
        est = s.get("tokens_after")
        if not provider and est is None:
            continue
        out.append(
            {
                "step": s["step"],
                "tokens_after_est": est,
                "tokens_after": provider or est,
                "source": "provider" if provider else "estimate",
            }
        )
    return out


def summary_sizes(t: Task) -> list[dict[str, Any]]:
    """Summaries next to the size the plan expected for them (spans that record it)."""
    return [
        {
            "step": c["step"],
            "expected": c["summary_expected_tokens"],
            "actual": c["summary_tokens_out"],
        }
        for c in t.compactions
        if c.get("summary") and c.get("summary_expected_tokens")
    ]


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
        "sizes_after_compaction": sizes_after_compaction(t),
        "summary_sizes": summary_sizes(t),
        "compactions_skipped": [c["reason"] for c in t.skipped],
        "repo": repo_of(t.instance_id),
        "tool_calls": tool_counts(t),
        "time_split": time_split(t),
        "patch": None if t.patch is None else patch_stats(t.patch),
        "ratio_unusual": sum(
            bool(c.get("ratio_unusual")) for c in [*t.compactions, *t.skipped]
        ),
    }


def _sizes_after(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sizes = [x for r in rows for x in r["sizes_after_compaction"]]
    exact = [x for x in sizes if x["source"] == "provider"]
    return {
        "provider_mean": mean([x["tokens_after"] for x in exact]),
        # only the exact sizes go into the mean; the estimated ones are counted, not mixed in
        "tokens_after_source": {
            "provider": len(exact),
            "estimate": len(sizes) - len(exact),
        },
        # how far the estimate in the compaction span was from the provider's count
        "estimate_error_mean": mean(
            [
                x["tokens_after_est"] / x["tokens_after"] - 1
                for x in exact
                if x["tokens_after_est"] is not None
            ]
        ),
    }


def _monitoring(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [x for r in rows for x in r["summary_sizes"]]
    reasons: dict[str, int] = {}
    for r in rows:
        for reason in r["compactions_skipped"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "summary_size": {
            "n": len(summaries),
            "over_expected": sum(x["actual"] > x["expected"] for x in summaries),
            "actual_over_expected_max": max(
                (x["actual"] / x["expected"] for x in summaries), default=None
            ),
        },
        "compactions_skipped": reasons,
        "ratio_unusual": sum(r["ratio_unusual"] for r in rows),
    }


def _quality(
    rows: list[dict[str, Any]],
    resolved: set[str] | None,
    score: dict[str, Any] | None,
    difficulty: dict[str, str] | None,
) -> dict[str, Any]:
    """docs/notes.md section 22.7, from what the runs already have."""
    tests = (score or {}).get("tests") or {}
    judged = [tests[r["instance_id"]] for r in rows if r["instance_id"] in tests]
    patches = [r["patch"] for r in rows if r["patch"] is not None]
    splits = [r["time_split"] for r in rows if r["time_split"]]
    tools: dict[str, int] = {}
    for r in rows:
        for name, k in r["tool_calls"].items():
            tools[name] = tools.get(name, 0) + k
    total_cost = sum(r["cost_usd"] for r in rows)
    k = (
        sum(r["instance_id"] in resolved for r in rows)
        if resolved is not None
        else None
    )

    def strata(key) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            label = key(r)
            if label is not None:
                groups.setdefault(label, []).append(r)
        return {
            label: {
                "n": len(g),
                "resolved": sum(x["instance_id"] in resolved for x in g)
                if resolved is not None
                else None,
                "cost_mean": mean([x["cost_usd"] for x in g]),
                "steps_mean": mean([x["steps"] for x in g]),
            }
            for label, g in sorted(groups.items())
        }

    return {
        "cost_per_resolved": total_cost / k if k else None,
        "tests": {
            "judged": len(judged),
            # a PASS_TO_PASS test that fails now: the patch broke something that worked
            "regressions": sum(t["p2p_fail"] > 0 for t in judged),
            "regression_rate": _share(
                sum(t["p2p_fail"] > 0 for t in judged), len(judged)
            ),
            # some FAIL_TO_PASS tests pass, not all: partly fixed
            "partial_fix": sum(0 < t["f2p_pass"] < t["f2p_total"] for t in judged),
        },
        "patch": {
            "n": len(patches),
            "touch_test_files": sum(bool(x["test_files"]) for x in patches),
            "add_new_files": sum(bool(x["new_files"]) for x in patches),
            "files_mean": mean([x["files"] for x in patches]),
            "lines_changed_median": percentile(
                [x["lines_added"] + x["lines_removed"] for x in patches], 0.5
            ),
        },
        "tool_calls": {
            "mean": mean([sum(r["tool_calls"].values()) for r in rows]),
            "by_tool": dict(sorted(tools.items())),
        },
        "time_split": None
        if not splits
        else {
            part: _share(sum(s[part] for s in splits), sum(s["wall"] for s in splits))
            for part in ("model", "tools", "other")
        },
        "by_repo": strata(lambda r: r["repo"]),
        "by_difficulty": strata(lambda r: (difficulty or {}).get(r["instance_id"]))
        if difficulty
        else None,
    }


# ---- two runs task by task ------------------------------------------------------------------------
def sign_test(a: int, b: int) -> float:
    """Two-sided exact sign test: a and b are the tasks only one or only the other resolved."""
    n = a + b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(a, b) + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired(
    base_rows: list[dict[str, Any]],
    other_rows: list[dict[str, Any]],
    base_resolved: set[str] | None,
    other_resolved: set[str] | None,
    *,
    seed: int = 0,
    rounds: int = 10_000,
) -> dict[str, Any]:
    """`other` against `base` on the tasks both completed (docs/notes.md section 22.1)."""
    base = {r["instance_id"]: r for r in base_rows}
    other = {r["instance_id"]: r for r in other_rows}
    common = sorted(set(base) & set(other))
    out: dict[str, Any] = {"tasks": len(common)}
    if base_resolved is not None and other_resolved is not None:
        only_base = sum(i in base_resolved and i not in other_resolved for i in common)
        only_other = sum(i in other_resolved and i not in base_resolved for i in common)
        out["resolved"] = {
            "both": sum(i in base_resolved and i in other_resolved for i in common),
            "only_base": only_base,
            "only_other": only_other,
            "sign_test_p": sign_test(only_base, only_other),
        }
    rng = random.Random(seed)
    for key in ("cost_usd", "steps", "wall_seconds"):
        ratios = [
            other[i][key] / base[i][key]
            for i in common
            if base[i][key] and other[i][key]
        ]
        if not ratios:
            continue
        medians = sorted(
            statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(rounds)
        )
        out[key] = {
            "median_ratio": statistics.median(ratios),
            "ci95": [medians[int(0.025 * rounds)], medians[int(0.975 * rounds) - 1]],
            "other_higher": sum(r > 1 for r in ratios),
            "n": len(ratios),
        }
    return out


def aggregate(
    tasks: dict[str, Task],
    resolved: set[str] | None = None,
    score: dict[str, Any] | None = None,
    difficulty: dict[str, str] | None = None,
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
        "fail_taxonomy": _taxonomy(rows, resolved, score),
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
            "tokens_after": _sizes_after(rows),
        },
        "monitoring": _monitoring(rows),
        "quality": _quality(rows, resolved, score, difficulty),
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


def end_category(
    instance_id: str,
    fail_reason: str | None,
    resolved: set[str] | None,
    score: dict[str, Any] | None = None,
) -> str:
    """How one run ended: its FailReason, `submitted`, or for a submitted run the scorer did not
    resolve `submitted_but_failed`. With the scorer's verdicts (`score`), a submitted run the
    scorer could not judge is `scoring_error` or `submitted_empty_patch`, not
    `submitted_but_failed`: a broken scoring environment is not the model's failure."""
    key = fail_reason or "submitted"
    if key != "submitted" or resolved is None or instance_id in resolved:
        return key
    score = score or {}
    if instance_id in set(score.get("empty_patch", [])):
        return "submitted_empty_patch"
    for name in ("error", "infra_failure", "ambiguous_failure", "incomplete"):
        if instance_id in set(score.get(name, [])):
            return "scoring_error"
    return "submitted_but_failed"


def _taxonomy(
    rows: list[dict[str, Any]],
    resolved: set[str] | None,
    score: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Count the runs by how they ended (`end_category`)."""
    counts: dict[str, int] = {}
    for r in rows:
        key = end_category(r["instance_id"], r["fail_reason"], resolved, score)
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


def load_difficulty(ids: list[str]) -> dict[str, str]:
    """The dataset's difficulty label for each id, at the revision the subsets were drawn from
    (configs/subsets/subsets_meta.json; read from the local Hugging Face cache)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "swebench"))
    from select_subset import META_NAME, SUBSET_DIR, load_rows

    meta = json.loads((SUBSET_DIR / META_NAME).read_text())
    rows, _ = load_rows(meta["dataset_id"], meta["split"], meta["revision"])
    wanted = set(ids)
    return {
        r["instance_id"]: r["difficulty"] for r in rows if r["instance_id"] in wanted
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--baseline", help="a second run id to compare with (paired, compacted tasks)"
    )
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--resolved", type=Path, help="JSON list of resolved instance ids")
    ap.add_argument(
        "--paired", help="a second run id to compare with task by task (all tasks)"
    )
    ap.add_argument(
        "--difficulty",
        action="store_true",
        help="also group by the dataset's difficulty label (dataset at the frozen revision)",
    )
    args = ap.parse_args(argv)

    run_dir = args.results_dir / args.run_id
    tasks = load_run(run_dir)
    if not tasks:
        raise SystemExit(f"no trajectories in {run_dir / 'trajectories'}")
    resolved = _resolved(run_dir, args.resolved)
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text()) if score_path.exists() else None
    difficulty = load_difficulty(list(tasks)) if args.difficulty else None
    result = aggregate(tasks, resolved, score, difficulty)
    if args.paired:
        base_dir = args.results_dir / args.paired
        base_tasks = load_run(base_dir)
        base_rows = [m for t in base_tasks.values() if (m := task_metrics(t))]
        rows = [m for t in tasks.values() if (m := task_metrics(t))]
        result["paired"] = {
            "base": args.paired,
            **paired(base_rows, rows, _resolved(base_dir, None), resolved),
        }
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
