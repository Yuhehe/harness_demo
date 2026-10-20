"""The system prompt variants (agent/prompts.py, config `prompt_variant`)."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

from harness.agent.prompts import system_prompt, task_message
from harness.config import AgentConfig, load_config

CONFIGS = Path(__file__).resolve().parents[2] / "configs"

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


# ---- task message and the `mini` variant (plan.md stage 3) -----------------------------------------
def test_v1_task_message_is_the_frozen_one():
    want = "96cf9efb6d927f2ad2c373af73c04c76942fd2acf1314b36dad246e25125bb5e"
    assert sha(task_message("Fix the bug.\n")) == want
    assert sha(task_message("Fix the bug.\n", AgentConfig())) == want
    assert (
        sha(task_message("Fix the bug.\n", AgentConfig(prompt_variant="lite"))) == want
    )


def test_existing_config_hashes_do_not_move():
    # v0.3-exp added config keys (bash_tool, tool_output.format / step_note,
    # llm.parallel_tool_calls), so every hash moved once (results/CHANGELOG.md); within v0.3-exp
    # they must not move. That the default requests did not change is pinned at the request
    # level: the prompt and task hashes above, tests/unit/test_mini_mechanics.py, the replay e2e.
    assert load_config(CONFIGS / "default.yaml").config_hash() == "fff4b25214bf"
    assert (
        load_config(CONFIGS / "ablations/prompt_lite.yaml").config_hash()
        == "1d7c45329897"
    )


def _mini_templates():
    yaml = pytest.importorskip("yaml")
    spec = importlib.util.find_spec("minisweagent")
    if spec is None:
        pytest.skip("mini-swe-agent (dev dependency) not installed")
    path = Path(spec.origin).parent / "config/benchmarks/swebench.yaml"
    return yaml.safe_load(path.read_text())["agent"]


def test_mini_is_mini_swe_agents_prompt_up_to_the_submission_section():
    agent = _mini_templates()
    cfg = AgentConfig(action_space="bash_only", prompt_variant="mini")
    from jinja2 import StrictUndefined, Template

    # what mini sends is the Jinja rendering, which drops the template's trailing newline
    rendered = Template(agent["system_template"], undefined=StrictUndefined).render()
    assert system_prompt(cfg) == rendered
    # braces must survive formatting; mini inserts the text as is (no strip)
    task = "\nMake `add` return a + b {not a - b}.\n"
    want_head = (
        agent["instance_template"].split("## Submission")[0].replace("{{task}}", task)
    )
    msg = task_message(task, cfg)
    assert msg.startswith(want_head)
    tail = msg[len(want_head) :]
    assert tail.startswith("## Submission\n") and tail.endswith("</instructions>")
    assert "call the submit tool" in tail and "[step 3/50" in tail
    assert (
        "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in msg
    )  # mini's own submit procedure


def test_mini_config_changes_the_two_settings_it_is_named_for():
    cfg = load_config(CONFIGS / "ablations/prompt_mini_bash.yaml")
    assert (cfg.action_space, cfg.prompt_variant) == ("bash_only", "mini")
