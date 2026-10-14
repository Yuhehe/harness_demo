"""The compaction plan (context/manager.py): the protected tail, the room under the low water
mark, the five levels, the head measurement, the minimum gain, and the retry hysteresis."""

import pytest
from ctx_helpers import MODEL, add_turn, manager, new_state, prime, reply, turns
from fakes import FakeSandbox, ScriptedLLM

from harness.config import ContextConfig
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
    plan = await m._plan()
    assert (
        0 < plan.head_tokens < 400
    )  # system prompt + task + (no tools in this fixture's count)


# ---- the levels --------------------------------------------------------------------------------
BIG = {"budget_tokens": 20000}


async def plan_for(strategy, n, *, chars=4000, llm=None, **kw):
    m = manager(strategy, llm=llm or ScriptedLLM([]), **kw)
    prime(m, turns(n, chars))
    return m, await m._plan()


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


# low mark 3500; full reservation 2500 + 500, minimal 1050 + 200; a turn is about 900 tokens,
# so two protected turns leave room for a minimal summary only and three for neither
SHORT = {
    "budget_tokens": 10000,
    "high_water": 0.6,
    "low_water": 0.35,
    "summary_max_ratio": 0.25,
}


async def test_level_minimal_when_only_a_short_summary_fits():
    _, plan = await plan_for(
        "hybrid",
        12,
        **SHORT,
        keep_recent_min_turns=2,
    )
    assert plan.level == "minimal" and plan.minimal


async def test_level_unreachable_when_even_the_shortest_summary_cannot_reach_the_low_mark():
    _, plan = await plan_for(
        "hybrid",
        12,
        **SHORT,
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
        return (await m._plan()).tail_tokens

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


async def _gain_and_manager(min_gain_ratio):
    """A summarize manager over 30 turns (over the trigger, under the budget, a full-size
    summary planned)."""
    kw = {"budget_tokens": 40000, "high_water": 0.5, "low_water": 0.3}
    llm = ScriptedLLM([reply()])
    m = manager("summarize", llm=llm, min_gain_ratio=min_gain_ratio, **kw)
    s = turns(30)
    prime(m, s)
    plan = await m._plan()
    assert not plan.minimal
    middle = await m.estimator.local_each(m._view[HEAD : plan.cut])
    removed = round(sum(middle) * m.estimator.ratio)
    # what a full summary and its state block put back: the summary at its expected size, the
    # block at its real size (it is built before the check)
    block = await m._block(
        s, m._original[HEAD : plan.cut], m._view[HEAD : plan.cut], False
    )
    assert 0 < block.tokens < m.cfg.restore_tokens_cap()  # not the cap: the real size
    gain = removed - (m.cfg.summary_expected_tokens(False) + block.tokens)
    return gain, m, llm, s


@pytest.mark.parametrize("margin, called", [(50, False), (-50, True)])
async def test_the_minimum_gain_counts_a_full_summary_at_its_measured_size(
    margin, called
):
    gain, m, _, _ = await _gain_and_manager(0.1)
    ratio = (
        gain + margin
    ) / m.cfg.budget_tokens  # the threshold just above / below the gain
    _, m, llm, s = await _gain_and_manager(ratio)
    await m.build(s)
    assert bool(llm.calls) is called


async def test_a_minimal_summary_is_reserved_at_its_measured_size():
    m, plan = await plan_for("hybrid", 12, **SHORT, keep_recent_min_turns=2)
    assert plan.level == "minimal"
    least = m.cfg.summary_expected_tokens(True) + m.cfg.summary_restore_min_tokens
    assert plan.room_tokens == m.cfg.low_target() - plan.head_tokens - least


async def test_a_minimal_summary_is_never_reserved_above_the_summary_cap():
    # a budget of 8000 caps the summary at 640 tokens, below the measured minimal size
    m, plan = await plan_for(
        "hybrid", 12, budget_tokens=8000, high_water=0.6, low_water=0.3
    )
    assert m.cfg.summary_tokens_cap() < m.cfg.summary_tokens_for(
        m.cfg.summary_words_minimal
    )
    assert plan.minimal
    least = m.cfg.summary_tokens_cap() + m.cfg.summary_restore_min_tokens
    assert plan.room_tokens == m.cfg.low_target() - plan.head_tokens - least


# ---- reasoning is part of the prompt size --------------------------------------------------------------
def test_replayed_reasoning_is_counted_because_the_provider_counts_it():
    # docs/notes.md section 17: 2064 characters of replayed reasoning add 492 prompt tokens
    text = "think about the failing test " * 300
    plain = {"role": "assistant", "content": "ok"}
    thinking = {**plain, "reasoning_content": text}
    as_text = {**plain, "content": "ok\n" + text}
    extra = count_messages(MODEL, [thinking]) - count_messages(MODEL, [plain])
    assert extra > 1000
    assert (
        abs(count_messages(MODEL, [thinking]) - count_messages(MODEL, [as_text])) <= 5
    )


async def test_an_estimate_after_a_summary_does_not_carry_the_reasoning_it_removed():
    # The ratio learned on a history full of reasoning must not inflate a view without it.
    est = TokenEstimator(MODEL, None)
    reasoning = "step " * 2000
    sent = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "reasoning_content": reasoning},
    ]
    # the provider counts the reasoning like any other text
    provider = count_messages(
        MODEL, [sent[0], {"role": "assistant", "content": reasoning}]
    )
    await est.calibrate(sent, provider)
    assert 0.95 <= est.ratio <= 1.05
    short = [
        {"role": "user", "content": "task"},
        {"role": "user", "content": "summary"},
    ]
    assert await est.estimate(short) <= await est.local(short) * 1.05


