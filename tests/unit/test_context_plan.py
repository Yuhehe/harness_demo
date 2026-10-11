"""The compaction plan (context/manager.py): the protected tail, the room under the low water
mark, the five levels, the head measurement, the minimum gain, and the retry hysteresis."""

import pytest
from ctx_helpers import add_turn, manager, new_state, prime, reply, turns
from fakes import ScriptedLLM

from harness.context.manager import ContextOverflow
from harness.context.strategies.types import HEAD, protected_from
from harness.context.tokens import TokenEstimator, count_messages
from harness.llm.types import tool_message
from harness.tracing.tracer import MemoryTracer


# ---- protected_from ------------------------------------------------------------------------
def _view(n_turns):
    view = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(n_turns):
        view += [
            {"role": "assistant", "content": "", "tool_calls": []},
            tool_message(f"c{i}", "x"),
        ]
    return view


def test_the_tail_has_at_least_min_turns_however_large():
    view = _view(5)
    sizes = [10, 10] + [1000, 1000] * 5
    assert protected_from(view, 2, 0, sizes) == len(view) - 4  # two turns, nothing more
    assert protected_from(view, 3, 0, sizes) == len(view) - 6


def test_the_tail_grows_over_older_turns_while_it_fits_the_token_limit():
    view = _view(5)
    sizes = [10, 10] + [500, 500] * 5  # a turn is 1000 tokens
    assert (
        protected_from(view, 2, 2500, sizes) == len(view) - 4
    )  # a third turn would be 3000
    assert protected_from(view, 2, 3000, sizes) == len(view) - 6
    assert (
        protected_from(view, 2, 99_999, sizes) == 2
    )  # every turn fits: the cut is the head


def test_the_cut_always_falls_on_an_assistant_message():
    view = _view(4)
    sizes = [5, 5] + [3, 7] * 4
    for limit in range(0, 60, 3):
        cut = protected_from(view, 1, limit, sizes)
        assert view[cut]["role"] == "assistant" or cut == HEAD


def test_fewer_turns_than_min_turns_protects_everything():
    assert protected_from(_view(1), 2, 100, [1] * 4) == HEAD


# ---- the head --------------------------------------------------------------------------------
async def test_the_head_is_the_providers_own_count_once_the_first_call_is_back():
    s = new_state()
    m = manager("truncate", budget_tokens=20000)
    await m.build(s)
    assert m._head_exact is None
    s.steps, s.last_prompt_tokens = 1, 777  # the first request WAS the head
    add_turn(s, 1)
    await m.build(s)
    assert m._head_exact == 777
    s.steps, s.last_prompt_tokens = 2, 5000  # later requests are not the head
    add_turn(s, 2)
    await m.build(s)
    assert m._head_exact == 777


async def test_before_the_first_call_the_head_is_a_local_estimate():
    s = turns(4)
    m = manager("truncate", budget_tokens=20000, high_water=0.3, low_water=0.2)
    prime(m, s)
    plan = await m._plan(5000)
    assert (
        0 < plan.head_tokens < 400
    )  # system prompt + task + (no tools in this fixture's count)


# ---- the levels --------------------------------------------------------------------------------
BIG = {"budget_tokens": 20000}


async def plan_for(strategy, n, *, chars=4000, llm=None, **kw):
    m = manager(strategy, llm=llm or ScriptedLLM([]), **kw)
    prime(m, turns(n, chars))
    return m, await m._plan(10**6)


async def test_level_normal_when_the_full_tail_fits_the_room():
    _, plan = await plan_for("hybrid", 30, high_water=0.9, low_water=0.8, **BIG)
    assert plan.level == "normal" and plan.room_tokens > 5000  # tail cap 0.25 * 20000


async def test_level_tail_shrunk_when_the_room_is_smaller_than_the_tail_cap():
    _, plan = await plan_for("hybrid", 30, high_water=0.5, low_water=0.3, **BIG)
    # room = 6000 - head - (1600 + 1000) is about 3300: less than the 5000 cap, still positive
    assert plan.level == "tail_shrunk" and 0 < plan.room_tokens < 5000
    assert (
        plan.tail_tokens <= plan.room_tokens + 900
    )  # at most one turn over, never more


async def test_level_minimal_when_only_a_short_summary_fits():
    _, plan = await plan_for(
        "hybrid",
        12,
        budget_tokens=8000,
        high_water=0.6,
        low_water=0.3,
        keep_recent_min_turns=2,
    )
    assert plan.level == "minimal" and plan.minimal


async def test_level_unreachable_when_even_the_shortest_summary_cannot_reach_the_low_mark():
    _, plan = await plan_for(
        "hybrid",
        12,
        budget_tokens=8000,
        high_water=0.6,
        low_water=0.3,
        keep_recent_min_turns=3,
    )
    assert plan.level == "unreachable" and plan.minimal


async def test_level_infeasible_when_the_head_and_the_newest_turns_do_not_fit_the_budget():
    _, plan = await plan_for(
        "hybrid", 6, chars=7000, budget_tokens=3000, high_water=0.6, low_water=0.3
    )
    assert plan.level == "infeasible"


