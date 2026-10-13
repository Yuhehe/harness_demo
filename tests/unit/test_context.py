"""Context strategies, the token estimator and the manager (no network, no cost)."""

import json
from itertools import pairwise

import pytest
from fakes import FakeSandbox, ScriptedLLM

from harness.agent.state import AgentState, Task
from harness.config import AgentConfig, ContextConfig, LLMConfig
from harness.context.manager import ContextManager, ContextOverflow
from harness.context.strategies.summarize import SUMMARY_PREFIX, is_summary, render
from harness.context.strategies.truncate import STUB_PREFIX
from harness.context.tokens import TokenEstimator, count_messages
from harness.llm import cost
from harness.llm.types import (
    LLMError,
    LLMResponse,
    Usage,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)
from harness.tools.base import OutputLimiter
from harness.tracing.tracer import MemoryTracer

MODEL = "deepseek/deepseek-flash"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run",
            "parameters": {"type": "object"},
        },
    }
]


def ccfg(strategy, **kw):
    kw.setdefault("budget_tokens", 3000)
    kw.setdefault("keep_recent_min_turns", 2)
    kw.setdefault("keep_recent_ratio", 0.01)
    kw.setdefault("min_clear_chars", 100)
    return ContextConfig(strategy=strategy, **kw)


def state_for(cfg: ContextConfig | None = None) -> AgentState:
    acfg = AgentConfig(llm=LLMConfig(model=MODEL, thinking="disabled"))
    s = AgentState(task=Task("t", "p"), cfg=acfg, model=MODEL)
    s.append(system_message("system"))
    s.append(user_message("fix the bug"))
    return s


def add_turn(state, i, size=2500, output=None):
    call = {
        "role": "assistant",
        "content": f"step {i}",
        "tool_calls": [
            {
                "id": f"c{i}",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": f"cmd {i}"}),
                },
            }
        ],
    }
    state.append(call)
    state.append(
        tool_message(f"c{i}", output if output is not None else ("word " * size)[:size])
    )


def summary_reply(text="## Tried so far\nlooked at things"):
    return LLMResponse(
        content=text,
        reasoning_content=None,
        tool_calls=(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=4000, completion_tokens=300, cached_tokens=0),
        model="deepseek-flash",
    )


def manager(strategy, *, llm=None, sb=None, tracer=None, **kw):
    cfg = ccfg(strategy, **kw)
    return ContextManager(
        cfg,
        model=MODEL,
        tools=TOOLS,
        sandbox=sb or FakeSandbox(),
        tracer=tracer or MemoryTracer(),
        summarizer=llm,
        summary_model=MODEL if llm else None,
    )


def paired(messages):
    """Every tool call has exactly one reply right after its assistant message."""
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            replies = [x["tool_call_id"] for x in messages[i + 1 : i + 1 + len(ids)]]
            assert replies == ids, f"call/reply pairing broken at {i}"


# ---- tokens ------------------------------------------------------------------------------
async def test_estimator_counts_tool_definitions_as_text():
    est = TokenEstimator(MODEL, TOOLS)
    assert (
        await est.tools_tokens() > 10
    )  # counted as JSON text: litellm.token_counter(tools=...) raises on the todo tool
    assert await TokenEstimator(MODEL, None).tools_tokens() == 0


async def test_anchor_makes_the_known_part_exact_and_ratio_scales_the_rest():
    est = TokenEstimator(MODEL, None)
    old = [user_message("hello " * 400)]
    new = [user_message("world " * 400)]
    local_new = count_messages(MODEL, new)
    got = await est.estimate(old + new, anchor_len=1, anchor_tokens=10_000)
    assert got == 10_000 + local_new  # ratio starts at 1.0
    await est.calibrate(old, 2 * count_messages(MODEL, old))
    assert est.ratio == pytest.approx(2.0, rel=0.02)
    got = await est.estimate(old + new, anchor_len=1, anchor_tokens=10_000)
    assert got == 10_000 + round(local_new * est.ratio)


async def test_ratio_is_clamped():
    est = TokenEstimator(MODEL, None)
    await est.calibrate([user_message("hi " * 100)], 10**9)
    assert est.ratio == 2.0


# ---- none --------------------------------------------------------------------------------
async def test_none_never_compacts_and_overflows_past_the_budget():
    s = state_for()
    mgr = manager("none", budget_tokens=1500)
    add_turn(s, 1, size=400)
    assert len(await mgr.build(s)) == 4
    add_turn(s, 2, size=10_000)
    with pytest.raises(ContextOverflow, match="budget 1500"):
        await mgr.build(s)


