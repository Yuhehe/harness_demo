"""`summarize`: keep the system prompt, the task and the newest turns; replace everything in
between with one structured summary written by the cheap model (CLAUDE.md 5.3).

The summarizer reads `env.original`, the history as the agent saw it, in full. Not the view:
in `hybrid` the view already has `[output cleared]` stubs, and a summary of stubs knows nothing.
OpenHands, Codex and the "Complexity Trap" hybrid all summarize the full text as well
(docs/notes.md section 12). Nothing is cut up front; the input is shrunk only when a summary
call fails for size (context overflow, timeout) and then retried with smaller items.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from harness.config import ContextConfig
from harness.context.restore import STATE_MARKER
from harness.context.strategies.types import HEAD, Env, Report
from harness.llm import cost
from harness.llm.types import LLMError, Message, system_message, user_message

SUMMARY_PREFIX = "[Summary of the earlier steps"
MIN_CLIP_CHARS = 200
WORDS_FULL, WORDS_MINIMAL = (
    700,
    200,
)  # two fixed sizes, not a continuous function of the room
TOKENS_PER_WORD = 1.5
RETRY_KINDS = (
    "context_overflow",
    "timeout",
)  # the input was too big: shrinking can help

_SYSTEM = """\
You write the handoff summary for an autonomous coding agent whose earlier history is about \
to be removed. The agent will continue the task from your summary alone: apart from a block \
the harness adds below it (the current patch, the plan, where long outputs are saved), the \
summary is all it will have of the earlier work. Report only what the history shows; do not \
invent facts. Be concrete: file paths, function names, commands, error messages. Do not copy \
diffs or file lists, the harness adds those."""

_INSTRUCTIONS = """\
Write the summary with exactly these sections, in plain text, at most {words} words:

