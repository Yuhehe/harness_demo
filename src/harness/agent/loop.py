"""The agent loop (CLAUDE.md 5.1).

Invariants worth keeping in mind while reading:
* The history only grows. Each tool call gets exactly one tool message (the API rejects
  anything else), so every call is answered even when it is not executed.
* A response cut off by max_tokens is NOT stored: its tool calls are unusable, and a huge
  half-written argument would fill the context. A user message tells the model instead.
* Failed LLM attempts are the client's business; the loop only sees a response or an LLMError.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from harness.agent import limits, prompts
from harness.agent.state import AgentState, FailReason, Task
from harness.config import AgentConfig, summary_llm_config
from harness.context.manager import NEEDS_SUMMARIZER, ContextManager, ContextOverflow
from harness.llm import cost
from harness.llm.client import LLMClient
from harness.llm.types import (
    LLMError,
    LLMResponse,
    Message,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)
from harness.sandbox.base import Sandbox
from harness.security.guard import Approver, Guard, tracked_files
from harness.tools.base import OutputLimiter, ToolResult
from harness.tools.mcp import (
    MCPStartError,
    MCPTool,
    allowed_names,
    start_servers,
    tools_hash,
)
from harness.tools.registry import Registry
from harness.tracing.tracer import Tracer

MAX_CONSECUTIVE = 3  # format errors, truncated responses, or text-only replies in a row


@dataclass
class RunResult:
    patch: str
    fail_reason: FailReason | None  # None: the model submitted
    fail_detail: str | None
    submitted: bool
    metrics: dict[str, Any]
    messages: list[Message]  # the final history, for tests and transcripts


def _llm_error_reason(e: LLMError) -> FailReason:
    if e.kind == "context_overflow":
        return FailReason.context_overflow
    if e.kind == "time_budget":
        return FailReason.time_limit
    return FailReason.env_error


log = logging.getLogger(__name__)


async def run_task(
    task: Task,
    cfg: AgentConfig,
    sandbox: Sandbox,
    llm: LLMClient,
    tracer: Tracer,
    *,
    context_mgr: ContextManager | None = None,
    summarizer: LLMClient | None = None,
    guard: Guard | None = None,
    approve: Approver | None = None,
    clock: Callable[[], float] = time.monotonic,
    meta: dict[str, Any] | None = None,
) -> RunResult:
    # MCP sessions (tools/mcp.py) are opened and closed inside this one task: anyio's cancel
    # scopes must be left in the task that entered them
    mcp_stack = AsyncExitStack()
    try:
        result = await _run_task(
            task,
            cfg,
            sandbox,
            llm,
            tracer,
            context_mgr=context_mgr,
            summarizer=summarizer,
            guard=guard,
            approve=approve,
            clock=clock,
            meta=meta,
            mcp_stack=mcp_stack,
        )
    finally:
        try:
            await mcp_stack.aclose()
        except Exception as e:  # noqa: BLE001 - the run is over: never lose its result to this
            log.warning("closing the MCP servers failed: %s: %s", type(e).__name__, e)
    return result


async def _run_task(
    task: Task,
    cfg: AgentConfig,
    sandbox: Sandbox,
    llm: LLMClient,
    tracer: Tracer,
    *,
    context_mgr: ContextManager | None,
    summarizer: LLMClient | None,
    guard: Guard | None,
    approve: Approver | None,
    clock: Callable[[], float],
    meta: dict[str, Any] | None,
    mcp_stack: AsyncExitStack,
) -> RunResult:
    model = cfg.llm.resolved_model()
    state = AgentState(task=task, cfg=cfg, model=model, started_at=clock())
    # Every message is traced as it is appended: a crash still leaves the whole conversation.
    # `step`: the number of LLM responses so far (0 for the system prompt and the task)
    state.on_append = lambda m: tracer.emit("message", step=state.steps, **m)
    limiter = OutputLimiter(cfg.tool_output.max_chars)
    registry = Registry(cfg, limiter, state.todo, state.submit_flag)
    mcp_tools: list[MCPTool] = []
    mcp_error: str | None = None
    if cfg.mcp.servers:
        try:
            mcp_tools = await start_servers(cfg.mcp, sandbox, mcp_stack, limiter)
            registry.add(mcp_tools)
        except (
            MCPStartError
        ) as e:  # the task ends with env_error, after a full run_start
            mcp_error = str(e)[:300]
    tools = registry.schemas()
    if context_mgr is None:
        context_mgr = ContextManager(
            cfg.context,
            model=model,
            tools=tools,
            sandbox=sandbox,
            tracer=tracer,
            summarizer=summarizer,
            summary_model=summary_llm_config(cfg).resolved_model()
            if cfg.context.strategy in NEEDS_SUMMARIZER
            else None,
        )
    guard = guard or Guard.from_config(cfg.guard, sandbox.workdir)
    guard.mcp_allowed = allowed_names(cfg.mcp, mcp_tools)
    if guard.needs_tracked and guard.tracked is None:
        # the repository's own files, before the agent touches anything (security/guard.py)
        guard.tracked = await tracked_files(sandbox)
    deadline = state.started_at + cfg.limits.max_wall_seconds

    tracer.emit(
        "run_start",
        config=cfg.model_dump(mode="json"),
        config_hash=cfg.config_hash(),
        model=model,
        thinking=cfg.llm.thinking,
        reasoning_effort=cfg.llm.reasoning_effort,
        max_tokens=cfg.llm.max_tokens,
        timeout_s=cfg.llm.timeout_s,
        seed=cfg.seed,
        tools=registry.names,
        prices_hash=cost.prices_hash(),
        guard_enabled=cfg.guard.enabled,
        summary_model=context_mgr.summary_model,
        policy_hash=getattr(guard, "policy_hash", None),
        guard_tracked_files=(
            len(guard.tracked) if getattr(guard, "tracked", None) is not None else None
        ),
        task=task.instance_id,
        **(
            {"mcp_tools_hash": tools_hash(mcp_tools), "mcp_error": mcp_error}
            if cfg.mcp.servers
            else {}
        ),
        **(meta or {}),
    )

    state.append(system_message(prompts.system_prompt(cfg, sandbox.workdir)))
    state.append(user_message(prompts.task_message(task.problem_statement, cfg)))

    def note() -> str:
        """The step and budget note, with its leading newline; empty when switched off."""
        if not cfg.tool_output.step_note:
            return ""
        lim = cfg.limits
        return "\n" + prompts.step_note(
            state.steps, lim.max_steps, state.cost_usd, lim.max_cost_per_task_usd
        )

    try:
        if mcp_error:
            state.finish(FailReason.env_error, mcp_error)
        while not state.done:
            wasted_cost = cost.prompt_cost_usd(
                model, llm.stats.wasted_prompt_tokens_est
            ) + cost.prompt_cost_usd(
                context_mgr.summary_model or model,
                context_mgr.wasted_prompt_tokens_est,
            )
            stop = limits.check(
                state, cfg.limits, wasted_cost_usd=wasted_cost, now=clock()
            )
            if stop:
                state.finish(*stop)
                break
            try:
                messages = await context_mgr.build(state, deadline=deadline)
            except ContextOverflow as e:
                state.finish(FailReason.context_overflow, str(e))
                break
            try:
                resp = await llm.complete(messages, tools, deadline=deadline)
            except LLMError as e:
                state.finish(_llm_error_reason(e), f"{e.kind}: {e}"[:300])
                break
            state.record_response(resp)
            await _handle_response(
                resp, state, registry, guard, sandbox, tracer, note, approve
            )
    finally:
        patch, patch_error = await _final_patch(sandbox)
        if patch_error:
            state.finish(FailReason.env_error, patch_error)
        crashed = sys.exc_info()[1]
        if (
            crashed is not None
        ):  # a bug in our code: still leave a complete run_end span
            state.finish(
                FailReason.env_error,
                f"unexpected {type(crashed).__name__}: {crashed}"[:300],
            )
        if not state.done:  # defensive: the loop above always finishes the state
            state.finish(FailReason.no_submit)
        metrics = {
            **state.metrics(),
            "failed_attempts": llm.stats.failed_attempts,
            "retry_rate": llm.stats.retry_rate,
            "wasted_prompt_tokens_est": llm.stats.wasted_prompt_tokens_est,
            "retry_reasons": dict(llm.stats.retry_reasons),
            "wall_seconds": round(clock() - state.started_at, 3),
        }
        tracer.emit(
            "run_end",
            fail_reason=state.fail_reason.value if state.fail_reason else None,
            fail_detail=state.fail_detail,
            submitted=state.submitted,
            patch_chars=len(patch),
            todo=state.todo.items,
            **metrics,
        )
    return RunResult(
        patch=patch,
        fail_reason=state.fail_reason,
        fail_detail=state.fail_detail,
        submitted=state.submitted,
        metrics=metrics,
        messages=state.messages,
    )


async def _final_patch(sandbox: Sandbox) -> tuple[str, str | None]:
    try:
        return await sandbox.git_diff(), None
    except Exception as e:  # noqa: BLE001 - a broken sandbox must not hide how the run ended
        return (
            "",
            f"could not read the patch from the sandbox: {type(e).__name__}: {e}"[:300],
        )


async def _handle_response(
    resp: LLMResponse,
    state: AgentState,
    registry: Registry,
    guard: Guard,
    sandbox: Sandbox,
    tracer: Tracer,
    note: Callable[[], str],
    approve: Approver | None = None,
) -> None:
    cfg = state.cfg

    if resp.finish_reason == "length":
        state.truncated_outputs += 1
        state.truncated_consecutive += 1
        tracer.emit(
            "truncation",
            step=state.steps,
            content_chars=len(resp.content),
            tool_calls=[tc.name for tc in resp.tool_calls],
            consecutive=state.truncated_consecutive,
        )
        if state.truncated_consecutive >= MAX_CONSECUTIVE:
            state.finish(FailReason.format_error, "output_truncated")
            return
        state.append(
            user_message(prompts.truncation_feedback(cfg.llm.max_tokens or 0) + note())
        )
        return
    state.truncated_consecutive = 0

    if resp.finish_reason == "content_filter":
        state.finish(
            FailReason.env_error, "the provider filtered the response (content_filter)"
        )
        return

    state.append(assistant_message(resp))

    if not resp.tool_calls:
        # Not a malformed call: the model stopped acting without calling submit. It is counted
        # apart from format errors so the two failure modes stay distinguishable.
        state.no_tool_consecutive += 1
        _count_format_errors(state, 0)  # a text-only turn breaks a run of format errors
        if state.no_tool_consecutive >= MAX_CONSECUTIVE:
            state.finish(FailReason.no_submit, "no_tool_call")
            return
        state.append(user_message(prompts.NO_TOOL_CALL_NUDGE + note()))
        return
    state.no_tool_consecutive = 0

    bad_calls = 0
    results: list[tuple[str, ToolResult]] = []
    for call in resp.tool_calls:
        started = time.monotonic()
        if state.submitted:
            result = ToolResult("Not executed: the task was already submitted.")
        else:
            decision = guard.check(call, state)
            approved: bool | None = None
            if decision.action == "ask" and approve is not None:
                try:
                    approved = bool(await approve(call, decision))
                except Exception:  # noqa: BLE001 - no answer is a no
                    approved = False
            tracer.emit(
                "guard_decision",
                tool=call.name,
                action=decision.action,
                rule_id=decision.rule_id,
                level=decision.level,
                **({} if approved is None else {"approved": approved}),
            )
            if decision.allowed or approved:
                result = await registry.run(call.name, call.arguments, sandbox)
            else:
                result = decision.as_tool_result(declined=approved is False)
        bad_calls += result.format_error
        tracer.emit(
            "tool_call",
            step=state.steps,
            tool=call.name,
            args_hash=hashlib.sha256(call.arguments.encode()).hexdigest()[:16],
            exit_code=result.exit_code,
            truncated=result.truncated,
            format_error=result.format_error,
            duration_s=round(time.monotonic() - started, 3),
        )
        results.append((call.id, result))

    for i, (call_id, result) in enumerate(results):
        text = result.output
        if i == len(results) - 1:
            text += note()  # dynamic info goes in the newest message, once, permanently
        state.append(tool_message(call_id, text))
    _count_format_errors(state, bad_calls)
    if state.submitted:
        state.finish(None)


def _count_format_errors(state: AgentState, bad: int) -> None:
    if bad:
        state.format_errors += bad
        state.format_errors_consecutive += 1
        if state.format_errors_consecutive >= MAX_CONSECUTIVE:
            state.finish(
                FailReason.format_error, f"{MAX_CONSECUTIVE} format errors in a row"
            )
    else:
        state.format_errors_consecutive = 0