# ---- skipped attempts are traced ---------------------------------------------------------------
async def test_a_summary_without_gain_is_traced_as_skipped_and_not_counted():
    tracer = MemoryTracer()
    llm = ScriptedLLM([])
    m = manager("summarize", llm=llm, tracer=tracer, keep_recent_min_turns=11, **GAIN)
    s = turns(14)
    await m.build(s)
    (span,) = tracer.of("compaction_skipped")
    assert span["reason"] == "no_gain" and not tracer.of("compaction")
    assert span["removed_tokens"] - span["kept_tokens"] < span["min_gain_tokens"]
    assert span["next_trigger"] > m.cfg.high_target() and s.compactions == 0
    assert not llm.calls


async def test_nothing_to_clear_is_traced_as_skipped():
    tracer = MemoryTracer()
    # about 5400 tokens: over the trigger (4800), under the budget (6000); no output is long
    # enough to clear
    m = manager(
        "truncate", tracer=tracer, budget_tokens=6000, high_water=0.8,
        min_clear_chars=10**6,
    )  # fmt: skip
    await m.build(turns(6))
    (span,) = tracer.of("compaction_skipped")
    assert span["reason"] == "nothing_to_clear"


async def test_an_infeasible_plan_under_the_budget_is_traced_as_skipped():
    tracer = MemoryTracer()
    # two protected turns of about 1800 tokens: over the high mark (1600) but under the budget
    # (2000), and no summary fits next to them
    m = manager(
        "hybrid", llm=ScriptedLLM([]), tracer=tracer, budget_tokens=2000,
        high_water=0.8, low_water=0.3, keep_recent_min_turns=2,
        summary_max_ratio=0.1, summary_restore_ratio=0.1,
    )  # fmt: skip
    await m.build(turns(2))
    (span,) = tracer.of("compaction_skipped")
    assert span["reason"] == "infeasible" and span["plan_level"] == "infeasible"


async def test_a_skipped_summary_does_not_commit_the_state_block_index():
    m = manager("summarize", llm=ScriptedLLM([]), keep_recent_min_turns=11, **GAIN)
    before = m._index
    await m.build(turns(14))
    assert m._index is before  # built for the check, dropped with the skip


