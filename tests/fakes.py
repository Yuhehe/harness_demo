"""Test doubles shared by the unit tests: a fake DeepSeek HTTP server and a scripted LLM."""

from __future__ import annotations

import copy
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

from harness.llm.types import CallStats, LLMResponse


@dataclass
class Step:
    """One scripted HTTP reply."""

    status: int = 200
    body: dict[str, Any] | bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    reset: bool = False  # drop the connection without answering


def ok_body(
    content: str = "pong",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    reasoning: str | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cached: int = 64,
    reasoning_tokens: int = 5,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "resp-1",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": prompt_tokens - cached,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def error_body(message: str, type_: str = "server_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": None, "code": None}}


def tool_call(id_: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": id_,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeLLMServer:
    """Plays a script of replies, one per request, and records what it received."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                outer.requests.append(
                    {
                        "path": self.path,
                        "body": json.loads(self.rfile.read(length) or b"{}"),
                    }
                )
                step = (
                    outer.steps.pop(0)
                    if outer.steps
                    else Step(500, error_body("script exhausted"))
                )
                if step.delay:
                    time.sleep(step.delay)
                if step.reset:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = step.body if step.body is not None else ok_body()
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(step.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                for k, v in step.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up (a timeout test)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class ScriptedLLM:
    """An LLMClient that plays scripted LLMResponses (or raises scripted exceptions)."""

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.stats = CallStats()

    async def complete(self, messages, tools=None, *, deadline=None):
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": tools, "deadline": deadline}
        )
        if not self.script:
            raise AssertionError("ScriptedLLM ran out of responses")
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        self.stats.calls += 1
        self.stats.attempts += 1
        return item


class FakeSandbox:
    """In-memory sandbox. `exec` answers from `handler(cmd)` (default: exit 0, no output).

    The harness's own `git ls-files -z` (the guard's list of tracked files) is answered from
    `tracked` and logged in `harness_calls`, not in `exec_calls`; without `tracked` the fake
    is not a git repository, so the guard keeps its policy v1 behaviour."""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        handler=None,
        diff: str = "",
        tracked: list[str] | None = None,
    ) -> None:
        self.workdir = "/testbed"
        self.files = dict(files or {})
        self.handler = handler
        self.diff = diff
        self.tracked = tracked
        self.exec_calls: list[tuple[str, int]] = []
        self.harness_calls: list[str] = []
        self.dirs: set[str] = set()

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.workdir}/{path}"

    async def exec(self, cmd: str, timeout: int = 120):
        from harness.sandbox.base import ExecResult

        if cmd == "git ls-files -z":
            self.harness_calls.append(cmd)
            if self.tracked is None:
                return ExecResult(128, "fatal: not a git repository")
            return ExecResult(0, "".join(f"{p}\0" for p in self.tracked))
        self.exec_calls.append((cmd, timeout))
        if self.handler:
            out = self.handler(cmd)
            if out is not None:
                return out if isinstance(out, ExecResult) else ExecResult(0, out)
        return ExecResult(0, "")

    async def read_file(self, path: str) -> str:
        path = self._abs(path)
        if path in self.dirs:
            raise IsADirectoryError(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[self._abs(path)] = content

    async def git_diff(self) -> str:
        return self.diff
