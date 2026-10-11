"""The `step` that the tracer writes on messages and tool calls (what aggregate.py splits a
run into "before" and "after" a compaction on)."""

import json

from fakes import FakeSandbox, ScriptedLLM

from harness.agent.loop import run_task
from harness.agent.state import Task
from harness.config import AgentConfig, LimitsConfig, LLMConfig
from harness.llm.types import LLMResponse, ToolCall, Usage
from harness.sandbox.base import ExecResult
from harness.tracing.tracer import MemoryTracer

TASK = Task("toy__toy-1", "add() returns the wrong result")


def reply(name, args, id_):
    return LLMResponse(
        content="",
        reasoning_content=None,
        tool_calls=(ToolCall(id_, name, json.dumps(args)),),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100, completion_tokens=20),
        model="deepseek-flash",
    )


async def test_messages_and_tool_calls_carry_the_step_they_belong_to():
    cfg = AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        limits=LimitsConfig(max_steps=30),
    )
    main = ScriptedLLM(
        [
            reply("bash", {"command": "ls"}, "c1"),
            reply("bash", {"command": "pwd"}, "c2"),
            reply("submit", {}, "c3"),
        ]
    )
    sandbox = FakeSandbox(handler=lambda c: ExecResult(0, "ok"), diff="DIFF")
    tracer = MemoryTracer()
    await run_task(TASK, cfg, sandbox, main, tracer)
    assert [(m["role"], m["step"]) for m in tracer.of("message")] == [
        ("system", 0), ("user", 0),
        ("assistant", 1), ("tool", 1),
        ("assistant", 2), ("tool", 2),
        ("assistant", 3), ("tool", 3),
    ]  # fmt: skip
    assert [c["step"] for c in tracer.of("tool_call")] == [1, 2, 3]
