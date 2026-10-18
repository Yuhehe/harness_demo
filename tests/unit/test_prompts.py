"""The system prompt variants (agent/prompts.py, config `prompt_variant`)."""

import hashlib

import pytest

from harness.agent.prompts import system_prompt
from harness.config import AgentConfig

# sha256 of the system prompt at v0.1.1 (prompts.py unchanged since v0.1-frozen): v1 must
# stay byte for byte what M0, B0's limits, A2 and A6 ran with
V1 = {
    "tools+planning": "65cb00700708bed1ebe21f6025e091165c9fd3802a5c8cfea4a805f8b9439d0e",
    "bash_only": "e1b9a43b458e4bb5c819179ac43f5c6e0d6e541ca1cc7e7fe1f4c31d9567a869",
    "planning off": "e1b9a43b458e4bb5c819179ac43f5c6e0d6e541ca1cc7e7fe1f4c31d9567a869",
}
CFG = {
    "tools+planning": {},
    "bash_only": {"action_space": "bash_only"},
    "planning off": {"planning": "off"},
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("name", V1)
def test_v1_is_the_frozen_prompt(name):
    assert AgentConfig(**CFG[name]).prompt_variant == "v1"  # the default
    assert sha(system_prompt(AgentConfig(**CFG[name]))) == V1[name]


@pytest.mark.parametrize("name", V1)
def test_lite_makes_exactly_the_three_edits(name):
    v1 = system_prompt(AgentConfig(**CFG[name])).splitlines()
    lite = system_prompt(AgentConfig(**CFG[name], prompt_variant="lite")).splitlines()
    assert "- Run the relevant tests after changing code, and read the output." in v1
    assert set(v1) - set(lite) == {
        "- Run the relevant tests after changing code, and read the output.",
        (
            "- Keep outputs small (use head, grep, line ranges). Long outputs are cut and saved "
            "to a file."
        ),
    }
    assert set(lite) - set(v1) == {
        (
            "- When commands do not depend on each other, issue them as several tool calls in "
            "one response."
        ),
        "- Long outputs are cut and saved to a file.",
    }
    # everything else, including the planning line and the budget note, is untouched
    assert len(lite) == len(v1)


def test_lite_keeps_the_planning_line_only_where_v1_has_it():
    plan = "- Before acting, write a short plan with the todo tool, and keep it up to date."
    assert plan in system_prompt(AgentConfig(prompt_variant="lite"))
    assert plan not in system_prompt(AgentConfig(planning="off", prompt_variant="lite"))


def test_the_variant_is_part_of_the_config_hash():
    assert (
        AgentConfig().config_hash() != AgentConfig(prompt_variant="lite").config_hash()
    )
    assert (
        '"prompt_variant": "lite"'
        in AgentConfig(prompt_variant="lite").canonical_json()
    )


def test_an_unknown_variant_is_rejected():
    with pytest.raises(ValueError):
        AgentConfig(prompt_variant="short")
