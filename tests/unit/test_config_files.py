"""configs/default.yaml and the ablation files: each ablation changes exactly one factor."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.config import (
    NEEDS_SUMMARIZER,
    AgentConfig,
    LLMConfig,
    config_diff,
    load_config,
    summary_llm_config,
)

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
ABLATIONS = sorted((CONFIGS / "ablations").glob("*.yaml"))

# file -> the leaf settings it must change, and nothing else
EXPECTED = {
    "ctx_none": {"context.strategy"},
    "bash_only": {"action_space"},
    "ctx_truncate": {"context.strategy"},
    "ctx_summarize": {"context.strategy"},
    "no_planning": {"planning"},
    "budget_32k": {"context.budget_tokens"},
    # the one deliberate pair (plan.md A6: budget pressure combined with A1)
    "budget_32k_none": {"context.budget_tokens", "context.strategy"},
    "no_guard": {"guard.enabled"},
    # exploratory, v0.2-exp (plan.md "M0 与 B0 差距的验证")
    "prompt_lite": {"prompt_variant"},
    # diagnostic pair, v0.2.1-exp (plan.md stage 3): bash only + mini-swe-agent's prompt
    "prompt_mini_bash": {"action_space", "prompt_variant"},
}


@pytest.fixture(scope="module")
def main():
    return load_config(CONFIGS / "default.yaml")


def test_every_ablation_file_is_listed_here():
    assert {p.stem for p in ABLATIONS} == set(EXPECTED)


@pytest.mark.parametrize("path", ABLATIONS, ids=lambda p: p.stem)
def test_an_ablation_changes_only_what_it_is_named_for(main, path):
    changed = {k for k, _, _ in config_diff(main, load_config(path))}
    assert changed == EXPECTED[path.stem]


def test_the_main_configuration_is_what_the_plan_says(main):
    assert main.action_space == "tools" and main.planning == "on"
    assert main.context.strategy == "hybrid" and main.context.budget_tokens == 128_000
    assert main.guard.enabled
    # thinking on, effort high, sent explicitly (never left to the server default)
    assert (main.llm.thinking, main.llm.reasoning_effort) == ("enabled", "high")
    # max_tokens and timeout stay a pair (CLAUDE.md 5.1)
    assert (main.llm.max_tokens, main.llm.timeout_s) == (65536, 900.0)
    assert main.sandbox.network_mode == "none"


def test_ablation_configs_differ_in_hash_and_main_is_stable(main):
    hashes = {load_config(p).config_hash() for p in ABLATIONS} | {main.config_hash()}
    assert len(hashes) == len(ABLATIONS) + 1
    assert main.config_hash() == load_config(CONFIGS / "default.yaml").config_hash()


def test_extends_merges_nested_keys_and_the_child_wins(tmp_path):
    (tmp_path / "base.yaml").write_text(
        "context:\n  strategy: truncate\n  budget_tokens: 1000\nplanning: 'off'\n"
    )
    (tmp_path / "child.yaml").write_text(
        "extends: base.yaml\ncontext:\n  budget_tokens: 2000\n"
    )
    cfg = load_config(tmp_path / "child.yaml")
    assert (cfg.context.strategy, cfg.context.budget_tokens, cfg.planning) == (
        "truncate",
        2000,
        "off",
    )


EXPERIMENT_CONFIGS = sorted(
    p for p in CONFIGS.rglob("*.yaml") if p.name != "policy.yaml"
)


@pytest.mark.parametrize("path", EXPERIMENT_CONFIGS, ids=lambda p: p.stem)
def test_every_config_file_leaves_room_for_a_full_summary(path):
    cfg = load_config(path)  # raises if the summary cap is below a full summary
    if cfg.context.strategy in NEEDS_SUMMARIZER:
        ctx = cfg.context
        assert ctx.summary_tokens_cap() >= ctx.summary_tokens_for(
            ctx.summary_words_full
        )


def test_a_summary_cap_below_a_full_summary_is_rejected_on_load(tmp_path):
    # 20k budget: the cap is 0.08 * 20000 = 1600, under the ~2040 a full summary is expected at
    path = tmp_path / "c.yaml"
    path.write_text("context:\n  strategy: hybrid\n  budget_tokens: 20000\n")
    with pytest.raises(ValueError, match="expected size of a full summary"):
        load_config(path)
    path.write_text("context:\n  strategy: none\n  budget_tokens: 20000\n")
    load_config(path)  # no summarizer, no cap to check
    path.write_text(
        "context:\n  strategy: hybrid\n  budget_tokens: 20000\n"
        "  summary_max_ratio: 0.11\n"
    )
    load_config(path)


def test_unknown_keys_are_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("context:\n  stratgy: none\n")
    with pytest.raises(ValidationError):
        load_config(tmp_path / "bad.yaml")


def test_bare_on_in_yaml_is_a_boolean_and_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("planning: on\n")
    with pytest.raises(ValidationError):
        load_config(tmp_path / "bad.yaml")


# ---- reasoning effort ---------------------------------------------------------------------
def test_effort_defaults_to_high_with_thinking_and_is_none_without():
    assert LLMConfig(thinking="enabled").reasoning_effort == "high"
    assert LLMConfig(thinking="disabled").reasoning_effort is None
    assert LLMConfig(reasoning_effort="max").reasoning_effort == "max"


def test_effort_without_thinking_is_an_error():
    with pytest.raises(ValidationError, match="needs thinking"):
        LLMConfig(thinking="disabled", reasoning_effort="high")


def test_effort_is_part_of_the_config_hash():
    a = AgentConfig(llm=LLMConfig(model="m", reasoning_effort="high"))
    b = AgentConfig(llm=LLMConfig(model="m", reasoning_effort="max"))
    assert a.config_hash() != b.config_hash()


def test_effort_values_other_than_high_and_max_are_rejected():
    # DeepSeek maps low/medium to high server-side; accepting them would hide a no-op
    for bad in ("low", "medium", "none"):
        with pytest.raises(ValidationError):
            LLMConfig(reasoning_effort=bad)


# ---- the summarizer's own client config -------------------------------------------------
def test_summary_config_uses_the_cheap_model_with_thinking_off(monkeypatch):
    monkeypatch.setenv("MODEL_CHEAP", "deepseek/deepseek-flash")
    cfg = AgentConfig(llm=LLMConfig(model="deepseek/deepseek-v4-pro"))
    s = summary_llm_config(cfg)
    assert s.resolved_model() == "deepseek/deepseek-flash"
    assert (s.thinking, s.reasoning_effort) == ("disabled", None)
    assert (s.max_tokens, s.timeout_s) == (cfg.context.summary_max_tokens, 300.0)
    assert s.max_attempts == cfg.llm.max_attempts  # same retry policy


def test_summary_model_can_be_set_in_the_config(monkeypatch):
    monkeypatch.delenv("MODEL_CHEAP", raising=False)
    cfg = AgentConfig.model_validate(
        {"context": {"summary_model": "deepseek/deepseek-flash"}}
    )
    assert summary_llm_config(cfg).resolved_model() == "deepseek/deepseek-flash"
    with pytest.raises(ValueError, match="no summary model"):
        summary_llm_config(AgentConfig())


def test_water_marks_must_be_ordered():
    for kw in (
        {"low_water": 0.9, "high_water": 0.8},
        {"high_water": 1.2},
        {"low_water": 0},
    ):
        with pytest.raises(ValidationError):
            AgentConfig.model_validate({"context": kw})


def test_word_counts_outside_the_calibrated_range_are_rejected_on_load(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("context:\n  strategy: hybrid\n  summary_words_full: 900\n")
    with pytest.raises(ValueError, match="outside the calibrated range"):
        load_config(path)
    path.write_text("context:\n  strategy: hybrid\n  summary_words_minimal: 150\n")
    with pytest.raises(ValueError, match="outside the calibrated range"):
        load_config(path)
    path.write_text("context:\n  strategy: none\n  summary_words_full: 900\n")
    load_config(path)  # no summarizer: nothing to check


def test_a_run_refuses_a_summarizer_it_was_not_calibrated_for(monkeypatch):
    from harness.cli import check_summarizer
    from harness.context.strategies.summarize import PROMPT_SHA

    main = load_config(CONFIGS / "default.yaml")
    monkeypatch.setenv("MODEL_CHEAP", "deepseek/deepseek-flash")
    check_summarizer(main)  # the calibrated model and prompt: fine
    assert main.context.summary_calibration_prompt == PROMPT_SHA

    monkeypatch.setenv("MODEL_CHEAP", "deepseek/deepseek-v4-pro")
    with pytest.raises(ValueError, match="calibrated with"):
        check_summarizer(main)

    monkeypatch.setenv("MODEL_CHEAP", "deepseek/deepseek-flash")
    ctx = main.context.model_copy(update={"summary_calibration_prompt": "0" * 12})
    with pytest.raises(ValueError, match="summary prompt changed"):
        check_summarizer(main.model_copy(update={"context": ctx}))

    monkeypatch.setenv("MODEL_CHEAP", "deepseek/deepseek-v4-pro")
    off = main.context.model_copy(update={"strategy": "none"})
    check_summarizer(main.model_copy(update={"context": off}))  # no summarizer
