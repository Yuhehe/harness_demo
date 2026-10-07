"""Cost from recorded token counts at a FIXED standard (peak) price.

We deliberately do not call litellm.completion_cost: it applies off-peak pricing from the
current UTC clock, so the same call would cost different amounts at different times
(docs/notes.md section 5). Prices are per token in USD, taken from LiteLLM 1.104.0's local
price map (standard fields). They are a fixed, comparable basis, not an invoice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from harness.llm.types import Usage


@dataclass(frozen=True)
class ModelPrice:
    input: float  # per uncached prompt token
    output: float  # per completion token (reasoning tokens are included in it)
    cache_read: float  # per prompt token served from the cache


PRICES: dict[str, ModelPrice] = {
    "deepseek-flash": ModelPrice(input=3e-07, output=1.2e-06, cache_read=6e-09),
    "deepseek-v4-pro": ModelPrice(input=1.32e-06, output=3.96e-06, cache_read=4.4e-08),
}


class UnknownModelPrice(KeyError):
    """The model has no entry in PRICES. The budget guard is a hard rule, so fail early."""


def normalize_model(model: str) -> str:
    """'deepseek/deepseek-flash' and 'openai/deepseek-flash' -> 'deepseek-flash'."""
    return model.rsplit("/", 1)[-1]


def price_for(model: str) -> ModelPrice:
    key = normalize_model(model)
    try:
        return PRICES[key]
    except KeyError:
        raise UnknownModelPrice(
            f"no price for model {model!r} (known: {sorted(PRICES)}); add it to llm/cost.py"
        ) from None


def cost_usd(model: str, usage: Usage) -> float:
    p = price_for(model)
    return (
        usage.uncached_prompt_tokens * p.input
        + usage.cached_tokens * p.cache_read
        + usage.completion_tokens * p.output
    )


def prompt_cost_usd(model: str, prompt_tokens: int) -> float:
    """Upper bound for tokens we sent but got no usage for (failed attempts): all uncached."""
    return prompt_tokens * price_for(model).input


def prices_hash() -> str:
    """Identifies the price table in run_start, so a changed table is visible."""
    blob = json.dumps(
        {k: [v.input, v.output, v.cache_read] for k, v in sorted(PRICES.items())},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
