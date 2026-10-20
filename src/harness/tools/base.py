"""Tool interface, argument validation and recoverable output truncation."""

from __future__ import annotations

import re
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


# The note the limiter writes into a cut output, and the parser for it. Two modules depend on
# this format: the limiter writes it, and the `truncate` context strategy reads the path from it
# to see that an output is already saved. They share these definitions so that changing the
# wording cannot silently break the second one (tests/unit/test_tools.py checks the round trip).
OUT_DIR = "/tmp/harness"
OUT_PATH = OUT_DIR + "/out_{n}.txt"
OMITTED_NOTE = (
    "[... {omitted} chars omitted; full output of {total} chars saved at {path} ...]"
)
_NOTE_RE = re.compile(
    re.escape(OMITTED_NOTE)
    .replace(re.escape("{omitted}"), r"\d+")
    .replace(re.escape("{total}"), r"(?P<total>\d+)")
    .replace(re.escape("{path}"), r"(?P<path>" + re.escape(OUT_DIR) + r"/out_\d+\.txt)")
)


def saved_output(text: str) -> tuple[str, int] | None:
    """(sandbox path, length in chars) of the full output, if `text` contains a limiter note;
    else None. Only the complete note counts, so text that merely mentions a path does not
    match."""
    match = _NOTE_RE.search(text)
    return (match["path"], int(match["total"])) if match else None


def saved_path(text: str) -> str | None:
    found = saved_output(text)
    return found[0] if found else None


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
        path = OUT_PATH.format(n=self._counter)
        await sandbox.write_file(path, text)
        half = self.max_chars // 2
        note = OMITTED_NOTE.format(
            omitted=len(text) - 2 * half, total=len(text), path=path
        )
        return text[:half] + f"\n{note}\n" + text[-half:], True


# mini-swe-agent's observation for one command (2.4.6, config/benchmarks/swebench.yaml,
# `observation_template`), reproduced in Python so the harness does not import it; the tests
# render the real template and compare byte for byte (tests/unit/test_mini_mechanics.py).
MINI_WARNING = (
    "<warning>\n"
    "The output of your last command was too long.\n"
    "Please try a different command that produces less output.\n"
    "If you're looking at a file you can try use head, tail or sed to view a smaller number "
    "of lines selectively.\n"
    "If you're using grep or find and it produced too much output, you can use a more "
    "selective search pattern.\n"
    "If you really need to see something from the full command's output, you can redirect "
    "output to a file and then search in that file.\n"
    "</warning>"
)


def mini_observation(
    output: str, returncode: int, limit: int = 10000
) -> tuple[str, bool]:
    """(text, truncated) the way mini-swe-agent shows a command's result. Its template has the
    limit fixed at 10 000 characters with 5000 kept at each end; here the limit is
    `tool_output.max_chars`."""
    head = f"<returncode>{returncode}</returncode>\n"
    if len(output) < limit:
        return head + f"<output>\n{output}</output>", False
    half = limit // 2
    return (
        head
        + MINI_WARNING
        + f"<output_head>\n{output[:half]}\n</output_head>\n"
        + f"<elided_chars>\n{len(output) - limit} characters elided\n</elided_chars>\n"
        + f"<output_tail>\n{output[-half:]}\n</output_tail>"
    ), True


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
