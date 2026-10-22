"""The sandbox interface the loop and tools depend on (a real Docker one and a fake one)."""

from __future__ import annotations

from typing import NamedTuple, Protocol


class ExecResult(NamedTuple):
    exit_code: int
    output: str  # stdout and stderr merged


class ProcessPipe(Protocol):
    """A long-running process in the sandbox with its stdin and stdout attached (an MCP
    server). stderr is kept apart, so its logs never mix into the stdout stream."""

    async def send(self, data: bytes) -> None: ...

    async def read(self) -> bytes:
        """The next stdout bytes; b"" once the process has closed its stdout."""
        ...

    def stderr_tail(self) -> str:
        """The last few thousand characters the process wrote to stderr."""
        ...

    async def close(self) -> None: ...


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

    async def open_process(self, argv: list[str]) -> ProcessPipe:
        """Start `argv` in the sandbox (not through a shell) with stdin and stdout attached.
        Used for MCP servers configured by the user, never with model text."""
        ...
