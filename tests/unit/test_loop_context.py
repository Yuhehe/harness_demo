"""The context manager inside the agent loop: cost, spans, prefix stability, the guard span."""

import json
from itertools import pairwise

import pytest
from fakes import FakeSandbox, ScriptedLLM

from harness.agent.loop import run_task
from harness.agent.state import FailReason, Task
from harness.config import (
    AgentConfig,
    ContextConfig,
    GuardConfig,
    LimitsConfig,
    LLMConfig,
)
from harness.llm import cost
from harness.llm.types import LLMResponse, ToolCall, Usage
from harness.sandbox.base import ExecResult
from harness.tracing.tracer import MemoryTracer

TASK = Task("toy__toy-1", "add() returns the wrong result")
MODEL = "deepseek/deepseek-flash"


def cfg(strategy="hybrid", budget=4000, **kw):
    return AgentConfig(
        llm=LLMConfig(model=MODEL, thinking="disabled"),
        context=ContextConfig(
            strategy=strategy,
            budget_tokens=budget,
            keep_recent_min_turns=2,
            keep_recent_ratio=0.01,
            min_clear_chars=100,
            summary_model=MODEL,
        ),
        limits=LimitsConfig(
            max_steps=30, max_cost_per_task_usd=0.5, max_wall_seconds=1000
        ),
        guard=GuardConfig(enabled=kw.pop("guard", False)),
        **kw,
    )


def reply(name, args, id_, prompt=100):
    return LLMResponse(
        content="",
        reasoning_content=None,
        tool_calls=(ToolCall(id_, name, json.dumps(args)),),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=prompt, completion_tokens=20, cached_tokens=0),
        model="deepseek-flash",
    )


def summary(text="## Tried so far\nran things", prompt=3000, completion=250):
    return LLMResponse(
        content=text,
        reasoning_content=None,
        tool_calls=(),
        finish_reason="stop",
        usage=Usage(
            prompt_tokens=prompt, completion_tokens=completion, cached_tokens=0
        ),
        model="deepseek-flash",
    )


