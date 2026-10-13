"""What the summarizer is shown (strategies/summarize.py) and how the view and the original
history stay aligned (strategies/types.py). Phases 0 and 1 of the summarize rework."""

import json

import pytest
from fakes import FakeSandbox, ScriptedLLM

from harness.agent.state import AgentState, Task
from harness.config import AgentConfig, ContextConfig, LLMConfig
from harness.context.manager import ContextManager
from harness.context.strategies.summarize import (
    MIN_CLIP_CHARS,
    clip_for_attempt,
    is_summary,
)
from harness.context.strategies.truncate import STUB_PREFIX
from harness.context.tokens import TokenEstimator
from harness.llm import cost
from harness.llm.types import (
    LLMError,
    LLMResponse,
    Usage,
    system_message,
    tool_message,
    user_message,
)
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
    kw.setdefault("budget_tokens", 8000)
    kw.setdefault("high_water", 0.4)
    kw.setdefault("low_water", 0.2)
    kw.setdefault("keep_recent_min_turns", 2)
    kw.setdefault("keep_recent_ratio", 0.01)
    kw.setdefault("min_clear_chars", 100)
    return ContextConfig(strategy=strategy, **kw)


def new_state():
    acfg = AgentConfig(llm=LLMConfig(model=MODEL, thinking="disabled"))
    s = AgentState(task=Task("t", "p"), cfg=acfg, model=MODEL)
    s.append(system_message("system"))
    s.append(user_message("fix the bug"))
    return s


