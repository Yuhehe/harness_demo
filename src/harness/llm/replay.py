"""Record real LLM responses and replay them (CLAUDE.md 5.8).

CI and the end-to-end test run in replay mode and cost nothing. A miss is an error, never
a guess.

What a request is matched on matters, because requests contain *tool outputs* and real
sandbox output is not reproducible across runs and machines (`Ran 1 test in 0.003s`, temp
paths, `ls` dates, and the ORDER of `find`/`ls` output, which depends on the host file
system). So the key is built from everything that determines the recorded conversation
path: model, thinking, reasoning_effort, max_tokens, tools, and every message EXCEPT the content of tool
results. Assistant messages (which come from the recording) and user messages are in it.

Tool outputs are still compared, through a normalized digest (volatile parts replaced by
placeholders): a difference is reported in `ReplayClient.mismatches` and logged, and raises
only in strict mode. The end-to-end test asserts the real outcome, so a harness change that
breaks the task still fails the test.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

from harness.config import LLMConfig
from harness.llm import cost
from harness.llm.client import LLMClient
from harness.llm.types import CallStats, LLMResponse, Message

_VOLATILE = [
    (  # the full `date` output: "Tue Oct  6 07:11:03 UTC 2026"
        re.compile(
            r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+"
            r"\d{2}:\d{2}:\d{2}\s+[A-Z]{2,5}\s+\d{4}\b"
        ),
        "<DATE>",
    ),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"), "<TIMESTAMP>"),
    (  # `ls -l` and `date`: "Oct  6 07:11", "Oct  6  2026"
        re.compile(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+(?:\d{2}:\d{2}|\d{4})\b"
        ),
        "<LSDATE>",
    ),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<TIME>"),
    (re.compile(r"\b\d+m\d+(?:\.\d+)?s\b"), "<DURATION>"),  # `time` output: 0m0.003s
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b"), "<DURATION>"),  # in 0.003s / 12 ms
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "0x<ADDR>"),
    (
        re.compile(r"/tmp/(?!harness/)[\w.\-/]+"),
        "/tmp/<TMP>",
    ),  # ours (/tmp/harness/) is stable
]


def normalize_text(text: str) -> str:
    for pattern, repl in _VOLATILE:
        text = pattern.sub(repl, text)
    return text


def _normalized(messages: list[Message]) -> list[Message]:
    out = []
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            m = {**m, "content": normalize_text(m["content"])}
        out.append(m)
    return out


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fingerprint(
    cfg: LLMConfig, messages: list[Message], tools: list[dict[str, Any]] | None
) -> tuple[str, list[str], list[str]]:
    """(request key, per-message normalized digests, per-message previews).

    The key ignores the content of tool results; the digests include it."""
    model = cfg.model or os.environ.get("MODEL_MAIN", "")
    sorted_tools = sorted(tools or [], key=lambda t: t["function"]["name"])
    norm = _normalized(messages)
    digests = [_digest(_dump(m)) for m in norm]
    previews = [f"{m.get('role')}: {str(m.get('content'))[:100]!r}" for m in norm]
    path = [
        {k: v for k, v in m.items() if k != "content"} if m.get("role") == "tool" else m
        for m in messages
    ]
    key = _digest(
        _dump(
            {
                "model": model,
                "thinking": cfg.thinking,
                "reasoning_effort": cfg.reasoning_effort,
                "max_tokens": cfg.max_tokens,
                "tools": sorted_tools,
                "messages": path,
            }
        )
    )
    return key, digests, previews


log = logging.getLogger("harness.replay")


class ReplayMiss(Exception):
    """The request was not recorded. The message says where it diverges."""


class RecordingClient:
    """Wraps a real client and appends every successful response to a JSONL file."""

    def __init__(self, inner: LLMClient, cfg: LLMConfig, path: str | Path) -> None:
        self.inner, self.cfg = inner, cfg
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def stats(self) -> CallStats:
        return self.inner.stats

    async def complete(self, messages, tools=None, *, deadline=None) -> LLMResponse:
        resp = await self.inner.complete(messages, tools, deadline=deadline)
        key, digests, previews = fingerprint(self.cfg, messages, tools)
        record = {
            "key": key,
            "msg_digests": digests,
            "msg_previews": previews,
            "response": resp.to_dict(),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(_dump(record) + "\n")
        return resp


class ReplayClient:
    def __init__(
        self,
        cfg: LLMConfig,
        path: str | Path,
        *,
        strict_tool_outputs: bool = False,
        tracer: Any = None,
        purpose: str = "agent",
    ) -> None:
        """With a `tracer`, every replayed answer writes the `llm_call` span a live call
        would (recorded usage, the same cost formula, latency 0, `replay: true`), so a replayed
        trajectory has the shape `aggregate.py` reads."""
        self.cfg = cfg
        self.tracer, self.purpose = tracer, purpose
        self.strict_tool_outputs = strict_tool_outputs
        self.stats = CallStats()
        self.mismatches: list[
            dict[str, Any]
        ] = []  # tool outputs that differ from the recording
        self._records: list[dict[str, Any]] = []
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            self._records.append(rec)
            self._queues.setdefault(rec["key"], deque()).append(rec)

    async def complete(self, messages, tools=None, *, deadline=None) -> LLMResponse:
        key, digests, previews = fingerprint(self.cfg, messages, tools)
        queue = self._queues.get(key)
        if not queue:
            raise ReplayMiss(
                self._explain(key, digests, previews, exhausted=key in self._queues)
            )
        # The same request can legitimately occur twice; answer in recorded order.
        rec = queue.popleft()
        self._compare_tool_outputs(rec, digests, previews)
        self.stats.calls += 1
        self.stats.attempts += 1
        resp = LLMResponse.from_dict(rec["response"])
        if self.tracer is not None:
            model = self.cfg.resolved_model()
            self.tracer.emit(
                "llm_call",
                attempt=1,
                outcome="ok",
                purpose=self.purpose,
                model=model,
                finish_reason=resp.finish_reason,
                latency_s=0.0,
                prompt_tokens=resp.usage.prompt_tokens,
                cached_tokens=resp.usage.cached_tokens,
                completion_tokens=resp.usage.completion_tokens,
                reasoning_tokens=resp.usage.reasoning_tokens,
                cost_usd=cost.cost_usd(model, resp.usage),
                response_id=resp.response_id,
                served_model=resp.model,
                system_fingerprint=resp.system_fingerprint,
                retries=0,
                replay=True,
            )
        return resp

    def _compare_tool_outputs(
        self, rec: dict[str, Any], digests: list[str], previews: list[str]
    ) -> None:
        for i, (now, then) in enumerate(zip(digests, rec["msg_digests"], strict=False)):
            if now == then:
                continue
            mismatch = {
                "index": i,
                "now": previews[i],
                "recorded": rec["msg_previews"][i],
            }
            if self.strict_tool_outputs:
                raise ReplayMiss(
                    f"message {i} differs from the recording (strict mode)\n"
                    f"  now:      {mismatch['now']}\n  recorded: {mismatch['recorded']}"
                )
            self.mismatches.append(mismatch)
            log.warning(
                "replay: message %d differs from the recording (continuing): %s",
                i,
                mismatch,
            )

    def _explain(
        self, key: str, digests: list[str], previews: list[str], *, exhausted: bool
    ) -> str:
        if exhausted:
            return f"request {key[:12]} was recorded, but every recorded response was already used"
        best, best_common = None, -1
        for rec in self._records:
            common = 0
            for a, b in zip(rec["msg_digests"], digests, strict=False):
                if a != b:
                    break
                common += 1
            if common > best_common:
                best, best_common = rec, common
        head = f"no recorded response for request {key[:12]} ({len(digests)} messages)."
        if best is None:
            return head + " The recording is empty."
        if best_common < len(digests) and best_common < len(best["msg_digests"]):
            return (
                f"{head} The closest recording shares the first {best_common} messages; "
                f"message {best_common} differs.\n  now:      {previews[best_common]}\n"
                f"  recorded: {best['msg_previews'][best_common]}"
            )
        return (
            head
            + " Model, thinking, reasoning_effort, max_tokens or tools differ from the recording."
        )
