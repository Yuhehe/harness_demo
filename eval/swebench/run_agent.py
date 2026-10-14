"""Run the agent over a subset of SWE-bench Verified, one task after another.

    uv run python eval/swebench/run_agent.py --config configs/default.yaml \\
        --subset configs/subsets/dev_5.txt --run-id M0-dev

Writes results/runs/<run_id>/: trajectories/, patches/, preds.jsonl (what `swebench eval`
reads: instance_id, model_name_or_path, model_patch), status.jsonl (how each task ended) and
run_meta.json (which config and commit the run belongs to).

* Resumable. A task is done when its row is in preds.jsonl; a rerun skips it. The row is
  written only after the task finished, so a crash mid-task leaves the task pending, and the
  half-written trajectory is moved aside before the rerun (cli.retire_stale_trajectory).
  A task that ended in env_error (the container did not start ...) counts as done and is
  flagged; --retry-env-errors runs those again.
* One run_id, one config. A rerun with a different config_hash is refused: results of two
  configs must never be mixed under one name.
* An experiment run needs a clean working tree (the commit is what the numbers belong to);
  --allow-dirty is for development runs and is recorded.
* Before the first task: the images must be present (nothing is pulled here), the estimated
  and the worst-case cost are printed, and above 10 tasks you are asked to confirm
  (CLAUDE.md 2.5). The run stops by itself at 80% of MAX_TOTAL_COST_USD.
* Sequential for now. --workers > 1 is a TODO; the safe number of sandboxes for this
  machine is printed so the TODO is concrete.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "report"))

from aggregate import load_run
from select_subset import META_NAME, SUBSET_DIR, read_ids

PREDS, STATUS, RUN_META = "preds.jsonl", "status.jsonl", "run_meta.json"
CONFIRM_ABOVE = 10  # tasks (CLAUDE.md 2.5)
STOP_AT_BUDGET = 0.8  # share of MAX_TOTAL_COST_USD (CLAUDE.md 2.5)
HOST_RESERVE_GB = (
    2.0  # Docker daemon, this process, headroom for the scorer's containers
)


class ConfigMismatch(SystemExit):
    pass


# ---- what the machine can carry ------------------------------------------------------------------
def parse_mem_gb(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)b?", text.strip().lower())
    if not m:
        raise ValueError(f"cannot read a memory size from {text!r}")
    unit = {"": 1, "k": 1 / 1024**2, "m": 1 / 1024, "g": 1, "t": 1024}[m[2]]
    return float(m[1]) * (unit if m[2] else 1 / 1024**3)


def max_sandboxes(
    mem_bytes: int, ncpu: int, mem_limit: str, cpus: float
) -> dict[str, Any]:
    """How many sandboxes fit at once if each may use its full limits. The limits are caps,
    not reservations, so more would usually run; this is the number that cannot run out of
    memory."""
    mem_gb = mem_bytes / 1024**3
    by_memory = int((mem_gb - HOST_RESERVE_GB) // parse_mem_gb(mem_limit))
    by_cpu = int(ncpu // cpus)
    n = max(min(by_memory, by_cpu), 1)
    return {
        "safe_max": n,
        "by_memory": by_memory,
        "by_cpu": by_cpu,
        "mem_gb": round(mem_gb, 1),
        "ncpu": ncpu,
    }


# ---- bookkeeping in the run directory ----------------------------------------------------------------
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def check_run_meta(
    run_dir: Path, cfg_hash: str, git: dict[str, Any], *, allow_dirty: bool
) -> None:
    """Create run_meta.json, or refuse if this run_id belongs to another config."""
    path = run_dir / RUN_META
    if path.exists():
        old = json.loads(path.read_text())
        if old["config_hash"] != cfg_hash:
            raise ConfigMismatch(
                f"{run_dir.name} was started with config {old['config_hash']}, this is "
                f"{cfg_hash}: use a new --run-id (results of two configs must not be mixed)"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "config_hash": cfg_hash,
                **git,
                "allow_dirty": allow_dirty,
                "created": time.time(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def done_ids(run_dir: Path, *, retry_env_errors: bool = False) -> set[str]:
    done = {r["instance_id"] for r in read_jsonl(run_dir / PREDS)}
    if retry_env_errors:
        latest = {r["instance_id"]: r for r in read_jsonl(run_dir / STATUS)}
        failed = {i for i, r in latest.items() if r.get("fail_reason") == "env_error"}
        if failed:
            rows = [
                r for r in read_jsonl(run_dir / PREDS) if r["instance_id"] not in failed
            ]
            (run_dir / PREDS).write_text(
                "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            )
            done -= failed
    return done


# ---- money ----------------------------------------------------------------------------------------------
def spent_so_far(results_dir: Path) -> float:
    """What this harness has already spent, summed over every finished trajectory under
    results/runs/ (standard price). Does not include B0: mini-swe-agent's trajectories have
    another format and are not read here."""
    total = 0.0
    for run in results_dir.glob("*/trajectories"):
        for path in run.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                if '"type": "run_end"' in line:
                    total += json.loads(line).get("cost_usd", 0.0)
    return total


def mean_cost_per_task(results_dir: Path, model: str) -> tuple[float, str] | None:
    """Mean cost of the finished tasks of earlier runs with the same model, with a label that
    says where the number comes from (an estimate must say what it rests on)."""
    costs: list[float] = []
    for run in results_dir.glob("*/trajectories"):
        for path in run.glob("*.jsonl"):
            spans = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
            start = next((s for s in spans if s["type"] == "run_start"), None)
            end = next((s for s in reversed(spans) if s["type"] == "run_end"), None)
            if start and end and start.get("model") == model:
                costs.append(end["cost_usd"])
    if not costs:
        return None
    return sum(costs) / len(
        costs
    ), f"mean of {len(costs)} earlier finished task(s) with {model}"


@dataclass
class Estimate:
    pending: int
    expected: float | None
    expected_source: str
    worst_case: float


def estimate(
    pending: int, per_task: tuple[float, str] | None, cap_usd: float
) -> Estimate:
    return Estimate(
        pending,
        per_task[0] * pending if per_task else None,
        per_task[1] if per_task else "no earlier run to base an estimate on",
        cap_usd * pending,
    )


# ---- the tasks -----------------------------------------------------------------------------------------------
def load_tasks(ids: list[str], meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The dataset rows of `ids`, at the revision recorded when the subsets were chosen. The
    dataset is loaded once for the whole batch."""
    from datasets import load_dataset

    ds = load_dataset(
        meta["dataset_id"], split=meta["split"], revision=meta["revision"]
    )
    wanted = set(ids)
    rows = {r["instance_id"]: dict(r) for r in ds if r["instance_id"] in wanted}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise SystemExit(
            f"not in {meta['dataset_id']}@{meta['revision'][:12]}: {missing}"
        )
    return rows


