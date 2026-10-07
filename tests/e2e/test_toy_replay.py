"""End to end on the toy repository, answered from a recording (no network, no LLM cost).

Recorded once with a real model:
    uv run harness run --config tests/fixtures/toy_config.yaml --repo tests/fixtures/toy_repo \
        --task "<TASK below>" --record tests/fixtures/replays/toy_fix.jsonl
Anything that changes the requests (prompts, tool schemas, the config) makes the replay miss
on purpose: re-record, and say why in the commit. Recordings must be made with a config that
sets `llm.model` itself: the key uses `cfg.model or $MODEL_MAIN`, and CI has no .env.
"""

import json
import os
from pathlib import Path

import pytest
from conftest import TOY_IMAGE

from harness.agent.loop import run_task
from harness.agent.state import Task
from harness.cli import main
from harness.config import load_config
from harness.llm.replay import ReplayClient, ReplayMiss
from harness.sandbox.docker_sandbox import DockerSandbox
from harness.tracing.tracer import MemoryTracer

ROOT = Path(__file__).resolve().parents[2]
TOY_REPO = ROOT / "tests/fixtures/toy_repo"
CONFIG = ROOT / "tests/fixtures/toy_config.yaml"
RECORDING = ROOT / "tests/fixtures/replays/toy_fix.jsonl"
TASK = "The unit tests of this library fail: add() returns a wrong result. Fix the bug in the library code."

pytestmark = pytest.mark.docker


async def test_the_recorded_run_fixes_the_toy_repo(docker_client):
    cfg = load_config(CONFIG)
    llm = ReplayClient(cfg.llm, RECORDING)
    tracer = MemoryTracer()
    # The starting state must be exactly the one that was recorded (running the tests first
    # would leave __pycache__ behind and change what the agent sees), so check it elsewhere.
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as untouched:
        await untouched.load_repo(TOY_REPO)
        before = await untouched.exec("python -m unittest discover -s tests")
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        await sb.load_repo(TOY_REPO)
        result = await run_task(Task("toy_repo", TASK), cfg, sb, llm, tracer)
        after = await sb.exec("python -m unittest discover -s tests")

    # the outcome that matters: the project's own failing test now passes
    assert before.exit_code != 0
    assert after.exit_code == 0, after.output
    assert result.submitted and result.fail_reason is None
    assert "calc/ops.py" in result.patch and "+    return a + b" in result.patch

    # nothing was improvised: every recorded response was used, no network call was made
    assert llm.stats.calls == result.metrics["steps"] == 5
    assert (
        result.metrics["format_errors"] == 0
        and result.metrics["truncated_outputs"] == 0
    )
    end = tracer.of("run_end")[0]
    assert end["submitted"] and end["tokens_in"] > 0 and end["cost_usd"] > 0
    assert tracer.of("run_start")[0]["model"] == "deepseek/deepseek-flash"


@pytest.mark.skipif(
    not os.environ.get("HARNESS_STRICT_REPLAY"),
    reason="opt-in: the order of `find`/`ls` output depends on the host file system, so this "
    "can fail on any machine other than the one that recorded. Run with HARNESS_STRICT_REPLAY=1.",
)
async def test_on_the_recording_machine_the_replayed_tool_outputs_match_exactly(
    docker_client,
):
    """Stricter than CI can be. Run it locally, after a refactor, to prove the harness still
    produces the very tool outputs that were recorded."""
    cfg = load_config(CONFIG)
    llm = ReplayClient(cfg.llm, RECORDING, strict_tool_outputs=True)
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        await sb.load_repo(TOY_REPO)
        result = await run_task(Task("toy_repo", TASK), cfg, sb, llm, MemoryTracer())
    assert result.submitted


async def test_the_replay_is_strict_a_changed_request_is_an_error(docker_client):
    cfg = load_config(CONFIG)
    tracer = MemoryTracer()
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        await sb.load_repo(TOY_REPO)
        with pytest.raises(ReplayMiss, match="message 1 differs"):
            await run_task(
                Task("toy_repo", TASK + " (reworded)"),
                cfg,
                sb,
                ReplayClient(cfg.llm, RECORDING),
                tracer,
            )
    assert (
        tracer.of("run_end")[0]["fail_reason"] == "env_error"
    )  # still a complete trajectory


def test_the_cli_replays_the_recording_and_writes_patch_and_trajectory(
    docker_client, tmp_path, capsys
):
    code = main(
        [
            "run",
            "--config", str(CONFIG),
            "--repo", str(TOY_REPO),
            "--task", TASK,
            "--replay", str(RECORDING),
            "--results-dir", str(tmp_path),
            "--run-id", "r1",
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "toy_repo: submitted" in out and "steps=5" in out
    assert "+    return a + b" in (tmp_path / "r1/patches/toy_repo.diff").read_text()
    spans = [
        json.loads(x)
        for x in (tmp_path / "r1/trajectories/toy_repo.jsonl").read_text().splitlines()
    ]
    assert spans[0]["type"] == "run_start" and spans[-1]["type"] == "run_end"
    assert {"message", "tool_call", "guard_decision"} <= {s["type"] for s in spans}
    assert all(s["run_id"] == "r1" and s["instance_id"] == "toy_repo" for s in spans)
