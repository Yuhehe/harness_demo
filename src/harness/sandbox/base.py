"""The sandbox interface the loop and tools depend on (a real Docker one and a fake one)."""

from __future__ import annotations

from typing import NamedTuple, Protocol


class ExecResult(NamedTuple):
    exit_code: int
    output: str  # stdout and stderr merged


class Sandbox(Protocol):
    workdir: str

    async def exec(self, cmd: str, timeout: int = 120) -> ExecResult:
        """Run a shell command inside the sandbox. Exit code 124 means it timed out."""
        ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def git_diff(self) -> str:
        """Everything the agent changed relative to HEAD, including new files."""
        ...
