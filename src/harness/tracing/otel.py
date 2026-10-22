"""Optional OpenTelemetry export of a run's spans (e.g. to Langfuse), next to the JSONL file.

Off unless HARNESS_OTEL=1. Where the spans go is set by the standard OTLP variables, read by
the exporter itself: OTEL_EXPORTER_OTLP_ENDPOINT (the exporter appends /v1/traces) and
OTEL_EXPORTER_OTLP_HEADERS. For Langfuse (docs/notes.md section 31): endpoint
https://cloud.langfuse.com/api/public/otel, headers
"Authorization=Basic <base64 of public_key:secret_key>,x-langfuse-ingestion-version=4".

The JSONL trajectory stays the only source of the reported numbers: OtelTracer hands every span
to the inner tracer first and unchanged, and a failure in the export turns the export off for
that task without touching the run. Nothing here affects what the agent does, so it is not part
of the config or its hash.

Mapping (one OTel trace per task):
  run_start          root span `harness.task`, Langfuse type `agent`; run id as the session,
                     config hash, model, commit and policy hash as trace metadata
  llm_call ok        `generation` child (start = end - latency_s), model, token usage, cost; its
                     input is what was added to the history since the previous reply (tool
                     results, nudges), its output the reply that follows (content, tool calls,
                     reasoning). A summary call (purpose summary) has no reply message: no output
  llm_call retry/fail  `generation` child with level WARNING / ERROR and the error, no usage
  tool_call          `tool` child named after the tool: arguments from the reply that made the
                     call, result from the tool message, the guard's decision as attributes
  compaction         `span` child with the token counts, cost and the summary text
  compaction_skipped, truncation   events on the root span
  run_end            the root span's output (submitted, fail_reason, steps, cost) and its end
Long texts are cut to HARNESS_OTEL_MAX_CHARS (default 8000) characters.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import time
from typing import Any

from harness.tracing.tracer import Tracer

log = logging.getLogger(__name__)
_PROVIDER: Any = None


def enabled() -> bool:
    return os.environ.get("HARNESS_OTEL") == "1"


def maybe_otel(inner: Tracer, *, run_id: str, instance_id: str) -> Tracer:
    """`inner` wrapped in an OtelTracer when HARNESS_OTEL=1, otherwise `inner` itself."""
    if not enabled():
        return inner
    try:
        otel_tracer = _provider().get_tracer("harness-lab")
    except Exception as e:  # noqa: BLE001 - the export must never stop a run
        log.warning("OpenTelemetry export disabled: %s", e)
        return inner
    return OtelTracer(
        inner, otel_tracer, run_id=run_id, instance_id=instance_id, flush=_flush
    )


def _provider() -> Any:
    global _PROVIDER
    if _PROVIDER is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "harness-lab"})
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        atexit.register(provider.shutdown)
        _PROVIDER = provider
    return _PROVIDER


def _flush() -> None:
    if _PROVIDER is not None:
        _PROVIDER.force_flush(timeout_millis=10_000)


def _cut(text: Any) -> str:
    limit = int(os.environ.get("HARNESS_OTEL_MAX_CHARS", "8000"))
    s = text if isinstance(text, str) else json.dumps(text, sort_keys=True, default=str)
    return s if len(s) <= limit else s[:limit] + f"... [{len(s) - limit} more chars]"


def _now() -> int:
    return time.time_ns()


def _ns(seconds: float | None) -> int:
    return int((seconds or 0) * 1e9)


class OtelTracer:
    """A Tracer that forwards to `inner` and mirrors the run into OpenTelemetry spans."""

    def __init__(
        self,
        inner: Tracer,
        otel_tracer: Any,
        *,
        run_id: str,
        instance_id: str,
        flush: Any = None,
    ) -> None:
        self.inner = inner
        self._ot = otel_tracer
        self.run_id, self.instance_id = run_id, instance_id
        self._flush = flush
        self._on = True
        self._root: Any = None
        self._ctx: Any = None
        self._task_input: str | None = None
        self._turn: list[dict[str, Any]] = []  # history added since the last reply
        self._gen: tuple[Any, int] | None = None  # pending generation and its end time
        self._calls: list[dict[str, Any]] = []  # tool calls of the last reply, in order
        self._next_call = 0
        self._guard: dict[str, Any] | None = None
        self._tools: dict[
            str, tuple[Any, int]
        ] = {}  # call id -> pending tool span, end time

    def __getattr__(self, name: str) -> Any:
        # anything else is the inner tracer's (e.g. JsonlTracer.path, which run_one reads)
        return getattr(self.__dict__["inner"], name)

    # ---- Tracer --------------------------------------------------------------------------------
    def emit(self, type: str, **fields: Any) -> None:
        self.inner.emit(type, **fields)
        if not self._on:
            return
        try:
            getattr(self, f"_on_{type}", self._ignore)(fields)
        except Exception as e:  # noqa: BLE001 - the export must never stop a run
            log.warning("OpenTelemetry export stopped for %s: %s", self.instance_id, e)
            self._on = False

    def close(self) -> None:
        try:
            if self._on and self._root is not None:
                from opentelemetry.trace import Status, StatusCode

                self._end_pending()
                self._root.set_status(
                    Status(StatusCode.ERROR, "run ended without run_end")
                )
                self._root.end()
                self._root = None
            if self._flush:
                self._flush()
        except Exception as e:  # noqa: BLE001
            log.warning("OpenTelemetry flush failed: %s", e)
        finally:
            self.inner.close()

    # ---- span types ----------------------------------------------------------------------------
    def _ignore(self, fields: dict[str, Any]) -> None:
        pass

    def _child(self, name: str, kind: str, start: int, attrs: dict[str, Any]) -> Any:
        return self._ot.start_span(
            name,
            context=self._ctx,
            start_time=start,
            attributes={"langfuse.observation.type": kind, **attrs},
        )

    def _on_run_start(self, f: dict[str, Any]) -> None:
        from opentelemetry import trace

        meta = {
            f"langfuse.trace.metadata.{k}": str(f[k])
            for k in (
                "config_hash",
                "model",
                "git_commit",
                "policy_hash",
                "reasoning_effort",
            )
            if f.get(k) is not None
        }
        self._root = self._ot.start_span(
            "harness.task",
            start_time=_now(),
            attributes={
                "langfuse.observation.type": "agent",
                "langfuse.trace.name": self.instance_id,
                "langfuse.session.id": self.run_id,
                "harness.run_id": self.run_id,
                "harness.instance_id": self.instance_id,
                **meta,
            },
        )
        self._ctx = trace.set_span_in_context(self._root)

    def _on_message(self, f: dict[str, Any]) -> None:
        role = f.get("role")
        if role == "assistant":
            self._calls = [
                {
                    "id": c.get("id"),
                    "name": c.get("function", {}).get("name", ""),
                    "arguments": c.get("function", {}).get("arguments", ""),
                }
                for c in f.get("tool_calls") or []
            ]
            self._next_call = 0
            if self._gen is not None:
                span, end = self._gen
                out = {"content": f.get("content"), "tool_calls": self._calls}
                if f.get("reasoning_content"):
                    out["reasoning"] = f["reasoning_content"]
                span.set_attribute("langfuse.observation.output", _cut(out))
                span.end(end_time=end)
                self._gen = None
            self._turn = []
            return
        if role == "tool":
            pending = self._tools.pop(f.get("tool_call_id") or "", None)
            if pending is not None:
                span, end = pending
                span.set_attribute(
                    "langfuse.observation.output", _cut(f.get("content") or "")
                )
                span.end(end_time=end)
        elif role == "user" and self._task_input is None:
            self._task_input = str(f.get("content") or "")
            self._root.set_attribute(
                "langfuse.observation.input", _cut(self._task_input)
            )
            return
        if role != "system":
            self._turn.append({"role": role, "content": f.get("content")})

    def _on_llm_call(self, f: dict[str, Any]) -> None:
        end = _now()
        purpose = f.get("purpose", "agent")
        attrs: dict[str, Any] = {
            "gen_ai.request.model": str(f.get("model") or ""),
            "harness.purpose": purpose,
            "harness.attempt": int(f.get("attempt") or 1),
            "harness.outcome": str(f.get("outcome")),
            "harness.latency_s": float(f.get("latency_s") or 0),
        }
        if f.get("outcome") == "ok":
            prompt, cached = (
                int(f.get("prompt_tokens") or 0),
                int(f.get("cached_tokens") or 0),
            )
            completion = int(f.get("completion_tokens") or 0)
            attrs |= {
                "gen_ai.response.model": str(
                    f.get("served_model") or f.get("model") or ""
                ),
                "gen_ai.usage.input_tokens": prompt,
                "gen_ai.usage.output_tokens": completion,
                "gen_ai.usage.cost": float(f.get("cost_usd") or 0),
                "langfuse.observation.usage_details": json.dumps(
                    {
                        "input": prompt - cached,
                        "input_cached": cached,
                        "output": completion,
                        "reasoning": int(f.get("reasoning_tokens") or 0),
                    },
                    sort_keys=True,
                ),
                "langfuse.observation.cost_details": json.dumps(
                    {"total": float(f.get("cost_usd") or 0)}
                ),
                "harness.finish_reason": str(f.get("finish_reason")),
                "harness.system_fingerprint": str(f.get("system_fingerprint")),
            }
        else:
            attrs |= {
                "langfuse.observation.level": "WARNING"
                if f.get("outcome") == "retry"
                else "ERROR",
                "langfuse.observation.status_message": str(
                    f.get("retry_reason") or f.get("error") or ""
                ),
            }
        span = self._child(
            f"llm {purpose}", "generation", end - _ns(f.get("latency_s")), attrs
        )
        if f.get("outcome") == "ok" and purpose == "agent":
            span.set_attribute("langfuse.observation.input", _cut(self._turn))
            self._end_pending_generation()
            self._gen = (span, end)  # its output is the reply message that follows
        else:
            span.end(end_time=end)

    def _on_guard_decision(self, f: dict[str, Any]) -> None:
        self._guard = f

    def _on_tool_call(self, f: dict[str, Any]) -> None:
        end = _now()
        call = (
            self._calls[self._next_call] if self._next_call < len(self._calls) else None
        )
        self._next_call += 1
        attrs: dict[str, Any] = {
            "harness.step": int(f.get("step") or 0),
            "harness.exit_code": -1
            if f.get("exit_code") is None
            else int(f["exit_code"]),
            "harness.truncated": bool(f.get("truncated")),
            "harness.format_error": bool(f.get("format_error")),
        }
        if self._guard is not None:
            attrs |= {
                "harness.guard.action": str(self._guard.get("action")),
                "harness.guard.rule_id": str(self._guard.get("rule_id")),
            }
            if self._guard.get("action") != "allow":
                attrs["langfuse.observation.level"] = "WARNING"
            self._guard = None
        if call is not None:
            attrs["langfuse.observation.input"] = _cut(call["arguments"])
        span = self._child(
            str(f.get("tool") or "tool"), "tool", end - _ns(f.get("duration_s")), attrs
        )
        if call is not None and call.get("id"):
            self._tools[call["id"]] = (
                span,
                end,
            )  # its result is the tool message that follows
        else:
            span.end(end_time=end)

    def _on_compaction(self, f: dict[str, Any]) -> None:
        end = _now()
        attrs = {
            f"harness.{k}": f[k]
            for k in (
                "step",
                "tokens_before",
                "tokens_after",
                "budget_tokens",
                "plan_level",
            )
            if isinstance(f.get(k), (int, float, str, bool))
        }
        attrs["harness.actions"] = _cut(f.get("actions"))
        attrs["gen_ai.usage.cost"] = float(f.get("cost_usd") or 0)
        if f.get("summary"):
            attrs["langfuse.observation.output"] = _cut(f["summary"])
        span = self._child(
            f"compaction {f.get('strategy')}",
            "span",
            end - _ns(f.get("duration_s")),
            attrs,
        )
        span.end(end_time=end)

    def _event(self, name: str, f: dict[str, Any]) -> None:
        self._root.add_event(
            name,
            {k: v for k, v in f.items() if isinstance(v, (int, float, str, bool))},
        )

    def _on_compaction_skipped(self, f: dict[str, Any]) -> None:
        self._event("compaction_skipped", f)

    def _on_truncation(self, f: dict[str, Any]) -> None:
        if self._gen is not None:  # the cut reply never enters the history
            span, end = self._gen
            span.set_attribute("langfuse.observation.level", "WARNING")
            span.set_attribute(
                "langfuse.observation.status_message", "output truncated"
            )
            span.end(end_time=end)
            self._gen = None
        self._event("truncation", f)

    def _on_run_end(self, f: dict[str, Any]) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._end_pending()
        out = {k: f.get(k) for k in ("submitted", "fail_reason", "steps", "cost_usd")}
        self._root.set_attribute("langfuse.observation.output", _cut(out))
        for k in ("steps", "tokens_in", "tokens_out", "tokens_cached", "cost_usd"):
            if isinstance(f.get(k), (int, float)):
                self._root.set_attribute(f"harness.{k}", f[k])
        if f.get("fail_reason"):
            self._root.set_attribute("langfuse.observation.level", "ERROR")
            self._root.set_status(Status(StatusCode.ERROR, str(f["fail_reason"])))
        self._root.end()
        self._root = None

    # ---- pending spans -------------------------------------------------------------------------
    def _end_pending_generation(self) -> None:
        if self._gen is not None:
            span, end = self._gen
            span.end(end_time=end)
            self._gen = None

    def _end_pending(self) -> None:
        self._end_pending_generation()
        for span, end in self._tools.values():
            span.end(end_time=end)
        self._tools.clear()