## Tried so far
What was done, in order, and what each step showed.
## Failed attempts
What did not work and why (error messages, wrong assumptions).
## Current hypothesis
What the agent currently believes the root cause is, and why.
## Next steps
What the agent was about to do, in order.
## Tests
Which tests or scripts were run, with the result, and which still fail.
## Files modified
Every file changed so far with a one-line description of the change ("none" if none).
## Useful facts
Paths, commands, test names and other findings worth keeping."""


@dataclass
class Spent:
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


class UnusableSummary(ValueError):
    """The summarizer answered, but with nothing usable (cut off by its length limit, or empty).
    The response was paid for, so what it cost travels with the exception."""

    def __init__(self, message: str, spent: Spent) -> None:
        super().__init__(message)
        self.spent = spent


def spent_by(error: Exception) -> Spent:
    """What a failed summary attempt cost (nothing for an error that returned no response)."""
    return getattr(error, "spent", None) or Spent()


def summary_text_of(m: Message) -> str:
    """A summary message without its state block: that block is a snapshot of one moment,
    rebuilt from the sandbox each time, and is not input for the next summary."""
    return str(m.get("content", "")).split(STATE_MARKER, 1)[0]


def is_summary(m: Message) -> bool:
    return m.get("role") == "user" and str(m.get("content", "")).startswith(
        SUMMARY_PREFIX
    )


def _clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    half = limit // 2
    return (
        f"{text[:half]}\n[... {len(text) - 2 * half} chars omitted ...]\n{text[-half:]}"
    )


def render(
    messages: list[Message], *, cap: int | None = None, reasoning: bool = True
) -> str:
    """The history as text for the summarizer. Full text unless `cap` is given, in which case
    every item is cut to `cap` characters (head and tail kept)."""
    parts: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            if reasoning and m.get("reasoning_content"):
                parts.append(f"AGENT THINKING: {_clip(m['reasoning_content'], cap)}")
            if m.get("content"):
                parts.append(f"AGENT: {_clip(m['content'], cap)}")
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                parts.append(f"AGENT CALLS {fn['name']}: {_clip(fn['arguments'], cap)}")
        elif role == "tool":
            parts.append(f"RESULT: {_clip(str(m.get('content', '')), cap)}")
        elif is_summary(m):
            parts.append(f"PREVIOUS SUMMARY: {_clip(summary_text_of(m), cap)}")
        else:
            parts.append(
                f"{str(role).upper()}: {_clip(str(m.get('content', '')), cap)}"
            )
    return "\n".join(parts)


def clip_for_attempt(cfg: ContextConfig, attempt: int) -> int | None:
    """Per-item character cap for summary attempt number `attempt` (1-based): none for the
    first, then summary_clip_chars, shrinking by summary_clip_scale on each further retry."""
    if attempt == 1:
        return None
    cap = cfg.summary_clip_chars * cfg.summary_clip_scale ** (attempt - 2)
    return max(MIN_CLIP_CHARS, int(cap))


def summary_message(text: str, state_block: str = "") -> Message:
    """The summary as the agent sees it: the summarizer's text, then (separated by a marker
    that `summary_text_of` cuts at) the state block the harness generated."""
    body = (
        f"{SUMMARY_PREFIX}, written by another model. Your earlier history was removed: "
        "this summary and the state below are what you have of it. Continue from here, and "
        f"use the tools to look up anything you need.]\n{text.strip()}"
    )
    return user_message(f"{body}\n\n{state_block}" if state_block else body)


def _prompt(history_text: str, words: int) -> list[Message]:
    return [
        system_message(_SYSTEM),
        user_message(
            f"<history>\n{history_text}\n</history>\n\n{_INSTRUCTIONS.format(words=words)}"
        ),
    ]


async def run(env: Env) -> Report | None:
    """Returns None when there is nothing worth summarizing. Raises LLMError when the
    summarizer fails and UnusableSummary (a ValueError) when it returns nothing usable; the
    manager falls back."""
    assert env.summarizer is not None and env.summary_model is not None
    cfg = env.cfg
    cut = env.cut
    middle = env.original[HEAD:cut]
    # Nothing but an earlier summary: summarizing it again changes nothing and costs a call.
    if not middle or (len(middle) == 1 and is_summary(middle[0])):
        return None

    words = WORDS_MINIMAL if env.minimal else WORDS_FULL
    if not env.over_budget:
        # A summary that removes little costs a call and breaks the cached prefix. Over the
        # budget it is the last chance, so it always runs.
        sizes = await env.estimator.local_each(env.view[HEAD:cut])
        removed = round(sum(sizes) * env.estimator.ratio)
        kept = int(words * TOKENS_PER_WORD) + cfg.restore_tokens_cap()
        if removed - kept < cfg.min_gain_ratio * cfg.budget_tokens:
            return None

    block = None
    if env.make_block is not None:
        try:
            block = await env.make_block(middle, env.view[HEAD:cut], env.minimal)
        except Exception as e:  # noqa: BLE001 - the state block is a help, never a reason to fail
            block_error = f"{type(e).__name__}: {e}"[:200]
        else:
            block_error = None

    full_chars = len(render(middle, reasoning=cfg.summary_include_reasoning))
    spent = Spent()
    started = time.monotonic()
    for attempt in range(1, cfg.summary_retries + 2):
        cap = clip_for_attempt(cfg, attempt)
        text_in = render(middle, cap=cap, reasoning=cfg.summary_include_reasoning)
        try:
            resp = await env.summarizer.complete(
                _prompt(text_in, words), None, deadline=env.deadline
            )
        except LLMError as e:
            if e.kind not in RETRY_KINDS or attempt > cfg.summary_retries:
                raise
            continue
        spent.cost_usd += cost.cost_usd(env.summary_model, resp.usage)
        spent.tokens_in += resp.usage.prompt_tokens
        spent.tokens_out += resp.usage.completion_tokens
        text = resp.content.strip()
        if resp.finish_reason == "length" or not text:
            raise UnusableSummary(
                f"unusable summary (finish_reason={resp.finish_reason})", spent
            )
        summary = summary_message(text, block.text if block else "")
        return Report(
            [*env.view[:HEAD], summary, *env.view[cut:]],
            ["summarize"],
            {
                "summarized_messages": len(middle),
                "summary_chars": len(text),
                "summary_latency_s": round(time.monotonic() - started, 3),
                "summary_model": env.summary_model,
                "summary_input_chars": len(text_in),
                "summary_input_chars_full": full_chars,
                "summary_attempts": attempt,
                "summary_clipped": attempt > 1,
                # the summarizer's own text, kept apart from the generated block so that a
                # check of what the summary says (file recall) is not fooled by the block
                "summary": text,
                "restored": block.parts if block else None,
                "restored_error": None
                if block or env.make_block is None
                else block_error,
            },
            cost_usd=spent.cost_usd,
            tokens_in=spent.tokens_in,
            tokens_out=spent.tokens_out,
            original=[*env.original[:HEAD], summary, *env.original[cut:]],
        )
    raise AssertionError("unreachable: the last attempt returns or raises")
