from __future__ import annotations

import shlex
from typing import Any

from harness.sandbox.base import Sandbox
from harness.tools.base import OutputLimiter, Tool, ToolResult, object_schema

MAX_MATCHES = 100


class SearchTool(Tool):
    name = "search"
    description = (
        "Search file contents with a regular expression (ripgrep when installed, otherwise "
        f"grep -E). Returns up to {MAX_MATCHES} 'path:line:text' matches. Skips .git."
    )
    parameters = object_schema(
        {
            "pattern": {
                "type": "string",
                "description": "Regular expression to look for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default .).",
            },
            "include": {
                "type": "string",
                "description": "Only files matching this glob, e.g. *.py.",
            },
        },
        ["pattern"],
    )

    def __init__(self, limiter: OutputLimiter) -> None:
        self.limiter = limiter
        self._has_rg: bool | None = None

    async def _rg_available(self, sandbox: Sandbox) -> bool:
        if self._has_rg is None:
            self._has_rg = (
                await sandbox.exec("command -v rg", timeout=10)
            ).exit_code == 0
        return self._has_rg

    def build_command(self, args: dict[str, Any], use_rg: bool) -> str:
        pattern = shlex.quote(args["pattern"])
        path = shlex.quote(args.get("path", "."))
        include = args.get("include")
        if use_rg:
            glob = f" -g {shlex.quote(include)}" if include else ""
            core = f"rg -n --no-heading -e {pattern}{glob} -- {path}"
        else:
            inc = f" --include={shlex.quote(include)}" if include else ""
            core = f"grep -rnIE --exclude-dir=.git{inc} -e {pattern} -- {path}"
        return f"{core} | head -n {MAX_MATCHES}"

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        cmd = self.build_command(args, await self._rg_available(sandbox))
        result = await sandbox.exec(cmd, timeout=60)
        if not result.output.strip():
            return ToolResult("No matches found.", exit_code=result.exit_code)
        text, truncated = await self.limiter.apply(result.output.rstrip("\n"), sandbox)
        if result.output.count("\n") >= MAX_MATCHES:
            text += f"\n[showing the first {MAX_MATCHES} matches; narrow the pattern or path]"
        return ToolResult(text, exit_code=result.exit_code, truncated=truncated)
