from __future__ import annotations

from typing import Any

from harness.sandbox.base import Sandbox
from harness.tools.base import Tool, ToolResult, object_schema


class SubmitFlag:
    def __init__(self) -> None:
        self.submitted = False


class SubmitTool(Tool):
    name = "submit"
    description = (
        "Call this when the task is finished. The patch is taken from the current state of the "
        "repository; nothing is passed with this call. You cannot continue afterwards."
    )
    parameters = object_schema({}, [])

    def __init__(self, flag: SubmitFlag) -> None:
        self.flag = flag

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        self.flag.submitted = True
        return ToolResult("Submitted. The changes in the repository are your answer.")
