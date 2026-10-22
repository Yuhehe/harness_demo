"""Asking a person about `ask` decisions (security/guard.py Approver, agent/loop.py, cli.py)."""

import json

import pytest
from fakes import FakeSandbox, ScriptedLLM

from harness import cli
from harness.agent.loop import run_task
from harness.agent.state import Task
from harness.config import AgentConfig, GuardConfig, LimitsConfig, LLMConfig
from harness.llm.types import LLMResponse, ToolCall, Usage
from harness.security.guard import Decision
from harness.tracing.tracer import MemoryTracer

ASK = "git reset --hard"  # ask.git_destructive in configs/policy.yaml
DENY = "cat ~/.ssh/id_rsa"  # deny.ssh_access


def reply(cmd, id_):
    call = ToolCall(id_, "bash", json.dumps({"command": cmd}))
    return LLMResponse("", None, (call,), "tool_calls", Usage(10, 1, 0, 0), "m")


def submit(id_):
    return LLMResponse(
        "",
        None,
        (ToolCall(id_, "submit", "{}"),),
        "tool_calls",
        Usage(10, 1, 0, 0),
        "m",
    )


def cfg():
    return AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        guard=GuardConfig(enabled=True),
        limits=LimitsConfig(
            max_steps=5, max_cost_per_task_usd=0.5, max_wall_seconds=100
        ),
    )


async def run(cmd, approve):
    sb = FakeSandbox()
    llm = ScriptedLLM([reply(cmd, "c1"), submit("c2")])
    tracer = MemoryTracer()
    result = await run_task(
        Task("t", "fix it"), cfg(), sb, llm, tracer, approve=approve
    )
    assert result.submitted
    answer = llm.calls[1]["messages"][-1]["content"]
    return sb, tracer.of("guard_decision")[0], answer


class Approver:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def __call__(self, call, decision):
        self.asked.append((call.name, decision.rule_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_an_approved_ask_runs_and_the_span_says_so():
    approve = Approver(True)
    sb, span, _ = await run(ASK, approve)
    assert approve.asked == [("bash", "ask.git_destructive")]
    assert [c[0] for c in sb.exec_calls] == [ASK]
    assert (span["action"], span["approved"]) == ("ask", True)


async def test_a_declined_ask_does_not_run_and_the_model_is_told():
    sb, span, answer = await run(ASK, Approver(False))
    assert sb.exec_calls == []
    assert span["approved"] is False
    assert "The user declined it. The call was not run." in answer


async def test_without_an_approver_ask_stays_a_deny_as_before():
    sb, span, answer = await run(ASK, None)
    assert sb.exec_calls == [] and "approved" not in span
    assert "not available in this run" in answer


async def test_an_approver_that_fails_counts_as_no():
    sb, span, _ = await run(ASK, Approver(RuntimeError("terminal closed")))
    assert sb.exec_calls == [] and span["approved"] is False


async def test_a_deny_is_never_put_to_the_user():
    approve = Approver(True)
    sb, span, _ = await run(DENY, approve)
    assert approve.asked == [] and sb.exec_calls == []
    assert span["action"] == "deny" and "approved" not in span


# ---- the terminal prompt --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("typed", "allowed"),
    [("y", True), ("YES\n", True), ("", False), ("n", False), ("sure", False)],
)
async def test_the_terminal_prompt(monkeypatch, capsys, typed, allowed):
    monkeypatch.setattr("builtins.input", lambda prompt: typed)
    call = ToolCall("c", "bash", json.dumps({"command": ASK}))
    decision = Decision("ask", "ask.git_destructive", "this discards work", "exec")
    assert await cli.ask_user(call, decision) is allowed
    shown = capsys.readouterr().out
    assert "ask.git_destructive" in shown and ASK in shown


def test_the_prompt_is_only_offered_on_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.approver_for_terminal() is cli.ask_user
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.approver_for_terminal() is None


# ---- wiring: harness run -> run_one -> run_task ---------------------------------------------------
class FakeDockerSandbox:
    def __init__(self, image, **kw):
        self.image = image

    async def __aenter__(self):
        return FakeSandbox()

    async def __aexit__(self, *exc):
        return False


async def test_run_one_hands_the_approver_to_the_loop(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from harness.agent import loop
    from harness.sandbox import docker_sandbox

    seen = {}

    async def fake_run_task(task, cfg_, sandbox, llm, tracer, **kw):
        seen.update(kw)
        return SimpleNamespace(patch="", submitted=True)

    monkeypatch.setattr(loop, "run_task", fake_run_task)
    monkeypatch.setattr(docker_sandbox, "DockerSandbox", FakeDockerSandbox)
    approve = Approver(True)
    await cli.run_one(
        Task("t", "x"),
        "img",
        cfg(),
        tmp_path,
        "R",
        docker_client=None,
        approve=approve,
        meta={},
    )
    assert seen["approve"] is approve


async def test_harness_run_asks_on_a_terminal(monkeypatch, tmp_path):
    # `harness run` passes the terminal prompt to run_one when stdin is a terminal
    import docker

    seen = {}

    async def fake_run_one(*a, **kw):
        seen.update(kw)
        raise SystemExit(0)

    class Client:
        class images:
            @staticmethod
            def get(name):
                return name

    monkeypatch.setattr(cli, "run_one", fake_run_one)
    monkeypatch.setattr(docker, "from_env", lambda: Client())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.chdir(tmp_path)
    args = cli.build_parser().parse_args(
        ["run", "--repo", str(tmp_path), "--task", "x", "--image", "img"]
    )
    with pytest.raises(SystemExit):
        await cli._run(args)
    assert seen["approve"] is cli.ask_user
