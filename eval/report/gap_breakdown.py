"""Where the M0 vs B0 step gap comes from, from the runs already made (no LLM call, no container).

    uv run python eval/report/gap_breakdown.py [--subset configs/subsets/verified_25.txt]

The analysis and its decision rules were written down before any pooled number was computed
(plan.md, "补充：零花费的合并分析"). Two parts:

1. Pooled paired ratios. Several configurations ran more than once on the same tasks (POOLS).
   Per task, the runs of one configuration are averaged in log space (geometric mean of steps
   or cost); the statistic is the median over tasks of the per-task ratio, as in rule B
   (CLAUDE.md §6). The interval is a two-level bootstrap: tasks are resampled, then within
   each drawn task the runs of each side are resampled, so run-to-run noise is in the
   interval. Same-configuration pairs (CALIBRATION) must come out with intervals that contain
   1. The single-run ratios of runs made side by side are listed too (SINGLES).
2. Where the extra steps go. Each model response is one step; its calls are sorted by
   `aggregate.classify_call` and a response with n calls gives 1/n of a step to each class,
   so the classes add up to the response count. A response with no call is `text`. Phases:
   `locate` (before the first source edit), `fix` (first to last edit), `verify` (after the
   last edit); a task with no edit is all `no_edit`. Per task the runs are averaged, then the
   per-task difference to B0 is taken. Means add up across classes, so a block's share of the
   gap is computed from means; the median difference and both intervals are reported as well.

Steps for the ratios are `run_end.steps` (every LLM response). The step profile counts the
responses in the history; a response cut by `finish_reason=length` never enters it, so the
two can differ by the (rare) truncated outputs.

Writes results/runs/_analysis/gap_breakdown.json (gitignored, like the runs) and, with
--samples, the commands for the hand check of the edit classifier.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
from pathlib import Path
from typing import Any

from aggregate import _SCRATCH, _TARGET, Task, classify_call, load_run, responses

ROOT = Path(__file__).resolve().parents[2]
POOLS: dict[str, tuple[str, ...]] = {
    "B0": ("B0", "B0-v021", "B0-v03"),
    "M0": ("M0", "M0-r2"),
    "A2": ("A2", "A2-v021"),
    "D": ("D-v021", "D-v03"),
    "Dstar": ("Dstar-v03",),
    # sensitivity only: assumes the mechanisms of D* change nothing
    "D+Dstar": ("D-v021", "D-v03", "Dstar-v03"),
    # calibration halves
    "B0[1,2]": ("B0", "B0-v021"),
    "B0[3]": ("B0-v03",),
    "M0[1]": ("M0",),
    "M0[2]": ("M0-r2",),
}
COMPARISONS = (
    ("A2", "B0"),  # the gap to explain (bash only, both)
    ("D", "A2"),  # the prompt
    ("D", "B0"),  # what is left after the prompt
    ("Dstar", "B0"),
    ("Dstar", "D"),
    ("M0", "B0"),  # the main comparison
    ("A2", "M0"),  # the tools
    ("D+Dstar", "B0"),
)
CALIBRATION = (("B0[3]", "B0[1,2]"), ("M0[2]", "M0[1]"))
SINGLES = (  # runs made side by side
    ("A2-v021", "B0-v021"),
    ("D-v021", "A2-v021"),
    ("D-v021", "B0-v021"),
    ("D-v03", "B0-v03"),
    ("Dstar-v03", "B0-v03"),
    ("Dstar-v03", "D-v03"),
    ("M0", "B0"),
    ("A2", "M0-r2"),
)
BREAKDOWN = (("A2", "B0"), ("D", "B0"), ("D", "A2"), ("M0", "B0"))
CLASSES = (
    "explore", "edit", "scratch", "test", "script", "todo", "submit", "reread_saved",
    "cleanup", "other", "text",
)  # fmt: skip
PHASES = ("locate", "fix", "verify", "no_edit")
EXTRAS = ("repeated_calls", "tests_after_last_edit", "steps_before_first_edit")
_PY_PATH = re.compile(
    r"""(?:Path|open)\(\s*['"]([^'"]+\.(?:py|rst|txt|cfg|toml))['"]"""
)
# write-like text in a command that is not sorted as an edit: candidates for the hand check
_WRITE_LIKE = re.compile(
    r">>|\bmv\s|\bcp\s|\.write\(|\.replace\(|git\s+(checkout|restore|stash|apply)|"
    r"\bed\s|\btruncate\b|\bpatch\b|open\([^)]*['\"][wa]"
)


# ---- one task --------------------------------------------------------------------------------------
def _norm(path: str) -> str:
    path = path.strip("'\"")
    path = re.sub(r"^/testbed/", "", path)
    return re.sub(r"^\./", "", path)


def edit_targets(tool: str, args: dict[str, Any]) -> list[str] | None:
    """Files an `edit` call writes, or None when the command does not name them (e.g. a
    Python heredoc that builds the path in a variable)."""
    if tool == "str_replace":
        return [_norm(str(args.get("path", "")))]
    cmd = str(args.get("command", ""))
    # shell writes (`sed -i`, `cat >`, ...) and the Python heredocs both sides use most:
    # `Path('x.py')` / `open('x.py', ...)` (a file only read there makes the set larger, which
    # only weakens the coverage check)
    found = [_norm(t) for t in _TARGET.findall(cmd) + _PY_PATH.findall(cmd)]
    found = [t for t in found if not _SCRATCH.search(t)]
    return found or None


def patch_files(patch: str | None) -> set[str]:
    return set(re.findall(r"^diff --git a/(\S+) b/", patch or "", flags=re.MULTILINE))


def profile(rs: list[list[tuple[str, dict[str, Any]]]]) -> dict[str, Any]:
    """Step classes, phases and the extra signals of one run of one task."""
    classes = dict.fromkeys(CLASSES, 0.0)
    kinds: list[list[str]] = []
    for calls in rs:
        ks = [classify_call(tool, args) for tool, args in calls]
        kinds.append(ks)
        if not ks:
            classes["text"] += 1
        for k in ks:
            classes[k] += 1 / len(ks)
    edits = [i for i, ks in enumerate(kinds) if "edit" in ks]
    phases = dict.fromkeys(PHASES, 0.0)
    if edits:
        first, last = edits[0], edits[-1]
        phases["locate"] = first
        phases["fix"] = last - first + 1
        phases["verify"] = len(rs) - last - 1
    else:
        phases["no_edit"] = len(rs)
    seen: set[str] = set()
    repeated = 0
    for calls in rs:
        for tool, args in calls:
            key = tool + json.dumps(args, sort_keys=True)
            repeated += key in seen
            seen.add(key)
    after = kinds[edits[-1] + 1 :] if edits else []
    return {
        "responses": len(rs),
        "classes": classes,
        "phases": phases,
        "repeated_calls": repeated,
        "tests_after_last_edit": sum(ks.count("test") for ks in after),
        "steps_before_first_edit": edits[0] if edits else len(rs),
    }


def record(t: Task) -> dict[str, Any]:
    rs = responses(t)
    rec = profile(rs)
    targets: set[str] = set()
    unnamed = 0
    for calls in rs:
        for tool, args in calls:
            if classify_call(tool, args) == "edit":
                got = edit_targets(tool, args)
                if got is None:
                    unnamed += 1
                else:
                    targets.update(got)
    source = {f for f in patch_files(t.patch) if not _SCRATCH.search(f)}
    rec.update(
        steps=t.run_end["steps"],
        cost=t.run_end["cost_usd"],
        compactions=len(t.compactions),
        patch_source_files=sorted(source),
        edit_targets=sorted(targets),
        unnamed_edits=unnamed,
        # every source file in the patch was written by a call sorted as `edit`
        edits_cover_patch=None
        if unnamed and not source <= targets
        else source <= targets,
    )
    return rec


# ---- pooling and the two-level bootstrap -----------------------------------------------------------
Pool = dict[str, list[dict[str, Any]]]  # instance id -> one record per run


def pool(runs: tuple[str, ...], results_dir: Path, ids: set[str]) -> Pool:
    out: Pool = {i: [] for i in ids}
    for r in runs:
        tasks = load_run(results_dir / r)
        missing = ids - {i for i, t in tasks.items() if t.run_end is not None}
        if missing:
            raise SystemExit(f"{r}: no finished run for {sorted(missing)[:5]}")
        for i in ids:
            out[i].append(record(tasks[i]))
    return out


def _ci(values: list[float], rounds: int) -> list[float]:
    values.sort()
    return [values[int(0.025 * rounds)], values[int(0.975 * rounds) - 1]]


def _log_ratio(a: list[float], b: list[float]) -> float:
    return math.exp(
        statistics.fmean(math.log(x) for x in a)
        - statistics.fmean(math.log(x) for x in b)
    )


def ratio(
    a: dict[str, list[float]],
    b: dict[str, list[float]],
    *,
    rounds: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    """Median over tasks of the per-task ratio of geometric means, with a two-level bootstrap."""
    tasks = sorted(i for i in a if i in b and all(a[i]) and all(b[i]))
    point = [_log_ratio(a[i], b[i]) for i in tasks]
    rng = random.Random(seed)
    meds = []
    for _ in range(rounds):
        draw = rng.choices(tasks, k=len(tasks))
        meds.append(
            statistics.median(
                _log_ratio(
                    rng.choices(a[i], k=len(a[i])), rng.choices(b[i], k=len(b[i]))
                )
                for i in draw
            )
        )
    return {
        "median_ratio": statistics.median(point),
        "ci95": _ci(meds, rounds),
        "other_higher": sum(r > 1 for r in point),
        "n": len(tasks),
        "runs": [len(a[tasks[0]]), len(b[tasks[0]])] if tasks else [0, 0],
    }


def difference(
    a: dict[str, list[float]],
    b: dict[str, list[float]],
    *,
    rounds: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    """Per-task difference of the run means: mean and median over tasks, two-level bootstrap."""
    tasks = sorted(i for i in a if i in b)
    point = [statistics.fmean(a[i]) - statistics.fmean(b[i]) for i in tasks]
    rng = random.Random(seed)
    means, meds = [], []
    for _ in range(rounds):
        draw = rng.choices(tasks, k=len(tasks))
        d = [
            statistics.fmean(rng.choices(a[i], k=len(a[i])))
            - statistics.fmean(rng.choices(b[i], k=len(b[i])))
            for i in draw
        ]
        means.append(statistics.fmean(d))
        meds.append(statistics.median(d))
    return {
        "mean": statistics.fmean(point),
        "mean_ci95": _ci(means, rounds),
        "median": statistics.median(point),
        "median_ci95": _ci(meds, rounds),
        "n": len(tasks),
    }


def values(p: Pool, get: Any) -> dict[str, list[float]]:
    return {i: [get(r) for r in recs] for i, recs in p.items()}


def breakdown(a: Pool, b: Pool, *, rounds: int, seed: int) -> dict[str, Any]:
    """Extra steps of `a` over `b` by class and by phase, with each block's share of the gap."""
    total = difference(values(a, lambda r: r["responses"]), values(b, lambda r: r["responses"]),
                       rounds=rounds, seed=seed)  # fmt: skip
    out: dict[str, Any] = {"total": total, "classes": {}, "phases": {}, "extras": {}}
    for group, keys in (("classes", CLASSES), ("phases", PHASES)):
        for k in keys:
            d = difference(values(a, lambda r, k=k, g=group: r[g][k]),
                           values(b, lambda r, k=k, g=group: r[g][k]),
                           rounds=rounds, seed=seed)  # fmt: skip
            d["share"] = d["mean"] / total["mean"] if total["mean"] else None
            d["a_mean"] = statistics.fmean(r[group][k] for rs in a.values() for r in rs)
            d["b_mean"] = statistics.fmean(r[group][k] for rs in b.values() for r in rs)
            # plan.md: a block counts only with more than half of the gap and both intervals
            # clear of 0
            d["qualifies"] = bool(
                d["share"] is not None and d["share"] > 0.5
                and (d["mean_ci95"][0] > 0 or d["mean_ci95"][1] < 0)
                and (d["median_ci95"][0] > 0 or d["median_ci95"][1] < 0)
            )  # fmt: skip
            out[group][k] = d
    for k in EXTRAS:
        out["extras"][k] = difference(values(a, lambda r, k=k: r[k]),
                                      values(b, lambda r, k=k: r[k]), rounds=rounds, seed=seed)  # fmt: skip
    return out


# ---- the hand check of the edit classifier ---------------------------------------------------------
def samples(tasks: dict[str, Task], k: int, seed: int) -> dict[str, list[str]]:
    edits, write_like = [], []
    for i in sorted(tasks):
        for calls in responses(tasks[i]):
            for tool, args in calls:
                text = (
                    str(args.get("command", "")) if tool == "bash" else f"{tool} {args}"
                )
                kind = classify_call(tool, args)
                if kind == "edit":
                    edits.append(f"{i}: {text}")
                elif (
                    tool == "bash"
                    and kind not in ("scratch", "submit")
                    and _WRITE_LIKE.search(text)
                ):
                    write_like.append(f"[{kind}] {i}: {text}")
    rng = random.Random(seed)
    pick = lambda xs: rng.sample(xs, min(k, len(xs)))
    return {"edit": pick(edits), "write_like_not_edit": pick(write_like),
            "counts": [len(edits), len(write_like)]}  # fmt: skip


# ---- main --------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--subset", type=Path, default=ROOT / "configs/subsets/verified_25.txt"
    )
    ap.add_argument("--results-dir", type=Path, default=ROOT / "results/runs")
    ap.add_argument(
        "--out", type=Path, default=ROOT / "results/runs/_analysis/gap_breakdown.json"
    )
    ap.add_argument(
        "--samples", type=Path, help="write commands for the hand check here"
    )
    ap.add_argument("--rounds", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    ids = {x.strip() for x in args.subset.read_text().splitlines() if x.strip()}
    runs = sorted(
        {r for rs in POOLS.values() for r in rs} | {r for p in SINGLES for r in p}
    )
    by_run = {r: pool((r,), args.results_dir, ids) for r in runs}
    pools: dict[str, Pool] = {
        name: {i: [rec for r in rs for rec in by_run[r][i]] for i in ids}
        for name, rs in POOLS.items()
    }
    kw = {"rounds": args.rounds, "seed": args.seed}

    def both(a: Pool, b: Pool, drop: set[str] = frozenset()) -> dict[str, Any]:
        a = {i: v for i, v in a.items() if i not in drop}
        b = {i: v for i, v in b.items() if i not in drop}
        return {key: ratio(values(a, lambda r, key=key: r[key]),
                           values(b, lambda r, key=key: r[key]), **kw)
                for key in ("steps", "cost")}  # fmt: skip

    compacted = {i for p in ("A2", "D", "Dstar", "M0") for i, recs in pools[p].items()
                 if any(r["compactions"] for r in recs)}  # fmt: skip
    out: dict[str, Any] = {
        "subset": args.subset.name,
        "pools": {k: list(v) for k, v in POOLS.items()},
        "rounds": args.rounds,
        "seed": args.seed,
        "pooled": {f"{a}/{b}": both(pools[a], pools[b]) for a, b in COMPARISONS},
        "calibration": {f"{a}/{b}": both(pools[a], pools[b]) for a, b in CALIBRATION},
        "single": {f"{a}/{b}": both(by_run[a], by_run[b]) for a, b in SINGLES},
        "without_compacted": {
            "dropped": sorted(compacted),
            "pooled": {f"{a}/{b}": both(pools[a], pools[b], compacted) for a, b in COMPARISONS},
        },
        "breakdown": {f"{a}/{b}": breakdown(pools[a], pools[b], **kw) for a, b in BREAKDOWN},
        "classifier_check": {
            r: {
                "edits_cover_patch": sum(rec[0]["edits_cover_patch"] is True for rec in p.values()),
                "missed": sorted(i for i, rec in p.items() if rec[0]["edits_cover_patch"] is False),
                "unverifiable": sorted(i for i, rec in p.items() if rec[0]["edits_cover_patch"] is None),
            }
            for r, p in by_run.items()
        },
    }  # fmt: skip
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    def fmt(s: dict[str, Any]) -> str:
        return f"{s['median_ratio']:.2f} [{s['ci95'][0]:.2f}, {s['ci95'][1]:.2f}] {s['other_higher']}/{s['n']}"

    for section in ("calibration", "pooled", "single"):
        print(f"== {section}")
        for k, v in out[section].items():
            print(f"  {k:16} steps {fmt(v['steps'])}   cost {fmt(v['cost'])}")
    print(f"== without compacted tasks {out['without_compacted']['dropped']}")
    for k, v in out["without_compacted"]["pooled"].items():
        print(f"  {k:16} steps {fmt(v['steps'])}   cost {fmt(v['cost'])}")
    for k, v in out["breakdown"].items():
        t = v["total"]
        print(
            f"== breakdown {k}: extra responses per task mean {t['mean']:.1f} {t['mean_ci95']}"
        )
        for group in ("phases", "classes"):
            for name, d in v[group].items():
                if abs(d["mean"]) < 0.05 and abs(d["a_mean"]) < 0.05:
                    continue
                print(f"  {group[:5]} {name:13} {d['a_mean']:5.1f} vs {d['b_mean']:5.1f}  "
                      f"mean {d['mean']:+5.1f} [{d['mean_ci95'][0]:+.1f}, {d['mean_ci95'][1]:+.1f}]  "
                      f"median {d['median']:+5.1f} [{d['median_ci95'][0]:+.1f}, {d['median_ci95'][1]:+.1f}]  "
                      f"share {d['share']:+.0%}{'  <- qualifies' if d['qualifies'] else ''}")  # fmt: skip
        for name, d in v["extras"].items():
            print(
                f"  extra {name:24} mean {d['mean']:+5.1f} {[round(x, 1) for x in d['mean_ci95']]}"
            )
    print(f"== classifier check {json.dumps(out['classifier_check'])}")
    if args.samples:
        s = {
            r: samples(load_run(args.results_dir / r), 20, args.seed)
            for r in ("D-v03", "B0-v03")
        }
        args.samples.parent.mkdir(parents=True, exist_ok=True)
        args.samples.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
        print(f"-> samples {args.samples}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
