"""Our own LLM request/response types.

LiteLLM's response objects and exceptions stay inside llm/client.py. Everything else
(loop, tracer, replay, cost) only sees these types, so provider quirks cannot leak out.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

Message = dict[str, Any]  # OpenAI-style chat message


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text exactly as the model produced it (may be invalid)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0  # includes reasoning tokens
    cached_tokens: int = 0  # prompt tokens served from the provider's prefix cache
    reasoning_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(self.prompt_tokens - self.cached_tokens, 0)


@dataclass(frozen=True)
class LLMResponse:
    content: str
    reasoning_content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    usage: Usage
    model: str = ""
    response_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMResponse:
        return cls(
            content=d["content"],
            reasoning_content=d.get("reasoning_content"),
            tool_calls=tuple(ToolCall(**tc) for tc in d["tool_calls"]),
            finish_reason=d["finish_reason"],
            usage=Usage(**d["usage"]),
            model=d.get("model", ""),
            response_id=d.get("response_id"),
        )


# ---- message constructors (plain dicts, so serialization stays trivial) ----------------


def system_message(text: str) -> Message:
    return {"role": "system", "content": text}


def user_message(text: str) -> Message:
    return {"role": "user", "content": text}


def tool_message(tool_call_id: str, text: str) -> Message:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": text}


def assistant_message(resp: LLMResponse, *, keep_tool_calls: bool = True) -> Message:
    """The assistant turn as it is stored in the history (and sent back next time).

    reasoning_content is kept: it is free (not counted as prompt tokens) and avoids the
    failure mode LiteLLM documents for thinking mode.
    """
    msg: Message = {"role": "assistant", "content": resp.content}
    if resp.reasoning_content:
        msg["reasoning_content"] = resp.reasoning_content
    if keep_tool_calls and resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in resp.tool_calls
        ]
    return msg


# ---- errors ------------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    attempt: int
    outcome: str  # "ok" | "retry" | "fail"
    retry_reason: str | None = None
    status_code: int | None = None
    latency_s: float = 0.0


class LLMError(Exception):
    """An LLM call that did not produce a usable response.

    kind: rate_limit | server_error | timeout | bad_response | interrupted  (retryable
    classes, raised when attempts ran out) | context_overflow | client_error | time_budget.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status_code: int | None = None,
        attempts: list[AttemptRecord] | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status_code = status_code
        self.attempts = attempts or []


@dataclass
class CallStats:
    """Cumulative over a client's lifetime. Failed attempts never reach the main token and
    cost metrics; they are reported here instead (CLAUDE.md 5.1)."""

    calls: int = 0  # complete() calls that returned a response
    attempts: int = 0  # every request sent, including failed ones
    failed_attempts: int = 0
    wasted_prompt_tokens_est: int = (
        0  # our own token count of requests that got no usage
    )
    retry_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def retry_rate(self) -> float:
        return self.failed_attempts / self.attempts if self.attempts else 0.0
