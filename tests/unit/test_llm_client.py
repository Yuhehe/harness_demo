"""The LLM client against a fake DeepSeek server (no network, no cost)."""

import time

import pytest
from fakes import FakeLLMServer, Step, error_body, ok_body, tool_call

from harness.config import LLMConfig
from harness.llm import cost
from harness.llm.client import BadResponse, LiteLLMClient, backoff_delay, classify
from harness.llm.types import LLMError
from harness.tracing.tracer import MemoryTracer

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "zeta",
            "description": "z",
            "parameters": {"type": "object"},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alpha",
            "description": "a",
            "parameters": {"type": "object"},
        },
    },
]
MSGS = [{"role": "user", "content": "ping"}]


@pytest.fixture
def make(monkeypatch):
    """make(steps, **cfg) -> (client, server, tracer, sleeps)."""
    servers = []

    def _make(steps, *, model="deepseek/deepseek-flash", rng=lambda: 1.0, **cfg):
        server = FakeLLMServer(steps).__enter__()
        servers.append(server)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        monkeypatch.setenv("DEEPSEEK_API_BASE", server.url)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_API_BASE", server.url)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        tracer = MemoryTracer()
        cfg.setdefault("timeout_s", 5)
        client = LiteLLMClient(
            LLMConfig(model=model, **cfg), tracer, sleep=fake_sleep, rng=rng
        )
        return client, server, tracer, sleeps

    yield _make
    for s in servers:
        s.__exit__()


# ---- the happy path --------------------------------------------------------------------
async def test_success_is_parsed_and_the_request_has_the_agreed_shape(make):
    body = ok_body(
        "done",
        reasoning="because",
        tool_calls=[tool_call("c1", "alpha", '{"x": 1}')],
        finish_reason="tool_calls",
    )
    client, server, tracer, _ = make([Step(body=body)], thinking="disabled")
    r = await client.complete(MSGS, TOOLS)
    assert (r.content, r.reasoning_content, r.finish_reason) == (
        "done",
        "because",
        "tool_calls",
    )
    assert r.tool_calls[0].name == "alpha" and r.tool_calls[0].arguments == '{"x": 1}'
    assert (r.usage.prompt_tokens, r.usage.cached_tokens, r.usage.reasoning_tokens) == (
        100,
        64,
        5,
    )

    sent = server.requests[0]["body"]
    assert sent["model"] == "deepseek-flash"  # provider prefix stripped on the wire
    assert sent["thinking"] == {"type": "disabled"} and sent["max_tokens"] == 16384
    assert [t["function"]["name"] for t in sent["tools"]] == ["alpha", "zeta"]  # sorted
    assert sent["tool_choice"] == "auto"

    (span,) = tracer.of("llm_call")
    assert span["outcome"] == "ok" and span["cached_tokens"] == 64
    assert span["cost_usd"] == pytest.approx(
        cost.cost_usd("deepseek/deepseek-flash", r.usage)
    )
    assert client.stats.calls == 1 and client.stats.failed_attempts == 0


async def test_generic_openai_route_sends_thinking_in_the_body(make):
    client, server, _, _ = make(
        [Step()], model="openai/deepseek-flash", thinking="enabled"
    )
    await client.complete(MSGS)
    sent = server.requests[0]["body"]
    assert sent["thinking"] == {"type": "enabled"} and sent["max_tokens"] == 65536
    assert "tools" not in sent


@pytest.mark.parametrize("model", ["deepseek/deepseek-flash", "openai/deepseek-flash"])
@pytest.mark.parametrize("effort", ["high", "max"])
async def test_reasoning_effort_reaches_the_server(make, model, effort):
    # LiteLLM's DeepSeek provider pops the `reasoning_effort=` keyword and keeps only on/off,
    # so the value has to travel in extra_body (docs/notes.md section 11).
    client, server, _, _ = make(
        [Step()], model=model, thinking="enabled", reasoning_effort=effort
    )
    await client.complete(MSGS)
    sent = server.requests[0]["body"]
    assert sent["reasoning_effort"] == effort
    assert sent["thinking"] == {"type": "enabled"}


async def test_default_effort_is_sent_explicitly_and_thinking_off_sends_none(make):
    client, server, _, _ = make([Step()], thinking="enabled")
    await client.complete(MSGS)
    assert server.requests[0]["body"]["reasoning_effort"] == "high"
    client, server, _, _ = make([Step()], thinking="disabled")
    await client.complete(MSGS)
    assert "reasoning_effort" not in server.requests[0]["body"]


