"""Shared builders for the context tests: an agent state with turns of a known size, and a
manager wired to scripted fakes."""

from __future__ import annotations

import json

from fakes import FakeSandbox

from harness.agent.state import AgentState, Task
from harness.config import AgentConfig, ContextConfig, LLMConfig
from harness.context.manager import ContextManager
from harness.llm.types import (
    LLMResponse,
    Usage,
    system_message,
    tool_message,
    user_message,
)
from harness.tracing.tracer import MemoryTracer

MODEL = "deepseek/deepseek-flash"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run",
            "parameters": {"type": "object"},
        },
    }
]


def new_state() -> AgentState:
    acfg = AgentConfig(llm=LLMConfig(model=MODEL, thinking="disabled"))
    s = AgentState(task=Task("t", "p"), cfg=acfg, model=MODEL)
    s.append(system_message("system"))
    s.append(user_message("fix the bug"))
    return s


def add_turn(state, i, chars=4000, reasoning=None, output=None, tool="bash", **args):
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"c{i}",
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": json.dumps(args or {"command": f"cmd {i}"}),
                },
            }
        ],
    }
    if reasoning:
        msg["reasoning_content"] = reasoning
    state.append(msg)
    state.append(
        tool_message(
            f"c{i}",
            output if output is not None else f"OUT{i} " + "word " * (chars // 5),
        )
    )


def turns(n, chars=4000, start=1):
    s = new_state()
    for i in range(start, start + n):
        add_turn(s, i, chars)
    return s


def reply(
    text="## Tried so far\nran things", *, finish="stop", prompt=3000, completion=250
):
    return LLMResponse(
        content=text,
        reasoning_content=None,
        tool_calls=(),
        finish_reason=finish,
        usage=Usage(prompt_tokens=prompt, completion_tokens=completion),
        model="deepseek-flash",
    )


def manager(strategy, llm=None, tracer=None, sandbox=None, **kw):
    return ContextManager(
        ContextConfig(strategy=strategy, **kw),
        model=MODEL,
        tools=TOOLS,
        sandbox=sandbox or FakeSandbox(),
        tracer=tracer or MemoryTracer(),
        summarizer=llm,
        summary_model=MODEL if llm else None,
    )


def prime(m, state):
    """Load the state's messages into the manager without building a prompt."""
    m._view, m._original, m._synced = (
        list(state.messages),
        list(state.messages),
        len(state.messages),
    )
