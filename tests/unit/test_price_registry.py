"""eval/baselines/litellm_registry.json makes LiteLLM's cost clock-free for the DeepSeek models.

LiteLLM applies off-peak pricing (50% of standard) based on the current UTC time. The registry
override sets the off-peak rates equal to the standard rates so every cost is the standard
("peak") price whatever time it is computed. mini-swe-agent loads it via
LITELLM_MODEL_REGISTRY_PATH (docs/notes.md section 5).
"""

import copy
import json
from pathlib import Path
from unittest import mock

import litellm
import pytest
from litellm import ModelResponse
from litellm.cost_calculator import completion_cost
from litellm.types.utils import Usage

REGISTRY = (
    Path(__file__).resolve().parents[2] / "eval" / "baselines" / "litellm_registry.json"
)
OFF_PEAK_CHECK = "litellm.litellm_core_utils.llm_cost_calc.utils._is_off_peak"
KEYS = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost")


@pytest.fixture
def registry():
    reg = json.loads(REGISTRY.read_text())
    saved = {k: copy.deepcopy(litellm.model_cost[k]) for k in reg}
    yield reg
    litellm.model_cost.update(saved)


def response(model: str) -> ModelResponse:
    r = ModelResponse(model=model)
    r.usage = Usage(
        prompt_tokens=10000,
        completion_tokens=500,
        total_tokens=10500,
        prompt_cache_hit_tokens=8000,
        prompt_cache_miss_tokens=2000,
    )
    return r


def standard_cost(rates: dict) -> float:
    return (
        2000 * rates["input_cost_per_token"]
        + 8000 * rates["cache_read_input_token_cost"]
        + 500 * rates["output_cost_per_token"]
    )


def test_registry_rates_equal_litellm_standard_rates(registry):
    """Guards drift: if LiteLLM changes a standard rate, this file must be regenerated."""
    for model, entry in registry.items():
        off_peak = entry["off_peak_pricing"]
        for key in KEYS:
            assert off_peak[key] == litellm.model_cost[model][key], (model, key)


def test_without_registry_cost_depends_on_the_clock():
    model = "deepseek/deepseek-flash"
    with mock.patch(OFF_PEAK_CHECK, return_value=False):
        peak = completion_cost(response(model), model=model)
    with mock.patch(OFF_PEAK_CHECK, return_value=True):
        off_peak = completion_cost(response(model), model=model)
    assert off_peak == pytest.approx(peak / 2)


@pytest.mark.parametrize(
    "model", ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]
)
def test_with_registry_cost_is_the_standard_price_at_any_time(registry, model):
    litellm.register_model(registry)
    expected = standard_cost(litellm.model_cost[model])
    for off_peak_now in (False, True):
        with mock.patch(OFF_PEAK_CHECK, return_value=off_peak_now):
            assert completion_cost(response(model), model=model) == pytest.approx(
                expected
            )