# ---- truncate ----------------------------------------------------------------------------
async def test_truncate_clears_the_oldest_outputs_and_keeps_every_call():
    sb, tracer = FakeSandbox(), MemoryTracer()
    s = state_for()
    mgr = manager("truncate", sb=sb, tracer=tracer)
    originals = {}
    for i in range(1, 6):
        add_turn(s, i, size=4000 + i)  # distinct sizes
        originals[f"c{i}"] = s.messages[-1]["content"]
    view = await mgr.build(s)

    paired(view)
    assert [m["role"] for m in view] == [
        m["role"] for m in s.messages
    ]  # nothing dropped
    cleared = [m for m in view if str(m.get("content", "")).startswith(STUB_PREFIX)]
    assert cleared and cleared[0]["tool_call_id"] == "c1"  # oldest first
    first = cleared[0]["content"]
    n, path = first.removeprefix(f"{STUB_PREFIX} ").split(" chars, saved at ")
    assert int(n) == len(originals["c1"])
    path = path.rstrip("]")
    assert path.startswith("/tmp/harness/ctx_") and sb.files[path] == originals["c1"]
    # the agent's own history is untouched
    assert s.messages[3]["content"] == originals["c1"]
    # the newest turns (keep_recent_min_turns=2) are never cleared
    assert (
        view[-1]["content"] == originals["c5"]
        and view[-3]["content"] == originals["c4"]
    )

    (span,) = tracer.of("compaction")
    assert span["strategy"] == "truncate" and span["actions"] == ["truncate"]
    assert span["tokens_after"] < span["tokens_before"]
    assert span["cleared_outputs"] == len(cleared)
    assert span["cost_usd"] == 0 and s.compactions == 1


async def test_truncate_stops_as_soon_as_the_low_water_mark_is_reached():
    s = state_for()
    mgr = manager("truncate", budget_tokens=3000, high_water=0.8, low_water=0.7)
    for i in range(1, 6):
        add_turn(s, i, size=3000)
    view = await mgr.build(s)
    cleared = [m for m in view if str(m.get("content", "")).startswith(STUB_PREFIX)]
    assert 0 < len(cleared) < 3  # not everything eligible: just enough


async def test_truncate_skips_short_outputs_and_reuses_the_limiters_saved_file():
    sb = FakeSandbox()
    s = state_for()
    mgr = manager("truncate", sb=sb, budget_tokens=1500)
    add_turn(s, 1, output="ok")  # too short to be worth clearing
    original = "B" * 12_000
    big, cut = await OutputLimiter(3000).apply(original, sb)  # a real limiter note
    assert cut and sb.files["/tmp/harness/out_1.txt"] == original
    add_turn(s, 2, output=big)
    for i in range(3, 6):
        add_turn(s, i, size=2500)
    view = await mgr.build(s)
    assert view[3]["content"] == "ok"
    stub = view[5]["content"]
    assert stub == f"{STUB_PREFIX} {len(big)} chars, saved at /tmp/harness/out_1.txt]"
    # the limiter's file already has the original: untouched, and no second copy is written
    assert sb.files["/tmp/harness/out_1.txt"] == original
    assert big not in sb.files.values()


async def test_text_that_only_mentions_a_saved_path_is_not_taken_for_a_saved_output():
    sb = FakeSandbox()
    s = state_for()
    mgr = manager("truncate", sb=sb, budget_tokens=1500)
    quoting = ("x " * 1500) + "see: saved at /tmp/harness/out_9.txt"
    add_turn(s, 1, output=quoting)
    for i in range(2, 6):
        add_turn(s, i, size=2500)
    view = await mgr.build(s)
    stub = view[3]["content"]
    assert stub.startswith(STUB_PREFIX) and "out_9.txt" not in stub
    (path,) = [p for p in sb.files if p.startswith("/tmp/harness/ctx_")][:1]
    assert sb.files[path] == quoting  # written, because it was never saved before


async def test_nothing_to_clear_in_the_protected_tail_means_overflow():
    s = state_for()
    mgr = manager("truncate", budget_tokens=1000, keep_recent_min_turns=6)
    for i in range(1, 4):
        add_turn(s, i, size=2500)
    with pytest.raises(ContextOverflow, match="no room"):
        await mgr.build(s)


