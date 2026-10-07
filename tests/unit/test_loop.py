import json
from itertools import pairwise

import pytest
from fakes import FakeSandbox, ScriptedLLM

from harness.agent.loop import run_task
from harness.agent.state import FailReason, Task
from harness.config import AgentConfig, ContextConfig, LimitsConfig, LLMConfig
from harness.llm.types import LLMError, LLMResponse, ToolCall, Usage
from harness.sandbox.base import ExecResult
from harness.security.guard import Decision, Guard
from harness.tracing.tracer import MemoryTracer

TASK = Task("toy__toy-1", "add() returns the wrong result")


def cfg(**kw):
    kw.setdefault(
        "llm", LLMConfig(model="deepseek/deepseek-flash", thinking="disabled")
    )
    kw.setdefault(
        "limits",
        LimitsConfig(max_steps=10, max_cost_per_task_usd=0.5, max_wall_seconds=1000),
    )
    return AgentConfig(**kw)


def call(name, args=None, id_="c1", raw=None):
    return ToolCall(id_, name, raw if raw is not None else json.dumps(args or {}))


def R(*calls, content="", finish="tool_calls", usage=None, reasoning=None):
    return LLMResponse(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tuple(calls),
        finish_reason=finish,
        usage=usage
        or Usage(
            prompt_tokens=1000,
            completion_tokens=50,
            cached_tokens=800,
            reasoning_tokens=10,
        ),
        model="deepseek-flash",
    )


def sandbox(**kw):
    return FakeSandbox(handler=lambda c: ExecResult(0, f"ran: {c}"), diff="DIFF", **kw)


async def run(script, *, config=None, sb=None, clock=None, guard=None):
    llm = ScriptedLLM(script)
    tracer = MemoryTracer()
    kwargs = {"clock": clock} if clock else {}
    if guard:
        kwargs["guard"] = guard
    result = await run_task(
        TASK, config or cfg(), sb or sandbox(), llm, tracer, **kwargs
    )
    return result, llm, tracer


def assert_paired(messages):
    """Every tool_call is answered by exactly one tool message, in order; no stray tool messages."""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            replies = messages[i + 1 : i + 1 + len(ids)]
            assert [r["role"] for r in replies] == ["tool"] * len(ids), (
                f"unanswered calls at {i}"
            )
            assert [r["tool_call_id"] for r in replies] == ids
            i += len(ids)
        else:
            assert m["role"] != "tool", f"stray tool message at {i}"
        i += 1


# ---- the happy path --------------------------------------------------------------------
async def test_run_then_submit():
    result, llm, tracer = await run(
        [R(call("bash", {"command": "ls"})), R(call("submit", id_="c2"))]
    )
    assert result.submitted and result.fail_reason is None and result.patch == "DIFF"
    assert result.metrics["steps"] == 2
    msgs = llm.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert (
        "ran: ls" in msgs[3]["content"] and "[step 1/10 | cost $" in msgs[3]["content"]
    )
    assert_paired(msgs)
    types = [s["type"] for s in tracer.spans]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert types.count("tool_call") == 2 and types.count("guard_decision") == 2


async def test_run_start_records_what_makes_the_run_reproducible():
    _, _, tracer = await run([R(call("submit"))])
    start = tracer.of("run_start")[0]
    assert (
        start["model"] == "deepseek/deepseek-flash" and start["thinking"] == "disabled"
    )
    assert (start["max_tokens"], start["timeout_s"]) == (16384, 300.0)
    assert start["tools"] == ["bash", "search", "str_replace", "submit", "todo", "view"]
    assert len(start["config_hash"]) == 12 and len(start["prices_hash"]) == 12


async def test_run_end_carries_the_metrics():
    result, _, tracer = await run([R(call("submit"))])
    end = tracer.of("run_end")[0]
    assert end["submitted"] and end["fail_reason"] is None and end["patch_chars"] == 4
    assert (
        end["steps"],
        end["tokens_in"],
        end["tokens_cached"],
        end["tokens_out"],
    ) == (1, 1000, 800, 50)
    assert (
        end["cost_usd"] == pytest.approx(result.metrics["cost_usd"])
        and end["cost_usd"] > 0
    )


