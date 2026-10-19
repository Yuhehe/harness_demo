"""The HTTP service with a fake runner: no Docker, no LLM (service/api.py, worker.py, store.py)."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from harness.service import worker as w
from harness.service.api import create_app
from harness.service.store import Store
from harness.service.worker import RequestError, Settings, config_path, instance_name

TASK = "fix the failing test"


def fake_runner(calls, *, fail=None, spans=3):
    async def run(task_id, request, repo, settings):
        calls.append((task_id, request, repo))
        if fail:
            raise fail
        traj = (
            settings.results_dir
            / task_id
            / "trajectories"
            / f"{request['instance_id']}.jsonl"
        )
        traj.parent.mkdir(parents=True, exist_ok=True)
        with traj.open("w") as f:
            for i in range(spans):
                f.write(json.dumps({"type": "message", "step": i}) + "\n")
                f.flush()
                await asyncio.sleep(0.05)
        return {"submitted": True, "fail_reason": None, "patch": "DIFF"}

    return run


@pytest.fixture
def make(tmp_path):
    (tmp_path / "repos" / "toy_repo").mkdir(parents=True)

    def _make(runner):
        s = Settings(results_dir=tmp_path / "res", repos_root=tmp_path / "repos")
        return TestClient(create_app(s, runner)), s

    return _make


def wait_done(client, task_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        rec = client.get(f"/tasks/{task_id}").json()
        if rec["status"] in ("done", "failed"):
            return rec
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_a_task_runs_and_its_spans_stream(make):
    calls = []
    client, _ = make(fake_runner(calls))
    with client:
        r = client.post(
            "/tasks", json={"task": TASK, "repo_path": "toy_repo", "config": "toy"}
        )
        assert r.status_code == 202
        rec = r.json()
        assert rec["status"] == "queued" and rec["request"]["instance_id"] == "toy_repo"
        with client.stream("GET", f"/tasks/{rec['id']}/events") as s:
            body = "".join(s.iter_text())
        events = [e for e in body.split("\n\n") if e.strip()]
        spans = [e for e in events if e.startswith("event: span")]
        assert (
            len(spans) == 3 and json.loads(spans[0].split("data: ", 1)[1])["step"] == 0
        )
        assert events[-1].startswith("event: end")
        final = json.loads(events[-1].split("data: ", 1)[1])
        assert final["status"] == "done" and final["result"]["patch"] == "DIFF"
        assert client.get(f"/tasks/{rec['id']}").json()["result"]["submitted"]
    ((_, request, repo),) = calls
    assert repo.name == "toy_repo" and request["config"] == "toy"


@pytest.mark.parametrize(
    "body",
    [
        {"task": TASK},  # no source
        {"task": TASK, "repo_path": "toy_repo", "git_url": "https://example.com/a.git"},
        {"task": "  ", "repo_path": "toy_repo"},
        {"task": TASK, "repo_path": "../../etc"},  # outside the root
        {"task": TASK, "repo_path": ".."},  # outside the root, and it exists
        {"task": TASK, "repo_path": "missing_dir"},
        {"task": TASK, "repo_path": "toy_repo", "config": "../default"},
        {"task": TASK, "repo_path": "toy_repo", "config": "nope"},
        {"task": TASK, "repo_path": "toy_repo", "config": "policy"},
        {"task": TASK, "git_url": "file:///etc/passwd"},
        {"task": TASK, "git_url": "http://example.com/a.git"},
        {"task": TASK, "git_url": "ssh://example.com/a.git"},
        {"task": TASK, "git_url": "https://example.com/x; rm -rf /"},
    ],
)
def test_bad_requests_are_rejected_before_queueing(make, body):
    calls = []
    client, _ = make(fake_runner(calls))
    with client:
        assert client.post("/tasks", json=body).status_code == 422
    assert calls == []


def test_a_runner_error_marks_the_task_failed(make):
    client, _ = make(
        fake_runner([], fail=RequestError("sandbox image x is not available"))
    )
    with client:
        rec = client.post("/tasks", json={"task": TASK, "repo_path": "toy_repo"}).json()
        done = wait_done(client, rec["id"])
        assert done["status"] == "failed" and "not available" in done["error"]
        # the events stream ends even though no trajectory was written
        with client.stream("GET", f"/tasks/{rec['id']}/events") as s:
            assert "event: end" in "".join(s.iter_text())


def test_unknown_task_is_404(make):
    client, _ = make(fake_runner([]))
    with client:
        assert client.get("/tasks/nope").status_code == 404
        assert client.get("/tasks/nope/events").status_code == 404


def test_tasks_left_unfinished_are_run_after_a_restart(make, tmp_path):
    calls = []
    client, s = make(fake_runner(calls))

    async def leave_one():
        store = Store(s.db_path)
        await store.open()
        rec = await store.create(
            {
                "task": TASK,
                "repo_path": "toy_repo",
                "git_url": None,
                "config": "toy",
                "instance_id": "toy_repo",
            }
        )
        await store.set_status(rec.id, "running")  # the process died mid-task
        await store.close()
        return rec.id

    task_id = asyncio.run(leave_one())
    with client:
        assert wait_done(client, task_id)["status"] == "done"
    assert [c[0] for c in calls] == [task_id]


def test_git_url_tasks_are_cloned_then_cleaned_up(make, monkeypatch):
    seen = {}

    async def fake_clone(url, dest):
        dest.mkdir(parents=True)
        seen["url"], seen["dest"] = url, dest
        return dest

    monkeypatch.setattr(w, "clone", fake_clone)
    calls = []
    client, _ = make(fake_runner(calls, spans=1))
    with client:
        rec = client.post(
            "/tasks", json={"task": TASK, "git_url": "https://github.com/a/b.git"}
        ).json()
        assert rec["request"]["instance_id"] == "b"
        assert wait_done(client, rec["id"])["status"] == "done"
    assert seen["url"] == "https://github.com/a/b.git" and calls[0][2] == seen["dest"]
    assert not seen["dest"].exists()  # the clone is removed after the run


def test_config_names_map_to_files():
    assert config_path("default").name == "default.yaml"
    assert (
        config_path("ablations/bash_only")
        .as_posix()
        .endswith("ablations/bash_only.yaml")
    )
    assert config_path("toy").name == "toy_config.yaml"
    for bad in ("Default", "a/b/c", "policy", "../x"):
        with pytest.raises(RequestError):
            config_path(bad)


@pytest.mark.parametrize(
    ("req", "want"),
    [
        ({"repo_path": "toy_repo"}, "toy_repo"),
        ({"repo_path": "/x/y/proj/"}, "proj"),
        ({"git_url": "https://github.com/a/b.git"}, "b"),
        ({"git_url": "https://h/a/we ird"}, "we_ird"),
    ],
)
def test_instance_name(req, want):
    assert instance_name(req) == want


def test_a_span_written_in_two_pieces_is_streamed_once_whole(make):
    async def run(task_id, request, repo, settings):
        traj = (
            settings.results_dir
            / task_id
            / "trajectories"
            / f"{request['instance_id']}.jsonl"
        )
        traj.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"type": "message", "content": "x" * 200, "step": 1})
        with traj.open("w") as f:
            f.write(line[:50])
            f.flush()
            await asyncio.sleep(0.6)  # longer than the stream's poll interval
            f.write(line[50:] + "\n")
            f.flush()
        return {"submitted": True}

    client, _ = make(run)
    with client:
        rec = client.post("/tasks", json={"task": TASK, "repo_path": "toy_repo"}).json()
        with client.stream("GET", f"/tasks/{rec['id']}/events") as s:
            body = "".join(s.iter_text())
    spans = [
        e.split("data: ", 1)[1]
        for e in body.split("\n\n")
        if e.startswith("event: span")
    ]
    assert len(spans) == 1 and json.loads(spans[0])["step"] == 1
