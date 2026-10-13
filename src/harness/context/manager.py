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
    overhead  what a summary and its state block may add (their caps).

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
    RESTORE_MIN_TOKENS,
    SUMMARY_MIN_TOKENS,
    Env,
    Report,
    protected_from,
)
from harness.context.tokens import TokenEstimator
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
        """When the next compaction attempt runs. Normally the high water mark. If the last
        attempt could not get below it (the protected tail alone is large) or found nothing
        to do, trying again on every turn would call the summarizer and invalidate the cached
        prefix each time, so the next attempt waits until the prompt has grown by a full water
        band. The budget caps it: a prompt over the budget always gets an attempt before the
        run overflows."""
        budget = self.cfg.budget_tokens
        high = self.cfg.high_target()
        if self._last_after is None:
            return high
        band = high - self.cfg.low_target()
        return min(max(high, self._last_after + band), budget)

    async def _plan(self, before: int) -> Plan:
        cfg = self.cfg
        needs = cfg.strategy in NEEDS_SUMMARIZER
        ratio = self.estimator.ratio
        sizes = [round(n * ratio) for n in await self.estimator.local_each(self._view)]
        tools = round(await self.estimator.tools_tokens() * ratio)
        head = self._head_exact or sum(sizes[:HEAD]) + tools
        full = cfg.summary_tokens_cap() + cfg.restore_tokens_cap() if needs else 0
        # the summary call's max_tokens is the cap, so no summary is longer than that
        least = (
            min(SUMMARY_MIN_TOKENS, cfg.summary_tokens_cap()) + RESTORE_MIN_TOKENS
            if needs
            else 0
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

    async def _compact(
        self, state: AgentState, before: int, deadline: float | None
    ) -> int:
        budget = self.cfg.budget_tokens
        plan = await self._plan(before)
        if plan.level == "infeasible":
            if before > budget:
                # No summary is paid for: the head and the newest turn alone do not fit.
                raise ContextOverflow(
                    f"the head ({plan.head_tokens} tokens) and the newest turn "
                    f"({plan.min_tail_tokens}) leave no room in a budget of {budget}"
                )
            self._last_after = before
            return before
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
            report = await truncate.run(env) or Report(list(self._view))
            spent = spent_by(e)  # a response that came back unusable still cost money
            report.cost_usd += spent.cost_usd
            report.tokens_in += spent.tokens_in
            report.tokens_out += spent.tokens_out
        if report is None:
            # Nothing worth compacting (everything is in the tail, or the gain is too small).
            # Remember the attempt so the next one waits for the prompt to grow.
            self._last_after = before
            return before

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