# ---- summarize ---------------------------------------------------------------------------
async def test_summarize_replaces_the_middle_and_charges_the_summary_call():
    llm, tracer = ScriptedLLM([summary_reply()]), MemoryTracer()
    s = state_for()
    mgr = manager("summarize", llm=llm, tracer=tracer)
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    view = await mgr.build(s)

    paired(view)
    assert view[0]["role"] == "system" and view[1]["content"] == "fix the bug"
    assert is_summary(view[2]) and view[2]["content"].startswith(SUMMARY_PREFIX)
    assert "looked at things" in view[2]["content"]
    assert [m["content"] for m in view[3:]] == [m["content"] for m in s.messages[-4:]]
    # the summarizer saw the middle of the history, as text
    (call,) = llm.calls
    assert (
        "cmd 1" in call["messages"][1]["content"]
        and "cmd 6" not in call["messages"][1]["content"]
    )
    assert call["tools"] is None

    expected = cost.cost_usd(MODEL, summary_reply().usage)
    (span,) = tracer.of("compaction")
    assert span["actions"] == ["summarize"] and span["cost_usd"] == pytest.approx(
        expected
    )
    assert (span["summary_tokens_in"], span["summary_tokens_out"]) == (4000, 300)
    assert span["tokens_after"] < span["tokens_before"]
    assert s.cost_usd == pytest.approx(expected) and s.compaction_cost_usd == s.cost_usd
    assert s.metrics()["compactions"] == 1


@pytest.mark.parametrize(
    "failure", [LLMError("server_error", "boom"), summary_reply(text="   ")]
)
async def test_a_failed_summary_falls_back_to_clearing_outputs(failure):
    tracer = MemoryTracer()
    mgr = manager("summarize", llm=ScriptedLLM([failure]), tracer=tracer)
    s = state_for()
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    view = await mgr.build(s)
    paired(view)
    assert any(str(m.get("content", "")).startswith(STUB_PREFIX) for m in view)
    (span,) = tracer.of("compaction")
    assert span["actions"] == ["truncate"] and span["fallback"].startswith(
        "truncate after"
    )


async def test_a_summary_is_not_summarized_again_when_nothing_else_is_old():
    llm = ScriptedLLM(
        [summary_reply()]
    )  # a second call would raise: the script is empty
    s = state_for()
    mgr = manager(
        "summarize", llm=llm, budget_tokens=2500, high_water=0.5, low_water=0.3
    )
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    first = await mgr.build(s)
    assert is_summary(first[2])
    # still above the high water mark, but the only thing in the middle is the summary
    again = await mgr.build(s)
    assert again == first and len(llm.calls) == 1


async def test_nothing_old_enough_means_no_summary_call_and_overflow():
    llm = ScriptedLLM([])
    mgr = manager("summarize", llm=llm, budget_tokens=800)
    s = state_for()
    add_turn(
        s, 1, size=4000
    )  # one turn: fewer than keep_recent, so all of it is protected
    with pytest.raises(ContextOverflow):
        await mgr.build(s)
    assert not llm.calls


def test_render_gives_the_full_text_by_default_and_the_reasoning_can_be_switched_off():
    msgs = [
        {
            **assistant_message(summary_reply("thinking aloud")),
            "reasoning_content": "SECRET THOUGHT",
        },
        tool_message("c1", "x" * 10_000),
    ]
    out = render(msgs)
    assert "SECRET THOUGHT" in out and "thinking aloud" in out
    assert "x" * 10_000 in out and "omitted" not in out  # nothing is cut up front
    assert "SECRET THOUGHT" not in render(msgs, reasoning=False)
    clipped = render(msgs, cap=1000)
    assert "chars omitted" in clipped and len(clipped) < 3000


# ---- hybrid ------------------------------------------------------------------------------
async def test_hybrid_only_clears_when_that_is_enough():
    llm = ScriptedLLM([])
    mgr = manager("hybrid", llm=llm)
    s = state_for()
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    view = await mgr.build(s)
    assert any(str(m.get("content", "")).startswith(STUB_PREFIX) for m in view)
    assert not llm.calls and not any(is_summary(m) for m in view)


async def test_hybrid_summarizes_when_clearing_is_not_enough():
    llm, tracer = ScriptedLLM([summary_reply()]), MemoryTracer()
    # small outputs the stub cannot shrink much, so clearing alone stays above the high mark;
    # enough turns that the cleared middle is still worth a summary once the clearing has got
    # the prompt back under the budget (with 9 turns it was not, and is now skipped: no_gain)
    mgr = manager(
        "hybrid",
        llm=llm,
        tracer=tracer,
        budget_tokens=6000,
        high_water=0.6,
        low_water=0.3,
        keep_recent_min_turns=4,
    )
    s = state_for()
    for i in range(1, 30):
        add_turn(s, i, size=4500)
    view = await mgr.build(s)
    paired(view)
    assert len(llm.calls) == 1 and any(is_summary(m) for m in view)
    (span,) = tracer.of("compaction")
    assert span["actions"] == ["truncate", "summarize"]
    assert span["cost_usd"] > 0 and span["cleared_outputs"] > 0


