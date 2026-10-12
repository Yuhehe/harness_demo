from __future__ import annotations

from typing import Any

from harness.sandbox.base import Sandbox
from harness.tools.base import Tool, ToolResult, object_schema

STATUSES = ["pending", "in_progress", "done"]
_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


class TodoList:
    """The plan lives in agent memory, not in the sandbox."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def render(self) -> str:
        if not self.items:
            return "(the todo list is empty)"
        return "\n".join(
            f"{i + 1}. {_MARK[it['status']]} {it['text']}"
            for i, it in enumerate(self.items)
        )


class TodoTool(Tool):
    name = "todo"
    description = (
        "Keep a short plan. Each call REPLACES the whole list with `items`. Use it to write "
        "the plan before acting and to update statuses as you progress."
    )
    parameters = object_schema(
        {
            "items": {
                "type": "array",
                "description": "The complete list, one object per step.",
                # the schema of one element (JSON Schema's `items` keyword); without it the
                # parameter named `items` has no element type at all
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "What the step is."},
                        "status": {"type": "string", "enum": STATUSES},
                    },
                    "required": ["text", "status"],
                    "additionalProperties": False,
                },
            }
        },
        ["items"],
    )

    def __init__(self, todo: TodoList) -> None:
        self.todo = todo

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        items = args["items"]
        for i, it in enumerate(items):
            if not isinstance(it, dict) or set(it) != {"text", "status"}:
                return ToolResult(
                    f"Error: item {i + 1} must be an object with exactly 'text' and 'status'.",
                    format_error=True,
                )
            if not isinstance(it["text"], str) or it["status"] not in STATUSES:
                return ToolResult(
                    f"Error: item {i + 1}: 'text' must be a string and 'status' one of {STATUSES}.",
                    format_error=True,
                )
        self.todo.items = [{"text": it["text"], "status": it["status"]} for it in items]
        return ToolResult(self.todo.render())