# ---- retries ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (429, "rate_limit"),
        (500, "server_error"),
        (502, "server_error"),
        (503, "server_error"),
    ],
)
async def test_retryable_statuses_are_retried(make, status, reason):
    client, server, tracer, sleeps = make([Step(status, error_body("x")), Step()])
    r = await client.complete(MSGS)
    assert r.content == "pong" and len(server.requests) == 2 and len(sleeps) == 1
    first, second = tracer.of("llm_call")
    assert (first["outcome"], first["retry_reason"], first["usage"]) == (
        "retry",
        reason,
        None,
    )
    assert (
        first["wasted_prompt_tokens_est"] > 0
        and second["outcome"] == "ok"
        and second["retries"] == 1
    )
    assert client.stats.failed_attempts == 1 and client.stats.retry_reasons == {
        reason: 1
    }
    assert client.stats.wasted_prompt_tokens_est == first["wasted_prompt_tokens_est"]


async def test_retry_after_is_honoured_and_capped(make):
    client, _, _, sleeps = make(
        [
            Step(429, error_body("slow", "rate_limit_error"), {"Retry-After": "7"}),
            Step(),
        ]
    )
    await client.complete(MSGS)
    assert sleeps == [7.0]
    client, _, _, sleeps = make(
        [Step(429, error_body("slow"), {"Retry-After": "3600"}), Step()]
    )
    await client.complete(MSGS)
    assert sleeps == [60.0]  # backoff_cap_s


async def test_backoff_is_exponential_with_jitter_and_exhaustion_raises(make):
    steps = [Step(500, error_body("boom"))] * 3
    client, server, tracer, sleeps = make(steps, rng=lambda: 1.0)
    with pytest.raises(LLMError) as ei:
        await client.complete(MSGS)
    assert ei.value.kind == "server_error" and ei.value.status_code == 500
    assert len(server.requests) == 3 and sleeps == [4.0, 12.0]  # base 4, factor 3
    assert [a.outcome for a in ei.value.attempts] == ["retry", "retry", "fail"]
    assert [s["outcome"] for s in tracer.of("llm_call")] == ["retry", "retry", "fail"]
    assert client.stats.failed_attempts == 3 and client.stats.calls == 0


def test_backoff_delay_values():
    cfg = LLMConfig(model="deepseek/deepseek-flash")
    assert [backoff_delay(cfg, n, lambda: 1.0) for n in (1, 2, 3, 4)] == [
        4.0,
        12.0,
        36.0,
        60.0,
    ]
    assert [backoff_delay(cfg, n, lambda: 0.0) for n in (1, 2)] == [2.0, 6.0]


async def test_timeout_is_retried(make):
    client, server, tracer, _ = make([Step(delay=1.5), Step()], timeout_s=0.4)
    r = await client.complete(MSGS)
    assert r.content == "pong" and len(server.requests) == 2
    assert tracer.of("llm_call")[0]["retry_reason"] == "timeout"


async def test_connection_reset_is_retried(make):
    client, server, _, _ = make([Step(reset=True), Step()])
    assert (await client.complete(MSGS)).content == "pong" and len(server.requests) == 2


@pytest.mark.parametrize(
    "bad",
    [
        b"this is not json",
        b'{"id": "x", "choices": [{"index"',
        {"id": "x", "object": "chat.completion", "created": 0, "model": "m"},
    ],
    ids=["not-json", "truncated", "no-choices"],
)
async def test_unusable_200_is_retried(make, bad):
    client, server, tracer, _ = make([Step(body=bad), Step()])
    assert (await client.complete(MSGS)).content == "pong" and len(server.requests) == 2
    assert tracer.of("llm_call")[0]["outcome"] == "retry"


@pytest.mark.parametrize("reason", ["insufficient_system_resource", "aborted"])
async def test_interrupted_generation_is_retried_and_the_partial_content_dropped(
    make, reason
):
    partial = ok_body("half an answ", finish_reason=reason)
    client, server, tracer, _ = make(
        [Step(body=partial), Step(body=ok_body("the full answer"))]
    )
    r = await client.complete(MSGS)
    assert r.content == "the full answer" and len(server.requests) == 2
    first = tracer.of("llm_call")[0]
    assert first["retry_reason"] == "interrupted" and first["usage"] is None


