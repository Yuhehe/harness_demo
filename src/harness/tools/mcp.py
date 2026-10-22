"""Tools from MCP servers (config `mcp.servers`), each server running inside the sandbox.

The official `mcp` SDK speaks the protocol (ClientSession: initialize, tools/list, tools/call);
this module only connects it to the harness (docs/notes.md section 32):

- Transport: the server is started in the sandbox with stdin and stdout attached
  (`sandbox.open_process`, the Docker SDK's exec socket), never on the host. Newline-delimited
  JSON-RPC both ways, as the SDK's own stdio transport; stderr stays apart.
- The tool list is taken once, when the task starts, and never changes during the task
  (`tools/list_changed` is ignored): it is part of the cached prompt prefix (CLAUDE.md 5.2).
  Names become `mcp__<server>__<tool>`, cut to what the model API accepts
  ([A-Za-z0-9_-], at most 128 characters: DeepSeek's limit); two tools that end up with the
  same name are an error. Descriptions come from the server, so they are untrusted text going
  into the prompt: they are cut to `mcp.max_description_chars` and labelled with the server.
  Input schemas are passed on with their keys sorted (deterministic serialization); a schema
  longer than `mcp.max_schema_chars` (serialized) is refused rather than cut, since a cut
  schema would no longer be valid.
- Arguments are not checked locally (`Tool.validate_locally`): an MCP schema can be anything
  JSON Schema allows, and the server checks its own arguments. A protocol error for an unknown
  tool or bad arguments (-32601, -32602) is a format error, like a malformed call to a built-in
  tool. A tool that ran and failed (`isError`) is exit code 1. Output goes through the same
  limiter as every other tool.
- Permissions: every MCP tool is an `ask` decision unless its server's `allow` lists it
  (security/guard.py). With `guard.enabled: false` they all run.
- A server that does not start within `start_timeout_s` (or dies) ends the task with
  `env_error`; the sessions are closed when the task ends, in the task that opened them
  (anyio cancel scopes).
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio

from harness.config import MCPConfig, MCPServerConfig
from harness.sandbox.base import ProcessPipe, Sandbox
from harness.tools.base import OutputLimiter, Tool, ToolResult

MAX_NAME = 128  # DeepSeek: [A-Za-z0-9_-]{1,128} (docs/notes.md section 32)
_FORMAT_ERRORS = {-32601, -32602}  # method / tool not found, invalid params


class MCPStartError(Exception):
    """A configured MCP server could not be started or listed."""


def tool_name(server: str, tool: str) -> str:
    """`mcp__<server>__<tool>`, with every character the model API refuses replaced and an
    over-long name cut, ending in a short hash of the original so cut names stay distinct."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    if len(name) > MAX_NAME:
        digest = hashlib.sha256(tool.encode()).hexdigest()[:8]
        name = f"{name[: MAX_NAME - 9]}_{digest}"
    return name


def _sorted(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True))


def _text(result: Any) -> str:
    parts = []
    for block in result.content or []:
        kind = getattr(block, "type", "?")
        if kind == "text":
            parts.append(block.text)
        elif kind == "resource" and hasattr(getattr(block, "resource", None), "text"):
            parts.append(block.resource.text)
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and getattr(result, "structured_content", None) is not None:
        parts.append(json.dumps(result.structured_content, sort_keys=True))
    return "\n".join(parts)


