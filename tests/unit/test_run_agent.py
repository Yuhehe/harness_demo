"""eval/swebench/run_agent.py without Docker, an LLM or the dataset: the runner is a fake."""

import json
from types import SimpleNamespace

import pytest
import run_agent as ra

from harness.agent.state import FailReason
from harness.config import AgentConfig, LLMConfig

CFG = AgentConfig(llm=LLMConfig(model="deepseek/deepseek-flash"))
GIT = {"git_commit": "abc", "git_dirty": False}
IDS = ["a__a-1", "b__b-2", "c__c-3"]
ROWS = {i: {"problem_statement": f"fix {i}", "image": f"img/{i}"} for i in IDS}


def outcome(patch="DIFF", cost=0.1, fail=None, submitted=True, steps=5):
    return SimpleNamespace(
        result=SimpleNamespace(
            patch=patch,
            fail_reason=fail,
            submitted=submitted,
            metrics={"cost_usd": cost, "steps": steps},
        )
    )


class Runner:
    """Plays one outcome (or exception) per call and records what it was asked."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    async def __call__(self, task, image, cfg, run_dir, run_id, meta):
        self.calls.append((task.instance_id, image, task.problem_statement, meta))
        item = self.script.pop(0) if self.script else outcome()
        if isinstance(item, BaseException):
            raise item
        return item


async def batch(tmp_path, runner, ids=IDS, **kw):
    return await ra.run_batch(
        ids, ROWS, CFG, tmp_path / "R", "R", runner, git=GIT, say=lambda *_: None, **kw
    )


# ---- the happy path -------------------------------------------------------------------------
async def test_every_task_gets_a_prediction_and_a_status(tmp_path):
    runner = Runner()
    result = await batch(tmp_path, runner)
    assert result.ran == IDS and not result.skipped and not result.stopped_for_budget
    assert [c[0] for c in runner.calls] == IDS
    assert runner.calls[0][1:3] == ("img/a__a-1", "fix a__a-1")
    assert runner.calls[0][3] == {"run_id": "R", **GIT, "workers": 1}
    preds = ra.read_jsonl(tmp_path / "R" / ra.PREDS)
    assert [p["instance_id"] for p in preds] == IDS
    assert preds[0] == {
        "instance_id": "a__a-1",
        "model_name_or_path": "deepseek/deepseek-flash",
        "model_patch": "DIFF",
    }
    status = ra.read_jsonl(tmp_path / "R" / ra.STATUS)
    assert (
        status[0]["submitted"] is True
        and status[0]["steps"] == 5
        and status[0]["fail_reason"] is None
    )


async def test_swebench_reads_the_predictions_file(tmp_path):
    # the scorer's own loader (swebench/harness/utils.py), not a copy of its rules
    from swebench.harness.utils import get_predictions_from_file

    await batch(tmp_path, Runner())
    loaded = get_predictions_from_file(
        str(tmp_path / "R" / ra.PREDS), "SWE-bench/SWE-bench_Verified", "test"
    )
    assert sorted(p["instance_id"] for p in loaded) == sorted(IDS)
    assert all(
        {"instance_id", "model_name_or_path", "model_patch"} <= set(p) for p in loaded
    )


# ---- resuming ------------------------------------------------------------------------------------
async def test_a_rerun_skips_the_tasks_that_are_done(tmp_path):
    await batch(tmp_path, Runner(), ids=IDS[:2])
    runner = Runner()
    result = await batch(tmp_path, runner)
    assert result.skipped == IDS[:2] and result.ran == IDS[2:]
    assert [c[0] for c in runner.calls] == IDS[2:]
    assert len(ra.read_jsonl(tmp_path / "R" / ra.PREDS)) == 3  # nothing written twice


async def test_a_task_that_never_finished_has_no_prediction_and_runs_again(tmp_path):
    class Crash(BaseException):  # not an Exception: it ends the batch, like Ctrl-C
        pass

    with pytest.raises(Crash):
        await batch(tmp_path, Runner(outcome(), Crash()), ids=IDS[:2])
    assert [p["instance_id"] for p in ra.read_jsonl(tmp_path / "R" / ra.PREDS)] == [
        IDS[0]
    ]
    runner = Runner()
    await batch(tmp_path, runner, ids=IDS[:2])
    assert [c[0] for c in runner.calls] == [IDS[1]]


# ---- a task that breaks ----------------------------------------------------------------------------
async def test_an_error_in_one_task_is_recorded_and_the_batch_goes_on(tmp_path):
    runner = Runner(outcome(), RuntimeError("container did not start"), outcome())
    result = await batch(tmp_path, runner)
    assert result.ran == IDS  # all three were attempted
    status = {s["instance_id"]: s for s in ra.read_jsonl(tmp_path / "R" / ra.STATUS)}
    assert (
        status[IDS[1]]["fail_reason"] == "env_error"
        and "container did not start" in status[IDS[1]]["error"]
    )
    preds = {p["instance_id"]: p for p in ra.read_jsonl(tmp_path / "R" / ra.PREDS)}
    assert (
        preds[IDS[1]]["model_patch"] == ""
    )  # something for the scorer, and it is empty


async def test_env_errors_count_as_done_unless_you_ask_to_retry_them(tmp_path):
    await batch(
        tmp_path,
        Runner(
            outcome(),
            RuntimeError("boom"),
            outcome(fail=FailReason.step_limit, submitted=False),
        ),
    )
    again = Runner()
    await batch(tmp_path, again)
    assert not again.calls  # nothing runs: all three are done

    retry = Runner()
    await batch(tmp_path, retry, retry_env_errors=True)
    assert [c[0] for c in retry.calls] == [
        IDS[1]
    ]  # only the env_error; the step_limit stays
    preds = ra.read_jsonl(tmp_path / "R" / ra.PREDS)
    assert sorted(p["instance_id"] for p in preds) == sorted(IDS)
    assert [p["instance_id"] for p in preds].count(
        IDS[1]
    ) == 1  # replaced, not duplicated


async def test_a_task_that_failed_for_a_model_reason_is_recorded_with_that_reason(
    tmp_path,
):
    await batch(
        tmp_path,
        Runner(outcome(fail=FailReason.cost_limit, submitted=False)),
        ids=IDS[:1],
    )
    (row,) = ra.read_jsonl(tmp_path / "R" / ra.STATUS)
    assert row["fail_reason"] == "cost_limit" and row["submitted"] is False


# ---- one run id, one config ------------------------------------------------------------------------
def test_a_run_id_belongs_to_one_config(tmp_path):
    run = tmp_path / "R"
    ra.check_run_meta(run, "hash1", GIT, allow_dirty=False)
    ra.check_run_meta(
        run, "hash1", GIT, allow_dirty=False
    )  # the same config may resume
    with pytest.raises(SystemExit, match="use a new --run-id"):
        ra.check_run_meta(run, "hash2", GIT, allow_dirty=False)
    meta = json.loads((run / ra.RUN_META).read_text())
    assert (
        meta["config_hash"] == "hash1"
        and meta["git_commit"] == "abc"
        and meta["allow_dirty"] is False
    )


# ---- money -------------------------------------------------------------------------------------------
async def test_the_run_stops_at_eighty_percent_of_the_total_budget(tmp_path):
    runner = Runner(outcome(cost=3.0), outcome(cost=3.0), outcome(cost=3.0))
    result = await batch(tmp_path, runner, budget_total=10.0, spent_before=3.0)
    # 3.0 before; after one task 6.0; after two 9.0 >= 8.0: the third does not start
    assert result.ran == IDS[:2] and result.stopped_for_budget
    assert result.spent == pytest.approx(6.0)
    # and a later run picks the third up
    again = Runner()
    await batch(tmp_path, again, budget_total=100.0)
    assert [c[0] for c in again.calls] == [IDS[2]]


async def test_no_budget_means_no_stop(tmp_path):
    result = await batch(tmp_path, Runner(*[outcome(cost=50.0)] * 3), budget_total=None)
    assert not result.stopped_for_budget and len(result.ran) == 3


def _trajectory(path, model, cost):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "run_start", "model": model})
        + "\n"
        + json.dumps({"type": "run_end", "cost_usd": cost})
        + "\n"
    )


def test_spent_so_far_sums_every_finished_trajectory(tmp_path):
    _trajectory(tmp_path / "r1" / "trajectories" / "a.jsonl", "m", 0.25)
    _trajectory(tmp_path / "r2" / "trajectories" / "b.jsonl", "m", 0.5)
    (tmp_path / "r2" / "trajectories" / "c.jsonl").write_text(
        json.dumps({"type": "run_start", "model": "m"})
    )  # unfinished
    assert ra.spent_so_far(tmp_path) == pytest.approx(0.75)


def test_the_estimate_says_what_it_rests_on(tmp_path):
    _trajectory(tmp_path / "r1" / "trajectories" / "a.jsonl", "m", 0.2)
    _trajectory(tmp_path / "r1" / "trajectories" / "b.jsonl", "m", 0.4)
    _trajectory(
        tmp_path / "r1" / "trajectories" / "c.jsonl", "other", 9.0
    )  # another model: not used
    mean, source = ra.mean_cost_per_task(tmp_path, "m")
    assert mean == pytest.approx(0.3) and "2 earlier" in source and "m" in source
    est = ra.estimate(10, (mean, source), cap_usd=1.5)
    assert est.expected == pytest.approx(3.0) and est.worst_case == 15.0
    none = ra.estimate(10, ra.mean_cost_per_task(tmp_path, "unseen"), cap_usd=1.5)
    assert (
        none.expected is None
        and none.worst_case == 15.0
        and "no earlier run" in none.expected_source
    )


def test_confirmation_is_asked_above_ten_tasks_only():
    assert not ra.should_ask(10, yes=False) and ra.should_ask(11, yes=False)
    assert not ra.should_ask(11, yes=True)


# ---- what the machine carries -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "gb"),
    [("4g", 4), ("512m", 0.5), ("2G", 2), ("1t", 1024), ("1073741824", 1)],
)
def test_memory_sizes_are_read(text, gb):
    assert ra.parse_mem_gb(text) == pytest.approx(gb)


def test_the_sandbox_count_follows_the_scarcer_resource():
    gb = 1024**3
    # this machine as Docker reports it: 15.6 GB, 10 cpus, sandboxes of 4g / 2 cpus
    this = ra.max_sandboxes(16_772_939_776, 10, "4g", 2.0)
    assert this == {
        "safe_max": 3,
        "by_memory": 3,
        "by_cpu": 5,
        "mem_gb": 15.6,
        "ncpu": 10,
    }
    assert (
        ra.max_sandboxes(12 * gb, 10, "4g", 2.0)["safe_max"] == 2
    )  # the WSL limit in plan.md
    assert ra.max_sandboxes(64 * gb, 4, "4g", 2.0)["safe_max"] == 2  # cpu bound
    assert ra.max_sandboxes(3 * gb, 1, "4g", 2.0)["safe_max"] == 1  # never below one


# ---- images --------------------------------------------------------------------------------------------------------
def test_missing_images_are_listed_and_never_pulled():
    import docker

    class Images:
        def get(self, name):
            if name == "img/b__b-2":
                raise docker.errors.ImageNotFound("no")

    client = SimpleNamespace(images=Images())
    assert ra.missing_images(client, ROWS, IDS) == ["b__b-2"]


# ---- the command line ------------------------------------------------------------------------------------------------
@pytest.fixture
def fake_docker(monkeypatch):
    import docker

    client = SimpleNamespace(
        info=lambda: {"MemTotal": 12 * 1024**3, "NCPU": 10},
        images=SimpleNamespace(get=lambda n: None),
    )
    monkeypatch.setattr(docker, "from_env", lambda: client)
    return client


ARGS = [
    "--config",
    "configs/default.yaml",
    "--subset",
    "configs/subsets/dev_5.txt",
    "--run-id",
    "T",
]


def test_more_workers_than_the_machine_carries_is_refused_and_names_its_limit(
    fake_docker, tmp_path
):
    with pytest.raises(SystemExit) as e:
        ra.main([*ARGS, "--results-dir", str(tmp_path), "--workers", "4"])
    assert "can carry 2 sandboxes" in str(e.value)  # 12 GB: memory allows two
    assert "--force-workers" in str(e.value)


def test_check_workers():
    cap = {"safe_max": 3, "by_memory": 3, "by_cpu": 5, "mem_gb": 15.6, "ncpu": 10}
    ra.check_workers(1, cap)
    ra.check_workers(3, cap)
    with pytest.raises(SystemExit, match="at least 1"):
        ra.check_workers(0, cap)
    with pytest.raises(SystemExit, match="can carry 3"):
        ra.check_workers(4, cap)
    ra.check_workers(4, cap, force=True)


def test_a_dirty_tree_is_refused_unless_you_say_it_is_a_development_run(
    fake_docker, tmp_path, monkeypatch
):
    import harness.cli

    monkeypatch.setattr(
        harness.cli, "_git_meta", lambda: {"git_commit": "x", "git_dirty": True}
    )
    with pytest.raises(SystemExit, match="uncommitted changes"):
        ra.main([*ARGS, "--results-dir", str(tmp_path)])


def test_a_summarizer_the_size_was_not_calibrated_for_stops_the_batch_before_any_task(
    fake_docker, tmp_path, monkeypatch
):
    # checked once up front: inside the batch the error would make every task an env_error
    monkeypatch.setenv(
        "MODEL_CHEAP", "deepseek/deepseek-v4-pro"
    )  # .env does not override it
    with pytest.raises(ValueError, match="calibrated with"):
        ra.main([*ARGS, "--results-dir", str(tmp_path)])
    assert not (tmp_path / ARGS[ARGS.index("--run-id") + 1]).exists()  # nothing started


# ---- running tasks side by side ------------------------------------------------------------------
class SlowRunner:
    """Each task takes `delay` seconds (of event-loop time) and records how many ran at once."""

    def __init__(self, delay=0.02, fail=(), cost=0.1, delays=None):
        self.delay, self.fail, self.cost = delay, set(fail), cost
        self.delays = delays or {}
        self.active = self.peak = 0
        self.calls, self.cleaned = [], []

    async def __call__(self, task, image, cfg, run_dir, run_id, meta):
        import asyncio

        self.calls.append((task.instance_id, meta))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delays.get(task.instance_id, self.delay))
            if task.instance_id in self.fail:
                raise RuntimeError("container did not start")
            return outcome(cost=self.cost)
        finally:
            self.active -= 1
            self.cleaned.append(task.instance_id)  # stands in for removing the sandbox


MANY = [f"r__r-{i}" for i in range(7)]
MANY_ROWS = {i: {"problem_statement": f"fix {i}", "image": f"img/{i}"} for i in MANY}


async def many(tmp_path, runner, **kw):
    return await ra.run_batch(
        MANY,
        MANY_ROWS,
        CFG,
        tmp_path / "R",
        "R",
        runner,
        git=GIT,
        say=lambda *_: None,
        **kw,
    )


async def test_tasks_run_side_by_side_but_never_more_than_the_workers(tmp_path):
    runner = SlowRunner()
    result = await many(tmp_path, runner, workers=3)
    assert runner.peak == 3  # really parallel, and capped
    assert sorted(result.ran) == MANY and not result.stopped_for_budget
    preds = ra.read_jsonl(tmp_path / "R" / ra.PREDS)
    assert (
        sorted(p["instance_id"] for p in preds) == MANY
    )  # every task once, whole rows
    status = ra.read_jsonl(tmp_path / "R" / ra.STATUS)
    assert {s["workers"] for s in status} == {3}
    assert {m["workers"] for _, m in runner.calls} == {3}  # into every task's run_start


async def test_one_failing_task_does_not_stop_the_others_in_parallel(tmp_path):
    runner = SlowRunner(fail={"r__r-2", "r__r-5"})
    result = await many(tmp_path, runner, workers=3)
    assert sorted(result.ran) == MANY
    status = {s["instance_id"]: s for s in ra.read_jsonl(tmp_path / "R" / ra.STATUS)}
    assert status["r__r-2"]["fail_reason"] == "env_error"
    assert status["r__r-0"]["fail_reason"] is None


async def test_the_budget_counts_what_the_tasks_in_flight_are_expected_to_spend(
    tmp_path,
):
    # 3.0 spent, stop line 8.0, each task expected (and actually) 2.0, 3 workers:
    # starts r0 (3+0 < 8), r1 (3+2 < 8), r2 (3+4 < 8); r0 finishes first, r1 and r2 are still
    # running: 3 + 2 (finished) + 4 (two in flight) = 9 >= 8, so r3 does not start. Without
    # the reserves it would see 5 < 8 and start.
    runner = SlowRunner(cost=2.0, delays={"r__r-0": 0.01, "r__r-1": 0.2, "r__r-2": 0.2})
    result = await many(
        tmp_path,
        runner,
        workers=3,
        budget_total=10.0,
        spent_before=3.0,
        reserve_per_task=2.0,
    )
    assert result.stopped_for_budget
    assert sorted(result.ran) == MANY[:3]
    assert result.spent == pytest.approx(6.0)


async def test_one_worker_checks_the_budget_as_the_sequential_runner_did(tmp_path):
    # a reserve does not count for the task about to start, so it changes nothing here
    runner = Runner(outcome(cost=3.0), outcome(cost=3.0), outcome(cost=3.0))
    result = await batch(
        tmp_path, runner, budget_total=10.0, spent_before=3.0, reserve_per_task=5.0
    )
    assert result.ran == IDS[:2] and result.stopped_for_budget


async def test_cancelling_the_batch_cancels_and_cleans_up_every_task_in_flight(
    tmp_path,
):
    import asyncio

    runner = SlowRunner(delay=5.0)
    job = asyncio.create_task(many(tmp_path, runner, workers=3))
    await asyncio.sleep(0.05)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert runner.active == 0 and sorted(runner.cleaned) == MANY[:3]
    assert not (tmp_path / "R" / ra.PREDS).exists()  # nothing marked done


def test_leftover_sandboxes_are_listed_by_their_label():
    from harness.sandbox.docker_sandbox import LABEL

    seen = {}

    def list_(all, filters):
        seen.update(all=all, filters=filters)
        return [SimpleNamespace(name="harness-b"), SimpleNamespace(name="harness-a")]

    client = SimpleNamespace(containers=SimpleNamespace(list=list_))
    assert ra.leftover_sandboxes(client) == ["harness-a", "harness-b"]
    assert seen == {"all": True, "filters": {"label": LABEL}}
