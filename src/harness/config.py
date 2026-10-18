"""Run configuration (pydantic v2 + YAML).

The code defaults below are DEVELOPMENT defaults. The experiment values live in
configs/default.yaml; every file in configs/ablations/ extends it and changes one factor.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

# max_tokens and timeout_s are set as a pair (docs/notes.md section 5): the timeout must
# exceed the time to fill max_tokens, so a runaway generation ends with finish_reason=length
# (not retried) instead of a timeout (retried 3 times).
_THINKING_DEFAULTS = {
    "enabled": {"max_tokens": 65536, "timeout_s": 900.0},
    "disabled": {"max_tokens": 16384, "timeout_s": 300.0},
}


NEEDS_SUMMARIZER = ("summarize", "hybrid")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Strict):
    model: str | None = None  # None -> $MODEL_MAIN
    thinking: Literal["enabled", "disabled"] = "enabled"
    # DeepSeek's `reasoning_effort` (docs/notes.md section 5). Only meaningful with thinking
    # enabled; None resolves to "high", the server default, so it is always sent explicitly.
    reasoning_effort: Literal["high", "max"] | None = None
    max_tokens: int | None = None  # None -> paired default for `thinking`
    timeout_s: float | None = None  # None -> paired default for `thinking`
    max_attempts: int = 3
    backoff_base_s: float = 4.0
    backoff_factor: float = 3.0
    backoff_cap_s: float = 60.0

    @model_validator(mode="after")
    def _fill_pair(self) -> LLMConfig:
        if self.thinking == "disabled":
            if self.reasoning_effort is not None:
                raise ValueError("reasoning_effort needs thinking: enabled")
        elif self.reasoning_effort is None:
            self.reasoning_effort = "high"
        d = _THINKING_DEFAULTS[self.thinking]
        if self.max_tokens is None:
            self.max_tokens = d["max_tokens"]
        if self.timeout_s is None:
            self.timeout_s = d["timeout_s"]
        return self

    def resolved_model(self) -> str:
        model = self.model or os.environ.get("MODEL_MAIN")
        if not model:
            raise ValueError(
                "no model: set llm.model in the config or MODEL_MAIN in .env"
            )
        return model


class ContextConfig(_Strict):
    strategy: Literal["none", "truncate", "summarize", "hybrid"] = "none"
    budget_tokens: int = 128_000  # counts the prompt only, not max_tokens
    # Compaction runs when the next prompt would exceed high_water * budget and shrinks it to
    # low_water * budget in one go (not a little every turn), so the cached prefix survives
    # between compactions (CLAUDE.md 5.2).
    high_water: float = 0.85
    low_water: float = 0.5
    # How far the next compaction may be put off after one that got below the high water mark
    # but not to the low one (manager._trigger): it waits for the prompt to grow by a water band,
    # but never past high + hysteresis_cap * (budget - high). The margin between the high mark
    # and the budget absorbs estimate error and a large turn; 0.5 gives half of it to spacing
    # out compactions (the KV cache) and keeps half (a design value, not measured;
    # docs/notes.md section 18). 0 retries at the high mark, 1 may wait until the budget.
    hysteresis_cap: float = 0.5
    # The newest turns are never cleared or summarized. How many: at least
    # `keep_recent_min_turns` (the latest turn is what the model must answer), and more up to
    # `keep_recent_ratio * budget_tokens` if the room left after the head and the summary allows
    # it. The ratio is an upper bound; the tail shrinks when the budget is tight.
    keep_recent_min_turns: int = 2
    keep_recent_ratio: float = 0.25
    # Skip a summary that would remove less than this share of the budget (it costs a call and
    # breaks the cached prefix). Not applied when the prompt is already over the budget.
    min_gain_ratio: float = 0.1
    min_clear_chars: int = (
        400  # clearing a shorter output saves less than the stub costs
    )
    summary_model: str | None = None  # None -> $MODEL_CHEAP
    # Output cap of the summary call (thinking is always off, timeout 300 s) and the cap of the
    # code-generated state block appended to the summary. Each is min(absolute, ratio * budget),
    # so they shrink with a small budget and an ablation of the budget stays a one-key change.
    summary_max_tokens: int = 4096
    summary_max_ratio: float = 0.08
    summary_restore_tokens: int = 2500
    summary_restore_ratio: float = 0.05
    summary_diff_chars: int = (
        6000  # patch text in the state block (the stat is always there)
    )
    # The two sizes of a summary (the plan picks one): the word count the summarizer is asked for.
    summary_words_full: int = 700
    summary_words_minimal: int = 200
    # How many tokens a summary of N words is expected to take (the plan and the minimum-gain
    # check reserve this): (base + per_word * N) * margin, never above the cap above, which is
    # the hard limit (max_tokens of the summary call). Not a fixed tokens-per-word: a summary
    # has a fixed part (seven section headings) and a short one overshoots its word count more.
    # Fitted (docs/notes.md section 19) on 24 summaries at 200, 450 and 700 words: probes on
    # three M0-dev trajectories plus the summaries of the real smoke runs (the 200-word ones all
    # from one task); every level within 6% of the line; margin = largest actual / fitted,
    # rounded up to 0.05. Valid for the summary model, prompt and word range below: a run
    # refuses to start otherwise (recalibrate: `summary_probe.py calibrate`).
    summary_tokens_base: float = 225.0
    summary_tokens_per_word: float = 1.757
    summary_tokens_margin: float = 1.4
    summary_calibration_model: str = "deepseek/deepseek-flash"
    summary_calibration_prompt: str = "ff0ad18124c9"  # summarize.PROMPT_SHA
    summary_calibration_words: tuple[int, int] = (200, 700)
    # Cap of the state block of a minimal summary (a design value: room for the diff stat and a
    # short plan; 78-172 tokens seen). The block is fitted to its cap by code, so it never exceeds it.
    summary_restore_min_tokens: int = 200
    summary_index_entries: int = 20  # saved-output index lines
    summary_viewed_files: int = 30  # viewed-file paths
    # What the summarizer is shown: the agent's history as the agent saw it, in full, including
    # its reasoning (in thinking mode the plan and the hypothesis live there). Only when a
    # summary call fails for size (context overflow, timeout) is the input shrunk and retried:
    # every item is cut to `summary_clip_chars`, then to that times `summary_clip_scale`, ...
    summary_include_reasoning: bool = True
    summary_retries: int = 3
    summary_clip_chars: int = 4000
    summary_clip_scale: float = 0.5

    @model_validator(mode="after")
    def _check_water_marks(self) -> ContextConfig:
        if not 0 < self.low_water < self.high_water <= 1:
            raise ValueError("need 0 < low_water < high_water <= 1")
        if not 0 <= self.hysteresis_cap <= 1:
            raise ValueError("need 0 <= hysteresis_cap <= 1")
        if not 0 < self.summary_clip_scale < 1:
            raise ValueError("need 0 < summary_clip_scale < 1")
        if self.summary_retries < 0 or self.summary_clip_chars < 200:
            raise ValueError("need summary_retries >= 0 and summary_clip_chars >= 200")
        if self.keep_recent_min_turns < 1:
            raise ValueError("need keep_recent_min_turns >= 1")
        if not (
            0 < self.summary_words_minimal <= self.summary_words_full
            and self.summary_restore_min_tokens > 0
        ):
            raise ValueError(
                "need 0 < the minimal summary size <= the full one (summary_words_*) and "
                "summary_restore_min_tokens > 0"
            )
        if not (
            self.summary_tokens_base >= 0
            and self.summary_tokens_per_word > 0
            and self.summary_tokens_margin >= 1
        ):
            raise ValueError(
                "need summary_tokens_base >= 0, summary_tokens_per_word > 0 and "
                "summary_tokens_margin >= 1"
            )
        for name in (
            "keep_recent_ratio",
            "min_gain_ratio",
            "summary_max_ratio",
            "summary_restore_ratio",
        ):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"need 0 < {name} <= 1")
        # Even with an empty head and a minimal tail, the summary and the state block must
        # fit under the level compaction aims for. The head itself is only known at run time.
        if self.summary_tokens_cap() + self.restore_tokens_cap() >= self.low_target():
            raise ValueError(
                "summary_max_tokens + summary_restore_tokens (as capped by their ratios) "
                "must be below low_water * budget_tokens"
            )
        return self

    def summary_tokens_cap(self) -> int:
        return min(
            self.summary_max_tokens, int(self.summary_max_ratio * self.budget_tokens)
        )

    def restore_tokens_cap(self) -> int:
        return min(
            self.summary_restore_tokens,
            int(self.summary_restore_ratio * self.budget_tokens),
        )

    def summary_words(self, minimal: bool) -> int:
        return self.summary_words_minimal if minimal else self.summary_words_full

    def summary_tokens_for(self, words: int) -> int:
        """Expected size of a summary asked for `words` words, before the cap."""
        return math.ceil(
            (self.summary_tokens_base + self.summary_tokens_per_word * words)
            * self.summary_tokens_margin
        )

    def summary_expected_tokens(self, minimal: bool) -> int:
        """Expected size of a summary of this size, never above the cap: the cap is the
        summary call's max_tokens, so no summary comes back longer."""
        return min(
            self.summary_tokens_for(self.summary_words(minimal)),
            self.summary_tokens_cap(),
        )

    def restore_cap(self, minimal: bool) -> int:
        """Cap of the state block for a summary of this size."""
        return self.summary_restore_min_tokens if minimal else self.restore_tokens_cap()

    def high_target(self) -> int:
        return int(self.high_water * self.budget_tokens)

    def low_target(self) -> int:
        return int(self.low_water * self.budget_tokens)


