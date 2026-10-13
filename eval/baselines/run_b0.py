"""B0, the external baseline: mini-SWE-agent with the limits of the harness configuration.

    uv run python eval/baselines/run_b0.py run     --config configs/default.yaml \
        --subset configs/subsets/dev_5.txt --run-id B0-dev
    uv run python eval/baselines/run_b0.py convert --config configs/default.yaml --run-id B0-dev

`run` starts mini-swe-agent (mini-extra swebench) on the host the way eval/baselines/
mini_swe_agent.md describes, with every limit read from the harness configuration so that
M0 and B0 cannot drift apart, then converts its output. `convert` alone is for a run that was
interrupted or finished by hand.

The same guards as run_agent.py, because the comparison with M0 rests on "the same config":
`run` refuses a working tree with uncommitted changes (--allow-dirty is for development runs
and is recorded), and a run_id belongs to one config: run_meta.json records the config hash
(with the git commit) when the run starts, and `run` and `convert` both refuse a different one.

The conversion turns mini-swe-agent's output (mini/preds.json and one *.traj.json per task)
into the layout the rest of the pipeline reads: preds.jsonl for score.py, status.jsonl, and a
trajectories/<id>.jsonl holding run_start and run_end only. That is enough for aggregate.py
(cost, tokens, steps, latency, fail reason) and for the spend count of run_agent.py; there
are no per-step spans, because mini-swe-agent's trajectory does not have our span types.

What the numbers mean, so a comparison reads them correctly:
* cost_usd is recomputed from the recorded token counts with the harness's own fixed standard
  price (llm/cost.py), the same function M0 uses; mini-swe-agent's own figure is kept as
  b0_reported_cost. Neither includes anything but the model calls (M0's cost also has the
  compaction calls, which B0 does not have).
* wall_seconds is the time between the first and the last model response. It leaves out the
  container start and the first call's latency, so it is a little short.
* a "step" is one model call in both (mini-swe-agent counts n_calls against step_limit).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "swebench"))
sys.path.insert(0, str(HERE.parents[0] / "report"))

from run_agent import ConfigMismatch, should_ask
from score import check_dataset_revision
from select_subset import (
    META_NAME,
    SUBSET_DIR,
    read_ids,
    resolve_revision,
)

REPO = HERE.parents[1]
REGISTRY = HERE / "litellm_registry.json"
SUBSET = "SWE-bench/SWE-bench_Verified"


# ---- the command, from the harness configuration ----------------------------------------------
def overrides(cfg: Any) -> list[str]:
    """`-c key=value` specs that give mini-swe-agent the limits and the model settings of `cfg`.
    Values are JSON, as mini-swe-agent parses them."""
    lim, sbx, llm = cfg.limits, cfg.sandbox, cfg.llm
    specs = {
        "agent.step_limit": lim.max_steps,
        "agent.cost_limit": lim.max_cost_per_task_usd,
        "agent.wall_time_limit_seconds": int(lim.max_wall_seconds),
        "environment.timeout": cfg.bash_timeout_s,
        "environment.run_args": [
            "--rm",
            "--network",
            sbx.network_mode,
            "--memory",
            sbx.mem_limit,
            "--cpus",
            str(sbx.cpus),
            "--pids-limit",
            str(sbx.pids_limit),
        ],
        "model.model_kwargs.max_tokens": llm.max_tokens,
        "model.model_kwargs.timeout": llm.timeout_s,
    }
    thinking = {"type": llm.thinking}
    body: dict[str, Any] = {}
    if llm.reasoning_effort:
        body["reasoning_effort"] = llm.reasoning_effort
    # Same split as llm/client.py: LiteLLM's DeepSeek provider maps a `thinking` keyword, the
    # generic OpenAI route rejects it (or drops it silently with drop_params), and the
    # reasoning_effort keyword never survives the DeepSeek provider.
    if llm.resolved_model().startswith("deepseek/"):
        specs["model.model_kwargs.thinking"] = thinking
    else:
        body["thinking"] = thinking
    if body:
        specs["model.model_kwargs.extra_body"] = body
    return [f"{k}={json.dumps(v)}" for k, v in specs.items()]


def command(cfg: Any, ids: list[str], out_dir: Path, binary: Path) -> list[str]:
    cmd = [str(binary), "swebench", "--subset", SUBSET, "--split", "test"]
    cmd += ["--filter", "^(" + "|".join(re.escape(i) for i in ids) + ")$"]
    cmd += ["-w", "1", "-m", cfg.llm.resolved_model(), "-c", "swebench.yaml"]
    for spec in overrides(cfg):
        cmd += ["-c", spec]
    return [*cmd, "-o", str(out_dir)]


def environment(base: dict[str, str]) -> dict[str, str]:
    return {
        **base,
        "MSWEA_GLOBAL_CONFIG_DIR": str(REPO / ".mswea"),
        "LITELLM_MODEL_REGISTRY_PATH": str(
            REGISTRY
        ),  # costs at the fixed standard price
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }


# ---- turning mini-swe-agent's output into ours -----------------------------------------------
def fail_reason(exit_status: str, steps: int, cost: float, cfg: Any) -> str | None:
    """Our FailReason for a mini-swe-agent exit status (None: it submitted)."""
    if exit_status == "Submitted":
        return None
    if exit_status == "LimitsExceeded":
        if 0 < cfg.limits.max_steps <= steps:
            return "step_limit"
        return (
            "cost_limit" if cost >= cfg.limits.max_cost_per_task_usd else "step_limit"
        )
    if exit_status == "TimeExceeded":
        return "time_limit"
    if exit_status in ("RepeatedFormatError", "FormatError"):
        return "format_error"
    return "env_error"


def trajectory_end(traj: dict[str, Any], cfg: Any) -> dict[str, Any]:
    """The run_end span for one mini-swe-agent trajectory."""
    from harness.llm import cost as price
    from harness.llm.types import Usage

    model = cfg.llm.resolved_model()
    calls = [m for m in traj["messages"] if m.get("role") == "assistant"]
    tin = tout = cached = reasoning = 0
    usd = 0.0
    for m in calls:
        u = (m.get("extra", {}).get("response") or {}).get("usage") or {}
        used = Usage(
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get(
                "cached_tokens", 0
            ),
            reasoning_tokens=(u.get("completion_tokens_details") or {}).get(
                "reasoning_tokens", 0
            ),
        )
        tin += used.prompt_tokens
        tout += used.completion_tokens
        cached += used.cached_tokens
        reasoning += used.reasoning_tokens
        usd += price.cost_usd(model, used)
    stamps = [
        m["extra"]["timestamp"] for m in calls if m.get("extra", {}).get("timestamp")
    ]
    info = traj.get("info", {})
    status = info.get("exit_status", "")
    steps = info.get("model_stats", {}).get("api_calls", len(calls))
    return {
        "type": "run_end",
        "fail_reason": fail_reason(status, steps, usd, cfg),
        "submitted": status == "Submitted",
        "steps": steps,
        "cost_usd": usd,
        "compaction_cost_usd": 0.0,
        "tokens_in": tin,
        "tokens_cached": cached,
        "tokens_out": tout,
        "tokens_reasoning": reasoning,
        "wall_seconds": round(stamps[-1] - stamps[0], 3) if len(stamps) > 1 else 0.0,
        "b0_exit_status": status,
        "b0_reported_cost": info.get("model_stats", {}).get("instance_cost"),
    }


def convert(run_dir: Path, cfg: Any, run_id: str) -> list[str]:
    """Write preds.jsonl, status.jsonl, run_meta.json and trajectories/ from mini/ . Returns
    the instance ids converted. Safe to repeat."""
    # before anything is written: a run started by `run` already has its meta (with the git
    # commit) and another config must not overwrite its files
    check_meta(run_dir, cfg)
    mini = run_dir / "mini"
    preds = json.loads((mini / "preds.json").read_text())
    traj_dir = run_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    pred_rows, status_rows = [], []
    for iid, pred in sorted(preds.items()):
        path = mini / iid / f"{iid}.traj.json"
        end = (
            trajectory_end(json.loads(path.read_text()), cfg)
            if path.exists()
            else {"type": "run_end", "fail_reason": "env_error", "submitted": False, "steps": 0,
                  "cost_usd": 0.0, "compaction_cost_usd": 0.0, "tokens_in": 0, "tokens_cached": 0,
                  "tokens_out": 0, "wall_seconds": 0.0, "b0_exit_status": "no trajectory"}
        )  # fmt: skip
        common = {"run_id": run_id, "instance_id": iid, "ts": time.time()}
        start = {
            "type": "run_start",
            "model": cfg.llm.resolved_model(),
            "b0": True,
            **common,
        }
        (traj_dir / f"{iid}.jsonl").write_text(
            json.dumps(start, sort_keys=True)
            + "\n"
            + json.dumps({**end, **common}, sort_keys=True)
            + "\n"
        )
        pred_rows.append({"instance_id": iid, "model_name_or_path": pred.get("model_name_or_path", cfg.llm.resolved_model()), "model_patch": pred.get("model_patch", "")})  # fmt: skip
        status_rows.append({"instance_id": iid, "fail_reason": end["fail_reason"], "submitted": end["submitted"], "steps": end["steps"], "cost_usd": end["cost_usd"], "wall_seconds": end["wall_seconds"]})  # fmt: skip
    for name, rows in (("preds.jsonl", pred_rows), ("status.jsonl", status_rows)):
        (run_dir / name).write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        )
    return sorted(preds)


RUN_META = "run_meta.json"


def check_meta(
    run_dir: Path,
    cfg: Any,
    git: dict[str, Any] | None = None,
    *,
    allow_dirty: bool = False,
) -> None:
    """Create run_meta.json for this run, or refuse when the run_id holds another config. An
    existing file is never rewritten: it records how the run was started."""
    path = run_dir / RUN_META
    cfg_hash = cfg.config_hash()
    if path.exists():
        old = json.loads(path.read_text()).get("harness_config_hash")
        if old != cfg_hash:
            raise ConfigMismatch(
                f"{run_dir.name} was started with config {old}, this is {cfg_hash}: use a "
                f"new --run-id (results of two configs must not be mixed)"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "b0": True,
                "harness_config_hash": cfg_hash,
                "overrides": overrides(cfg),
                **(git or {}),
                "allow_dirty": allow_dirty,
                "created": time.time(),
            },
            indent=2,
            sort_keys=True,
        )
    )


# ---- command line ---------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("run", "convert"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        p.add_argument("--run-id", required=True)
        p.add_argument("--results-dir", type=Path, default=Path("results/runs"))
        if name == "run":
            p.add_argument("--subset", type=Path, required=True)
            p.add_argument("--yes", action="store_true")
            p.add_argument(
                "--print-command", action="store_true", help="show the command and stop"
            )
            p.add_argument(
                "--allow-dirty",
                action="store_true",
                help="development run on an uncommitted tree",
            )
    return ap


def main(
    argv: list[str] | None = None,
    *,
    run=subprocess.run,
    newest_revision=resolve_revision,
    git_meta=None,
) -> int:
    from dotenv import load_dotenv

    from harness.cli import _git_meta
    from harness.config import load_config

    load_dotenv(".env")
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    run_dir = args.results_dir / args.run_id
    if args.action == "convert":
        ids = convert(run_dir, cfg, args.run_id)
        print(f"converted {len(ids)} task(s) -> {run_dir}")
        return 0

    ids = read_ids(args.subset)
    cmd = command(
        cfg, ids, run_dir / "mini", Path(sys.executable).with_name("mini-extra")
    )
    if args.print_command:
        print(" ".join(cmd))
        return 0
    meta = json.loads((SUBSET_DIR / META_NAME).read_text())
    check_dataset_revision(
        meta, newest_revision(meta["dataset_id"], None)
    )  # mini loads the newest
    print(f"B0 {args.run_id}: {len(ids)} task(s), step_limit {cfg.limits.max_steps}, cost_limit ${cfg.limits.max_cost_per_task_usd}, worst case ${len(ids) * cfg.limits.max_cost_per_task_usd:.2f}")  # fmt: skip
    git = (git_meta or _git_meta)()
    if git["git_dirty"] and not args.allow_dirty:
        raise SystemExit(
            "the working tree has uncommitted changes: commit first, or pass --allow-dirty for a development run"
        )
    check_meta(run_dir, cfg, git, allow_dirty=args.allow_dirty)
    if should_ask(len(ids), args.yes) and input("go on? [y/N] ").strip().lower() != "y":
        return 2
    done = run(
        cmd, env=environment(dict(os.environ)), check=False
    )  # a fixed command we build
    if done.returncode != 0:
        print(
            f"mini-extra exited with {done.returncode}; converting what it wrote",
            file=sys.stderr,
        )
    converted = convert(run_dir, cfg, args.run_id)
    print(f"converted {len(converted)} task(s) -> {run_dir}")
    return 0 if done.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
