"""The HTTP service: submit a task, read its state, stream its trajectory.

    uv run uvicorn --factory harness.service.api:create_app --host 0.0.0.0 --port 8000

    POST /tasks              {"task": "...", "repo_path": "toy_repo" | "git_url": "https://...",
                              "config": "default"}  -> 202 with the task record
    GET  /tasks/{id}         the record: status (queued | running | done | failed), the request,
                             and when done the result (submitted, fail_reason, patch, metrics)
    GET  /tasks/{id}/events  Server-Sent Events: one `span` event per trajectory line as the
                             tracer writes it (CLAUDE.md section 5.7), then one `end` event with
                             the final record
    GET  /healthz

Configs are named, never given as paths (`worker.config_path`); repository paths must be inside
HARNESS_REPOS_ROOT. Settings: `worker.Settings.from_env`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator

from harness.service.store import Store
from harness.service.worker import (
    RequestError,
    Runner,
    Settings,
    Workers,
    check_git_url,
    config_path,
    instance_name,
    repo_path,
    run_with_harness,
)

POLL_S = 0.2


class TaskRequest(BaseModel):
    task: str
    repo_path: str | None = None
    git_url: str | None = None
    config: str = "default"

    @model_validator(mode="after")
    def _one_source(self) -> TaskRequest:
        if (self.repo_path is None) == (self.git_url is None):
            raise ValueError("give exactly one of repo_path and git_url")
        if not self.task.strip():
            raise ValueError("task is empty")
        return self


def trajectory_path(settings: Settings, task_id: str, request: dict[str, Any]) -> Path:
    return (
        settings.results_dir
        / task_id
        / "trajectories"
        / f"{request['instance_id']}.jsonl"
    )


def create_app(
    settings: Settings | None = None, runner: Runner = run_with_harness
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    workers = Workers(store, settings, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await store.open()
        await workers.start()
        try:
            yield
        finally:
            await workers.stop()
            await store.close()

    app = FastAPI(title="harness-lab", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.workers = settings, store, workers

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "replay": settings.replay is not None}

    @app.post("/tasks", status_code=202)
    async def create(req: TaskRequest) -> dict[str, Any]:
        try:  # reject what can be checked now, before queueing
            config_path(req.config)
            if req.repo_path is not None:
                repo_path(req.repo_path, settings.repos_root)
            else:
                check_git_url(req.git_url or "")
        except RequestError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        request = req.model_dump()
        request["instance_id"] = instance_name(request)
        rec = await store.create(request)
        workers.submit(rec.id)
        return rec.to_dict()

    async def record(task_id: str) -> dict[str, Any]:
        rec = await store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id}")
        return rec.to_dict()

    @app.get("/tasks/{task_id}")
    async def get(task_id: str) -> dict[str, Any]:
        return await record(task_id)

    @app.get("/tasks/{task_id}/events")
    async def events(task_id: str) -> StreamingResponse:
        first = await record(task_id)
        path = trajectory_path(settings, task_id, first["request"])

        async def stream() -> AsyncIterator[str]:
            offset = 0
            while True:
                rec = await record(task_id)  # read before the file: no span is missed
                if path.exists():
                    with path.open("rb") as f:
                        f.seek(offset)
                        data = f.read()
                    complete, _, _ = data.rpartition(b"\n")  # only whole lines
                    if complete:
                        offset += len(complete) + 1
                        for line in complete.decode().splitlines():
                            if line.strip():
                                yield f"event: span\ndata: {line}\n\n"
                if rec["status"] in ("done", "failed"):
                    yield f"event: end\ndata: {json.dumps(rec, sort_keys=True)}\n\n"
                    return
                await asyncio.sleep(POLL_S)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
