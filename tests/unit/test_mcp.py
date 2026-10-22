"""tools/mcp.py: MCP servers in the sandbox (here a local process; tests/unit/test_mcp_docker.py
runs one in the toy container)."""

import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from fakes import FakeSandbox, ScriptedLLM

from harness.agent.loop import run_task
from harness.agent.state import Task
from harness.config import AgentConfig, GuardConfig, LimitsConfig, LLMConfig, MCPConfig
from harness.llm.types import LLMResponse, ToolCall, Usage
from harness.security.guard import Guard
from harness.security.policy import Policy
from harness.tools.base import OutputLimiter
from harness.tools.mcp import (
    MCPStartError,
    allowed_names,
    start_servers,
    tool_name,
    tools_hash,
)
from harness.tools.registry import Registry
from harness.tools.submit import SubmitFlag
from harness.tools.todo import TodoList
from harness.tracing.tracer import MemoryTracer

ROOT = Path(__file__).resolve().parents[2]
SERVER = [sys.executable, str(ROOT / "tests/fixtures/mcp_server.py")]
POLICY = ROOT / "configs/policy.yaml"


def mcp_cfg(*flags, allow=(), start_timeout_s=10.0, **kw):
    return MCPConfig(
        servers=[{"name": "fx", "command": [*SERVER, *flags], "allow": list(allow),
                  "start_timeout_s": start_timeout_s}],
        **kw,
    )  # fmt: skip


async def started(cfg, sb=None):
    sb = sb or FakeSandbox()
    stack = AsyncExitStack()
    try:
        tools = await start_servers(cfg, sb, stack, OutputLimiter(8000))
    except BaseException:
        await stack.aclose()  # as run_task does: the caller closes what was opened
        raise
    return stack, {t.name: t for t in tools}, sb


# ---- names --------------------------------------------------------------------------------------
def test_names_are_prefixed_and_sanitized():
    assert tool_name("fx", "echo") == "mcp__fx__echo"
    assert tool_name("fx", "math.add") == "mcp__fx__math_add"
    long_a, long_b = tool_name("fx", "a" * 200), tool_name("fx", "a" * 199 + "b")
    assert len(long_a) == len(long_b) == 128 and long_a != long_b
    assert all(c.isalnum() or c in "_-" for c in tool_name("fx", "x y/z:ü"))


# ---- discovery and calls ------------------------------------------------------------------------
async def test_tools_are_listed_once_sorted_labelled_and_their_schemas_sorted():
    stack, tools, _ = await started(mcp_cfg())
    async with stack:
        assert list(tools) == ["mcp__fx__echo", "mcp__fx__fail", "mcp__fx__math_add"]
        echo = tools["mcp__fx__echo"]
        assert echo.description == "(MCP server `fx`, tool `echo`) Echo the text back."
        assert list(echo.parameters) == sorted(echo.parameters)
        assert tools["mcp__fx__fail"].parameters == {"type": "object"}


async def test_a_long_description_is_cut():
    stack, tools, _ = await started(
        mcp_cfg("--long-description", max_description_chars=100)
    )
    async with stack:
        desc = tools["mcp__fx__echo"].description
        assert desc.endswith("x" * 100 + " [description cut]") and len(desc) < 160


async def test_calls_results_errors_and_format_errors():
    stack, tools, sb = await started(mcp_cfg())
    async with stack:
        r = await tools["mcp__fx__echo"].run({"text": "hello"}, sb)
        assert (r.output, r.exit_code, r.format_error) == ("hello", 0, False)
        r = await tools["mcp__fx__math_add"].run({"a": 2, "b": 3}, sb)
        assert r.output == "5"
        r = await tools["mcp__fx__fail"].run({}, sb)  # the tool ran and failed
        assert (r.output, r.exit_code, r.format_error) == ("boom", 1, False)
        r = await tools["mcp__fx__math_add"].run(
            {"a": "x"}, sb
        )  # bad arguments: the call's fault
        assert r.exit_code == 1 and r.format_error and "Invalid params" in r.output


async def test_a_server_that_dies_mid_task_fails_the_call_not_the_run():
    stack, tools, sb = await started(mcp_cfg())
    async with stack:
        await sb.opened[0].close()
        r = await tools["mcp__fx__echo"].run({"text": "hi"}, sb)
        # the SDK reports the lost connection (-32000): a failed call, not a format error
        assert r.exit_code == 1 and not r.format_error and "MCP server fx" in r.output


async def test_a_server_that_does_not_start_is_reported_with_its_stderr():
    with pytest.raises(
        MCPStartError, match="fx did not start.*fixture MCP server ready"
    ):
        await started(mcp_cfg("--die"))


async def test_a_server_that_never_answers_times_out():
    cfg = MCPConfig(servers=[{"name": "fx", "command": [sys.executable, "-c",
                    "import time; time.sleep(60)"], "start_timeout_s": 0.5}])  # fmt: skip
    with pytest.raises(MCPStartError, match="did not start"):
        await started(cfg)


