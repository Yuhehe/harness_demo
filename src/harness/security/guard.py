"""Permission layer. Day 1: the call site and the span exist, every call is allowed.
The real policy (configs/policy.yaml) is Day 2/3."""

from __future__ import annotations

from dataclasses import dataclass

from harness.llm.types import ToolCall
from harness.tools.base import ToolResult


@dataclass(frozen=True)
class Decision:
    action: str = "allow"  # allow | deny | ask
    rule_id: str | None = None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    def as_tool_result(self) -> ToolResult:
        return ToolResult(
            f"Blocked by the permission layer ({self.rule_id}): {self.reason}"
        )


class Guard:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def check(self, call: ToolCall, state: object) -> Decision:
        return Decision()