class GuardConfig(_Strict):
    enabled: bool = True
    policy_path: str = "configs/policy.yaml"


class SandboxConfig(_Strict):
    network_mode: str = "none"
    mem_limit: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 512


class ToolOutputConfig(_Strict):
    max_chars: int = 8000


class LimitsConfig(_Strict):
    max_steps: int = 50  # dev default
    max_cost_per_task_usd: float = 0.5  # dev default; fixed standard price
    max_wall_seconds: float = 1800.0  # dev default


class AgentConfig(_Strict):
    llm: LLMConfig = LLMConfig()
    action_space: Literal["bash_only", "tools"] = "tools"
    planning: Literal["on", "off"] = "on"
    # the system prompt: v1 is the frozen text of v0.1; lite drops two instructions and adds
    # one (agent/prompts.py, docs/notes.md section 25.1, plan.md "M0 与 B0 差距的验证")
    prompt_variant: Literal["v1", "lite"] = "v1"
    context: ContextConfig = ContextConfig()
    guard: GuardConfig = GuardConfig()
    sandbox: SandboxConfig = SandboxConfig()
    tool_output: ToolOutputConfig = ToolOutputConfig()
    limits: LimitsConfig = LimitsConfig()
    bash_timeout_s: int = 120
    seed: int = 0

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True)

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:12]


