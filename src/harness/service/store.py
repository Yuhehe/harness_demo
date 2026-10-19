"""Task state for the service, in SQLite (aiosqlite).

One row per submitted task. The trajectory itself is not copied here: it is the JSONL file the
tracer writes (results/service/<task id>/trajectories/), which the events endpoint streams.
Status moves queued -> running -> done | failed; `done` means the agent loop finished (it may
still have ended with a FailReason), `failed` means the service could not run it at all (bad
repository, missing image, ...), with the reason in `error`.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiosqlite

STATUSES = ("queued", "running", "done", "failed")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    request TEXT NOT NULL,
    result TEXT,
    error TEXT
)
"""


@dataclass
class TaskRecord:
    id: str
    status: str
    created: float
    updated: float
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store is not open")
        return self._db

    async def create(self, request: dict[str, Any]) -> TaskRecord:
        now = time.time()
        rec = TaskRecord(uuid.uuid4().hex[:12], "queued", now, now, request)
        await self.db.execute(
            "INSERT INTO tasks (id, status, created, updated, request) VALUES (?, ?, ?, ?, ?)",
            (rec.id, rec.status, now, now, json.dumps(request, sort_keys=True)),
        )
        await self.db.commit()
        return rec

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self.db.execute(
            "SELECT id, status, created, updated, request, result, error FROM tasks WHERE id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TaskRecord(
            row[0], row[1], row[2], row[3], json.loads(row[4]),
            json.loads(row[5]) if row[5] else None, row[6],
        )  # fmt: skip

    async def set_status(
        self,
        task_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        await self.db.execute(
            "UPDATE tasks SET status = ?, updated = ?, result = ?, error = ? WHERE id = ?",
            (
                status,
                time.time(),
                json.dumps(result, sort_keys=True) if result is not None else None,
                error,
                task_id,
            ),
        )
        await self.db.commit()

    async def unfinished(self) -> list[str]:
        """Tasks a previous process left queued or running (re-queued at start-up)."""
        async with self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('queued', 'running') ORDER BY created"
        ) as cur:
            return [r[0] for r in await cur.fetchall()]
