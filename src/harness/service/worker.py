"""Background execution for the service: an asyncio queue and N workers.

Each task is assembled by `cli.run_one`, the same function `harness run` and the batch runner
use, so the service cannot drift from them. A task's files go to
<results_dir>/<task id>/ (trajectory, patch), the trajectory being what the events endpoint
streams. Model output still runs only in the sandbox container (CLAUDE.md section 2.2); the
host runs one trusted command, `git clone`, for git-URL tasks, with the URL as an argument
(no shell) and only for https URLs.

Settings come from the environment (`Settings.from_env`):

    HARNESS_RESULTS_DIR   where task files go (default results/service)
    HARNESS_DB            the SQLite file (default <results dir>/tasks.sqlite)
    HARNESS_REPOS_ROOT    local repository paths must be inside this directory
                          (default tests/fixtures)
    HARNESS_IMAGE         sandbox image for repository tasks (default harness-toy:latest)
    HARNESS_REPLAY        a recording to answer from instead of the API (CI, demos)
    HARNESS_WORKERS       tasks run side by side (default 1)
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.service.store import Store

CONFIGS_DIR = Path("configs")
TOY_CONFIG = Path("tests/fixtures/toy_config.yaml")
_CONFIG_NAME = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)?$")
CLONE_TIMEOUT_S = 300


class RequestError(ValueError):
    """The request cannot be run as given (reported to the client, not a server error)."""


@dataclass
class Settings:
    results_dir: Path = Path("results/service")
    db: Path | None = None
    repos_root: Path = Path("tests/fixtures")
    image: str = "harness-toy:latest"
    replay: Path | None = None
    workers: int = 1

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        results = Path(env.get("HARNESS_RESULTS_DIR", "results/service"))
        return cls(
            results_dir=results,
            db=Path(env["HARNESS_DB"]) if env.get("HARNESS_DB") else None,
            repos_root=Path(env.get("HARNESS_REPOS_ROOT", "tests/fixtures")),
            image=env.get("HARNESS_IMAGE", "harness-toy:latest"),
            replay=Path(env["HARNESS_REPLAY"]) if env.get("HARNESS_REPLAY") else None,
            workers=int(env.get("HARNESS_WORKERS", "1")),
        )

    @property
    def db_path(self) -> Path:
        return self.db or self.results_dir / "tasks.sqlite"


def config_path(name: str) -> Path:
    """A config name to its file: `toy` (the replay fixture) or a YAML under configs/."""
    if name == "toy":
        return TOY_CONFIG
    if not _CONFIG_NAME.match(name):
        raise RequestError(
            f"config {name!r}: use a name like 'default' or 'ablations/bash_only'"
        )
    path = CONFIGS_DIR / f"{name}.yaml"
    if not path.is_file() or name == "policy":  # policy.yaml is the guard's rules
        raise RequestError(f"config {name!r} not found under {CONFIGS_DIR}/")
    return path


def repo_path(raw: str, root: Path) -> Path:
    """A local repository path, which must be a directory inside `root`."""
    root = root.resolve()
    path = Path(raw)
    path = (path if path.is_absolute() else root / path).resolve()
    if path != root and root not in path.parents:
        raise RequestError(f"repo_path must be inside {root}")
    if not path.is_dir():
        raise RequestError(f"repo_path {raw!r} is not a directory")
    return path


def check_git_url(url: str) -> str:
    if not re.match(r"^https://[\w.-]+(:\d+)?/[\w./~-]+$", url):
        raise RequestError("git_url must be a plain https URL")
    return url


async def clone(url: str, dest: Path) -> Path:
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--", url, str(dest),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CLONE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise RequestError(f"git clone timed out after {CLONE_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise RequestError(f"git clone failed: {out.decode(errors='replace')[-500:]}")
    return dest


def instance_name(request: dict[str, Any]) -> str:
    """The name the task's trajectory and patch files get (the repository's directory name)."""
    src = request.get("repo_path") or request.get("git_url") or "repo"
    name = re.sub(r"\.git$", "", src.rstrip("/").rsplit("/", 1)[-1])
    return re.sub(r"[^\w.-]", "_", name) or "repo"


def summarize(outcome: Any) -> dict[str, Any]:
    r, m = outcome.result, outcome.result.metrics
    return {
        "submitted": r.submitted,
        "fail_reason": r.fail_reason,
        "fail_detail": r.fail_detail,
        "patch": r.patch,
        "metrics": {
            k: m.get(k)
            for k in (
                "steps",
                "cost_usd",
                "tokens_in",
                "tokens_cached",
                "tokens_out",
                "wall_seconds",
                "format_errors",
                "compactions",
            )
        },
        "trajectory": str(outcome.trajectory_path),
        "patch_path": str(outcome.patch_path),
    }


Runner = Callable[[str, dict[str, Any], Path, Settings], Awaitable[dict[str, Any]]]


async def run_with_harness(
    task_id: str, request: dict[str, Any], repo: Path, settings: Settings
) -> dict[str, Any]:
    """The real runner: `cli.run_one` with the request's config."""
    import docker

    from harness.agent.state import Task
    from harness.cli import check_summarizer, run_one
    from harness.config import load_config

    cfg = load_config(config_path(request["config"]))
    check_summarizer(cfg)
    client = docker.from_env()
    try:
        client.images.get(settings.image)
    except docker.errors.ImageNotFound:
        raise RequestError(f"sandbox image {settings.image} is not available") from None
    outcome = await run_one(
        Task(request["instance_id"], request["task"]),
        settings.image,
        cfg,
        settings.results_dir / task_id,
        task_id,
        docker_client=client,
        repo=repo,
        replay=settings.replay,
    )
    return summarize(outcome)


class Workers:
    def __init__(
        self, store: Store, settings: Settings, runner: Runner = run_with_harness
    ):
        self.store, self.settings, self.runner = store, settings, runner
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for (
            task_id
        ) in await self.store.unfinished():  # left behind by a stopped process
            self.queue.put_nowait(task_id)
        self._tasks = [
            asyncio.create_task(self._loop(), name=f"harness-worker-{i}")
            for i in range(max(self.settings.workers, 1))
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def submit(self, task_id: str) -> None:
        self.queue.put_nowait(task_id)

    async def _loop(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                await self.run(task_id)
            finally:
                self.queue.task_done()

    async def run(self, task_id: str) -> None:
        rec = await self.store.get(task_id)
        if rec is None:
            return
        await self.store.set_status(task_id, "running")
        tmp: Path | None = None
        try:
            if rec.request.get("git_url"):
                tmp = Path(tempfile.mkdtemp(prefix="harness-clone-"))
                repo = await clone(check_git_url(rec.request["git_url"]), tmp / "repo")
            else:
                repo = repo_path(rec.request["repo_path"], self.settings.repos_root)
            result = await self.runner(task_id, rec.request, repo, self.settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - reported on the task, never kills the worker
            await self.store.set_status(
                task_id, "failed", error=f"{type(e).__name__}: {e}"
            )
        else:
            await self.store.set_status(task_id, "done", result=result)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
