"""The mechanism switches of v0.3-exp (docs/notes.md §28): defaults pinned byte for byte, the
`mini` settings checked against mini-swe-agent's own code."""

import importlib.util
from pathlib import Path

import pytest
from fakes import FakeLLMServer, FakeSandbox, ScriptedLLM, Step, ok_body, tool_call

from harness.agent.loop import run_task
from harness.agent.prompts import system_prompt, task_message
from harness.agent.state import Task
from harness.config import AgentConfig, LimitsConfig, LLMConfig, load_config
from harness.llm.client import LiteLLMClient
from harness.llm.replay import fingerprint
from harness.llm.types import LLMResponse, ToolCall, Usage
from harness.sandbox.base import ExecResult
from harness.tools.base import OutputLimiter, mini_observation
from harness.tools.bash import BashTool
from harness.tracing.tracer import MemoryTracer

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
V1_BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a shell command in the repository sandbox (bash, working directory = the "
            "repository root, no network). Returns the combined stdout/stderr and the exit "
            "code. Commands that run too long are killed. Each call starts a fresh shell: `cd` "
            "and environment variables do not persist between calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


def mini_module(name):
    if importlib.util.find_spec("minisweagent") is None:
        pytest.skip("mini-swe-agent (dev dependency) not installed")
    return importlib.import_module(name)


def mini_template():
    yaml = pytest.importorskip("yaml")
    spec = importlib.util.find_spec("minisweagent")
    if spec is None:
        pytest.skip("mini-swe-agent (dev dependency) not installed")
    path = Path(spec.origin).parent / "config/benchmarks/swebench.yaml"
    return yaml.safe_load(path.read_text())["model"]["observation_template"]


def render_mini(output, returncode):
    from jinja2 import StrictUndefined, Template

    return Template(mini_template(), undefined=StrictUndefined).render(
        output={"output": output, "returncode": returncode, "exception_info": ""}
    )


# ---- the bash tool ----------------------------------------------------------------------------------
def test_the_default_bash_tool_is_unchanged():
    assert BashTool(OutputLimiter(8000)).schema() == V1_BASH_SCHEMA


def test_the_mini_bash_tool_is_mini_swe_agents():
    mod = mini_module("minisweagent.models.utils.actions_toolcall")
    assert BashTool(OutputLimiter(8000), variant="mini").schema() == mod.BASH_TOOL
    # instance attributes: the class (and so every other instance) keeps the v1 definition
    assert BashTool(OutputLimiter(8000)).schema() == V1_BASH_SCHEMA


async def test_the_default_result_format_is_unchanged():
    sb = FakeSandbox(handler=lambda c: ExecResult(0, "a\nb\n"))
    r = await BashTool(OutputLimiter(8000)).run({"command": "ls"}, sb)
    assert r.output == "a\nb\n\n[exit code: 0]"
    sb = FakeSandbox(handler=lambda c: ExecResult(1, ""))
    r = await BashTool(OutputLimiter(8000)).run({"command": "true"}, sb)
    assert r.output == "(no output)\n[exit code: 1]"


@pytest.mark.parametrize(
    ("output", "rc"),
    [
        ("a\nb\n", 0),
        ("no trailing newline", 1),
        ("", 0),
        ("x" * 9999, 0),  # just under the limit: shown whole
        ("x" * 10000, 0),  # at the limit: cut
        ("".join(f"line {i:05d}\n" for i in range(2000)), 124),
    ],
)
def test_mini_rendering_matches_mini_swe_agents_template(output, rc):
    text, truncated = mini_observation(output, rc, 10000)
    assert text == render_mini(output, rc)
    assert truncated == (len(output) >= 10000)


async def test_mini_format_saves_nothing_in_the_sandbox():
    sb = FakeSandbox(handler=lambda c: ExecResult(0, "y" * 20000))
    tool = BashTool(OutputLimiter(10000), output_format="mini")
    r = await tool.run({"command": "cat big"}, sb)
    assert r.truncated and r.output == render_mini("y" * 20000, 0)
    assert sb.files == {}


# ---- the step note ------------------------------------------------------------------------------------
def R(*calls):
    return LLMResponse("", None, tuple(calls), "tool_calls", Usage(100, 10, 50, 0), "m")


async def run(cfg):
    llm = ScriptedLLM(
        [
            R(ToolCall("c1", "bash", '{"command": "ls"}')),
            R(ToolCall("c2", "submit", "{}")),
        ]
    )
    sb = FakeSandbox(handler=lambda c: ExecResult(0, "a\n"))
    await run_task(Task("t", "fix it"), cfg, sb, llm, MemoryTracer())
    return llm.calls[-1]


def cfg(**kw):
    return AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        limits=LimitsConfig(
            max_steps=5, max_cost_per_task_usd=0.5, max_wall_seconds=100
        ),
        **kw,
    )


