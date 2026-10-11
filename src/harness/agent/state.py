"""Per-run state. The message list only ever grows (CLAUDE.md 5.2)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from harness.config import AgentConfig
from harness.llm import cost
from harness.llm.types import LLMResponse, Message
from harness.tools.submit import SubmitFlag
from harness.tools.todo import TodoList


class FailReason(StrEnum):
    context_overflow = "context_overflow"
    step_limit = "step_limit"
    cost_limit = "cost_limit"
    time_limit = "time_limit"
    format_error = "format_error"
    env_error = "env_error"
    no_submit = "no_submit"


@dataclass(frozen=True)
class Task:
    instance_id: str
    problem_statement: str


@dataclass
class AgentState:
    task: Task
    cfg: AgentConfig
    model: str
    messages: list[Message] = field(default_factory=list)
    todo: TodoList = field(default_factory=TodoList)
    submit_flag: SubmitFlag = field(default_factory=SubmitFlag)
    started_at: float = field(default_factory=time.monotonic)
    on_append: Callable[[Message], None] | None = None  # the loop traces every message

    steps: int = 0  # LLM calls that returned a response
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0  # successful calls only, fixed standard price
    last_prompt_tokens: int = 0
    # Compaction makes LLM calls of its own: their cost is part of cost_usd (and so of the
    # budget guard), their tokens are kept apart from the agent's tokens_in / tokens_out.
    compactions: int = 0
    compaction_cost_usd: float = 0.0
    compaction_tokens_in: int = 0
    compaction_tokens_out: int = 0

    format_errors: int = 0
    format_errors_consecutive: int = 0
    truncated_outputs: int = 0
    truncated_consecutive: int = 0
    no_tool_consecutive: int = 0  # replies with no tool call, in a row

    fail_reason: FailReason | None = None
    fail_detail: str | None = None
    done: bool = False

    @property
    def submitted(self) -> bool:
        return self.submit_flag.submitted

    def append(self, message: Message) -> None:
        self.messages.append(message)
        if self.on_append:
            self.on_append(message)

    def record_response(self, resp: LLMResponse) -> None:
        self.steps += 1
        u = resp.usage
        self.prompt_tokens += u.prompt_tokens
        self.cached_tokens += u.cached_tokens
        self.completion_tokens += u.completion_tokens
        self.reasoning_tokens += u.reasoning_tokens
        self.last_prompt_tokens = u.prompt_tokens
        self.cost_usd += cost.cost_usd(self.model, u)

    def add_compaction(self, cost_usd: float, tokens_in: int, tokens_out: int) -> None:
        self.compactions += 1
        self.compaction_cost_usd += cost_usd
        self.compaction_tokens_in += tokens_in
        self.compaction_tokens_out += tokens_out
        self.cost_usd += cost_usd

    def finish(self, reason: FailReason | None, detail: str | None = None) -> None:
        self.done = True
        if reason is not None and self.fail_reason is None:
            self.fail_reason, self.fail_detail = reason, detail

    def metrics(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tokens_in": self.prompt_tokens,
            "tokens_cached": self.cached_tokens,
            "tokens_out": self.completion_tokens,
            "tokens_reasoning": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "format_errors": self.format_errors,
            "truncated_outputs": self.truncated_outputs,
            "compactions": self.compactions,
            "compaction_cost_usd": self.compaction_cost_usd,
            "compaction_tokens_in": self.compaction_tokens_in,
            "compaction_tokens_out": self.compaction_tokens_out,
        }