def missing_images(
    client: Any, rows: dict[str, dict[str, Any]], ids: list[str]
) -> list[str]:
    import docker

    out = []
    for i in ids:
        try:
            client.images.get(rows[i]["image"])
        except docker.errors.ImageNotFound:
            out.append(i)
    return out


# ---- the batch --------------------------------------------------------------------------------------------------
@dataclass
class BatchResult:
    ran: list[str]
    skipped: list[str]
    stopped_for_budget: bool
    spent: float


async def run_batch(
    ids: list[str],
    rows: dict[str, dict[str, Any]],
    cfg: Any,
    run_dir: Path,
    run_id: str,
    runner: Any,
    *,
    git: dict[str, Any],
    spent_before: float = 0.0,
    budget_total: float | None = None,
    retry_env_errors: bool = False,
    say: Any = print,
) -> BatchResult:
    """Run every task of `ids` that is not done yet, one after another. `runner(task, image,
    cfg, run_dir, run_id, meta)` returns a cli.RunOutcome; tests pass a fake."""
    from harness.agent.state import Task

    done = done_ids(run_dir, retry_env_errors=retry_env_errors)
    skipped = [i for i in ids if i in done]
    ran: list[str] = []
    spent = 0.0
    for n, iid in enumerate((i for i in ids if i not in done), 1):
        if budget_total and spent_before + spent >= STOP_AT_BUDGET * budget_total:
            say(
                f"stopping: ${spent_before + spent:.2f} spent is {STOP_AT_BUDGET:.0%} of "
                f"MAX_TOTAL_COST_USD (${budget_total:.2f}); raise it or confirm to go on"
            )
            return BatchResult(ran, skipped, True, spent)
        row = rows[iid]
        started = time.monotonic()
        status: dict[str, Any] = {"instance_id": iid}
        try:
            out = await runner(
                Task(iid, row["problem_statement"]), row["image"], cfg, run_dir, run_id,
                {"run_id": run_id, **git},
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001 - one bad task must not end the batch
            patch = ""
            status |= {
                "fail_reason": "env_error",
                "submitted": False,
                "error": f"{type(e).__name__}: {e}"[:300],
                "cost_usd": 0.0,
            }
        else:
            r = out.result
            patch = r.patch
            spent += r.metrics["cost_usd"]
            status |= {
                "fail_reason": r.fail_reason.value if r.fail_reason else None,
                "submitted": r.submitted,
                "steps": r.metrics["steps"],
                "cost_usd": r.metrics["cost_usd"],
            }
        status["wall_seconds"] = round(time.monotonic() - started, 1)
        # the row that marks the task done goes in last
        append_jsonl(run_dir / STATUS, status)
        append_jsonl(
            run_dir / PREDS,
            {"instance_id": iid, "model_name_or_path": cfg.llm.resolved_model(), "model_patch": patch},
        )  # fmt: skip
        ran.append(iid)
        say(
            f"[{n}] {iid}: {status['fail_reason'] or 'submitted'}"
            f"  ${status['cost_usd']:.4f}  {status['wall_seconds']}s"
            + (f"  ({status['error']})" if "error" in status else "")
        )
    return BatchResult(ran, skipped, False, spent)


# ---- command line -----------------------------------------------------------------------------------------------------
def should_ask(pending: int, yes: bool) -> bool:
    """Above 10 tasks a run must be confirmed by a person (CLAUDE.md 2.5)."""
    return pending > CONFIRM_ABOVE and not yes


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument(
        "--subset",
        type=Path,
        required=True,
        help="a file of instance ids, one per line",
    )
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    ap.add_argument("--workers", type=int, default=1, help="only 1 for now (TODO)")
    ap.add_argument(
        "--yes",
        action="store_true",
        help="do not ask to confirm a run of more than 10 tasks",
    )
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="development run on an uncommitted tree",
    )
    ap.add_argument("--retry-env-errors", action="store_true")
    return ap


