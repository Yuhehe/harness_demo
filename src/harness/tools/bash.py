from __future__ import annotations

from typing import Any

from harness.sandbox.base import Sandbox
from harness.tools.base import (
    OutputLimiter,
    Tool,
    ToolResult,
    mini_observation,
    object_schema,
)

# `bash_tool: mini` (config): mini-swe-agent's BASH_TOOL (models/utils/actions_toolcall.py),
# which has no `additionalProperties`
MINI_DESCRIPTION = "Execute a bash command"
MINI_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The bash command to execute"}
    },
    "required": ["command"],
}


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

    def __init__(
        self,
        limiter: OutputLimiter,
        timeout_s: int = 120,
        *,
        variant: str = "v1",
        output_format: str = "v1",
    ) -> None:
        self.limiter, self.timeout_s = limiter, timeout_s
        self.output_format = output_format
        if variant == "mini":  # instance attributes shadow the class ones
            self.description, self.parameters = MINI_DESCRIPTION, MINI_PARAMETERS

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        result = await sandbox.exec(args["command"], timeout=self.timeout_s)
        if self.output_format == "mini":
            text, truncated = mini_observation(
                result.output, result.exit_code, self.limiter.max_chars
            )
            return ToolResult(
                output=text, exit_code=result.exit_code, truncated=truncated
            )
        text, truncated = await self.limiter.apply(result.output, sandbox)
        body = text if text.strip() else "(no output)"
        return ToolResult(
            output=f"{body}\n[exit code: {result.exit_code}]",
            exit_code=result.exit_code,
            truncated=truncated,
        )
