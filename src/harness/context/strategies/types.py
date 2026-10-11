"""What every strategy gets and returns.

The manager owns two aligned lists, `view` and `original`, of the same length:

* `view` is what is actually sent to the model. It only grows, except when a strategy compacts
  it (an output replaced by a stub, a stretch replaced by a summary).
* `original` holds the same messages as the agent saw them. `truncate` changes only `view`;
  `summarize` replaces the same stretch in both. A summarizer reads `original`, so it sees
  what the agent saw and not the stubs that `truncate` put in the view.

The agent's own history (`state.messages`) is never touched, so traces and transcripts stay
complete.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from harness.config import ContextConfig
from harness.context.tokens import TokenEstimator
from harness.llm.client import LLMClient
from harness.llm.types import Message
from harness.sandbox.base import Sandbox

HEAD = 2  # view[0] is the system prompt, view[1] the task; neither is ever compacted
# The smallest a summary and its state block can be made (a summary of about 200 words, the
# diff stat alone). Used to tell "tight" from "impossible".
SUMMARY_MIN_TOKENS = 300
RESTORE_MIN_TOKENS = 200


@dataclass
class Env:
    cfg: ContextConfig
    view: list[Message]
    original: list[Message]  # aligned with `view`: same length, same positions
    est: int  # estimated tokens of the next prompt, before compaction
    estimator: TokenEstimator
    sandbox: Sandbox
    summarizer: LLMClient | None
    summary_model: str | None
    next_path: Callable[[], str]  # a fresh /tmp/harness/ctx_<n>.txt path in the sandbox
    cut: int  # view[cut:] is the protected tail; only view[HEAD:cut] may be compacted
    over_budget: bool = False  # the prompt is already over the budget: the last attempt
    minimal: bool = False  # no room for a full summary and state block: keep them short
    deadline: float | None = (
        None  # time.monotonic() value the summarizer call must not outlive
    )
    # (original slice, view slice, minimal) -> the state block for a summary that replaces
    # that stretch; set by the manager, which knows the step, the todo list and the sandbox.
    make_block: (
        Callable[[list[Message], list[Message], bool], Awaitable[Any]] | None
    ) = None

    @property
    def high(self) -> int:
        return int(self.cfg.high_water * self.cfg.budget_tokens)

    @property
    def low(self) -> int:
        return int(self.cfg.low_water * self.cfg.budget_tokens)


@dataclass
class Report:
    view: list[Message]
    actions: list[str] = field(default_factory=list)  # e.g. ["truncate", "summarize"]
    details: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0  # of the LLM calls the compaction itself made
    tokens_in: int = 0
    tokens_out: int = 0
    original: list[Message] | None = (
        None  # None: unchanged (a strategy that only edits view)
    )


def protected_from(
    view: list[Message], min_turns: int, max_tokens: int, sizes: list[int]
) -> int:
    """Index where the protected tail starts. The tail begins at an assistant message, so a
    tool call is never separated from its replies (every tool message follows the assistant
    message that made the call).

    It holds at least `min_turns` assistant turns, however large they are, and grows over
    older turns while the whole tail stays within `max_tokens` (`sizes[i]` is the token count
    of view[i]). Returns HEAD (everything protected) when there are fewer than `min_turns`
    turns."""
    turns = [
        i
        for i in range(len(view) - 1, HEAD - 1, -1)
        if view[i].get("role") == "assistant"
    ]
    if len(turns) < min_turns:
        return HEAD
    cut = turns[min_turns - 1]
    tail = sum(sizes[cut:])
    for older in turns[min_turns:]:
        extra = sum(sizes[older:cut])
        if tail + extra > max_tokens:
            break
        tail, cut = tail + extra, older
    return cut