async def _main(args: argparse.Namespace) -> int:
    import docker
    from dotenv import load_dotenv

    from harness.cli import _git_meta, check_summarizer, describe_config, run_one
    from harness.config import load_config

    load_dotenv(".env")
    cfg = load_config(args.config)
    check_summarizer(cfg)
    ids = read_ids(args.subset)
    run_dir = args.results_dir / args.run_id
    client = docker.from_env()

    cap = max_sandboxes(
        client.info()["MemTotal"],
        client.info()["NCPU"],
        cfg.sandbox.mem_limit,
        cfg.sandbox.cpus,
    )
    if args.workers != 1:
        raise SystemExit(
            f"--workers {args.workers}: parallel runs are a TODO. This machine could carry "
            f"{cap['safe_max']} sandboxes at their limits (memory allows {cap['by_memory']}, "
            f"cpus {cap['by_cpu']}; {cap['mem_gb']} GB, {cap['ncpu']} cpus)."
        )
    git = _git_meta()
    if git["git_dirty"] and not args.allow_dirty:
        raise SystemExit(
            "the working tree has uncommitted changes: commit first, or pass --allow-dirty for a development run"
        )
    check_run_meta(run_dir, cfg.config_hash(), git, allow_dirty=args.allow_dirty)

    meta = json.loads(Path(SUBSET_DIR / META_NAME).read_text())
    rows = load_tasks(ids, meta)
    pending = [
        i
        for i in ids
        if i not in done_ids(run_dir, retry_env_errors=args.retry_env_errors)
    ]
    absent = missing_images(client, rows, pending)
    if absent:
        raise SystemExit(
            f"{len(absent)} image(s) are not present (nothing is pulled here): {absent[:5]}... run eval/swebench/pull_images.py"
        )

    print("\n".join(describe_config(cfg, args.config)))
    est = estimate(
        len(pending),
        mean_cost_per_task(args.results_dir, cfg.llm.resolved_model()),
        cfg.limits.max_cost_per_task_usd,
    )
    budget = float(os.environ.get("MAX_TOTAL_COST_USD", "0")) or None
    spent_before = spent_so_far(args.results_dir)
    expected = f"${est.expected:.2f}" if est.expected is not None else "unknown"
    print(
        f"run {args.run_id}: {len(ids)} tasks, {len(ids) - len(pending)} done, {est.pending} to run (sequential)"
    )
    print(f"  expected cost : {expected}  ({est.expected_source})")
    print(
        f"  worst case    : ${est.worst_case:.2f}  ({est.pending} x ${cfg.limits.max_cost_per_task_usd} per-task cap)"
    )
    print(
        f"  budget        : ${spent_before:.2f} spent so far of ${budget:.2f} MAX_TOTAL_COST_USD (B0 runs are not counted)"
        if budget
        else "  budget        : MAX_TOTAL_COST_USD is not set"
    )
    print(
        f"  sandboxes     : up to {cap['safe_max']} could run at once on this machine; running 1"
    )
    if not pending:
        print("nothing to run")
        return 0
    if (
        should_ask(len(pending), args.yes)
        and input("go on? [y/N] ").strip().lower() != "y"
    ):
        return 2

    async def runner(task, image, cfg_, run_dir_, run_id, meta_):
        return await run_one(
            task, image, cfg_, run_dir_, run_id, docker_client=client, meta=meta_
        )

    result = await run_batch(
        ids, rows, cfg, run_dir, args.run_id, runner,
        git=git, spent_before=spent_before, budget_total=budget, retry_env_errors=args.retry_env_errors,
    )  # fmt: skip
    from aggregate import aggregate

    metrics = aggregate(load_run(run_dir))
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True))
    print(
        f"ran {len(result.ran)}, spent ${result.spent:.4f}; metrics -> {run_dir / 'metrics.json'}"
    )
    return 3 if result.stopped_for_budget else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