async def test_reasoning_content_is_kept_in_the_history():
    _, llm, _ = await run(
        [
            R(call("bash", {"command": "ls"}), reasoning="think"),
            R(call("submit", id_="c2")),
        ]
    )
    assistant = llm.calls[1]["messages"][2]
    assert (
        assistant["reasoning_content"] == "think"
        and assistant["tool_calls"][0]["id"] == "c1"
    )


# ---- the history only grows ------------------------------------------------------------
async def test_each_request_extends_the_previous_one_without_changing_it():
    script = [
        R(call("bash", {"command": "ls"})),
        R(call("bash", {"command": "pwd"}, "c2")),
        R(call("submit", id_="c3")),
    ]
    _, llm, _ = await run(script)
    sent = [c["messages"] for c in llm.calls]
    for earlier, later in pairwise(sent):
        assert later[: len(earlier)] == earlier  # nothing already sent was rewritten
    # and the tool list never changes either (prefix stability for the KV cache)
    assert llm.calls[0]["tools"] == llm.calls[1]["tools"]
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == sorted(
        t["function"]["name"] for t in llm.calls[0]["tools"]
    )


async def test_the_step_note_is_deterministic_and_has_no_wall_clock_values():
    _, l1, _ = await run(
        [R(call("bash", {"command": "ls"})), R(call("submit", id_="c2"))]
    )
    _, l2, _ = await run(
        [R(call("bash", {"command": "ls"})), R(call("submit", id_="c2"))]
    )
    assert l1.calls[1]["messages"] == l2.calls[1]["messages"]
    assert l1.calls[1]["messages"][3]["content"].endswith(
        "[step 1/10 | cost $0.0001 of $0.50]"
    )


# ---- several calls in one response -----------------------------------------------------
async def test_multiple_calls_run_in_order_each_gets_one_reply_and_only_the_last_has_the_note():
    sb = sandbox()
    script = [
        R(call("bash", {"command": "a"}, "c1"), call("bash", {"command": "b"}, "c2")),
        R(call("submit", id_="c3")),
    ]
    _, llm, _ = await run(script, sb=sb)
    assert [c[0] for c in sb.exec_calls] == ["a", "b"]
    msgs = llm.calls[1]["messages"]
    assert_paired(msgs)
    assert "[step" not in msgs[3]["content"] and "[step 1/10" in msgs[4]["content"]


async def test_calls_after_a_submit_are_answered_but_not_executed():
    sb = sandbox()
    script = [R(call("submit", id_="c1"), call("bash", {"command": "rm -rf x"}, "c2"))]
    result, _, tracer = await run(script, sb=sb)
    assert result.submitted and sb.exec_calls == []
    assert_paired(
        result.messages
    )  # the stored history is valid even though the run ended here
    assert result.messages[-1]["content"].startswith(
        "Not executed: the task was already submitted"
    )
    assert len(tracer.of("tool_call")) == 2


# ---- format errors ---------------------------------------------------------------------
async def test_unknown_tool_and_bad_json_are_answered_with_errors_and_the_run_continues():
    script = [
        R(call("nope", id_="c1")),
        R(call("bash", raw="{not json", id_="c2")),
        R(call("submit", id_="c3")),
    ]
    result, llm, _ = await run(script)
    assert result.submitted and result.metrics["format_errors"] == 2
    msgs = llm.calls[2]["messages"]
    assert (
        "unknown tool 'nope'" in msgs[3]["content"]
        and "not valid JSON" in msgs[5]["content"]
    )
    assert (
        msgs[4]["tool_calls"][0]["function"]["arguments"] == "{not json"
    )  # stored as sent
    assert_paired(msgs)


async def test_three_format_errors_in_a_row_end_the_run():
    script = [R(call("nope", id_=f"c{i}")) for i in range(5)]
    result, llm, _ = await run(script)
    assert result.fail_reason == FailReason.format_error and len(llm.calls) == 3
    assert not result.submitted


