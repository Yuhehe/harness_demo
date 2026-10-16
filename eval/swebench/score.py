"""Score a run with the official `swebench eval` and fold the result into its metrics.

    uv run python eval/swebench/score.py --run-id M0-dev

Reads results/runs/<run_id>/preds.jsonl (written by run_agent.py) and writes, in the same
directory: resolved.json (the instance ids that passed, the file aggregate.py reads),
score.json (every id by what the scorer said about it) and an updated metrics.json.

* `swebench eval verified` uses the NEWEST revision of the dataset and cannot be pinned, so
  before it runs the newest revision is compared with the one the subsets were chosen on
  (configs/subsets/subsets_meta.json). If they differ, nothing is scored.
* Ids the scorer could not judge (infrastructure failures, errors, an empty patch) are kept
  apart from "judged and not fixed": a broken scoring environment is not the model's failure.
* swebench writes logs/ into the current directory, so it is run inside the run directory
  (results/runs/ is gitignored).
* Per instance, score.json also keeps how the tests went (`tests`): of the FAIL_TO_PASS tests
  (the ones the fix must make pass) how many passed, and of the PASS_TO_PASS tests (the ones
  that passed before) how many now fail, i.e. regressions. Read from the per-instance
  report.json under logs/run_evaluation/.
* --reuse-report reads an existing report again without scoring (to refresh score.json and
  metrics.json after this script learned to read more from it).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "report"))

from aggregate import aggregate, load_run
from select_subset import META_NAME, SUBSET_DIR, resolve_revision

# what the scorer's report calls each group of ids
GROUPS = {
    "resolved": "resolved_ids",
    "unresolved": "unresolved_ids",
    "empty_patch": "empty_patch_ids",
    "error": "error_ids",
    "infra_failure": "infra_failure_ids",
    "ambiguous_failure": "ambiguous_failure_ids",
    "incomplete": "incomplete_ids",
}
NOT_JUDGED = (
    "empty_patch",
    "error",
    "infra_failure",
    "ambiguous_failure",
    "incomplete",
)


@dataclass
class Score:
    ids: dict[str, list[str]] = field(default_factory=dict)
    tests: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def resolved(self) -> set[str]:
        return set(self.ids.get("resolved", []))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {name: sorted(self.ids.get(name, [])) for name in GROUPS}
        out["tests"] = dict(sorted(self.tests.items()))
        return out


def check_dataset_revision(meta: dict[str, Any], newest: str) -> None:
    if newest != meta["revision"]:
        raise SystemExit(
            f"the dataset has changed since the subsets were chosen (newest {newest[:12]}, "
            f"subsets {meta['revision'][:12]}) and `swebench eval verified` cannot be pinned: "
            "decide what to do about it before scoring (docs/notes.md section 3)"
        )


def swebench_command(
    binary: Path,
    preds: Path,
    run_id: str,
    ids: list[str],
    *,
    workers: int,
    timeout: int,
) -> list[str]:
    cmd = [str(binary), "eval", "verified", "-p", str(preds), "--run-id", run_id]
    cmd += ["-j", str(workers), "-t", str(timeout), "--report-dir", "."]
    for i in ids:
        cmd += ["-i", i]
    return cmd


def find_report(run_dir: Path, run_id: str) -> Path:
    """The report is named after the model and the run id; look for the run id, do not rebuild
    the file name."""
    found = sorted(run_dir.glob(f"*.{run_id}.json"))
    if len(found) != 1:
        raise SystemExit(
            f"expected one report matching *.{run_id}.json in {run_dir}, found {len(found)}"
        )
    return found[0]


def read_report(path: Path) -> Score:
    data = json.loads(path.read_text())
    return Score({name: list(data.get(key, [])) for name, key in GROUPS.items()})


def read_tests(run_dir: Path, run_id: str) -> dict[str, dict[str, int]]:
    """Per instance: FAIL_TO_PASS passed / total, PASS_TO_PASS failed / total. Instances whose
    report has no test status (the patch did not apply, the run broke) are left out."""
    out: dict[str, dict[str, int]] = {}
    for path in sorted(
        (run_dir / "logs" / "run_evaluation" / run_id).glob("*/*/report.json")
    ):
        for iid, rep in json.loads(path.read_text()).items():
            status = rep.get("tests_status")
            if not status:
                continue
            f2p, p2p = status["FAIL_TO_PASS"], status["PASS_TO_PASS"]
            out[iid] = {
                "f2p_pass": len(f2p["success"]),
                "f2p_total": len(f2p["success"]) + len(f2p["failure"]),
                "p2p_fail": len(p2p["failure"]),
                "p2p_total": len(p2p["success"]) + len(p2p["failure"]),
            }
    return out


def write_score(run_dir: Path, score: Score) -> None:
    (run_dir / "resolved.json").write_text(json.dumps(sorted(score.resolved)))
    (run_dir / "score.json").write_text(
        json.dumps(score.to_json(), indent=2, sort_keys=True)
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument(
        "--workers", type=int, default=1, help="instances scored in parallel"
    )
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per instance")
    ap.add_argument(
        "--reuse-report",
        action="store_true",
        help="read the existing report again instead of scoring",
    )
    return ap


def main(
    argv: list[str] | None = None,
    *,
    run=subprocess.run,
    newest_revision=resolve_revision,
) -> int:
    args = build_parser().parse_args(argv)
    run_dir = (args.results_dir / args.run_id).resolve()
    preds = run_dir / "preds.jsonl"
    if not preds.exists():
        raise SystemExit(
            f"{preds} does not exist: run eval/swebench/run_agent.py first"
        )
    ids = [
        json.loads(x)["instance_id"]
        for x in preds.read_text().splitlines()
        if x.strip()
    ]

    if not args.reuse_report:
        meta = json.loads((SUBSET_DIR / META_NAME).read_text())
        check_dataset_revision(meta, newest_revision(meta["dataset_id"], None))

        cmd = swebench_command(
            Path(sys.executable).with_name("swebench"), preds, args.run_id, ids,
            workers=args.workers, timeout=args.timeout,
        )  # fmt: skip
        print(f"scoring {len(ids)} prediction(s) with swebench ...")
        done = run(
            cmd, cwd=run_dir, check=False
        )  # a fixed command we build, not model output
        if done.returncode != 0:
            raise SystemExit(f"swebench exited with {done.returncode}")

    score = read_report(find_report(run_dir, args.run_id))
    score.tests = read_tests(run_dir, args.run_id)
    write_score(run_dir, score)
    metrics = aggregate(load_run(run_dir), score.resolved, score.to_json())
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True))

    n = len(ids)
    print(f"resolved {len(score.resolved)}/{n}")
    for name in ("unresolved", *NOT_JUDGED):
        if score.ids.get(name):
            print(f"  {name}: {sorted(score.ids[name])}")
    print(f"-> {run_dir / 'resolved.json'}, score.json, metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