async def test_the_step_note_is_on_by_default_and_off_when_switched_off():
    on = (await run(cfg()))["messages"]
    assert on[3]["content"].startswith("a\n\n[exit code: 0]\n[step 1/5 | cost $")
    off = (await run(cfg(tool_output={"step_note": False})))["messages"]
    assert off[3]["content"] == "a\n\n[exit code: 0]"
    assert "Every tool result ends with a note" in on[0]["content"]
    assert "Every tool result ends with a note" not in off[0]["content"]


def test_without_the_note_the_prompts_lose_only_its_sentence():
    for kw in ({}, {"prompt_variant": "lite"}, {"action_space": "bash_only"}):
        with_note = system_prompt(AgentConfig(**kw))
        without = system_prompt(AgentConfig(**kw, tool_output={"step_note": False}))
        assert (
            with_note.replace(
                "\nEvery tool result ends with a note like [step 3/50 | cost $0.0123 of $0.50]. "
                "It only tells you how much budget is left.",
                "",
            )
            == without
        )
    mini = AgentConfig(prompt_variant="mini", tool_output={"step_note": False})
    assert task_message("T", mini).endswith("not part of the fix.\n</instructions>")


# ---- parallel tool calls ------------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, True])
async def test_parallel_tool_calls_is_sent_only_when_set(monkeypatch, value):
    body = ok_body(
        tool_calls=[tool_call("c1", "bash", '{"command": "ls"}')],
        finish_reason="tool_calls",
    )
    with FakeLLMServer([Step(body=body)]) as server:
        monkeypatch.setenv("DEEPSEEK_API_BASE", server.url)
        client = LiteLLMClient(
            LLMConfig(
                model="deepseek/deepseek-flash",
                thinking="disabled",
                parallel_tool_calls=value,
            ),
            MemoryTracer(),
        )
        await client.complete([{"role": "user", "content": "hi"}], [V1_BASH_SCHEMA])
        sent = server.requests[0]["body"]
    if value is None:
        assert "parallel_tool_calls" not in sent
    else:
        assert sent["parallel_tool_calls"] is True


def test_the_replay_key_changes_only_when_parallel_tool_calls_is_set():
    msgs = [{"role": "user", "content": "x"}]
    base = LLMConfig(model="deepseek/deepseek-flash", thinking="disabled")
    key = fingerprint(base, msgs, [V1_BASH_SCHEMA])[0]
    assert fingerprint(base.model_copy(), msgs, [V1_BASH_SCHEMA])[0] == key
    on = base.model_copy(update={"parallel_tool_calls": True})
    assert fingerprint(on, msgs, [V1_BASH_SCHEMA])[0] != key


# ---- D* as a whole -----------------------------------------------------------------------------------
async def test_d_star_sends_mini_swe_agents_tool_and_results():
    mod = mini_module("minisweagent.models.utils.actions_toolcall")
    c = load_config(CONFIGS / "ablations/mech_aligned.yaml")
    assert not c.guard.enabled and c.llm.parallel_tool_calls is True
    # no compaction in a two-step run; `none` spares the test a summarizer client
    c = c.model_copy(
        update={
            "limits": cfg().limits,
            "context": c.context.model_copy(update={"strategy": "none"}),
        }
    )
    call = await run(c)
    sent = call["messages"]
    assert sent[0]["content"] == (
        "You are a helpful assistant that can interact with a computer shell to solve "
        "programming tasks."
    )
    assert sent[3]["content"] == render_mini(
        "a\n", 0
    )  # no exit-code line, no step note
    tools = {t["function"]["name"]: t for t in call["tools"]}
    assert set(tools) == {"bash", "submit"} and tools["bash"] == mod.BASH_TOOL