async def test_a_clean_step_resets_the_consecutive_count():
    script = [
        R(call("nope", id_="c1")),
        R(call("nope", id_="c2")),
        R(call("bash", {"command": "ls"}, "c3")),
        R(call("nope", id_="c4")),
        R(call("nope", id_="c5")),
        R(call("submit", id_="c6")),
    ]
    result, _, _ = await run(script)
    assert result.submitted and result.metrics["format_errors"] == 4


async def test_a_reply_without_a_tool_call_is_a_format_error_with_a_nudge():
    script = [
        R(content="I think it is fine.", finish="stop"),
        R(call("submit", id_="c2")),
    ]
    result, llm, _ = await run(script)
    assert result.submitted and result.metrics["format_errors"] == 1
    msgs = llm.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "no tool call" in msgs[3]["content"] and "[step 1/10" in msgs[3]["content"]


async def test_three_text_only_replies_end_the_run():
    result, _, _ = await run([R(content="x", finish="stop")] * 5)
    assert result.fail_reason == FailReason.format_error


# ---- truncation ------------------------------------------------------------------------
async def test_a_truncated_response_is_discarded_not_executed_and_the_model_is_told():
    sb = sandbox()
    cut = R(call("bash", raw='{"command": "echo aaaa'), finish="length", content="")
    result, llm, tracer = await run([cut, R(call("submit", id_="c2"))], sb=sb)
    assert result.submitted and sb.exec_calls == []
    msgs = llm.calls[1]["messages"]
    assert [m["role"] for m in msgs] == [
        "system",
        "user",
        "user",
    ]  # the cut turn is not stored
    assert (
        "output length limit (16384 tokens)" in msgs[2]["content"]
        and "[step 1/10" in msgs[2]["content"]
    )
    assert (
        result.metrics["truncated_outputs"] == 1
        and result.metrics["format_errors"] == 0
    )
    assert (
        result.metrics["steps"] == 2 and result.metrics["tokens_in"] == 2000
    )  # its tokens still count
    assert tracer.of("truncation")[0]["tool_calls"] == ["bash"]


async def test_three_truncations_in_a_row_end_the_run_as_a_format_error_with_a_detail():
    cut = R(finish="length")
    result, llm, _ = await run([cut, cut, cut, cut])
    assert (
        result.fail_reason == FailReason.format_error
        and result.fail_detail == "output_truncated"
    )
    assert len(llm.calls) == 3


async def test_a_normal_response_resets_the_truncation_count():
    cut = R(finish="length")
    script = [
        cut,
        cut,
        R(call("bash", {"command": "ls"})),
        cut,
        cut,
        R(call("submit", id_="c2")),
    ]
    result, _, _ = await run(script)
    assert result.submitted and result.metrics["truncated_outputs"] == 4


# ---- limits ----------------------------------------------------------------------------
async def test_step_limit():
    script = [R(call("bash", {"command": "ls"}, f"c{i}")) for i in range(10)]
    result, llm, _ = await run(script, config=cfg(limits=LimitsConfig(max_steps=3)))
    assert result.fail_reason == FailReason.step_limit and len(llm.calls) == 3


async def test_cost_limit_counts_the_cost_of_successful_calls():
    big = Usage(
        prompt_tokens=2_000_000, completion_tokens=0, cached_tokens=0
    )  # $0.60 at flash prices
    result, llm, _ = await run(
        [R(call("bash", {"command": "ls"}), usage=big), R(call("submit", id_="c2"))],
        config=cfg(limits=LimitsConfig(max_steps=10, max_cost_per_task_usd=0.5)),
    )
    assert result.fail_reason == FailReason.cost_limit and len(llm.calls) == 1


async def test_the_budget_guard_includes_the_estimated_waste_but_the_reported_cost_does_not():
    llm = ScriptedLLM([R(call("submit"))])
    llm.stats.wasted_prompt_tokens_est = (
        10_000_000  # $3 of failed attempts at flash prices
    )
    result = await run_task(TASK, cfg(), sandbox(), llm, MemoryTracer())
    assert result.fail_reason == FailReason.cost_limit and llm.calls == []
    assert (
        result.metrics["cost_usd"] == 0
        and result.metrics["wasted_prompt_tokens_est"] == 10_000_000
    )


