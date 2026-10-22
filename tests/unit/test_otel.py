"""tracing/otel.py: the OpenTelemetry mirror of a run (in-memory exporter, no network)."""

import json

import pytest
from fakes import FakeLLMServer, FakeSandbox, Step, ok_body, tool_call
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harness.agent.loop import run_task
from harness.agent.state import Task
from harness.config import AgentConfig, GuardConfig, LimitsConfig, LLMConfig
from harness.llm.client import LiteLLMClient
from harness.sandbox.base import ExecResult
from harness.tracing import otel
from harness.tracing.otel import OtelTracer, maybe_otel
from harness.tracing.tracer import MemoryTracer


def exporter_and_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def cfg():
    return AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled"),
        guard=GuardConfig(enabled=True),
        limits=LimitsConfig(
            max_steps=6, max_cost_per_task_usd=0.5, max_wall_seconds=100
        ),
    )


SCRIPT = [
    Step(body=ok_body(tool_calls=[tool_call("c1", "bash", '{"command": "ls"}')],
                      finish_reason="tool_calls")),
    Step(body=ok_body(tool_calls=[tool_call("c2", "bash", '{"command": "cat ~/.ssh/id_rsa"}'),
                                  tool_call("c3", "bash", '{"command": "echo hi"}')],
                      finish_reason="tool_calls")),
    Step(body=ok_body(tool_calls=[tool_call("c4", "submit", "{}")], finish_reason="tool_calls")),
]  # fmt: skip


async def run(monkeypatch, tracer):
    with FakeLLMServer(list(SCRIPT)) as server:
        monkeypatch.setenv("DEEPSEEK_API_BASE", server.url)
        sb = FakeSandbox(
            handler=lambda c: ExecResult(0, "README.md\n" if c == "ls" else "hi\n")
        )
        llm = LiteLLMClient(cfg().llm, tracer)
        return await run_task(Task("t1", "fix it"), cfg(), sb, llm, tracer)


async def test_the_run_becomes_one_trace_with_generations_and_tools(monkeypatch):
    exporter, ot = exporter_and_tracer()
    inner = MemoryTracer()
    tracer = OtelTracer(inner, ot, run_id="R1", instance_id="t1")
    result = await run(monkeypatch, tracer)
    tracer.close()
    assert result.submitted
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "harness.task")
    assert root.attributes["langfuse.observation.type"] == "agent"
    assert root.attributes["langfuse.session.id"] == "R1"
    assert root.attributes["langfuse.trace.name"] == "t1"
    assert "fix it" in root.attributes["langfuse.observation.input"]
    assert (
        json.loads(root.attributes["langfuse.observation.output"])["submitted"] is True
    )
    children = [s for s in spans if s is not root]
    assert all(s.parent.span_id == root.context.span_id for s in children)
    # trace-level attributes on every observation, not only the root: Langfuse stores them
    # per observation (with them on the root alone, its table shows no trace name or session)
    for key in (
        "langfuse.session.id",
        "langfuse.trace.name",
        "langfuse.trace.metadata.model",
    ):
        assert {s.attributes.get(key) for s in children} == {root.attributes[key]}, key
    assert len({s.context.trace_id for s in spans}) == 1
    assert all(
        root.start_time <= s.start_time <= s.end_time <= root.end_time for s in children
    )

    gens = [
        s for s in children if s.attributes["langfuse.observation.type"] == "generation"
    ]
    assert len(gens) == 3 == len(inner.of("llm_call"))
    first = gens[0]
    assert first.attributes["gen_ai.request.model"] == "deepseek/deepseek-flash"
    assert (
        first.attributes["gen_ai.usage.input_tokens"]
        == inner.of("llm_call")[0]["prompt_tokens"]
    )
    out = json.loads(first.attributes["langfuse.observation.output"])
    assert out["tool_calls"][0]["arguments"] == '{"command": "ls"}'
    # the second generation's input is what the first turn added: the tool result
    assert "README.md" in gens[1].attributes["langfuse.observation.input"]

    tools = [s for s in children if s.attributes["langfuse.observation.type"] == "tool"]
    assert [s.name for s in tools] == ["bash", "bash", "bash", "submit"]
    assert "README.md" in tools[0].attributes["langfuse.observation.output"]
    denied = tools[1]
    assert denied.attributes["harness.guard.action"] == "deny"
    assert denied.attributes["harness.guard.rule_id"] == "deny.ssh_access"
    assert denied.attributes["langfuse.observation.level"] == "WARNING"
    assert "~/.ssh" in denied.attributes["langfuse.observation.input"]
    assert (
        "Blocked by the permission layer"
        in denied.attributes["langfuse.observation.output"]
    )
    assert tools[2].attributes["harness.guard.action"] == "allow"


async def test_the_jsonl_side_gets_exactly_what_it_gets_without_the_export(monkeypatch):
    plain = MemoryTracer()
    await run(monkeypatch, plain)
    inner = MemoryTracer()
    _, ot = exporter_and_tracer()
    await run(monkeypatch, OtelTracer(inner, ot, run_id="R", instance_id="t1"))
    timing = {"latency_s", "duration_s", "wall_seconds"}
    strip = lambda spans: [
        {k: v for k, v in s.items() if k not in timing} for s in spans
    ]
    assert strip(inner.spans) == strip(plain.spans)


class Broken:
    def start_span(self, *a, **k):
        raise RuntimeError("collector down")


