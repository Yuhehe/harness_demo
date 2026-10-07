"""Run configuration (pydantic v2 + YAML).

Day 1 holds only what the loop needs. The ablation files and the diff printing are Day 2.
Limits below are DEVELOPMENT defaults, not experiment values: the real step, cost and
wall-clock limits are fixed in configs/default.yaml when the main experiment is set up.
"""

from __future__ import annotations

import hashlib
import json
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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Strict):
    model: str | None = None  # None -> $MODEL_MAIN
    thinking: Literal["enabled", "disabled"] = (
        "enabled"  # open question: decide on dev_5
    )
    max_tokens: int | None = None  # None -> paired default for `thinking`
    timeout_s: float | None = None  # None -> paired default for `thinking`
    max_attempts: int = 3
    backoff_base_s: float = 4.0
    backoff_factor: float = 3.0
    backoff_cap_s: float = 60.0

    @model_validator(mode="after")
    def _fill_pair(self) -> LLMConfig:
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


class GuardConfig(_Strict):
    enabled: bool = True


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
    context: ContextConfig = ContextConfig()
    guard: GuardConfig = GuardConfig()
    tool_output: ToolOutputConfig = ToolOutputConfig()
    limits: LimitsConfig = LimitsConfig()
    bash_timeout_s: int = 120
    seed: int = 0

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True)

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:12]


def load_config(path: str | Path | None) -> AgentConfig:
    if path is None:
        return AgentConfig()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return AgentConfig.model_validate(data)