# ---- hybrid: over the budget is judged again after the clearing ------------------------------
async def test_hybrid_does_not_summarize_at_a_loss_once_the_clearing_is_under_the_budget():
    # Over the budget before the clearing (no minimum gain), under it after. The cleared middle
    # is so small that a summary would put back more than it removes, so it must be skipped.
    tracer, llm = MemoryTracer(), ScriptedLLM([])
    m = manager(
        "hybrid", llm=llm, tracer=tracer, budget_tokens=6000,
        high_water=0.6, low_water=0.3, keep_recent_min_turns=4,
    )  # fmt: skip
    s = new_state()
    for i in range(1, 10):
        add_turn(s, i, chars=4500)
    index = m._index
    await m.build(s)
    (span,) = tracer.of("compaction")
    assert span["tokens_before"] > 6000 and span["actions"] == ["truncate"]
    assert span["summary_skipped"] == "no_gain" and not llm.calls
    assert (
        m._index is index
    )  # the block built for the check is dropped with the summary


# ---- the plan reserves the expected summary, never above the cap -----------------------------
async def test_a_full_summary_is_reserved_at_its_expected_size():
    # a budget of 40000: the cap (3200) is above the expected size (1950)
    m, plan = await plan_for(
        "hybrid", 60, high_water=0.5, low_water=0.3, budget_tokens=40000
    )
    assert not plan.minimal
    assert (
        m.cfg.summary_tokens_for(m.cfg.summary_words_full) < m.cfg.summary_tokens_cap()
    )
    full = m.cfg.summary_expected_tokens(False) + m.cfg.restore_tokens_cap()
    assert plan.room_tokens == m.cfg.low_target() - plan.head_tokens - full


async def test_the_reserved_summary_is_capped_by_the_summary_cap():
    m, plan = await plan_for(
        "hybrid", 30, high_water=0.5, low_water=0.3, summary_max_ratio=0.05, **BIG
    )
    assert m.cfg.summary_tokens_cap() == 1000
    assert m.cfg.summary_tokens_for(m.cfg.summary_words_full) > 1000
    assert not plan.minimal
    full = m.cfg.summary_tokens_cap() + m.cfg.restore_tokens_cap()
    assert plan.room_tokens == m.cfg.low_target() - plan.head_tokens - full


async def test_the_minimal_state_block_cap_is_configurable():
    from harness.context.restore import RestoreIndex, build_block

    cfg = ContextConfig(
        strategy="hybrid", budget_tokens=20000, summary_restore_min_tokens=150
    )
    est = TokenEstimator(MODEL, None)
    block = await build_block(
        step=3, sandbox=FakeSandbox(), todo=None, index=RestoreIndex(),
        cfg=cfg, estimator=est, minimal=True,
    )  # fmt: skip
    assert block.parts["cap_tokens"] == 150


def test_the_minimal_size_must_not_exceed_the_full_size():
    with pytest.raises(ValueError, match="minimal summary size"):
        ContextConfig(summary_words_minimal=800)


@pytest.mark.parametrize(
    "bad",
    [
        {"summary_tokens_base": -1},
        {"summary_tokens_per_word": 0},
        {"summary_tokens_margin": 0.9},
    ],
)
def test_the_size_coefficients_must_make_sense(bad):
    with pytest.raises(ValueError, match="summary_tokens_"):
        ContextConfig(**bad)


def test_the_expected_size_follows_the_word_count():
    cfg = ContextConfig(
        summary_tokens_base=200, summary_tokens_per_word=2.0, summary_tokens_margin=1.5
    )
    assert cfg.summary_tokens_for(100) == 600  # (200 + 2 * 100) * 1.5
    assert cfg.summary_tokens_for(450) == 1650
    at_450 = ContextConfig(summary_words_full=450)
    assert at_450.summary_expected_tokens(False) == at_450.summary_tokens_for(450)