async def test_wall_clock_limit_is_checked_before_a_call_and_passed_as_a_deadline():
    now = [0.0]
    cfg_ = cfg(limits=LimitsConfig(max_steps=10, max_wall_seconds=100))

    def jump(messages):
        now[0] = 150.0
        return R(call("bash", {"command": "ls"}))

    result, llm, _ = await run(
        [jump, R(call("submit", id_="c2"))], config=cfg_, clock=lambda: now[0]
    )
    assert result.fail_reason == FailReason.time_limit and len(llm.calls) == 1
    assert llm.calls[0]["deadline"] == 100.0  # start (0) + max_wall_seconds


# ---- errors from the LLM client --------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("context_overflow", FailReason.context_overflow),
        ("time_budget", FailReason.time_limit),
        ("server_error", FailReason.env_error),
        ("client_error", FailReason.env_error),
        ("rate_limit", FailReason.env_error),
    ],
)
async def test_llm_errors_map_to_fail_reasons(kind, reason):
    result, _, _ = await run([LLMError(kind, "boom")])
    assert (
        result.fail_reason == reason
        and kind in result.fail_detail
        and not result.submitted
    )


async def test_a_content_filter_stop_is_an_env_error():
    result, _, _ = await run([R(finish="content_filter")])
    assert (
        result.fail_reason == FailReason.env_error
        and "content_filter" in result.fail_detail
    )


async def test_context_overflow_for_the_none_strategy_uses_the_previous_prompt_size():
    huge = Usage(prompt_tokens=200_000, completion_tokens=10)
    result, llm, _ = await run(
        [R(call("bash", {"command": "ls"}), usage=huge), R(call("submit", id_="c2"))],
        config=cfg(context=ContextConfig(strategy="none", budget_tokens=128_000)),
    )
    assert result.fail_reason == FailReason.context_overflow and len(llm.calls) == 1


# ---- guard, sandbox failure, bugs ------------------------------------------------------
class Deny(Guard):
    def check(self, call, state):
        return (
            Decision("deny", "R1", "no bash today")
            if call.name == "bash"
            else Decision()
        )


async def test_a_denied_call_is_answered_not_executed_and_traced():
    sb = sandbox()
    result, llm, tracer = await run(
        [R(call("bash", {"command": "ls"})), R(call("submit", id_="c2"))],
        sb=sb,
        guard=Deny(),
    )
    assert result.submitted and sb.exec_calls == []
    assert (
        "Blocked by the permission layer (R1): no bash today"
        in llm.calls[1]["messages"][3]["content"]
    )
    assert tracer.of("guard_decision")[0]["action"] == "deny"


async def test_a_broken_sandbox_does_not_hide_how_the_run_ended():
    class Broken(FakeSandbox):
        async def git_diff(self):
            raise RuntimeError("container gone")

    result, _, tracer = await run([R(call("submit"))], sb=Broken())
    assert (
        result.patch == ""
        and result.fail_reason == FailReason.env_error
        and "container gone" in result.fail_detail
    )
    assert tracer.of("run_end")


async def test_an_unexpected_exception_still_writes_run_end_and_propagates():
    class Bug(ScriptedLLM):
        async def complete(self, *a, **k):
            raise KeyError("oops")

    tracer = MemoryTracer()
    with pytest.raises(KeyError):
        await run_task(TASK, cfg(), sandbox(), Bug([]), tracer)
    end = tracer.of("run_end")[0]
    assert end["fail_reason"] == "env_error" and "KeyError" in end["fail_detail"]


# ---- configuration reaches the loop ----------------------------------------------------
async def test_bash_only_action_space_offers_only_bash_and_submit():
    _, llm, _ = await run([R(call("submit"))], config=cfg(action_space="bash_only"))
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == ["bash", "submit"]


async def test_the_system_prompt_mentions_planning_only_when_it_is_on_and_has_no_time():
    _, on, _ = await run([R(call("submit"))], config=cfg(planning="on"))
    _, off, _ = await run([R(call("submit"))], config=cfg(planning="off"))
    assert "todo tool" in on.calls[0]["messages"][0]["content"]
    assert "todo tool" not in off.calls[0]["messages"][0]["content"]
    assert "202" not in on.calls[0]["messages"][0]["content"]  # no year, no date
