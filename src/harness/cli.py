"""`harness run`: wire a config, a sandbox, an LLM client and a tracer together and run one task."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SUBSETS_META = Path("configs/subsets/subsets_meta.json")
MAIN_CONFIG = Path("configs/default.yaml")
DEFAULT_LOCAL_IMAGE = "harness-toy:latest"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="harness-lab")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the agent on one task")
    run.add_argument(
        "--config", type=Path, help="YAML config (default: built-in dev defaults)"
    )
    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--repo",
        type=Path,
        help="a local repository directory (copied into the sandbox)",
    )
    src.add_argument(
        "--instance", help="a SWE-bench Verified instance_id (its image must be pulled)"
    )
    run.add_argument("--task", help="the task text (required with --repo)")
    run.add_argument(
        "--image", default=DEFAULT_LOCAL_IMAGE, help="sandbox image for --repo"
    )
    run.add_argument("--run-id", help="default: a timestamp")
    run.add_argument("--results-dir", type=Path, default=Path("results/runs"))
    mode = run.add_mutually_exclusive_group()
    mode.add_argument(
        "--record", type=Path, help="also record every LLM response to this JSONL"
    )
    mode.add_argument(
        "--replay", type=Path, help="answer from this recording; no network"
    )
    return p


def describe_config(cfg: Any, path: Path | None) -> list[str]:
    """Lines printed at startup: which config, and how it differs from the main one (M0)."""
    from harness.config import config_diff, load_config

    name = str(path) if path else "built-in development defaults"
    lines = [f"config: {name} (hash {cfg.config_hash()})"]
    if not MAIN_CONFIG.exists() or (path and path.resolve() == MAIN_CONFIG.resolve()):
        return lines
    diff = config_diff(load_config(MAIN_CONFIG), cfg)
    if not diff:
        return lines + [f"  identical to {MAIN_CONFIG}"]
    return (
        lines
        + [f"  differs from {MAIN_CONFIG}:"]
        + [f"    {key}: {old!r} -> {new!r}" for key, old, new in diff]
    )


def _git_meta() -> dict[str, Any]:
    # Trusted host commands (not model output): which code produced this run.
    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""

    return {
        "git_commit": git("rev-parse", "HEAD") or None,
        "git_dirty": bool(git("status", "--porcelain")),
    }


def load_swebench_task(instance_id: str) -> tuple[Any, str]:
    """(Task, image) for one instance, from the dataset revision recorded in subsets_meta.json."""
    from datasets import load_dataset

    from harness.agent.state import Task

    meta = json.loads(SUBSETS_META.read_text())
    ds = load_dataset(
        meta["dataset_id"], split=meta["split"], revision=meta["revision"]
    )
    rows = ds.filter(lambda r: r["instance_id"] == instance_id)
    if len(rows) != 1:
        raise SystemExit(
            f"{instance_id} not found in {meta['dataset_id']}@{meta['revision'][:12]}"
        )
    row = rows[0]
    return Task(instance_id, row["problem_statement"]), row["image"]


async def _run(args: argparse.Namespace) -> int:
    import docker
    from dotenv import load_dotenv

    from harness.agent.loop import run_task
    from harness.agent.state import Task
    from harness.config import load_config, summary_llm_config
    from harness.context.manager import NEEDS_SUMMARIZER
    from harness.llm.client import LiteLLMClient
    from harness.llm.replay import RecordingClient, ReplayClient
    from harness.sandbox.docker_sandbox import DockerSandbox
    from harness.tracing.tracer import JsonlTracer

    load_dotenv(".env")
    cfg = load_config(args.config)
    print("\n".join(describe_config(cfg, args.config)))
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")

    if args.repo:
        if not args.task:
            raise SystemExit("--task is required with --repo")
        task, image = Task(args.repo.resolve().name, args.task), args.image
    else:
        task, image = load_swebench_task(args.instance)

    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        hint = (
            "docker build -t harness-toy tests/fixtures/toy_image"
            if args.repo
            else "pull_images.py"
        )
        raise SystemExit(f"image {image} is not available locally ({hint})") from None

    run_dir = args.results_dir / run_id
    tracer = JsonlTracer(
        run_dir / "trajectories" / f"{task.instance_id}.jsonl",
        run_id=run_id,
        instance_id=task.instance_id,
    )
    try:
        summarizer = None
        summary_cfg = (
            summary_llm_config(cfg)
            if cfg.context.strategy in NEEDS_SUMMARIZER
            else None
        )
        if args.replay:
            llm = ReplayClient(cfg.llm, args.replay)
            if summary_cfg:
                summarizer = ReplayClient(summary_cfg, args.replay)
        else:
            llm = LiteLLMClient(cfg.llm, tracer)
            if summary_cfg:
                summarizer = LiteLLMClient(summary_cfg, tracer, purpose="summary")
            if args.record:
                llm = RecordingClient(llm, cfg.llm, args.record)
                if summarizer:
                    summarizer = RecordingClient(summarizer, summary_cfg, args.record)
        sbx = cfg.sandbox
        async with DockerSandbox(
            image,
            docker_client=client,
            network_mode=sbx.network_mode,
            mem_limit=sbx.mem_limit,
            cpus=sbx.cpus,
            pids_limit=sbx.pids_limit,
        ) as sandbox:
            if args.repo:
                await sandbox.load_repo(args.repo)
            result = await run_task(
                task,
                cfg,
                sandbox,
                llm,
                tracer,
                summarizer=summarizer,
                meta={"run_id": run_id, **_git_meta()},
            )
    finally:
        tracer.close()

    patch_path = run_dir / "patches" / f"{task.instance_id}.diff"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(result.patch)

    m = result.metrics
    hit = m["tokens_cached"] / m["tokens_in"] if m["tokens_in"] else 0.0
    status = "submitted" if result.submitted else f"FAILED: {result.fail_reason}"
    print(
        f"{task.instance_id}: {status}"
        + (f" ({result.fail_detail})" if result.fail_detail else "")
    )
    print(
        f"  steps={m['steps']}  tokens in/cached/out={m['tokens_in']}/{m['tokens_cached']}/{m['tokens_out']}"
        f" (cache hit {hit:.1%}, reasoning {m['tokens_reasoning']})"
    )
    print(
        f"  cost=${m['cost_usd']:.4f} (fixed standard price)  failed_attempts={m['failed_attempts']}"
        f"  format_errors={m['format_errors']}  truncated={m['truncated_outputs']}  wall={m['wall_seconds']:.0f}s"
    )
    if m["compactions"]:
        print(
            f"  compactions={m['compactions']} (cost ${m['compaction_cost_usd']:.4f}, "
            f"summary tokens in/out {m['compaction_tokens_in']}/{m['compaction_tokens_out']})"
        )
    print(f"  patch: {len(result.patch)} chars -> {patch_path}")
    print(f"  trajectory: {tracer.path}")
    return 0 if result.submitted else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