async def test_a_failing_export_never_touches_the_run(monkeypatch):
    inner = MemoryTracer()
    tracer = OtelTracer(inner, Broken(), run_id="R", instance_id="t1")
    result = await run(monkeypatch, tracer)
    tracer.close()
    assert result.submitted and inner.of("run_end")
    assert tracer._on is False


def test_a_run_that_dies_still_closes_its_trace():
    exporter, ot = exporter_and_tracer()
    tracer = OtelTracer(MemoryTracer(), ot, run_id="R", instance_id="t1")
    tracer.emit("run_start", config_hash="abc")
    tracer.emit("llm_call", outcome="ok", purpose="agent", model="m", latency_s=0.1,
                prompt_tokens=10, cached_tokens=0, completion_tokens=2, cost_usd=0.0)  # fmt: skip
    tracer.close()  # no run_end
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "harness.task")
    assert root.status.status_code.name == "ERROR"
    assert len(spans) == 2  # the pending generation was ended too


def test_failed_attempts_and_truncation_are_marked():
    exporter, ot = exporter_and_tracer()
    tracer = OtelTracer(MemoryTracer(), ot, run_id="R", instance_id="t1")
    tracer.emit("run_start")
    tracer.emit("llm_call", outcome="retry", purpose="agent", model="m", latency_s=0.1,
                retry_reason="http_429")  # fmt: skip
    tracer.emit("llm_call", outcome="ok", purpose="agent", model="m", latency_s=0.1,
                prompt_tokens=10, cached_tokens=4, completion_tokens=7, reasoning_tokens=5,
                cost_usd=0.001)  # fmt: skip
    tracer.emit("truncation", step=1, content_chars=10, tool_calls=0, consecutive=1)
    tracer.emit(
        "run_end", submitted=False, fail_reason="format_error", steps=1, cost_usd=0.001
    )
    spans = {
        s.name + str(s.attributes.get("harness.outcome")): s
        for s in exporter.get_finished_spans()
    }
    assert spans["llm agentretry"].attributes["langfuse.observation.level"] == "WARNING"
    ok = spans["llm agentok"]
    assert ok.attributes["langfuse.observation.status_message"] == "output truncated"
    # buckets that do not overlap: completion includes reasoning, prompt includes cached
    assert json.loads(ok.attributes["langfuse.observation.usage_details"]) == {
        "input": 6, "input_cached_tokens": 4, "output": 2, "output_reasoning_tokens": 5,
        "total": 17,
    }  # fmt: skip
    assert spans["harness.taskNone"].status.status_code.name == "ERROR"


def test_long_texts_are_cut(monkeypatch):
    monkeypatch.setenv("HARNESS_OTEL_MAX_CHARS", "10")
    assert otel._cut("x" * 25) == "x" * 10 + "... [15 more chars]"
    assert otel._cut("short") == "short"


def test_off_unless_asked(monkeypatch):
    inner = MemoryTracer()
    monkeypatch.delenv("HARNESS_OTEL", raising=False)
    assert maybe_otel(inner, run_id="R", instance_id="t") is inner
    monkeypatch.setenv("HARNESS_OTEL", "1")
    monkeypatch.setattr(otel, "_PROVIDER", TracerProvider())
    wrapped = maybe_otel(inner, run_id="R", instance_id="t")
    assert isinstance(wrapped, OtelTracer) and wrapped.inner is inner


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_only_the_value_1_turns_it_on(monkeypatch, value):
    monkeypatch.setenv("HARNESS_OTEL", value)
    assert not otel.enabled()


def test_the_wrapper_is_transparent_for_the_inner_tracers_attributes(tmp_path):
    # run_one reads tracer.path after the run (the first real export crashed on it)
    from harness.tracing.tracer import JsonlTracer

    inner = JsonlTracer(tmp_path / "t.jsonl", run_id="R", instance_id="t")
    _, ot = exporter_and_tracer()
    wrapped = OtelTracer(inner, ot, run_id="R", instance_id="t")
    assert wrapped.path == inner.path
    with pytest.raises(AttributeError):
        _ = wrapped.no_such_attribute
    wrapped.close()


class SlowExporter:
    def export(self, spans):
        import time

        from opentelemetry.sdk.trace.export import SpanExportResult

        time.sleep(2)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=None):
        return True


def test_a_slow_collector_never_blocks_the_run(monkeypatch):
    # several tasks share one event loop (run_agent --workers, the service): closing a task's
    # tracer must not wait for the collector
    import time

    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(SlowExporter(), schedule_delay_millis=1)
    )
    monkeypatch.setenv("HARNESS_OTEL", "1")
    monkeypatch.setattr(otel, "_PROVIDER", provider)
    tracer = maybe_otel(MemoryTracer(), run_id="R", instance_id="t")
    started = time.monotonic()
    for _ in range(3):
        tracer.emit("run_start")
        tracer.emit("run_end", submitted=True, steps=1, cost_usd=0.0)
    tracer.close()
    assert time.monotonic() - started < 0.5


def test_tests_never_export_whatever_the_developer_set():
    # tests/conftest.py: a developer's .env with HARNESS_OTEL=1 and Langfuse keys must not
    # send test runs anywhere (CLAUDE.md section 8)
    import os

    assert os.environ.get("HARNESS_OTEL") == "0"
    assert not [k for k in os.environ if k.startswith("OTEL_EXPORTER_OTLP")]
