"""JSONL span tracer: one line per span, written and flushed immediately.

A run that dies half way still leaves its trajectory on disk. Span types (CLAUDE.md 5.7):
run_start, llm_call, tool_call, guard_decision, compaction, run_end.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol, Self


class Tracer(Protocol):
    def emit(self, type: str, **fields: Any) -> None: ...

    def close(self) -> None: ...


class JsonlTracer:
    def __init__(self, path: str | Path, *, run_id: str, instance_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.instance_id = instance_id
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, type: str, **fields: Any) -> None:
        span = {
            "type": type,
            "ts": time.time(),
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            **fields,
        }
        self._fh.write(json.dumps(span, sort_keys=True, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class MemoryTracer:
    """For tests: keeps spans in a list."""

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def emit(self, type: str, **fields: Any) -> None:
        self.spans.append({"type": type, **fields})

    def close(self) -> None:
        pass

    def of(self, type: str) -> list[dict[str, Any]]:
        return [s for s in self.spans if s["type"] == type]


class NullTracer:
    def emit(self, type: str, **fields: Any) -> None:
        pass

    def close(self) -> None:
        pass
