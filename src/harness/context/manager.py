"""Context management (CLAUDE.md 5.2, 5.3).

The manager owns two aligned lists (strategies/types.py): the *view*, what is actually sent,
and the *original*, the same messages as the agent saw them. The view mirrors the agent's
history and is only appended to, so consecutive prompts share a byte-identical prefix (KV
cache). The one exception is a compaction, which rewrites the view once, in one go, from the
high water mark down to the low water mark, and records a `compaction` span. The agent's own
history (`state.messages`) is never modified.

Before every compaction the manager makes a plan from what is known at that moment:

    head      system prompt + tool definitions + task. Fixed for the run; exact after the
              first call (the provider reports it), a local estimate before.
    tail      the newest turns, kept as they are. At most keep_recent_ratio * budget, and
              less when the room left under the low water mark is smaller.
    overhead  what a summary and its state block may add: the expected size of the summary
              (measured, ContextConfig.summary_expected_tokens_*) plus the cap of the block
              (the block is fitted to its cap by code, so the cap is a true upper bound).

    room = low water mark - head - overhead

The plan has five levels. `normal`: the full tail fits. `tail_shrunk`: the tail was cut down
to the room. `minimal`: even the shortest tail leaves no room for a full summary, so the
summary and state block are kept short. `unreachable`: still above the low water mark, but
within the budget; the compaction goes ahead and says so. `infeasible`: the head and the
newest turn alone do not fit the budget; nothing can help, so no summary is paid for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial
from typing import Any

from harness.agent.state import AgentState
from harness.config import NEEDS_SUMMARIZER, ContextConfig
from harness.context.restore import Block, RestoreIndex, build_block
from harness.context.strategies import hybrid, none, summarize, truncate
from harness.context.strategies.summarize import spent_by
from harness.context.strategies.types import (
    HEAD,
    Env,
    Report,
    Skip,
    protected_from,
)
from harness.context.tokens import TokenEstimator, ratio_unusual
from harness.llm.client import LLMClient
from harness.llm.types import LLMError, Message
from harness.sandbox.base import Sandbox
from harness.tracing.tracer import Tracer

_STRATEGIES = {
    "none": none,
    "truncate": truncate,
    "summarize": summarize,
    "hybrid": hybrid,
}


class ContextOverflow(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    cut: int  # view[cut:] is the protected tail
    head_tokens: int
    tail_tokens: int
    room_tokens: int  # low water mark - head - overhead (negative: no room)
    level: str  # normal | tail_shrunk | minimal | unreachable | infeasible
    min_tail_tokens: int

    @property
    def minimal(self) -> bool:
        return self.level in ("minimal", "unreachable")


class ContextManager:
    def __init__(
        self,
        cfg: ContextConfig,
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        sandbox: Sandbox,
        tracer: Tracer,
        summarizer: LLMClient | None = None,
        summary_model: str | None = None,
    ) -> None:
        if cfg.strategy in NEEDS_SUMMARIZER and (
            summarizer is None or not summary_model
        ):
            raise ValueError(
                f"context strategy {cfg.strategy!r} needs a summarizer client and model"
            )
        self.cfg = cfg
        self.sandbox, self.tracer = sandbox, tracer
        self.summarizer, self.summary_model = summarizer, summary_model
        self.estimator = TokenEstimator(model, tools)
        self._view: list[Message] = []
        self._original: list[Message] = []  # aligned with _view (strategies/types.py)
        self._synced = 0  # how many of state.messages the view already holds
        self._sent: tuple[int, int] | None = (
            None  # (view length, state.steps) at the last build
        )
        self._ctx_files = 0
        self._last_after: int | None = (
            None  # estimated size after the last compaction attempt
        )
        self._head_exact: int | None = (
            None  # the provider's own count of the head, once known
        )
        # Where the dropped tool outputs are saved and which files were viewed, across all
        # compactions. A summary adds its stretch to a copy (`_pending_index`); the copy
        # becomes the real index only if that summary is the one that was applied.
        self._index = RestoreIndex()
        self._pending_index: RestoreIndex | None = None

    @property
    def wasted_prompt_tokens_est(self) -> int:
        stats = getattr(self.summarizer, "stats", None)
        return stats.wasted_prompt_tokens_est if stats else 0

    def _next_path(self) -> str:
        self._ctx_files += 1
        return f"/tmp/harness/ctx_{self._ctx_files}.txt"

    async def _estimate(self, state: AgentState, view: list[Message]) -> int:
        """Size of the next prompt. The previous request's real size anchors it when the view
        has only been appended to since (context/tokens.py)."""
        if self._sent and state.steps > self._sent[1] and state.last_prompt_tokens:
            sent_len = self._sent[0]
            # The very first request was exactly the head (system prompt, task, tool
            # definitions), so the provider's count of it IS the size of the head.
            if self._head_exact is None and sent_len == HEAD and self._sent[1] == 0:
                self._head_exact = state.last_prompt_tokens
            await self.estimator.calibrate(view[:sent_len], state.last_prompt_tokens)
            return await self.estimator.estimate(
                view, anchor_len=sent_len, anchor_tokens=state.last_prompt_tokens
            )
        return await self.estimator.estimate(view)

    async def build(
        self, state: AgentState, *, deadline: float | None = None
    ) -> list[Message]:
        new = state.messages[self._synced :]
        self._view.extend(new)
        self._original.extend(new)
        self._synced = len(state.messages)

        budget = self.cfg.budget_tokens
        est = await self._estimate(state, self._view)
        if self.cfg.strategy != "none" and est > self._trigger():
            est = await self._compact(state, est, deadline)
            if est > budget:
                raise ContextOverflow(
                    f"prompt would be ~{est} tokens after compaction (budget {budget})"
                )
        elif est > budget:
            raise ContextOverflow(f"prompt would be ~{est} tokens (budget {budget})")

        self._sent = (len(self._view), state.steps)
        return list(self._view)

    def _trigger(self) -> int:
        """When the next compaction attempt runs, from where the last one left the prompt
        (`_last_after`; for an attempt that changed nothing, the size it started at).

        - at or below the low water mark (it did its job): the high water mark. The prompt has
          to grow by a full water band first, so the cached prefix lives that long.
        - between the two marks (in hybrid mostly: the clearing got below the high mark, so no
          summary was tried; or a summary was not worth it): wait for a water band of growth,
          but no later than the safety line high + hysteresis_cap * (budget - high), so the
          margin under the budget is not used up. By then more outputs have left the protected
          tail; if clearing them is not enough, the summary is tried.
        - above the high water mark (it could not get lower: the tail alone is large) or
          nothing was done: wait for a water band of growth, at most until the budget. A
          lower cap would put the trigger under the prompt and retry on every turn.

        Docs/notes.md section 18 has the reasoning and the cases this replaces."""
        cfg = self.cfg
        budget, high, low = cfg.budget_tokens, cfg.high_target(), cfg.low_target()
        last = self._last_after
        if last is None or last <= low:
            return high
        band = high - low
        if last <= high:
            safety = high + int(cfg.hysteresis_cap * (budget - high))
            return min(last + band, safety)
        return min(last + band, budget)

    async def _plan(self) -> Plan:
        cfg = self.cfg
        needs = cfg.strategy in NEEDS_SUMMARIZER
        ratio = self.estimator.ratio
        sizes = [round(n * ratio) for n in await self.estimator.local_each(self._view)]
        tools = round(await self.estimator.tools_tokens() * ratio)
        head = self._head_exact or sum(sizes[:HEAD]) + tools
        full = (
            cfg.summary_expected_tokens(False) + cfg.restore_cap(False) if needs else 0
        )
        # the summary call's max_tokens is the cap, so no summary is longer than that
        least = (
            cfg.summary_expected_tokens(True) + cfg.restore_cap(True) if needs else 0
        )
        low, budget = cfg.low_target(), cfg.budget_tokens
        tail_cap = int(cfg.keep_recent_ratio * budget)
        turns = cfg.keep_recent_min_turns

        min_cut = protected_from(self._view, turns, 0, sizes)
        min_tail = sum(sizes[min_cut:])

        room = low - head - full
        level = "normal"
        if head + min_tail + least > budget:
            level = "infeasible"
        elif head + min_tail + full > low:
            # not even the shortest tail leaves room for a full summary
            room = low - head - least
            level = "minimal" if head + min_tail + least <= low else "unreachable"
        cut = protected_from(self._view, turns, min(tail_cap, max(room, 0)), sizes)
        if level == "normal" and cut != protected_from(
            self._view, turns, tail_cap, sizes
        ):
            level = "tail_shrunk"
        return Plan(cut, head, sum(sizes[cut:]), room, level, min_tail)

    async def _block(
        self,
        state: AgentState,
        original: list[Message],
        shown: list[Message],
        minimal: bool,
    ) -> Block:
        """The state block for a summary that replaces `original` / `shown`."""
        self._pending_index = self._index.with_range(original, shown)
        return await build_block(
            step=state.steps,
            sandbox=self.sandbox,
            todo=state.todo.render() if state.todo.items else None,
            index=self._pending_index,
            cfg=self.cfg,
            estimator=self.estimator,
            minimal=minimal,
        )

    def _skipped(self, state: AgentState, before: int, plan: Plan, skip: Skip) -> int:
        """An attempt that changed nothing. Remember it so the next one waits for the prompt
        to grow (_trigger), and trace it: otherwise a moved threshold has no visible cause."""
        self._last_after = before
        self.tracer.emit(
            "compaction_skipped",
            step=state.steps,
            strategy=self.cfg.strategy,
            reason=skip.reason,
            tokens_before=before,
            budget_tokens=self.cfg.budget_tokens,
            plan_level=plan.level,
            head_tokens=plan.head_tokens,
            tail_tokens=plan.tail_tokens,
            room_tokens=plan.room_tokens,
            ratio=round(self.estimator.ratio, 4),
            ratio_unusual=ratio_unusual(self.estimator.ratio),
            next_trigger=self._trigger(),
            **skip.details,
        )
        return before

    async def _compact(
        self, state: AgentState, before: int, deadline: float | None
    ) -> int:
        budget = self.cfg.budget_tokens
        plan = await self._plan()
        if plan.level == "infeasible":
            if before > budget:
                # No summary is paid for: the head and the newest turn alone do not fit.
                raise ContextOverflow(
                    f"the head ({plan.head_tokens} tokens) and the newest turn "
                    f"({plan.min_tail_tokens}) leave no room in a budget of {budget}"
                )
            return self._skipped(state, before, plan, Skip("infeasible"))
        env = Env(
            cfg=self.cfg,
            view=self._view,
            original=self._original,
            est=before,
            estimator=self.estimator,
            sandbox=self.sandbox,
            summarizer=self.summarizer,
            summary_model=self.summary_model,
            next_path=self._next_path,
            cut=plan.cut,
            over_budget=before > budget,
            minimal=plan.minimal,
            deadline=deadline,
            make_block=partial(self._block, state),
        )
        self._pending_index = None
        started = time.monotonic()
        fallback = None
        try:
            report = await _STRATEGIES[self.cfg.strategy].run(env)
        except (LLMError, ValueError) as e:
            if self.cfg.strategy != "summarize":
                raise
            # The summarizer failed: clearing old outputs is the best we can still do.
            fallback = f"{type(e).__name__}: {e}"[:200]
            cleared = await truncate.run(env)
            report = (
                cleared if isinstance(cleared, Report) else Report(list(self._view))
            )
            spent = spent_by(e)  # a response that came back unusable still cost money
            report.cost_usd += spent.cost_usd
            report.tokens_in += spent.tokens_in
            report.tokens_out += spent.tokens_out
        if not isinstance(report, Report):
            # Nothing worth compacting (everything is in the tail, or the gain is too small).
            return self._skipped(
                state, before, plan, report or Skip("nothing_to_compact")
            )

        if "summarize" in report.actions and self._pending_index is not None:
            self._index = self._pending_index
        self._view = report.view
        if report.original is not None:
            self._original = report.original
        assert len(self._view) == len(self._original), "view and original misaligned"
        after = await self.estimator.estimate(self._view)
        self._last_after = after
        state.add_compaction(report.cost_usd, report.tokens_in, report.tokens_out)
        details = dict(report.details)
        if fallback:
            details["fallback"] = f"truncate after {fallback}"
        self.tracer.emit(
            "compaction",
            step=state.steps,
            strategy=self.cfg.strategy,
            actions=report.actions,
            tokens_before=before,
            tokens_after=after,
            budget_tokens=budget,
            ratio=round(self.estimator.ratio, 4),
            ratio_unusual=ratio_unusual(self.estimator.ratio),
            plan_level=plan.level,
            target_unreachable=plan.level == "unreachable",
            head_tokens=plan.head_tokens,
            tail_tokens=plan.tail_tokens,
            room_tokens=plan.room_tokens,
            cost_usd=report.cost_usd,
            summary_tokens_in=report.tokens_in,
            summary_tokens_out=report.tokens_out,
            duration_s=round(time.monotonic() - started, 3),
            **details,
        )
        return after