async def test_an_oversized_input_schema_is_refused():
    with pytest.raises(MCPStartError, match="input schema of 'echo' is .* over"):
        await started(mcp_cfg("--big-schema"))


async def test_names_that_clash_after_sanitizing_are_refused():
    with pytest.raises(MCPStartError, match="same name"):
        await started(mcp_cfg("--dup"))


async def test_the_hash_follows_what_the_model_sees():
    s1, t1, _ = await started(mcp_cfg())
    s2, t2, _ = await started(mcp_cfg("--long-description"))
    async with s1, s2:
        assert tools_hash(list(t1.values())) != tools_hash(list(t2.values()))
        assert tools_hash(list(t1.values())) == tools_hash(list(reversed(t1.values())))


# ---- registry and guard -------------------------------------------------------------------------
async def test_the_registry_leaves_argument_checks_to_the_server():
    stack, tools, sb = await started(mcp_cfg())
    async with stack:
        reg = Registry(AgentConfig(), OutputLimiter(8000), TodoList(), SubmitFlag())
        reg.add(list(tools.values()))
        assert reg.names == sorted(reg.names)
        r = await reg.run("mcp__fx__echo", '{"text": "hi", "extra": 1}', sb)
        assert r.output == "hi"  # not rejected locally for the extra key
        r = await reg.run("mcp__fx__echo", "[1]", sb)
        assert r.format_error and "JSON object" in r.output
        with pytest.raises(ValueError, match="already taken"):
            reg.add([tools["mcp__fx__echo"]])


def call(name, args="{}"):
    return ToolCall("c", name, args)


async def test_mcp_tools_ask_unless_allowed():
    stack, tools, _ = await started(mcp_cfg(allow=["echo"]))
    async with stack:
        guard = Guard(Policy.load(POLICY, "/testbed"))
        guard.mcp_allowed = allowed_names(mcp_cfg(allow=["echo"]), list(tools.values()))
        assert guard.check(call("mcp__fx__echo")).rule_id == "allow.mcp"
        d = guard.check(call("mcp__fx__math_add"))
        assert (d.action, d.rule_id) == ("ask", "ask.mcp_tool")
        assert Guard(None, enabled=False).check(call("mcp__fx__math_add")).allowed


# ---- in a run -----------------------------------------------------------------------------------
def R(*calls):
    return LLMResponse("", None, tuple(calls), "tool_calls", Usage(10, 1, 0, 0), "m")


def run_cfg(mcp, steps=6):
    return AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        guard=GuardConfig(enabled=True),
        limits=LimitsConfig(
            max_steps=steps, max_cost_per_task_usd=0.5, max_wall_seconds=100
        ),
        mcp=mcp,
    )


async def test_a_run_uses_mcp_tools_and_closes_the_server():
    llm = ScriptedLLM([
        R(ToolCall("c1", "mcp__fx__echo", '{"text": "from mcp"}')),
        R(ToolCall("c2", "mcp__fx__math_add", '{"a": 1, "b": 2}')),  # not allowed: ask -> deny
        R(ToolCall("c3", "submit", "{}")),
    ])  # fmt: skip
    sb, tracer = FakeSandbox(), MemoryTracer()
    result = await run_task(
        Task("t", "x"), run_cfg(mcp_cfg(allow=["echo"])), sb, llm, tracer
    )
    assert result.submitted
    sent = llm.calls[1]
    assert "mcp__fx__echo" in [t["function"]["name"] for t in sent["tools"]]
    assert sent["messages"][-1]["content"].startswith("from mcp")
    blocked = llm.calls[2]["messages"][-1]["content"]
    assert "ask.mcp_tool" in blocked and "not available in this run" in blocked
    start = tracer.of("run_start")[0]
    assert (
        "mcp__fx__echo" in start["tools"]
        and start["mcp_tools_hash"]
        and start["mcp_error"] is None
    )
    assert sb.opened[0].proc.returncode is not None  # the server was stopped


async def test_a_run_stopped_by_a_limit_still_closes_the_server():
    llm = ScriptedLLM([R(ToolCall("c1", "mcp__fx__echo", '{"text": "a"}'))] * 3)
    sb = FakeSandbox()
    result = await run_task(Task("t", "x"), run_cfg(mcp_cfg(allow=["echo"]), steps=1), sb, llm,
                            MemoryTracer())  # fmt: skip
    assert result.fail_reason.value == "step_limit"
    assert sb.opened[0].proc.returncode is not None


async def test_a_server_that_does_not_start_ends_the_task_cleanly():
    tracer = MemoryTracer()
    llm = ScriptedLLM([])
    result = await run_task(
        Task("t", "x"), run_cfg(mcp_cfg("--die")), FakeSandbox(), llm, tracer
    )
    assert (
        result.fail_reason.value == "env_error"
        and "fx did not start" in result.fail_detail
    )
    assert tracer.of("run_start")[0]["mcp_error"] and tracer.of("run_end")
    assert llm.calls == []


