"""Tool interface, argument validation and recoverable output truncation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from harness.sandbox.base import Sandbox

_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass
class ToolResult:
    output: str
    exit_code: int | None = None
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    # True when the CALL itself was malformed (bad JSON, unknown tool, missing or mistyped
    # parameter). Those count toward format_errors; a tool that ran and failed does not.
    format_error: bool = False


class OutputLimiter:
    """Keeps only the head and tail of a long output and saves the whole thing in the sandbox,
    so the model can read the rest when it needs it (CLAUDE.md 5.3)."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self._counter = 0

    async def apply(self, text: str, sandbox: Sandbox) -> tuple[str, bool]:
        if len(text) <= self.max_chars:
            return text, False
        self._counter += 1
        path = f"/tmp/harness/out_{self._counter}.txt"
        await sandbox.write_file(path, text)
        half = self.max_chars // 2
        omitted = len(text) - 2 * half
        note = f"\n[... {omitted} chars omitted; full output of {len(text)} chars saved at {path} ...]\n"
        return text[:half] + note + text[-half:], True


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (an object)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult: ...


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_args(schema: dict[str, Any], args: Any) -> str | None:
    """A minimal JSON-Schema check (object, required, additionalProperties, basic types,
    enum). Returns an error message for the model, or None."""
    if not isinstance(args, dict):
        return f"arguments must be a JSON object, got {type(args).__name__}"
    props = schema.get("properties", {})
    unknown = sorted(set(args) - set(props))
    if unknown:
        return f"unknown parameter(s) {unknown}; allowed: {sorted(props)}"
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return f"missing required parameter(s) {missing}"
    for key, value in args.items():
        spec = props[key]
        expected = _TYPES.get(spec.get("type", ""))
        if expected is None:
            continue
        bad_bool = spec.get("type") == "integer" and isinstance(value, bool)
        if not isinstance(value, expected) or bad_bool:
            return f"parameter '{key}' must be of type {spec['type']}, got {type(value).__name__}"
        if "enum" in spec and value not in spec["enum"]:
            return f"parameter '{key}' must be one of {spec['enum']}"
    return None