# ---- the ratio is in the trace --------------------------------------------------------------
async def test_a_compaction_records_the_ratio_and_flags_an_unusual_one():
    from harness.context.tokens import ratio_unusual

    tracer = MemoryTracer()
    m = manager("truncate", tracer=tracer, budget_tokens=4000)
    await m.build(turns(6))
    (span,) = tracer.of("compaction")
    assert span["ratio"] == pytest.approx(m.estimator.ratio, abs=1e-4)
    assert span["ratio_unusual"] is False
    assert ratio_unusual(2.0) and ratio_unusual(0.6) and not ratio_unusual(1.1)


async def test_an_unusual_ratio_is_flagged_on_both_compacted_and_skipped_attempts():
    tracer = MemoryTracer()
    m = manager("truncate", tracer=tracer, budget_tokens=4000)
    m.estimator.ratio = 1.8  # e.g. reasoning left out of the local count again
    await m.build(turns(6))
    (span,) = tracer.of("compaction")
    assert span["ratio"] == 1.8 and span["ratio_unusual"] is True

    tracer = MemoryTracer()
    # nothing long enough to clear: skipped whatever the ratio; about 9700 tokens at 1.8,
    # over the trigger (8400) and under the budget (12000)
    m = manager(
        "truncate", tracer=tracer, budget_tokens=12000, high_water=0.7,
        min_clear_chars=10**6,
    )  # fmt: skip
    m.estimator.ratio = 1.8
    await m.build(turns(6))
    (span,) = tracer.of("compaction_skipped")
    assert span["ratio"] == 1.8 and span["ratio_unusual"] is True


# ---- when the next compaction runs (manager._trigger) ------------------------------------------
# budget 24000: high 20400, low 12000, band 8400; safety line at cap 0.5: 20400 + 1800 = 22200
HYST = {"budget_tokens": 24000, "high_water": 0.85, "low_water": 0.5}


def _trigger_after(last, **kw):
    m = manager("hybrid", llm=ScriptedLLM([]), **{**HYST, **kw})
    m._last_after = last
    return m._trigger()


def test_before_any_compaction_and_after_one_that_reached_the_low_mark_it_is_the_high_mark():
    assert _trigger_after(None) == 20400
    assert _trigger_after(12000) == 20400 and _trigger_after(9000) == 20400


def test_between_the_marks_it_waits_for_a_band_but_stops_at_the_safety_line():
    assert _trigger_after(12500) == 20900  # a band of growth, under the safety line
    assert _trigger_after(18503) == 22200  # the 24k smoke: was 24000, the budget itself
    assert _trigger_after(20400) == 22200  # right at the high mark: still some spacing
    assert _trigger_after(18503) < HYST["budget_tokens"]


def test_the_safety_line_is_configurable():
    assert _trigger_after(18503, hysteresis_cap=0.0) == 20400  # retry at the high mark
    assert (
        _trigger_after(18503, hysteresis_cap=1.0) == 24000
    )  # may wait until the budget
    assert _trigger_after(18503, hysteresis_cap=0.25) == 20400 + 900


def test_above_the_high_mark_it_waits_for_a_band_up_to_the_budget_never_below_the_prompt():
    assert _trigger_after(21000) == 24000  # 29400 capped by the budget
    # high 12000, low 9600, band 2400, safety line 18000; the prompt was left at 19000, above
    # the safety line: capping by it would put the trigger under the prompt
    last = 19000
    assert _trigger_after(last, high_water=0.5, low_water=0.4) == last + 2400


def test_hysteresis_cap_must_be_a_share_of_the_margin():
    with pytest.raises(ValueError, match="hysteresis_cap"):
        ContextConfig(hysteresis_cap=1.5)
    with pytest.raises(ValueError, match="hysteresis_cap"):
        ContextConfig(hysteresis_cap=-0.1)
