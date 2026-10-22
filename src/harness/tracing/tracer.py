"""JSONL span tracer: one line per span, written and flushed immediately.

A run that dies half way still leaves its trajectory on disk. This docstring is the catalogue
of span types and their fields; CLAUDE.md 5.7 keeps only the conventions (what a run must
record to be reproducible). JsonlTracer adds `type`, `ts`, `run_id` and `instance_id` to every
span.

run_start       config, config_hash, model, thinking, reasoning_effort, max_tokens, timeout_s,
                seed, tools, prices_hash, guard_enabled, policy_hash, summary_model, task, and
                the caller's meta (run_id, git_commit, git_dirty)
llm_call        one per attempt: attempt, outcome (ok | retry | fail), purpose (agent | summary),
                model, latency_s; ok: finish_reason, prompt_tokens, cached_tokens,
                completion_tokens, reasoning_tokens, cost_usd, response_id, served_model and
                system_fingerprint (what the provider says answered: `model` is only the name
                asked for, and a model can change behind it), retries;
                retry / fail: retry_reason, status_code, usage (None), wasted_prompt_tokens_est,
                error
tool_call       step, tool, args_hash, exit_code, truncated, format_error, duration_s
guard_decision  tool, action, rule_id, level; approved (true / false) when a person was asked
                about an `ask` decision (`harness run` on a terminal)
compaction      step, strategy, actions, tokens_before, tokens_after, budget_tokens, plan_level,
                target_unreachable, head_tokens, tail_tokens, room_tokens, ratio, ratio_unusual,
                cost_usd, summary_tokens_in, summary_tokens_out, duration_s; truncate adds
                cleared_outputs and cleared_chars; a summary adds summarized_messages,
                summary_words, summary_expected_tokens, summary (the summarizer's own text
                only), summary_chars, summary_latency_s, summary_model, summary_input_chars,
                summary_input_chars_full, summary_attempts, summary_clipped, restored (what
                went into the state block), restored_error; fallback or summary_failed when the
                summary did not work out; in hybrid, summary_skipped (and summary_removed_tokens,
                summary_kept_tokens, summary_min_gain_tokens) when the clearing stood alone.
                tokens_before and tokens_after are estimates: the provider's count of the
                compacted prompt is the prompt_tokens of the next llm_call
compaction_skipped  an attempt that changed nothing (it still moves the retry threshold):
                step, strategy, reason (infeasible | nothing_to_clear | nothing_to_summarize |
                no_gain | strategy_none), tokens_before, budget_tokens, plan_level, head_tokens,
                tail_tokens, room_tokens, ratio, ratio_unusual, next_trigger; no_gain adds
                removed_tokens, kept_tokens, min_gain_tokens
message         step, and the message itself (role, content, tool_calls, tool_call_id,
                reasoning_content)
truncation      step, content_chars, tool_calls, consecutive
run_end         fail_reason, fail_detail, submitted, patch_chars, todo, and the run's metrics:
                steps, tokens_in, tokens_cached, tokens_out, tokens_reasoning, cost_usd (with the
                compaction calls), compaction_cost_usd, compactions, compaction_tokens_in,
                compaction_tokens_out, format_errors, truncated_outputs, failed_attempts,
                retry_rate, wasted_prompt_tokens_est, retry_reasons, wall_seconds
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol, Self


class Tracer(Protocol):
    def emit(self, type: str, **fields: Any) -> None: ...

    def close(self) -> None: ...


class JsonlTracer:
    def __init__(self, path: str | Path, *, run_id: str, instance_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.instance_id = instance_id
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, type: str, **fields: Any) -> None:
        span = {
            "type": type,
            "ts": time.time(),
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            **fields,
        }
        self._fh.write(json.dumps(span, sort_keys=True, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class MemoryTracer:
    """For tests: keeps spans in a list."""

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def emit(self, type: str, **fields: Any) -> None:
        self.spans.append({"type": type, **fields})

    def close(self) -> None:
        pass

    def of(self, type: str) -> list[dict[str, Any]]:
        return [s for s in self.spans if s["type"] == type]


class NullTracer:
    def emit(self, type: str, **fields: Any) -> None:
        pass

    def close(self) -> None:
        pass