class MCPTool(Tool):
    validate_locally = False

    def __init__(
        self,
        server: MCPServerConfig,
        session: Any,
        spec: Any,
        limiter: OutputLimiter,
        max_description_chars: int,
    ) -> None:
        self.server, self.session, self.limiter = server, session, limiter
        self.remote_name = spec.name
        self.name = tool_name(server.name, spec.name)
        desc = (spec.description or "").strip()
        if len(desc) > max_description_chars:
            desc = desc[:max_description_chars] + " [description cut]"
        self.description = (
            f"(MCP server `{server.name}`, tool `{spec.name}`) {desc}".strip()
        )
        schema = dict(spec.input_schema or {})
        schema.setdefault("type", "object")
        self.parameters = _sorted(schema)

    async def run(self, args: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        from mcp.shared.exceptions import MCPError

        try:
            result = await self.session.call_tool(
                self.remote_name, args, read_timeout_seconds=self.server.call_timeout_s
            )
        except MCPError as e:
            return ToolResult(
                f"Error from MCP server {self.server.name}: {e.message} (code {e.code})",
                exit_code=1,
                format_error=e.code in _FORMAT_ERRORS,
            )
        except Exception as e:  # noqa: BLE001 - a broken server is a failed call, not a crash
            return ToolResult(
                f"MCP server {self.server.name} failed: {type(e).__name__}: {e}"[:500],
                exit_code=1,
            )
        if not hasattr(
            result, "content"
        ):  # e.g. an input-required result: not supported
            return ToolResult(
                f"MCP server {self.server.name} returned an unsupported result "
                f"({type(result).__name__})",
                exit_code=1,
            )
        text, truncated = await self.limiter.apply(_text(result), sandbox)
        return ToolResult(
            text or "(no output)",
            exit_code=1 if result.is_error else 0,
            truncated=truncated,
        )


@asynccontextmanager
async def pipe_streams(pipe: ProcessPipe) -> Any:
    """(read_stream, write_stream) for the SDK's ClientSession over a ProcessPipe: one
    JSON-RPC message per line, parsed and written exactly as the SDK's stdio transport does."""
    from mcp import types
    from mcp.shared.message import SessionMessage

    read_send, read_recv = anyio.create_memory_object_stream[Any](0)
    write_send, write_recv = anyio.create_memory_object_stream[Any](0)

    # A server that dies mid-task must look like a closed connection to the session (its
    # pending call then fails with "Connection closed"), never like an error in these tasks:
    # that would surface only when the task group closes, after the run (as the SDK's own
    # stdio transport does).
    async def reader() -> None:
        buffer = b""
        async with read_send:
            try:
                while chunk := await pipe.read():
                    lines = (buffer + chunk).split(b"\n")
                    buffer = lines.pop()
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            msg = types.jsonrpc_message_adapter.validate_json(
                                line, by_name=False
                            )
                            await read_send.send(SessionMessage(msg))
                        except ValueError as e:
                            await read_send.send(e)
            except (OSError, anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass

    async def writer() -> None:
        try:
            async with write_recv:
                async for message in write_recv:
                    data = message.message.model_dump_json(
                        by_alias=True, exclude_unset=True
                    )
                    await pipe.send(data.encode() + b"\n")
        except (OSError, anyio.ClosedResourceError, anyio.BrokenResourceError):
            await read_send.aclose()  # the session sees the connection end

    async with anyio.create_task_group() as tg:
        tg.start_soon(reader)
        tg.start_soon(writer)
        try:
            yield read_recv, write_send
        finally:
            await pipe.close()  # wakes the reader
            tg.cancel_scope.cancel()


async def _start_server(
    server: MCPServerConfig,
    sandbox: Sandbox,
    stack: AsyncExitStack,
    limiter: OutputLimiter,
    max_description_chars: int,
    max_schema_chars: int,
) -> list[MCPTool]:
    from mcp import ClientSession, types

    pipe: ProcessPipe | None = None
    try:
        # a command missing from the image can fail right here (a Docker API error) or by
        # writing an error and exiting: both are "did not start"
        pipe = await sandbox.open_process(server.command)
        # the transport and the session hold task groups that stay open with `stack`: enter
        # them outside the timeout's cancel scope, which must close before they do
        read, write = await stack.enter_async_context(pipe_streams(pipe))
        session = await stack.enter_async_context(
            ClientSession(
                read,
                write,
                client_info=types.Implementation(name="harness-lab", version="0.1.0"),
            )
        )
        with anyio.fail_after(server.start_timeout_s):
            await session.initialize()
            specs, cursor = [], None
            while True:
                page = await session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor)
                    if cursor
                    else None
                )
                specs += page.tools
                cursor = page.next_cursor
                if not cursor:
                    break
    except Exception as e:
        tail = pipe.stderr_tail().strip()[-500:] if pipe is not None else ""
        raise MCPStartError(
            f"MCP server {server.name} did not start: {type(e).__name__}: {e}"
            + (f" (stderr: {tail})" if tail else "")
        ) from e
    tools = [
        MCPTool(server, session, spec, limiter, max_description_chars)
        for spec in sorted(specs, key=lambda s: s.name)
    ]
    for (
        t
    ) in tools:  # schemas are server text in the cached prefix too: refuse, don't cut
        size = len(json.dumps(t.parameters, sort_keys=True))
        if size > max_schema_chars:
            raise MCPStartError(
                f"MCP server {server.name}: the input schema of {t.remote_name!r} is "
                f"{size} characters, over mcp.max_schema_chars ({max_schema_chars})"
            )
    return tools


async def start_servers(
    cfg: MCPConfig, sandbox: Sandbox, stack: AsyncExitStack, limiter: OutputLimiter
) -> list[MCPTool]:
    """Start every configured server in the sandbox and return its tools. Sessions close with
    `stack`, which the caller must close in the same task."""
    tools: list[MCPTool] = []
    for server in cfg.servers:
        tools += await _start_server(
            server,
            sandbox,
            stack,
            limiter,
            cfg.max_description_chars,
            cfg.max_schema_chars,
        )
    names = [t.name for t in tools]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise MCPStartError(f"MCP tools with the same name after sanitizing: {dupes}")
    return tools


def allowed_names(cfg: MCPConfig, tools: list[MCPTool]) -> frozenset[str]:
    """The harness names of the tools that run without asking."""
    allow = {(s.name, t) for s in cfg.servers for t in s.allow}
    return frozenset(t.name for t in tools if (t.server.name, t.remote_name) in allow)


def tools_hash(tools: list[MCPTool]) -> str:
    """A hash of what the model sees of the MCP tools (names, descriptions, schemas)."""
    payload = [t.schema() for t in sorted(tools, key=lambda t: t.name)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
