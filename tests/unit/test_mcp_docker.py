"""An MCP server inside a real sandbox container, end to end through the agent loop and the
LiteLLM client against the local fake model server (no API cost)."""

from pathlib import Path

import pytest
from fakes import FakeLLMServer, Step, ok_body, tool_call

from harness.agent.loop import run_task
from harness.agent.state import Task
from harness.config import AgentConfig, GuardConfig, LimitsConfig, LLMConfig, MCPConfig
from harness.llm.client import LiteLLMClient
from harness.sandbox.docker_sandbox import DockerSandbox
from harness.tracing.tracer import MemoryTracer

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[2]
TOY_IMAGE = "harness-toy:latest"


async def test_an_mcp_tool_runs_in_the_container_and_its_result_reaches_the_model(
    docker_client, monkeypatch
):
    cfg = AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        guard=GuardConfig(enabled=True),
        limits=LimitsConfig(
            max_steps=5, max_cost_per_task_usd=0.5, max_wall_seconds=120
        ),
        mcp=MCPConfig(
            servers=[
                {
                    "name": "fx",
                    "command": ["python3", "/tmp/mcp_server.py"],
                    "allow": ["echo"],
                }
            ]
        ),
    )
    script = [
        Step(body=ok_body(tool_calls=[tool_call("c1", "mcp__fx__echo", '{"text": "inside"}')],
                          finish_reason="tool_calls")),
        Step(body=ok_body(tool_calls=[tool_call("c2", "submit", "{}")],
                          finish_reason="tool_calls")),
    ]  # fmt: skip
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        # outside /testbed, so it never shows in the patch
        await sb.write_file(
            "/tmp/mcp_server.py", (ROOT / "tests/fixtures/mcp_server.py").read_text()
        )
        with FakeLLMServer(script) as server:
            monkeypatch.setenv("DEEPSEEK_API_BASE", server.url)
            tracer = MemoryTracer()
            llm = LiteLLMClient(cfg.llm, tracer)
            result = await run_task(
                Task("toy", "use the echo tool"), cfg, sb, llm, tracer
            )
            requests = [r["body"] for r in server.requests]
        # the server is gone with the task: nothing of it left running in the container
        _, ps = await sb.exec("ps -eo args")
    assert result.submitted and result.patch == ""
    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert {"mcp__fx__echo", "mcp__fx__fail", "mcp__fx__math_add"} <= set(names)
    assert names == sorted(names)
    reply = requests[1]["messages"][-1]
    assert reply["role"] == "tool" and reply["content"].startswith("inside")
    assert "mcp_server.py" not in ps


async def test_a_server_missing_from_the_image_ends_the_task_cleanly(docker_client):
    # the likeliest real mistake: the configured command is not in the sandbox image
    import time

    from fakes import ScriptedLLM

    cfg = AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        limits=LimitsConfig(
            max_steps=5, max_cost_per_task_usd=0.5, max_wall_seconds=120
        ),
        mcp=MCPConfig(
            servers=[
                {"name": "gone", "command": ["no-such-binary"], "start_timeout_s": 3}
            ]
        ),
    )
    tracer, llm = MemoryTracer(), ScriptedLLM([])
    started = time.monotonic()
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        result = await run_task(Task("toy", "x"), cfg, sb, llm, tracer)
    assert time.monotonic() - started < 20
    assert result.fail_reason.value == "env_error"
    assert "gone did not start" in result.fail_detail
    assert tracer.of("run_start") and tracer.of("run_end") and llm.calls == []