async def test_hybrid_keeps_the_clearing_when_the_summarizer_fails():
    tracer = MemoryTracer()
    mgr = manager(
        "hybrid", llm=ScriptedLLM([LLMError("server_error", "boom")]), tracer=tracer,
        budget_tokens=6000, high_water=0.6, low_water=0.3, keep_recent_min_turns=4,
    )  # fmt: skip
    s = state_for()
    for i in range(1, 30):  # a summary worth making (see the test above)
        add_turn(s, i, size=4500)
    try:
        await mgr.build(s)
    except ContextOverflow:
        pass
    (span,) = tracer.of("compaction")
    assert span["actions"] == ["truncate"] and "summary_failed" in span


# ---- the view is append-only between compactions (KV cache) -------------------------------
async def test_consecutive_prompts_share_a_byte_identical_prefix():
    s = state_for()
    mgr = manager("hybrid", llm=ScriptedLLM([]), budget_tokens=100_000)
    prompts = []
    for i in range(1, 6):
        add_turn(s, i, size=500)
        prompts.append(await mgr.build(s))
    for a, b in pairwise(prompts):
        ja = [json.dumps(m, sort_keys=True) for m in a]
        jb = [json.dumps(m, sort_keys=True) for m in b]
        assert jb[: len(ja)] == ja and len(jb) > len(ja)


async def test_a_compaction_leaves_the_system_prompt_and_task_untouched():
    s = state_for()
    mgr = manager("truncate")
    add_turn(s, 1, size=500)
    before = await mgr.build(s)
    for i in range(2, 8):
        add_turn(s, i, size=4000)
    after = await mgr.build(s)
    assert after[:2] == before[:2]
    assert mgr.estimator.ratio > 0


# ---- no compaction on every turn when the protected tail alone is above the high mark --------
async def test_the_summary_does_not_rerun_every_turn_under_sustained_pressure():
    # keep_recent_min_turns=3 keeps ~2400 tokens, above the high water mark (0.4 * 6000), so the view
    # cannot get below it. Without hysteresis every new turn would summarize again.
    llm = ScriptedLLM([summary_reply() for _ in range(10)])
    mgr = manager(
        "summarize",
        llm=llm,
        budget_tokens=6000,
        high_water=0.4,
        low_water=0.2,
        keep_recent_min_turns=3,
    )
    s = state_for()
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    await mgr.build(s)
    assert len(llm.calls) == 1
    after = mgr._last_after
    assert after > 0.4 * 6000  # the premise: still above the high water mark

    counts = []
    for i in range(7, 12):
        add_turn(s, i, size=4000)
        try:
            await mgr.build(s)
        except ContextOverflow:
            break
        counts.append(len(llm.calls))
    assert counts[0] == 1, (
        "a new turn right after a compaction must not summarize again"
    )
    # it does run again once the prompt has grown by a full water band (0.2 * 6000)
    assert max(counts) >= 2 and counts == sorted(counts)
    assert counts.index(2) >= 1


async def test_a_prompt_over_the_budget_always_gets_a_compaction_attempt():
    llm = ScriptedLLM([summary_reply(), summary_reply()])
    mgr = manager(
        "summarize",
        llm=llm,
        budget_tokens=5000,
        high_water=0.4,
        low_water=0.2,
        keep_recent_min_turns=3,
    )
    s = state_for()
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    await mgr.build(s)
    mgr._last_after = 4900  # pretend the last compaction ended just under the budget
    for i in range(7, 11):  # ~3200 more tokens: the estimate passes the 5000 budget
        add_turn(s, i, size=4000)
    try:
        await mgr.build(s)
    except ContextOverflow:
        pass
    assert len(llm.calls) == 2  # the trigger is capped at the budget, not past it


async def test_the_summarizer_gets_the_runs_deadline():
    llm = ScriptedLLM([summary_reply()])
    mgr = manager("summarize", llm=llm)
    s = state_for()
    for i in range(1, 7):
        add_turn(s, i, size=4000)
    await mgr.build(s, deadline=123.5)
    assert llm.calls[0]["deadline"] == 123.5
