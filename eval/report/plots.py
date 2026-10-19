"""Two figures for the README, from results/runs (no number is typed in by hand).

    uv run python eval/report/plots.py [--subset configs/subsets/verified_25.txt]

    docs/img/cost_vs_resolve.png     mean cost per task against resolve rate, one point per run,
                                     with the 95% Wilson interval of the resolve rate
    docs/img/failure_taxonomy.png    how each run's tasks ended, stacked

Every run is cut to the same tasks (`--subset`, verified_25 by default: the ablations ran on
those 25, M0 and B0 on all 50), so the points and bars compare like with like. A run that does
not exist is skipped; one that is unscored or unfinished is an error (same rule as
make_tables.py).

Colors: the categorical reference order of the dataviz method (slots 1-4, validated for
adjacent pairs in light mode by its author; this machine has no node to rerun the validator).
Slots 3 and 4 are below 3:1 contrast on the light surface, so every stacked segment carries
its count as a label. The scatter has one series (no color encoding); points are named by
direct labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aggregate import end_category, load_run, wilson
from failure_taxonomy import outcome_class

ROOT = Path(__file__).resolve().parents[2]
RUNS = ("M0", "B0", "M0-r2", "A2", "A5", "A6", "A6xA1")
SURFACE = "#fcfcfb"
TEXT, TEXT_2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")  # reference order, slots 1-4
CLASSES = ("resolved", "wrong fix", "partial or regression", "stopped early")


def ending(
    iid: str, run_end: dict[str, Any], resolved: set[str], score: dict[str, Any] | None
) -> str:
    """One of CLASSES for a task."""
    if iid in resolved:
        return "resolved"
    end = end_category(iid, run_end["fail_reason"], resolved, score)
    if end != "submitted_but_failed":
        return "stopped early"  # context_overflow, step_limit, ... (resolved already taken)
    tests = ((score or {}).get("tests") or {}).get(iid)
    return (
        "wrong fix"
        if outcome_class(tests) == "target_fail"
        else "partial or regression"
    )


def run_points(run_id: str, results_dir: Path, ids: set[str]) -> dict[str, Any] | None:
    run_dir = results_dir / run_id
    if not run_dir.is_dir():
        return None
    tasks = load_run(run_dir)
    resolved_path = run_dir / "resolved.json"
    if not resolved_path.exists():
        raise SystemExit(f"{run_id}: score the run first")
    resolved = set(json.loads(resolved_path.read_text()))
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text()) if score_path.exists() else None
    mine = {i: t for i, t in tasks.items() if i in ids}
    if set(mine) != ids:
        raise SystemExit(f"{run_id}: missing {sorted(ids - set(mine))[:5]}")
    if any(t.run_end is None for t in mine.values()):
        raise SystemExit(f"{run_id}: unfinished tasks")
    counts = dict.fromkeys(CLASSES, 0)
    for iid, t in mine.items():
        counts[ending(iid, t.run_end, resolved, score)] += 1
    n, k = len(mine), counts["resolved"]
    return {
        "run_id": run_id,
        "n": n,
        "resolved": k,
        "rate": k / n,
        "wilson95": wilson(k, n),
        "cost_mean": sum(t.run_end["cost_usd"] for t in mine.values()) / n,
        "endings": counts,
    }


def _style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=9)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_cost_vs_resolve(points: list[dict[str, Any]], subset: str, out: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4.2), facecolor=SURFACE)
    _style(ax)
    for p in points:
        lo, hi = p["wilson95"]
        x, y = p["cost_mean"], 100 * p["rate"]
        ax.plot([x, x], [100 * lo, 100 * hi], color=SERIES[0], linewidth=2, alpha=0.35)
        ax.plot(x, y, "o", markersize=8, color=SERIES[0], markeredgecolor=SURFACE,
                markeredgewidth=2)  # fmt: skip
        # a point close by on the right would collide with a label there: put it left
        crowded = any(0 < q["cost_mean"] - x < 0.02 for q in points)
        ax.annotate(p["run_id"], (x, y), xytext=(-7 if crowded else 7, 4),
                    textcoords="offset points", fontsize=9, color=TEXT,
                    ha="right" if crowded else "left")  # fmt: skip
    ax.set_xlabel("Mean cost per task (USD, standard price)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("Resolved (%), 95% Wilson interval", color=TEXT_2, fontsize=9)
    ax.set_title(
        f"Cost against resolve rate, same {points[0]['n']} tasks ({subset})",
        color=TEXT, fontsize=11, loc="left",
    )  # fmt: skip
    ax.set_ylim(0, 100)
    ax.set_xlim(left=0)
    fig.tight_layout()
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_taxonomy(points: list[dict[str, Any]], subset: str, out: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 0.55 * len(points) + 1.6), facecolor=SURFACE)
    _style(ax)
    ax.grid(axis="y", visible=False)
    names = [p["run_id"] for p in points][::-1]
    left = [0.0] * len(points)
    for cls, color in zip(CLASSES, SERIES, strict=True):
        vals = [p["endings"][cls] for p in points][::-1]
        bars = ax.barh(names, vals, left=left, color=color, height=0.6,
                       edgecolor=SURFACE, linewidth=2, label=cls)  # fmt: skip
        for bar, v in zip(bars, vals, strict=True):
            if v:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height() / 2,
                        str(v), ha="center", va="center", fontsize=8, color=TEXT)  # fmt: skip
        left = [a + b for a, b in zip(left, vals, strict=True)]
    ax.set_xlabel("Tasks", color=TEXT_2, fontsize=9)
    ax.set_title(
        f"How the tasks ended, same {points[0]['n']} tasks ({subset})",
        color=TEXT, fontsize=11, loc="left",
    )  # fmt: skip
    ax.legend(ncol=4, fontsize=8, frameon=False, loc="upper left",
              bbox_to_anchor=(0, -0.18), labelcolor=TEXT_2)  # fmt: skip
    fig.tight_layout()
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--subset", type=Path, default=ROOT / "configs/subsets/verified_25.txt"
    )
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--out-dir", type=Path, default=Path("docs/img"))
    args = ap.parse_args(argv)

    import matplotlib

    matplotlib.use("Agg")
    ids = {x.strip() for x in args.subset.read_text().splitlines() if x.strip()}
    points = [
        p for r in RUNS if (p := run_points(r, args.results_dir, ids)) is not None
    ]
    if not points:
        raise SystemExit("no runs to plot")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    plot_cost_vs_resolve(points, args.subset.stem, args.out_dir / "cost_vs_resolve.png")
    plot_taxonomy(points, args.subset.stem, args.out_dir / "failure_taxonomy.png")
    for p in points:
        print(
            f"{p['run_id']}: {p['resolved']}/{p['n']}, ${p['cost_mean']:.3f}, {p['endings']}"
        )
    print(f"-> {args.out_dir}/cost_vs_resolve.png, failure_taxonomy.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
