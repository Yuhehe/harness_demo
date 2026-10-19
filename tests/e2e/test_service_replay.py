"""The service end to end with the real harness: Docker sandbox, toy repository, replayed model.

The same recording as test_toy_replay.py, submitted over HTTP: the task must finish, the patch
must fix the bug, and the events stream must carry the trajectory from run_start to run_end.
No API key and no network (the replay answers every request).
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.service.api import create_app
from harness.service.worker import Settings

ROOT = Path(__file__).resolve().parents[2]
TASK = "The unit tests of this library fail: add() returns a wrong result. Fix the bug in the library code."

pytestmark = pytest.mark.docker


def test_the_service_runs_the_recorded_toy_task(docker_client, tmp_path):
    settings = Settings(
        results_dir=tmp_path / "service",
        repos_root=ROOT / "tests/fixtures",
        image="harness-toy:latest",
        replay=ROOT / "tests/fixtures/replays/toy_fix.jsonl",
    )
    with TestClient(create_app(settings)) as client:
        r = client.post(
            "/tasks", json={"task": TASK, "repo_path": "toy_repo", "config": "toy"}
        )
        assert r.status_code == 202, r.text
        task_id = r.json()["id"]
        with client.stream("GET", f"/tasks/{task_id}/events") as s:
            body = "".join(s.iter_text())
        deadline = time.time() + 120
        while (rec := client.get(f"/tasks/{task_id}").json())["status"] not in (
            "done",
            "failed",
        ):
            assert time.time() < deadline, "the task did not finish"
            time.sleep(0.2)

    assert rec["status"] == "done", rec.get("error")
    result = rec["result"]
    assert result["submitted"] and result["fail_reason"] is None
    assert "+    return a + b" in result["patch"]
    spans = [
        json.loads(e.split("data: ", 1)[1])
        for e in body.split("\n\n")
        if e.startswith("event: span")
    ]
    types = [s["type"] for s in spans]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert spans[0]["config"]["prompt_variant"] == "v1"
    # replayed calls leave the same llm_call spans a live run does (recorded usage)
    calls = [x for x in spans if x["type"] == "llm_call"]
    assert len(calls) == 5 and all(
        c["replay"] and c["prompt_tokens"] > 0 for c in calls
    )
    assert body.rstrip().split("\n\n")[-1].startswith("event: end")
