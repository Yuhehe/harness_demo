from __future__ import annotations

from typing import Any

from harness.sandbox.base import Sandbox
from harness.tools.base import OutputLimiter, Tool, ToolResult, object_schema


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command in the repository sandbox (bash, working directory = the repository "
        "root, no network). Returns the combined stdout/stderr and the exit code. Commands that "
        "run too long are killed. Each call starts a fresh shell: `cd` and environment "
        "variables do not persist between calls."
    )
    parameters = object_schema(
        {"command": {"type": "string", "description": "The shell command to run."}},
        ["command"],
    )

    def __init__(self, limiter: OutputLimiter, timeout_s: int = 120) -> None:
        self.limiter, self.timeout_s = limiter, timeout_s

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        result = await sandbox.exec(args["command"], timeout=self.timeout_s)
        text, truncated = await self.limiter.apply(result.output, sandbox)
        body = text if text.strip() else "(no output)"
        return ToolResult(
            output=f"{body}\n[exit code: {result.exit_code}]",
            exit_code=result.exit_code,
            truncated=truncated,
        )
