"""Context management. Day 1: only the `none` strategy, as the seam Day 2 plugs into."""

from __future__ import annotations

from harness.agent.state import AgentState
from harness.config import ContextConfig
from harness.llm.types import Message


class ContextOverflow(Exception):
    pass


class ContextManager:
    def __init__(self, cfg: ContextConfig) -> None:
        if cfg.strategy != "none":
            raise NotImplementedError(
                f"context strategy {cfg.strategy!r} comes on Day 2"
            )
        self.cfg = cfg

    async def build(self, state: AgentState) -> list[Message]:
        # `none`: no compression; running past the budget ends the run (CLAUDE.md 5.3).
        # Day 1 approximation: the provider-reported prompt size of the previous call, until
        # context/tokens.py counts the next request itself.
        if state.last_prompt_tokens > self.cfg.budget_tokens:
            raise ContextOverflow(
                f"prompt was {state.last_prompt_tokens} tokens (budget {self.cfg.budget_tokens})"
            )
        return list(state.messages)