def big_sandbox(chars=4000):
    return FakeSandbox(
        handler=lambda c: ExecResult(0, "line\n" * (chars // 5)), diff="DIFF"
    )


# Reported prompt sizes: small, then one call above the high water mark (0.85 * 4000), then
# small again as the compacted view is sent. The estimate is anchored on these numbers.
PROMPTS = [500, 500, 500, 500, 3800, 1500, 1600]


async def test_compaction_cost_is_charged_to_the_task_and_traced():
    main = ScriptedLLM(
        [
            reply("bash", {"command": f"cmd {i}"}, f"c{i}", prompt=PROMPTS[i - 1])
            for i in range(1, 7)
        ]
        + [reply("submit", {}, "cs", prompt=PROMPTS[6])]
    )
    summarizer = ScriptedLLM([summary()])
    tracer = MemoryTracer()
    result = await run_task(
        TASK,
        cfg("summarize", budget=4000),
        big_sandbox(),
        main,
        tracer,
        summarizer=summarizer,
    )
    assert result.submitted and len(summarizer.calls) == 1

    spent = cost.cost_usd(MODEL, summary().usage)
    m = result.metrics
    assert m["compactions"] == 1 and m["compaction_cost_usd"] == pytest.approx(spent)
    main_cost = sum(cost.cost_usd(MODEL, Usage(p, 20, 0)) for p in PROMPTS)
    assert m["cost_usd"] == pytest.approx(
        main_cost + spent
    )  # cost_per_task includes compaction
    # the agent's own token metrics do not mix in the summarizer's tokens
    assert m["tokens_in"] == sum(PROMPTS)
    assert (m["compaction_tokens_in"], m["compaction_tokens_out"]) == (3000, 250)

    (span,) = tracer.of("compaction")
    assert span["strategy"] == "summarize" and span["step"] == 5
    assert span["tokens_after"] < span["tokens_before"]
    assert tracer.of("run_end")[0]["compactions"] == 1


async def test_the_budget_guard_counts_compaction_cost():
    main = ScriptedLLM(
        [
            reply("bash", {"command": f"cmd {i}"}, f"c{i}", prompt=1000 * i)
            for i in range(1, 8)
        ]
    )
    expensive = summary(prompt=10_000_000, completion=0)  # ~$3 on the cheap model
    config = cfg("summarize", budget=4000)
    result = await run_task(
        TASK,
        config,
        big_sandbox(),
        main,
        MemoryTracer(),
        summarizer=ScriptedLLM([expensive]),
    )
    assert result.fail_reason == FailReason.cost_limit and not result.submitted


async def test_a_prompt_that_cannot_be_shrunk_ends_the_run_with_context_overflow():
    big = LLMResponse(
        content="",
        reasoning_content=None,
        tool_calls=(ToolCall("c1", "bash", json.dumps({"command": "x"})),),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100, completion_tokens=20),
        model="deepseek-flash",
    )
    # one huge tool output inside the protected tail: nothing can be cleared or summarized
    result = await run_task(
        TASK,
        cfg("truncate", budget=800),
        big_sandbox(chars=8000),
        ScriptedLLM([big, big]),
        MemoryTracer(),
    )
    assert result.fail_reason == FailReason.context_overflow


async def test_consecutive_requests_in_a_real_run_share_a_byte_identical_prefix():
    n = 8
    main = ScriptedLLM(
        [reply("bash", {"command": f"cmd {i}"}, f"c{i}") for i in range(1, n)]
        + [reply("submit", {}, "cs")]
    )
    result = await run_task(
        TASK,
        cfg("hybrid", budget=1_000_000),
        big_sandbox(500),
        main,
        MemoryTracer(),
        summarizer=ScriptedLLM([]),  # never called: nothing comes near the budget
    )
    assert result.submitted and len(main.calls) == n
    dumps = [[json.dumps(m, sort_keys=True) for m in c["messages"]] for c in main.calls]
    for a, b in pairwise(dumps):
        assert b[: len(a)] == a and len(b) > len(a)
    # the tool definitions, which come first in the cached prefix, never change or reorder
    assert all(c["tools"] == main.calls[0]["tools"] for c in main.calls)
    names = [t["function"]["name"] for t in main.calls[0]["tools"]]
    assert names == sorted(names)


async def test_run_start_records_effort_and_policy_and_guard_spans_carry_the_level():
    config = AgentConfig(
        llm=LLMConfig(model=MODEL, reasoning_effort="max"),
        guard=GuardConfig(enabled=True),
        limits=LimitsConfig(max_steps=5),
    )
    main = ScriptedLLM(
        [reply("bash", {"command": "ls"}, "c1"), reply("submit", {}, "c2")]
    )
    tracer = MemoryTracer()
    await run_task(TASK, config, big_sandbox(100), main, tracer)
    (start,) = tracer.of("run_start")
    assert start["reasoning_effort"] == "max" and start["guard_enabled"] is True
    assert start["summary_model"] is None  # strategy none: no summarizer
    assert isinstance(start["policy_hash"], str) and len(start["policy_hash"]) == 12
    decisions = tracer.of("guard_decision")
    assert [(d["tool"], d["rule_id"], d["level"]) for d in decisions] == [
        ("bash", "allow.read", "read"),
        ("submit", "allow.read", "read"),
    ]


async def test_a_denied_command_in_a_real_policy_is_not_executed():
    config = AgentConfig(
        llm=LLMConfig(model=MODEL, thinking="disabled"),
        guard=GuardConfig(enabled=True),
        limits=LimitsConfig(max_steps=5),
    )
    sb = big_sandbox(100)
    main = ScriptedLLM(
        [
            reply("bash", {"command": "cat ~/.ssh/id_rsa"}, "c1"),
            reply("submit", {}, "c2"),
        ]
    )
    result = await run_task(TASK, config, sb, main, MemoryTracer())
    assert result.submitted and sb.exec_calls == []
    assert "deny.ssh_access" in main.calls[1]["messages"][3]["content"]


async def test_run_start_records_the_resolved_summary_model_and_the_deadline_reaches_it():
    main = ScriptedLLM([reply("submit", {}, "c1")])
    summarizer = ScriptedLLM([])
    tracer = MemoryTracer()
    await run_task(
        TASK, cfg("hybrid"), big_sandbox(100), main, tracer, summarizer=summarizer
    )
    assert tracer.of("run_start")[0]["summary_model"] == MODEL