async def test_without_servers_nothing_starts_and_run_start_is_unchanged():
    sb, tracer = FakeSandbox(), MemoryTracer()
    llm = ScriptedLLM([R(ToolCall("c1", "submit", "{}"))])
    await run_task(Task("t", "x"), run_cfg(MCPConfig()), sb, llm, tracer)
    assert sb.opened == []
    assert "mcp_tools_hash" not in tracer.of("run_start")[0]
    assert not any(
        t["function"]["name"].startswith("mcp__") for t in llm.calls[0]["tools"]
    )


def test_the_raw_protocol_is_what_notes_32_shows(tmp_path):
    # keep the documented exchange honest: initialize, initialized, tools/list, tools/call
    import asyncio

    log = tmp_path / "wire.log"

    async def go():
        stack, tools, sb = await started(mcp_cfg("--log", str(log)))
        async with stack:
            await tools["mcp__fx__echo"].run({"text": "hi"}, sb)

    asyncio.run(go())
    methods = [json.loads(line[3:]).get("method") for line in log.read_text().splitlines()
               if line.startswith("-> ")]  # fmt: skip
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    first = json.loads(log.read_text().splitlines()[0][3:])
    assert first["params"]["clientInfo"]["name"] == "harness-lab"


class NoExecSandbox(FakeSandbox):
    async def open_process(self, argv):
        from harness.sandbox.docker_sandbox import SandboxError

        raise SandboxError("exec failed: no such container")


async def test_a_sandbox_that_cannot_start_the_process_still_ends_the_task_cleanly():
    # open_process itself can raise (a Docker API error): still run_start, run_end, env_error
    tracer, llm = MemoryTracer(), ScriptedLLM([])
    result = await run_task(
        Task("t", "x"), run_cfg(mcp_cfg()), NoExecSandbox(), llm, tracer
    )
    assert result.fail_reason.value == "env_error"
    assert "fx did not start: SandboxError" in result.fail_detail
    assert tracer.of("run_start") and tracer.of("run_end") and llm.calls == []


@pytest.mark.parametrize(
    "attempt", range(5)
)  # the race this guards against was intermittent
async def test_a_server_that_dies_mid_run_fails_that_call_and_the_run_still_ends_well(
    attempt,
):
    llm = ScriptedLLM([
        R(ToolCall("c1", "mcp__fx__echo", '{"text": "one"}')),
        R(ToolCall("c2", "mcp__fx__echo", '{"text": "two"}')),  # the server is gone by now
        R(ToolCall("c3", "submit", "{}")),
    ])  # fmt: skip
    tracer = MemoryTracer()
    result = await run_task(
        Task("t", "x"), run_cfg(mcp_cfg("--exit-after", "1", allow=["echo"])), FakeSandbox(),
        llm, tracer,
    )  # fmt: skip
    assert result.submitted and tracer.of("run_end")
    second = llm.calls[2]["messages"][-1]["content"]
    assert second.startswith("Error from MCP server fx") or "MCP server fx" in second
    assert [s["exit_code"] for s in tracer.of("tool_call")][:2] == [0, 1]


class DeadPipe:
    """A server already gone: every send fails, reads wait until the pipe is closed."""

    def __init__(self):
        import asyncio

        self.closed = asyncio.Event()

    async def send(self, data):
        raise ConnectionResetError("Connection lost")

    async def read(self):
        await self.closed.wait()
        return b""

    def stderr_tail(self):
        return ""

    async def close(self):
        self.closed.set()


async def test_a_failed_send_closes_the_connection_instead_of_raising_later():
    # deterministic version of the race: the write fails before the read sees the end
    import anyio
    from mcp import types
    from mcp.shared.message import SessionMessage

    from harness.tools.mcp import pipe_streams

    request = types.jsonrpc_message_adapter.validate_python(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    )
    async with pipe_streams(DeadPipe()) as (read, write):
        await write.send(SessionMessage(request))
        with anyio.fail_after(5), pytest.raises(anyio.EndOfStream):
            await read.receive()  # the session would see "Connection closed"
    # leaving the context raised nothing


async def test_an_error_while_closing_the_servers_never_loses_the_run(monkeypatch):
    from harness.agent import loop

    real = loop.start_servers

    async def start_then_break_on_close(cfg, sandbox, stack, limiter):
        tools = await real(cfg, sandbox, stack, limiter)

        async def boom():
            raise RuntimeError("teardown failed")

        stack.push_async_callback(boom)
        return tools

    monkeypatch.setattr(loop, "start_servers", start_then_break_on_close)
    llm = ScriptedLLM([R(ToolCall("c1", "submit", "{}"))])
    result = await run_task(
        Task("t", "x"), run_cfg(mcp_cfg()), FakeSandbox(), llm, MemoryTracer()
    )
    assert result.submitted
