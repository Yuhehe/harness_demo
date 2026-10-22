"""Which tools exist for a run, their schemas, and one validated entry point to run them."""

from __future__ import annotations

import json
from typing import Any

from harness.config import AgentConfig
from harness.sandbox.base import Sandbox
from harness.tools.base import OutputLimiter, Tool, ToolResult, validate_args
from harness.tools.bash import BashTool
from harness.tools.edit import StrReplaceTool, ViewTool
from harness.tools.search import SearchTool
from harness.tools.submit import SubmitFlag, SubmitTool
from harness.tools.todo import TodoList, TodoTool


class Registry:
    def __init__(
        self,
        cfg: AgentConfig,
        limiter: OutputLimiter,
        todo: TodoList,
        submit_flag: SubmitFlag,
    ) -> None:
        tools: list[Tool] = [
            BashTool(
                limiter,
                cfg.bash_timeout_s,
                variant=cfg.bash_tool,
                output_format=cfg.tool_output.format,
            ),
            SubmitTool(submit_flag),
        ]
        if cfg.action_space == "tools":
            tools += [ViewTool(limiter), StrReplaceTool(), SearchTool(limiter)]
            if cfg.planning == "on":
                tools.append(TodoTool(todo))
        # Sorted by name: a deterministic tool list keeps the prompt prefix identical (KV cache).
        self._tools = {t.name: t for t in sorted(tools, key=lambda t: t.name)}

    def add(self, tools: list[Tool]) -> None:
        """More tools (MCP, tools/mcp.py), added before the first request; names are unique."""
        clash = sorted(set(self._tools) & {t.name for t in tools})
        if clash:
            raise ValueError(f"tool names already taken: {clash}")
        merged = [*self._tools.values(), *tools]
        self._tools = {t.name: t for t in sorted(merged, key=lambda t: t.name)}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    async def run(self, name: str, arguments: str, sandbox: Sandbox) -> ToolResult:
        """Run one call. Malformed calls come back as format errors, never as exceptions."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                f"Error: unknown tool {name!r}. Available tools: {', '.join(self.names)}.",
                format_error=True,
            )
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            return ToolResult(
                f"Error: the arguments of {name} are not valid JSON ({e.msg} at position {e.pos}).",
                format_error=True,
            )
        problem = (
            validate_args(tool.parameters, args) if tool.validate_locally else None
        )
        if not tool.validate_locally and not isinstance(args, dict):
            problem = f"arguments must be a JSON object, got {type(args).__name__}"
        if problem:
            return ToolResult(f"Error calling {name}: {problem}.", format_error=True)
        return await tool.run(args, sandbox)
