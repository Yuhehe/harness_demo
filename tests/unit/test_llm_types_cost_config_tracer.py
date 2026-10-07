import json
from unittest import mock

import litellm
import pytest
from litellm import ModelResponse
from litellm.cost_calculator import completion_cost
from litellm.types.utils import Usage as LiteUsage

from harness.config import AgentConfig, LLMConfig, load_config
from harness.llm import cost
from harness.llm.types import (
    LLMResponse,
    ToolCall,
    Usage,
    assistant_message,
    tool_message,
)
from harness.tracing.tracer import JsonlTracer, MemoryTracer


def make_response(**kw):
    base = {
        "content": "hi",
        "reasoning_content": "thinking",
        "tool_calls": (ToolCall("c1", "bash", '{"command": "ls"}'),),
        "finish_reason": "tool_calls",
        "usage": Usage(
            prompt_tokens=100,
            completion_tokens=20,
            cached_tokens=64,
            reasoning_tokens=5,
        ),
        "model": "deepseek-flash",
        "response_id": "r1",
    }
    base.update(kw)
    return LLMResponse(**base)


# ---- types -----------------------------------------------------------------------------
def test_response_roundtrip_through_json():
    r = make_response()
    assert LLMResponse.from_dict(json.loads(json.dumps(r.to_dict()))) == r


def test_assistant_message_keeps_reasoning_and_tool_calls():
    m = assistant_message(make_response())
    assert m["reasoning_content"] == "thinking"
    assert m["tool_calls"][0]["function"] == {
        "name": "bash",
        "arguments": '{"command": "ls"}',
    }
    assert m["tool_calls"][0]["type"] == "function"


def test_assistant_message_can_drop_tool_calls():
    assert "tool_calls" not in assistant_message(make_response(), keep_tool_calls=False)


def test_assistant_message_without_reasoning_has_no_key():
    assert "reasoning_content" not in assistant_message(
        make_response(reasoning_content=None)
    )


def test_tool_message_shape():
    assert tool_message("c1", "ok") == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "ok",
    }


# ---- cost ------------------------------------------------------------------------------
def test_cost_formula_matches_the_documented_example():
    usage = Usage(prompt_tokens=10000, completion_tokens=500, cached_tokens=8000)
    # (2000 * 3e-7) + (8000 * 6e-9) + (500 * 1.2e-6) = 0.001248
    assert cost.cost_usd("deepseek/deepseek-flash", usage) == pytest.approx(0.001248)


def test_prefixes_are_stripped():
    assert cost.normalize_model("openai/deepseek-v4-pro") == "deepseek-v4-pro"
    assert cost.price_for("deepseek/deepseek-v4-pro") == cost.PRICES["deepseek-v4-pro"]


def test_unknown_model_fails_early():
    with pytest.raises(cost.UnknownModelPrice):
        cost.price_for("deepseek/not-a-model")


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_table_equals_litellm_standard_price_whatever_the_clock(model):
    """Guards drift against LiteLLM's table, and shows our number is the peak price."""
    for key in (model, f"deepseek/{model}"):
        entry = litellm.model_cost[key]
        assert entry["input_cost_per_token"] == cost.PRICES[model].input
        assert entry["output_cost_per_token"] == cost.PRICES[model].output
        assert entry["cache_read_input_token_cost"] == cost.PRICES[model].cache_read
    usage = Usage(prompt_tokens=10000, completion_tokens=500, cached_tokens=8000)
    r = ModelResponse(model=model)
    r.usage = LiteUsage(
        prompt_tokens=10000,
        completion_tokens=500,
        total_tokens=10500,
        prompt_cache_hit_tokens=8000,
        prompt_cache_miss_tokens=2000,
    )
    off_peak = "litellm.litellm_core_utils.llm_cost_calc.utils._is_off_peak"
    with mock.patch(off_peak, return_value=False):
        assert cost.cost_usd(model, usage) == pytest.approx(
            completion_cost(r, model=f"deepseek/{model}")
        )


def test_prices_hash_is_stable():
    assert cost.prices_hash() == cost.prices_hash() and len(cost.prices_hash()) == 12


# ---- config ----------------------------------------------------------------------------
def test_thinking_pairs_max_tokens_and_timeout():
    on = LLMConfig(thinking="enabled")
    off = LLMConfig(thinking="disabled")
    assert (on.max_tokens, on.timeout_s) == (65536, 900.0)
    assert (off.max_tokens, off.timeout_s) == (16384, 300.0)


def test_explicit_values_are_kept():
    c = LLMConfig(thinking="disabled", max_tokens=100, timeout_s=5)
    assert (c.max_tokens, c.timeout_s) == (100, 5)


def test_model_comes_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_MAIN", "deepseek/deepseek-flash")
    assert LLMConfig().resolved_model() == "deepseek/deepseek-flash"
    monkeypatch.delenv("MODEL_MAIN")
    with pytest.raises(ValueError, match="no model"):
        LLMConfig().resolved_model()


def test_unknown_config_keys_are_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("planing: on\n")
    with pytest.raises(Exception, match="planing"):
        load_config(p)


def test_load_config_and_hash(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("action_space: bash_only\nllm:\n  thinking: disabled\n")
    cfg = load_config(p)
    assert cfg.action_space == "bash_only" and cfg.llm.max_tokens == 16384
    assert cfg.config_hash() == load_config(p).config_hash()
    assert cfg.config_hash() != AgentConfig().config_hash()


# ---- tracer ----------------------------------------------------------------------------
def test_jsonl_tracer_writes_one_flushed_line_per_span(tmp_path):
    path = tmp_path / "a" / "t.jsonl"
    t = JsonlTracer(path, run_id="r", instance_id="i")
    t.emit("run_start", model="m")
    t.emit("llm_call", attempt=1)
    # readable before close: every span is flushed immediately
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["type"] for x in lines] == ["run_start", "llm_call"]
    assert (
        lines[0]["run_id"] == "r"
        and lines[0]["instance_id"] == "i"
        and "ts" in lines[0]
    )
    t.close()


def test_memory_tracer_filters_by_type():
    t = MemoryTracer()
    t.emit("a", x=1)
    t.emit("b")
    assert t.of("a") == [{"type": "a", "x": 1}]