def summary_llm_config(cfg: AgentConfig) -> LLMConfig:
    """The client config for compaction calls: the cheap model, thinking always off (it is a
    plain summarization), retry policy shared with the main client."""
    model = cfg.context.summary_model or os.environ.get("MODEL_CHEAP")
    if not model:
        raise ValueError(
            "no summary model: set context.summary_model in the config or MODEL_CHEAP in .env"
        )
    return cfg.llm.model_copy(
        update={
            "model": model,
            "thinking": "disabled",
            "reasoning_effort": None,
            "max_tokens": cfg.context.summary_tokens_cap(),
            "timeout_s": 300.0,
        }
    )


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    parent = data.pop("extends", None)  # a path relative to this file
    if parent:
        data = _merge(_load_yaml(path.parent / parent), data)
    return data


def check_summary_sizes(ctx: ContextConfig) -> None:
    """Checked on every config loaded from a file (the experiment configs), not in the model
    itself: unit tests use budgets of a few thousand tokens with a fake summarizer.

    - the word counts must lie in the range the expected size was calibrated on: outside it
      the line is an extrapolation
    - the summary cap (the summary call's max_tokens) must hold a full summary, or it ends
      with finish_reason=length and is thrown away"""
    if ctx.strategy not in NEEDS_SUMMARIZER:
        return
    lo, hi = ctx.summary_calibration_words
    for name in ("summary_words_minimal", "summary_words_full"):
        if not lo <= getattr(ctx, name) <= hi:
            raise ValueError(
                f"{name} = {getattr(ctx, name)} is outside the calibrated range {lo}-{hi} "
                f"of the expected summary size: recalibrate (summary_probe.py calibrate)"
            )
    expected = ctx.summary_tokens_for(ctx.summary_words_full)
    if ctx.summary_tokens_cap() < expected:
        raise ValueError(
            f"the summary cap min(summary_max_tokens, summary_max_ratio * budget_tokens) "
            f"= {ctx.summary_tokens_cap()} is below the expected size of a full summary "
            f"({expected} tokens for {ctx.summary_words_full} words); raise summary_max_ratio"
        )


def load_config(path: str | Path | None) -> AgentConfig:
    if path is None:
        return AgentConfig()
    cfg = AgentConfig.model_validate(_load_yaml(Path(path)))
    check_summary_sizes(cfg.context)
    return cfg


def config_diff(
    base: AgentConfig, other: AgentConfig
) -> list[tuple[str, object, object]]:
    """Leaf settings where `other` differs from `base`: [(dotted.path, base, other)]."""

    def flat(d: dict, prefix: str = "") -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in d.items():
            out.update(
                flat(v, f"{prefix}{k}.") if isinstance(v, dict) else {f"{prefix}{k}": v}
            )
        return out

    a, b = flat(base.model_dump(mode="json")), flat(other.model_dump(mode="json"))
    return [(k, a.get(k), b.get(k)) for k in sorted(a | b) if a.get(k) != b.get(k)]
