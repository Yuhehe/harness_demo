"""LLM client: LiteLLM transport plus our retry policy (CLAUDE.md 5.1, docs/notes.md section 5).

* Non-streaming only. thinking, reasoning_effort, max_tokens and the timeout come from the config
  (max_tokens and the timeout are paired).
* LiteLLM does no retrying of its own (num_retries=0); every retry decision is made here,
  from (exception type, status code, response shape), never from the status code alone:
  a wrong API key is a BadRequestError with status 401, a malformed 200 is an APIError
  with status 200, a missing `choices` is an APIError with status 500.
* finish_reason=length is NOT an error: the response is returned and the loop handles it.
* Failed attempts are traced and counted separately; they never enter the main metrics.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import litellm
import openai

from harness.config import LLMConfig
from harness.llm import cost
from harness.llm.types import (
    AttemptRecord,
    CallStats,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    Usage,
    strip_reasoning,
)
from harness.tracing.tracer import Tracer

# finish_reasons that mean "the server cut generation off": the content is partial, throw it
# away and ask again.
INTERRUPTED_FINISH_REASONS = frozenset({"insufficient_system_resource", "aborted"})


class LLMClient(Protocol):
    stats: CallStats

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        deadline: float | None = None,
    ) -> LLMResponse:
        """`deadline` is a time.monotonic() value; the call never outlives it."""
        ...


# LiteLLM's RateLimitError, InternalServerError, Timeout ... derive from *openai's* APIError,
# while its own APIError (malformed bodies) is a separate class: both families are ours to handle.
_TRANSPORT_ERRORS = (openai.APIError, litellm.exceptions.APIError)


class BadResponse(Exception):
    """A 200 whose body is unusable (raised by our own parsing)."""


@dataclass(frozen=True)
class Decision:
    kind: str  # rate_limit | server_error | timeout | bad_response | interrupted
    #            | context_overflow | client_error
    retry: bool
    status_code: int | None = None
    retry_after: float | None = None


def _retry_after(exc: Exception) -> float | None:
    for attr in ("litellm_response_headers", "headers"):
        headers = getattr(exc, attr, None)
        if headers:
            value = {k.lower(): v for k, v in dict(headers).items()}.get("retry-after")
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
    return None


def classify(exc: Exception) -> Decision | None:
    """Retry decision for an exception, or None if it is not an LLM/transport error at all
    (a bug in our code must propagate, not be retried)."""
    if isinstance(exc, BadResponse):
        return Decision("bad_response", True, 200)
    if not isinstance(exc, _TRANSPORT_ERRORS):
        return None
    status = getattr(exc, "status_code", None)
    if isinstance(exc, litellm.exceptions.ContextWindowExceededError):
        return Decision("context_overflow", False, status)
    if isinstance(exc, litellm.exceptions.Timeout):
        return Decision("timeout", True, status)
    if status == 429:
        return Decision("rate_limit", True, 429, _retry_after(exc))
    if status is not None and status >= 500:
        return Decision("server_error", True, status)
    if isinstance(exc, litellm.exceptions.APIConnectionError):
        return Decision(
            "timeout", True, status
        )  # no response at all: same family as a timeout
    if status in (None, 200):
        return Decision("bad_response", True, status)  # e.g. a body that is not JSON
    return Decision("client_error", False, status)  # 4xx: retrying cannot help


def _finish_reason(choice: Any) -> str:
    """LiteLLM rewrites finish reasons it does not know (DeepSeek's `aborted` and
    `insufficient_system_resource`) to `stop`, with only a log warning. The provider's own
    value survives in provider_specific_fields; without it a cut-off generation would look
    like a normal, complete answer."""
    native = (getattr(choice, "provider_specific_fields", None) or {}).get(
        "native_finish_reason"
    )
    return str(native or getattr(choice, "finish_reason", "") or "")


def parse_response(raw: Any, model: str) -> LLMResponse:
    """LiteLLM ModelResponse -> LLMResponse; raises BadResponse when the shape is wrong."""
    choices = getattr(raw, "choices", None)
    if not choices:
        raise BadResponse("response has no choices")
    choice = choices[0]
    msg = getattr(choice, "message", None)
    if msg is None:
        raise BadResponse("choice has no message")
    usage = getattr(raw, "usage", None)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None and getattr(details, "cached_tokens", None):
        cached = int(details.cached_tokens)
    elif getattr(usage, "prompt_cache_hit_tokens", None):
        cached = int(usage.prompt_cache_hit_tokens)
    cdetails = getattr(usage, "completion_tokens_details", None)
    reasoning = int(getattr(cdetails, "reasoning_tokens", 0) or 0)
    calls = tuple(
        ToolCall(
            id=str(tc.id),
            name=str(tc.function.name),
            arguments=tc.function.arguments or "",
        )
        for tc in (getattr(msg, "tool_calls", None) or [])
    )
    return LLMResponse(
        content=getattr(msg, "content", None) or "",
        reasoning_content=getattr(msg, "reasoning_content", None) or None,
        tool_calls=calls,
        finish_reason=_finish_reason(choice),
        usage=Usage(prompt, completion, cached, reasoning),
        model=str(getattr(raw, "model", "") or model),
        response_id=getattr(raw, "id", None),
    )


def backoff_delay(cfg: LLMConfig, attempt: int, rng: Callable[[], float]) -> float:
    """Delay before the retry that follows failed attempt number `attempt` (1-based):
    exponential, capped, with equal jitter (half fixed, half random)."""
    base = min(
        cfg.backoff_cap_s, cfg.backoff_base_s * cfg.backoff_factor ** (attempt - 1)
    )
    return base * (0.5 + 0.5 * rng())


class LiteLLMClient:
    def __init__(
        self,
        cfg: LLMConfig,
        tracer: Tracer,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        purpose: str = "agent",  # "agent" or "summary": tells the two apart in the trace
    ) -> None:
        self.cfg = cfg
        self.purpose = purpose
        self.model = cfg.resolved_model()
        cost.price_for(self.model)  # the budget guard is a hard rule: no price, no run
        self.tracer = tracer
        self.stats = CallStats()
        self._sleep, self._rng, self._clock = sleep, rng, clock

    # ---- request ---------------------------------------------------------------------
    def request_kwargs(
        self, messages: list[Message], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.cfg.max_tokens,
            "num_retries": 0,  # retries are ours
        }
        if tools:
            kwargs["tools"] = sorted(tools, key=lambda t: t["function"]["name"])
            kwargs["tool_choice"] = "auto"
        thinking = {"type": self.cfg.thinking}
        body: dict[str, Any] = {}
        if self.model.startswith("deepseek/"):
            kwargs["thinking"] = thinking  # LiteLLM maps it for its DeepSeek provider
        else:
            # The generic OpenAI route rejects `thinking=` (or silently drops it with
            # drop_params), so it has to travel in the request body.
            body["thinking"] = thinking
        if self.cfg.reasoning_effort:
            # Never as the `reasoning_effort=` keyword: LiteLLM's DeepSeek provider pops it and
            # keeps only on/off, so "high"/"max" would never reach the server (verified against
            # a local server, docs/notes.md section 11).
            body["reasoning_effort"] = self.cfg.reasoning_effort
        if body:
            kwargs["extra_body"] = body
        return kwargs

    async def _estimate_prompt_tokens(
        self, messages: list[Message], tools: list[dict[str, Any]] | None
    ) -> int:
        def count() -> int:
            try:
                return int(
                    litellm.token_counter(
                        model=cost.normalize_model(self.model),
                        messages=strip_reasoning(messages),
                        tools=tools,
                    )
                )
            except Exception:  # noqa: BLE001 - an estimate must never break a run
                return len(json.dumps(messages, default=str)) // 4

        return await asyncio.to_thread(count)

    # ---- the call --------------------------------------------------------------------
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        deadline: float | None = None,
    ) -> LLMResponse:
        cfg = self.cfg
        assert cfg.timeout_s is not None
        kwargs = self.request_kwargs(messages, tools)
        records: list[AttemptRecord] = []

        for attempt in range(1, cfg.max_attempts + 1):
            remaining = None if deadline is None else deadline - self._clock()
            if remaining is not None and remaining <= 0:
                raise LLMError(
                    "time_budget", "wall-clock limit reached", attempts=records
                )
            deadline_binds = remaining is not None and remaining < cfg.timeout_s
            timeout = (
                min(cfg.timeout_s, remaining)
                if remaining is not None
                else cfg.timeout_s
            )

            started = self._clock()
            self.stats.attempts += 1
            decision: Decision | None = None
            error: Exception | None = None
            try:
                raw = await litellm.acompletion(**kwargs, timeout=timeout)
                resp = parse_response(raw, self.model)
                if resp.finish_reason in INTERRUPTED_FINISH_REASONS:
                    decision = Decision("interrupted", True, 200)
                    error = BadResponse(f"finish_reason={resp.finish_reason}")
            except Exception as e:
                decision = classify(e)
                if decision is None:
                    raise
                error = e
            latency = self._clock() - started

            if error is None:
                self.stats.calls += 1
                records.append(AttemptRecord(attempt, "ok", None, 200, latency))
                self.tracer.emit(
                    "llm_call",
                    attempt=attempt,
                    outcome="ok",
                    purpose=self.purpose,
                    model=self.model,
                    finish_reason=resp.finish_reason,
                    latency_s=round(latency, 3),
                    prompt_tokens=resp.usage.prompt_tokens,
                    cached_tokens=resp.usage.cached_tokens,
                    completion_tokens=resp.usage.completion_tokens,
                    reasoning_tokens=resp.usage.reasoning_tokens,
                    cost_usd=cost.cost_usd(self.model, resp.usage),
                    response_id=resp.response_id,
                    retries=attempt - 1,
                )
                return resp

            assert decision is not None
            # a failed attempt: no usage, so the best we can do is count what we sent
            wasted = await self._estimate_prompt_tokens(messages, tools)
            self.stats.failed_attempts += 1
            self.stats.wasted_prompt_tokens_est += wasted
            self.stats.retry_reasons[decision.kind] = (
                self.stats.retry_reasons.get(decision.kind, 0) + 1
            )
            # The deadline cut this attempt short: that is a time limit, not a flaky provider.
            deadline_hit = decision.kind == "timeout" and deadline_binds
            will_retry = (
                decision.retry and attempt < cfg.max_attempts and not deadline_hit
            )
            records.append(
                AttemptRecord(
                    attempt, "retry" if will_retry else "fail", decision.kind,
                    decision.status_code, latency,
                )
            )  # fmt: skip
            self.tracer.emit(
                "llm_call",
                attempt=attempt,
                outcome="retry" if will_retry else "fail",
                retry_reason=decision.kind,
                status_code=decision.status_code,
                purpose=self.purpose,
                model=self.model,
                latency_s=round(latency, 3),
                usage=None,
                wasted_prompt_tokens_est=wasted,
                error=f"{type(error).__name__}: {str(error)[:300]}",
            )
            if deadline_hit:
                raise LLMError(
                    "time_budget",
                    "wall-clock limit reached during a call",
                    attempts=records,
                ) from error
            if not will_retry:
                raise LLMError(
                    decision.kind, str(error)[:300], status_code=decision.status_code,
                    attempts=records,
                ) from error  # fmt: skip

            delay = backoff_delay(cfg, attempt, self._rng)
            if decision.retry_after is not None:
                delay = min(
                    decision.retry_after, cfg.backoff_cap_s
                )  # honour Retry-After
            if deadline is not None and self._clock() + delay >= deadline:
                raise LLMError(
                    "time_budget",
                    "no time left to wait before the retry",
                    attempts=records,
                ) from error
            await self._sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover
