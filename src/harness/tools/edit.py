"""view and str_replace."""

from __future__ import annotations

from typing import Any

from harness.sandbox.base import Sandbox
from harness.tools.base import OutputLimiter, Tool, ToolResult, object_schema

VIEW_WINDOW = 400  # lines shown when no range is given, and the most one call may show


def _numbered(lines: list[str], first: int) -> str:
    return "\n".join(f"{first + i:6d}\t{line}" for i, line in enumerate(lines))


class ViewTool(Tool):
    name = "view"
    description = (
        "Show a file with line numbers, or list a directory. Shows at most "
        f"{VIEW_WINDOW} lines per call; use start_line and end_line (1-based, inclusive) to "
        "read other parts of a long file."
    )
    parameters = object_schema(
        {
            "path": {"type": "string", "description": "File or directory path."},
            "start_line": {
                "type": "integer",
                "description": "First line to show (default 1).",
            },
            "end_line": {"type": "integer", "description": "Last line to show."},
        },
        ["path"],
    )

    def __init__(self, limiter: OutputLimiter) -> None:
        self.limiter = limiter

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        path = args["path"]
        try:
            content = await sandbox.read_file(path)
        except FileNotFoundError:
            return ToolResult(f"Error: no such file or directory: {path}")
        except IsADirectoryError:
            listing = await sandbox.exec(f"ls -la -- '{path}'")
            text, truncated = await self.limiter.apply(listing.output, sandbox)
            return ToolResult(text, truncated=truncated)
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # a trailing newline does not start another line
        total = len(lines)
        start = args.get("start_line", 1)
        end = args.get("end_line", min(total, start + VIEW_WINDOW - 1))
        if start < 1 or end < start:
            return ToolResult(
                f"Error: invalid range {start}-{end} (file has {total} lines)"
            )
        end = min(end, total, start + VIEW_WINDOW - 1)
        if start > total:
            return ToolResult(
                f"Error: start_line {start} is past the end of the file ({total} lines)"
            )
        text = _numbered(lines[start - 1 : end], start)
        footer = (
            f"\n[showing lines {start}-{end} of {total}]"
            if (start > 1 or end < total)
            else ""
        )
        text, truncated = await self.limiter.apply(text, sandbox)
        return ToolResult(text + footer, truncated=truncated)


class StrReplaceTool(Tool):
    name = "str_replace"
    description = (
        "Replace one exact piece of text in a file. old_str must appear in the file EXACTLY "
        "ONCE (including whitespace and indentation); if it appears zero times or more than "
        "once the call fails and nothing is changed. Include enough surrounding lines to make "
        "old_str unique."
    )
    parameters = object_schema(
        {
            "path": {"type": "string", "description": "File to edit."},
            "old_str": {
                "type": "string",
                "description": "Exact text to replace (must be unique).",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text (may be empty).",
            },
        },
        ["path", "old_str", "new_str"],
    )

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        path, old, new = args["path"], args["old_str"], args["new_str"]
        if not old:
            return ToolResult("Error: old_str must not be empty.")
        if old == new:
            return ToolResult(
                "Error: old_str and new_str are identical; nothing to change."
            )
        try:
            content = await sandbox.read_file(path)
        except FileNotFoundError:
            return ToolResult(f"Error: no such file: {path}")
        except IsADirectoryError:
            return ToolResult(f"Error: {path} is a directory, not a file.")
        count = content.count(old)
        if count == 0:
            return ToolResult(self._no_match(path, content, old))
        if count > 1:
            return ToolResult(self._many_matches(path, content, old, count))
        updated = content.replace(old, new, 1)
        await sandbox.write_file(path, updated)
        first = content[: content.index(old)].count("\n") + 1
        last = first + new.count("\n")
        new_lines = updated.split("\n")
        lo, hi = max(first - 3, 1), min(last + 3, len(new_lines))
        return ToolResult(
            f"Edited {path}: replaced 1 occurrence (now lines {first}-{last}).\n"
            + _numbered(new_lines[lo - 1 : hi], lo)
        )

    @staticmethod
    def _context(lines: list[str], index: int, radius: int) -> str:
        lo, hi = max(index - radius, 0), min(index + radius + 1, len(lines))
        return _numbered(lines[lo:hi], lo + 1)

    def _no_match(self, path: str, content: str, old: str) -> str:
        first_line = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
        lines = content.split("\n")
        hits = [i for i, ln in enumerate(lines) if first_line and first_line in ln][:3]
        msg = f"Error: old_str was not found in {path}; nothing was changed."
        if hits:
            msg += (
                f"\nThe first line of old_str ({first_line!r}) does appear near these places, so "
                "the difference is probably whitespace, indentation or a later line:"
            )
            for i in hits:
                msg += f"\n--- around line {i + 1} ---\n{self._context(lines, i, 3)}"
        else:
            msg += (
                f"\nNo line of the file contains the first line of old_str ({first_line!r}). "
                "Check the file with the view tool."
            )
        return msg

    def _many_matches(self, path: str, content: str, old: str, count: int) -> str:
        lines = content.split("\n")
        starts, pos = [], content.find(old)
        while pos != -1 and len(starts) < 5:
            starts.append(content[:pos].count("\n"))
            pos = content.find(old, pos + 1)
        msg = (
            f"Error: old_str appears {count} times in {path}; it must appear exactly once. "
            "Nothing was changed. Add surrounding lines to make it unique. Occurrences:"
        )
        for i in starts:
            msg += f"\n--- line {i + 1} ---\n{self._context(lines, i, 2)}"
        if count > len(starts):
            msg += f"\n... and {count - len(starts)} more"
        return msg