async def test_truncate_has_no_summary_overhead_in_its_plan():
    _, plan = await plan_for("truncate", 12, high_water=0.5, low_water=0.3, **BIG)
    assert plan.level == "normal" and plan.room_tokens == 6000 - plan.head_tokens


# ---- what the manager does with an infeasible plan ----------------------------------------------
INFEASIBLE = {"budget_tokens": 3000, "high_water": 0.6, "low_water": 0.3}


async def test_infeasible_and_over_budget_overflows_without_paying_for_a_summary():
    llm = ScriptedLLM([])  # a call would raise
    m = manager("hybrid", llm=llm, **INFEASIBLE)
    with pytest.raises(ContextOverflow, match="leave no room"):
        await m.build(turns(6, chars=7000))
    assert not llm.calls


async def test_infeasible_but_still_within_the_budget_just_carries_on():
    llm = ScriptedLLM([])
    m = manager("hybrid", llm=llm, **INFEASIBLE)
    s = turns(2, chars=7000)  # about 2900 tokens: over the trigger, under the budget
    view = await m.build(s)
    assert len(view) == len(s.messages) and not llm.calls


async def test_head_alone_over_the_budget_is_reported_as_such():
    m = manager("none", budget_tokens=100)
    s = new_state()
    s.messages[1]["content"] = "issue " * 400
    with pytest.raises(ContextOverflow, match="budget 100"):
        await m.build(s)


# ---- the tail follows the ratio ------------------------------------------------------------------
async def test_a_larger_ratio_protects_more_turns():
    async def tail(ratio):
        m = manager(
            "truncate", budget_tokens=20000, high_water=0.9, low_water=0.8,
            keep_recent_ratio=ratio,
        )  # fmt: skip
        prime(m, turns(20))
        return (await m._plan(10**6)).tail_tokens

    small, large = await tail(0.01), await tail(0.25)
    assert small < 1900  # the two newest turns only
    assert 3500 < large <= 5000  # as many turns as fit under 0.25 * 20000


async def test_the_tail_shrinks_to_fit_a_tight_budget_but_keeps_the_newest_turns():
    tracer = MemoryTracer()
    llm = ScriptedLLM([reply()])
    m = manager(
        "hybrid", llm=llm, tracer=tracer, budget_tokens=20000, high_water=0.5, low_water=0.3,
        keep_recent_ratio=0.25, min_clear_chars=100,
    )  # fmt: skip
    s = turns(30)
    view = await m.build(s)
    (span,) = tracer.of("compaction")
    assert (
        span["plan_level"] == "tail_shrunk"
        and span["tail_tokens"] <= span["room_tokens"] + 900
    )
    assert (
        view[-1] is s.messages[-1] and view[-2] is s.messages[-2]
    )  # the newest turn intact
    assert span["tokens_after"] <= 0.5 * 20000


# ---- the minimum gain ---------------------------------------------------------------------------------
GAIN = {
    "budget_tokens": 20000,
    "high_water": 0.5,
    "low_water": 0.3,
    "keep_recent_ratio": 0.01,
}


async def test_a_summary_that_would_remove_little_is_skipped_when_within_the_budget():
    llm = ScriptedLLM([])
    m = manager("summarize", llm=llm, keep_recent_min_turns=11, **GAIN)
    s = turns(
        14
    )  # about 11300 tokens: over the trigger (10000); the middle is only 4 turns
    view = await m.build(s)
    assert not llm.calls and len(view) == len(s.messages)


async def test_a_skipped_attempt_is_not_repeated_every_turn():
    llm = ScriptedLLM([])
    m = manager("summarize", llm=llm, keep_recent_min_turns=11, **GAIN)
    s = turns(14)
    await m.build(s)
    assert m._last_after is not None and m._trigger() > m.cfg.high_target()
    calls = {"n": 0}
    real = TokenEstimator.local_each

    async def counting(self, messages):
        calls["n"] += 1
        return await real(self, messages)

    TokenEstimator.local_each = counting
    try:
        add_turn(s, 15)
        await m.build(s)  # the prompt grew a little, not a full band: no new plan
    finally:
        TokenEstimator.local_each = real
    assert calls["n"] == 0 and not llm.calls


async def test_over_the_budget_the_minimum_gain_does_not_apply():
    # The prompt is 1% over the budget and the middle would remove less than the 10% minimum.
    # Skipping it would end the run with context_overflow although a summary fixes it.
    llm = ScriptedLLM([reply("## Tried so far\nshort")])
    m = manager("summarize", llm=llm, keep_recent_min_turns=22, **GAIN)
    s = turns(25)  # about 20300 tokens: just over the budget; the middle is 3 turns
    view = await m.build(s)
    assert len(llm.calls) == 1
    assert view[2]["content"].startswith("[Summary of the earlier steps")


# ---- reasoning is not part of the prompt size ---------------------------------------------------------
def test_replayed_reasoning_is_not_counted_because_the_provider_does_not_count_it():
    plain = {"role": "assistant", "content": "ok"}
    thinking = {**plain, "reasoning_content": "think " * 3000}
    assert count_messages("deepseek/deepseek-flash", [thinking]) == count_messages(
        "deepseek/deepseek-flash", [plain]
    )