def add_turn(state, i, output, reasoning=None):
    msg = {
        "role": "assistant",
        "content": "",
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
    if reasoning:
        msg["reasoning_content"] = reasoning
    state.append(msg)
    state.append(tool_message(f"c{i}", output))


def reply(
    text="## Tried so far\nran things", *, finish="stop", prompt=3000, completion=250
):
    return LLMResponse(
        content=text,
        reasoning_content=None,
        tool_calls=(),
        finish_reason=finish,
        usage=Usage(prompt_tokens=prompt, completion_tokens=completion),
        model="deepseek-flash",
    )


def mgr(strategy, llm, tracer=None, **kw):
    return ContextManager(
        ccfg(strategy, **kw),
        model=MODEL,
        tools=TOOLS,
        sandbox=FakeSandbox(),
        tracer=tracer or MemoryTracer(),
        summarizer=llm,
        summary_model=MODEL,
    )


def history(n=6, size=4000, reasoning=None):
    s = new_state()
    for i in range(1, n + 1):
        add_turn(s, i, f"OUT{i} " + "word " * (size // 5), reasoning=reasoning)
    return s


def prompt_text(llm, call=0):
    return llm.calls[call]["messages"][1]["content"]


# ---- the summarizer sees the original ----------------------------------------------------
async def test_the_summary_input_is_the_full_text_not_a_clipped_version():
    llm = ScriptedLLM([reply()])
    s = history(6, size=6000)
    await mgr("summarize", llm).build(s)
    text = prompt_text(llm)
    assert "OUT1 " in text and "omitted" not in text
    assert (
        text.count("word ") >= 1200
    )  # a 6000-char output arrives whole (it was cut at 1500)


async def test_in_hybrid_the_summarizer_reads_the_outputs_not_their_stubs():
    # the regression behind phase 1: hybrid cleared the outputs and then summarized the stubs
    llm, tracer = ScriptedLLM([reply()]), MemoryTracer()
    s = history(60, size=4500)  # enough that the cleared middle is worth a summary
    m = mgr("hybrid", llm, tracer, keep_recent_min_turns=4)
    view = await m.build(s)

    (span,) = tracer.of("compaction")
    assert span["actions"] == ["truncate", "summarize"] and span["cleared_outputs"] > 0
    text = prompt_text(llm)
    assert STUB_PREFIX not in text, "the summarizer was handed [output cleared] stubs"
    assert "OUT1 " in text and "OUT5 " in text
    assert any(is_summary(x) for x in view)
    assert not any(str(x.get("content", "")).startswith(STUB_PREFIX) for x in view[:3])


async def test_the_first_summary_input_contains_no_summary_but_the_second_includes_the_first():
    llm = ScriptedLLM([reply("## Tried so far\nFIRST SUMMARY"), reply("second")])
    s = history(6, size=4000)
    m = mgr("summarize", llm)
    await m.build(s)
    assert "PREVIOUS SUMMARY" not in prompt_text(llm, 0)
    for i in range(7, 12):
        add_turn(s, i, f"OUT{i} " + "word " * 800)
    await m.build(s)
    assert len(llm.calls) == 2
    assert "PREVIOUS SUMMARY" in prompt_text(llm, 1) and "FIRST SUMMARY" in prompt_text(
        llm, 1
    )


# ---- reasoning -----------------------------------------------------------------------------
async def test_the_agents_reasoning_is_in_the_summary_input_by_default():
    llm = ScriptedLLM([reply()])
    await mgr("summarize", llm).build(history(6, reasoning="THE HYPOTHESIS IS X"))
    assert "AGENT THINKING: THE HYPOTHESIS IS X" in prompt_text(llm)


async def test_the_reasoning_can_be_left_out():
    llm = ScriptedLLM([reply()])
    await mgr("summarize", llm, summary_include_reasoning=False).build(
        history(6, reasoning="THE HYPOTHESIS IS X")
    )
    assert "HYPOTHESIS" not in prompt_text(llm)


# ---- shrink and retry only when a call fails for size -------------------------------------
async def test_a_context_overflow_is_retried_with_a_smaller_input_and_traced():
    llm, tracer = (
        ScriptedLLM([LLMError("context_overflow", "too big"), reply()]),
        MemoryTracer(),
    )
    m = mgr("summarize", llm, tracer, summary_clip_chars=1000)
    await m.build(history(6, size=6000))
    assert len(llm.calls) == 2
    first, second = prompt_text(llm, 0), prompt_text(llm, 1)
    assert (
        len(second) < len(first)
        and "chars omitted" in second
        and "omitted" not in first
    )
    (span,) = tracer.of("compaction")
    assert (span["summary_attempts"], span["summary_clipped"]) == (2, True)
    assert span["summary_input_chars"] < span["summary_input_chars_full"]


async def test_each_retry_cuts_deeper_until_the_attempts_run_out():
    errs = [LLMError("timeout", "slow") for _ in range(3)]
    llm = ScriptedLLM([*errs, LLMError("timeout", "slow")])  # 1 + 3 retries = 4 calls
    tracer = MemoryTracer()
    m = mgr("summarize", llm, tracer, summary_clip_chars=1200, summary_retries=3)
    await m.build(
        history(6, size=6000)
    )  # every call fails: falls back to clearing outputs
    sizes = [len(prompt_text(llm, k)) for k in range(4)]
    assert (
        len(llm.calls) == 4
        and sizes == sorted(sizes, reverse=True)
        and sizes[1] > sizes[3]
    )
    (span,) = tracer.of("compaction")
    assert span["actions"] == ["truncate"] and span["fallback"].startswith(
        "truncate after"
    )


@pytest.mark.parametrize(
    "kind", ["server_error", "rate_limit", "time_budget", "client_error"]
)
async def test_errors_that_a_smaller_input_cannot_fix_are_not_retried(kind):
    llm = ScriptedLLM([LLMError(kind, "x")])
    await mgr("summarize", llm).build(
        history(6)
    )  # falls back to clearing, no second call
    assert len(llm.calls) == 1


def test_the_clip_schedule():
    cfg = ccfg("summarize", summary_clip_chars=4000, summary_clip_scale=0.5)
    assert [clip_for_attempt(cfg, k) for k in (1, 2, 3, 4)] == [None, 4000, 2000, 1000]
    assert (
        clip_for_attempt(
            ccfg("summarize", summary_clip_chars=400, summary_clip_scale=0.1), 6
        )
        == MIN_CLIP_CHARS
    )


def test_the_new_settings_are_validated():
    for bad in (
        {"summary_clip_scale": 1.0},
        {"summary_clip_scale": 0},
        {"summary_retries": -1},
        {"summary_clip_chars": 10},
    ):
        with pytest.raises(ValueError):
            ContextConfig(**bad)


# ---- an unusable answer still costs money -------------------------------------------------
@pytest.mark.parametrize("strategy", ["summarize", "hybrid"])
async def test_an_unusable_summary_is_charged_to_the_task(strategy):
    cut_off = reply(text="partial", finish="length", prompt=3000, completion=4096)
    llm, tracer = ScriptedLLM([cut_off]), MemoryTracer()
    s = history(
        60, size=4500
    )  # in hybrid: enough that the cleared middle is worth a summary
    m = mgr(strategy, llm, tracer, keep_recent_min_turns=4)
    await m.build(s)
    paid = cost.cost_usd(MODEL, cut_off.usage)
    assert paid > 0 and s.compaction_cost_usd == pytest.approx(paid)
    (span,) = tracer.of("compaction")
    assert span["cost_usd"] == pytest.approx(paid) and (
        span["summary_tokens_in"],
        span["summary_tokens_out"],
    ) == (3000, 4096)


# ---- view and original stay aligned ---------------------------------------------------------
async def test_truncate_changes_only_the_view_and_keeps_the_original_text():
    s = history(6, size=4000)
    m = mgr("truncate", None)
    view = await m.build(s)
    assert len(m._view) == len(m._original)
    cleared = [
        i
        for i, x in enumerate(view)
        if str(x.get("content", "")).startswith(STUB_PREFIX)
    ]
    assert cleared
    for i in cleared:
        assert m._original[i]["content"].startswith("OUT")  # the text the agent saw
        assert m._original[i] is s.messages[i]


async def test_summarize_replaces_the_same_stretch_in_both_lists_through_several_compactions():
    llm = ScriptedLLM([reply(w) for w in ("first", "second", "third", "fourth")])
    s = history(6, size=4000)
    m = mgr("summarize", llm)
    for step in range(3):
        await m.build(s)
        assert len(m._view) == len(m._original)
        assert m._view[:2] == m._original[:2] == s.messages[:2]
        assert [is_summary(x) for x in m._view] == [is_summary(x) for x in m._original]
        for i in range(7 + 5 * step, 12 + 5 * step):
            add_turn(s, i, f"OUT{i} " + "word " * 800)
    await m.build(s)  # picks up the turns added after the last build
    assert m._original[-1] is s.messages[-1] and m._view[-1] is s.messages[-1]


@pytest.mark.parametrize("minimal", [False, True])
async def test_the_word_count_asked_for_comes_from_the_config(minimal):
    from harness.context.strategies import summarize
    from harness.context.strategies.types import Env

    cfg = ccfg("summarize", summary_words_full=333, summary_words_minimal=111)
    s = history(6, size=4000)
    llm = ScriptedLLM([reply()])
    env = Env(
        cfg=cfg, view=list(s.messages), original=list(s.messages), est=10**6,
        estimator=TokenEstimator(MODEL, None), sandbox=FakeSandbox(), summarizer=llm,
        summary_model=MODEL, next_path=lambda: "/tmp/harness/ctx_1.txt",
        cut=len(s.messages) - 2, over_budget=True, minimal=minimal,
    )  # fmt: skip
    report = await summarize.run(env)
    assert f"at most {111 if minimal else 333} words" in prompt_text(llm)
    assert report.details["summary_words"] == (111 if minimal else 333)
    assert report.details["summary_expected_tokens"] == cfg.summary_expected_tokens(
        minimal
    )