# ---- no retry --------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 404])
async def test_4xx_is_not_retried(make, status):
    client, server, _, sleeps = make(
        [Step(status, error_body("nope", "invalid_request_error"))]
    )
    with pytest.raises(LLMError) as ei:
        await client.complete(MSGS)
    assert ei.value.kind == "client_error" and ei.value.status_code == status
    assert len(server.requests) == 1 and sleeps == []


async def test_context_window_error_is_its_own_kind(make):
    msg = "This model's maximum context length is 1000 tokens. However, your messages resulted in 5000 tokens."
    client, server, _, _ = make([Step(400, error_body(msg, "invalid_request_error"))])
    with pytest.raises(LLMError) as ei:
        await client.complete(MSGS)
    assert ei.value.kind == "context_overflow" and len(server.requests) == 1


async def test_length_is_returned_to_the_caller_not_retried(make):
    cut = ok_body(
        "",
        finish_reason="length",
        tool_calls=[tool_call("c1", "write", '{"path": "a.py", "content": "# line 1')],
    )
    client, server, _, _ = make([Step(body=cut)])
    r = await client.complete(MSGS, TOOLS)
    assert r.finish_reason == "length" and len(server.requests) == 1
    assert client.stats.failed_attempts == 0


# ---- the wall-clock deadline -----------------------------------------------------------
async def test_a_passed_deadline_sends_nothing(make):
    client, server, _, _ = make([Step()])
    with pytest.raises(LLMError) as ei:
        await client.complete(MSGS, deadline=time.monotonic() - 1)
    assert ei.value.kind == "time_budget" and server.requests == []


async def test_a_timeout_caused_by_the_deadline_is_time_budget_not_a_retry(make):
    client, server, _, sleeps = make([Step(delay=2.0), Step()], timeout_s=30)
    with pytest.raises(LLMError) as ei:
        await client.complete(MSGS, deadline=time.monotonic() + 0.4)
    assert ei.value.kind == "time_budget" and len(server.requests) == 1 and sleeps == []


async def test_no_point_waiting_for_a_retry_that_cannot_happen(make):
    client, server, _, sleeps = make([Step(500, error_body("boom"))], rng=lambda: 1.0)
    with pytest.raises(LLMError) as ei:
        await client.complete(
            MSGS, deadline=time.monotonic() + 1.0
        )  # first delay would be 4 s
    assert ei.value.kind == "time_budget" and len(server.requests) == 1 and sleeps == []


# ---- guards ----------------------------------------------------------------------------
def test_a_model_without_a_price_fails_at_construction():
    with pytest.raises(cost.UnknownModelPrice):
        LiteLLMClient(LLMConfig(model="deepseek/not-a-model"), MemoryTracer())


async def test_bugs_in_our_own_code_are_not_swallowed_as_retries(make, monkeypatch):
    import litellm

    client, *_ = make([Step()])

    async def boom(**kw):
        raise ValueError("a bug")

    monkeypatch.setattr(litellm, "acompletion", boom)
    with pytest.raises(ValueError, match="a bug"):
        await client.complete(MSGS)


def test_classify_ignores_non_llm_exceptions_and_knows_our_bad_response():
    assert classify(ValueError("x")) is None
    d = classify(BadResponse("no choices"))
    assert d is not None and d.kind == "bad_response" and d.retry


async def test_the_estimate_for_a_failed_attempt_counts_the_tools_with_the_tokenizer(
    make,
):
    # it used to raise inside litellm.token_counter on the todo tool and silently fall back to
    # len(json.dumps(messages)) // 4, which ignores the tool definitions
    import json

    from harness.config import AgentConfig
    from harness.context.tokens import count_messages
    from harness.tools.base import OutputLimiter
    from harness.tools.registry import Registry
    from harness.tools.submit import SubmitFlag
    from harness.tools.todo import TodoList

    client, _, _, _ = make([Step()])
    tools = Registry(
        AgentConfig(), OutputLimiter(8000), TodoList(), SubmitFlag()
    ).schemas()
    assert "todo" in [t["function"]["name"] for t in tools]
    estimate = await client._estimate_prompt_tokens(MSGS, tools)
    assert estimate != len(json.dumps(MSGS)) // 4  # not the fallback
    assert (
        estimate >= count_messages("deepseek/deepseek-flash", MSGS) + 400
    )  # the tools are in
