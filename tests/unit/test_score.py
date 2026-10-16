"""eval/swebench/score.py with a fake `swebench` (no containers). The report it reads back is
a real one, produced by `swebench eval` on the B0 smoke run (tests/fixtures/score/)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import score as sc
from aggregate import aggregate, load_run

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "score"
    / "swebench_report_resolved.json"
)
META = json.loads(Path("configs/subsets/subsets_meta.json").read_text())


# ---- reading the scorer's report --------------------------------------------------------------
def test_the_real_report_is_read():
    score = sc.read_report(FIXTURE)
    assert score.resolved == {"django__django-11066"}
    assert all(score.ids[g] == [] for g in sc.GROUPS if g != "resolved")


def test_every_group_the_scorer_reports_is_kept_apart(tmp_path):
    report = json.loads(FIXTURE.read_text())
    report.update(
        resolved_ids=["a"], unresolved_ids=["b"], empty_patch_ids=["c"],
        error_ids=["d"], infra_failure_ids=["e"], ambiguous_failure_ids=["f"], incomplete_ids=["g"],
    )  # fmt: skip
    path = tmp_path / "m.R.json"
    path.write_text(json.dumps(report))
    score = sc.read_report(path)
    assert {k: v for k, v in score.ids.items()} == {
        "resolved": ["a"], "unresolved": ["b"], "empty_patch": ["c"], "error": ["d"],
        "infra_failure": ["e"], "ambiguous_failure": ["f"], "incomplete": ["g"],
    }  # fmt: skip


def test_the_report_is_found_by_run_id_not_by_rebuilding_its_name(tmp_path):
    with pytest.raises(SystemExit, match="found 0"):
        sc.find_report(tmp_path, "R")
    (tmp_path / "deepseek__deepseek-flash.R.json").write_text("{}")
    (tmp_path / "other.json").write_text("{}")
    assert sc.find_report(tmp_path, "R").name == "deepseek__deepseek-flash.R.json"
    (tmp_path / "second__model.R.json").write_text("{}")
    with pytest.raises(SystemExit, match="found 2"):
        sc.find_report(tmp_path, "R")


# ---- the dataset revision ----------------------------------------------------------------------------
def test_scoring_is_refused_when_the_dataset_has_moved_on():
    sc.check_dataset_revision(META, META["revision"])
    with pytest.raises(SystemExit) as e:
        sc.check_dataset_revision(META, "f" * 40)
    assert (
        "has changed" in str(e.value)
        and "ffffffffffff" in str(e.value)
        and META["revision"][:12] in str(e.value)
    )


# ---- the command ---------------------------------------------------------------------------------------------
def test_the_command_limits_the_scorer_to_our_instances_and_our_directory():
    cmd = sc.swebench_command(
        Path("/venv/bin/swebench"),
        Path("/r/preds.jsonl"),
        "R",
        ["a-1", "b-2"],
        workers=2,
        timeout=900,
    )
    assert cmd[:3] == ["/venv/bin/swebench", "eval", "verified"]
    assert (
        cmd[cmd.index("-p") + 1] == "/r/preds.jsonl"
        and cmd[cmd.index("--run-id") + 1] == "R"
    )
    assert cmd[cmd.index("-j") + 1] == "2" and cmd[cmd.index("-t") + 1] == "900"
    assert cmd[cmd.index("--report-dir") + 1] == "."  # the cwd is the run directory
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "-i"] == ["a-1", "b-2"]


# ---- the whole thing, with a fake scorer ----------------------------------------------------------------------
def make_run(tmp_path, ids=("a__a-1", "b__b-2", "c__c-3")):
    run = tmp_path / "R"
    (run / "trajectories").mkdir(parents=True)
    (run / "preds.jsonl").write_text(
        "".join(
            json.dumps(
                {"instance_id": i, "model_name_or_path": "m", "model_patch": "P"}
            )
            + "\n"
            for i in ids
        )
    )
    for i in ids:
        (run / "trajectories" / f"{i}.jsonl").write_text(
            json.dumps({"type": "run_end", "steps": 4, "cost_usd": 0.1, "compaction_cost_usd": 0.0, "fail_reason": None,
                        "submitted": True, "wall_seconds": 5.0, "tokens_in": 100, "tokens_cached": 80, "tokens_out": 10})
        )  # fmt: skip
    return run


class FakeScorer:
    """Stands in for subprocess.run: records the call, writes the report swebench would."""

    def __init__(self, report, returncode=0):
        self.report, self.returncode, self.calls = report, returncode, []

    def __call__(self, cmd, cwd, check):
        self.calls.append((cmd, cwd))
        if self.returncode == 0:
            (cwd / "m__x.R.json").write_text(json.dumps(self.report))
        return SimpleNamespace(returncode=self.returncode)


def score_it(tmp_path, scorer, newest=None):
    return sc.main(
        ["--run-id", "R", "--results-dir", str(tmp_path)],
        run=scorer,
        newest_revision=lambda *_: newest or META["revision"],
    )


def test_scoring_writes_resolved_score_and_metrics(tmp_path):
    run = make_run(tmp_path)
    scorer = FakeScorer(
        {
            "resolved_ids": ["a__a-1"],
            "unresolved_ids": ["b__b-2"],
            "empty_patch_ids": ["c__c-3"],
        }
    )
    assert score_it(tmp_path, scorer) == 0

    ((cmd, cwd),) = scorer.calls
    assert cwd == run.resolve() and cmd.count("-i") == 3
    assert json.loads((run / "resolved.json").read_text()) == ["a__a-1"]
    assert json.loads((run / "score.json").read_text())["empty_patch"] == ["c__c-3"]
    metrics = json.loads((run / "metrics.json").read_text())
    assert (
        metrics["resolve_rate"]["resolved"] == 1 and metrics["resolve_rate"]["n"] == 3
    )
    # judged and not fixed is told apart from not judged at all
    assert metrics["fail_taxonomy"] == {
        "submitted": 1,
        "submitted_but_failed": 1,
        "submitted_empty_patch": 1,
    }


def test_a_scoring_environment_problem_is_not_the_models_failure(tmp_path):
    run = make_run(tmp_path)
    scorer = FakeScorer(
        {
            "resolved_ids": [],
            "unresolved_ids": ["a__a-1"],
            "infra_failure_ids": ["b__b-2"],
            "error_ids": ["c__c-3"],
        }
    )
    score_it(tmp_path, scorer)
    taxonomy = json.loads((run / "metrics.json").read_text())["fail_taxonomy"]
    assert taxonomy == {"submitted_but_failed": 1, "scoring_error": 2}


def test_nothing_is_scored_against_a_moved_dataset(tmp_path):
    make_run(tmp_path)
    scorer = FakeScorer({})
    with pytest.raises(SystemExit, match="has changed"):
        score_it(tmp_path, scorer, newest="e" * 40)
    assert not scorer.calls  # the scorer never ran


def test_a_failing_scorer_is_an_error_and_leaves_no_resolved_file(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(SystemExit, match="exited with 2"):
        score_it(tmp_path, FakeScorer({}, returncode=2))
    assert not (run / "resolved.json").exists()


def test_scoring_needs_predictions(tmp_path):
    (tmp_path / "R").mkdir()
    with pytest.raises(SystemExit, match="run_agent.py first"):
        score_it(tmp_path, FakeScorer({}))


def test_aggregate_with_a_score_but_no_resolved_set_keeps_the_old_behaviour(tmp_path):
    make_run(tmp_path)
    assert aggregate(load_run(tmp_path / "R"))["fail_taxonomy"] == {"submitted": 3}


# ---- per-test outcomes, and reading a report again ----------------------------------------------
def _instance_report(run, run_id, iid, f2p_ok, f2p_bad, p2p_ok, p2p_bad):
    d = run / "logs" / "run_evaluation" / run_id / "m__x" / iid
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({iid: {"tests_status": {
        "FAIL_TO_PASS": {"success": f2p_ok, "failure": f2p_bad},
        "PASS_TO_PASS": {"success": p2p_ok, "failure": p2p_bad},
    }}}))  # fmt: skip


def test_the_per_test_outcome_is_read_from_each_instance_report(tmp_path):
    run = make_run(tmp_path)
    _instance_report(run, "R", "a__a-1", ["t1"], [], ["p1", "p2"], [])
    _instance_report(
        run, "R", "b__b-2", ["t1"], ["t2"], ["p1"], ["p2"]
    )  # partial, broke p2
    (run / "logs" / "run_evaluation" / "R" / "m__x" / "c__c-3").mkdir(parents=True)
    tests = sc.read_tests(run, "R")
    assert tests == {
        "a__a-1": {"f2p_pass": 1, "f2p_total": 1, "p2p_fail": 0, "p2p_total": 2},
        "b__b-2": {"f2p_pass": 1, "f2p_total": 2, "p2p_fail": 1, "p2p_total": 2},
    }  # c__c-3 has no report: left out


def test_reuse_report_reads_again_without_scoring(tmp_path):
    run = make_run(tmp_path)
    (run / "m__x.R.json").write_text(
        json.dumps({"resolved_ids": ["a__a-1"], "unresolved_ids": ["b__b-2"]})
    )
    _instance_report(run, "R", "b__b-2", [], ["t1"], ["p1"], ["p2"])
    scorer = FakeScorer({})
    assert sc.main(["--run-id", "R", "--results-dir", str(tmp_path), "--reuse-report"],
                   run=scorer, newest_revision=lambda *_: "moved") == 0  # fmt: skip
    assert not scorer.calls  # nothing scored, the dataset revision not even checked
    score = json.loads((run / "score.json").read_text())
    assert score["tests"]["b__b-2"]["p2p_fail"] == 1
    metrics = json.loads((run / "metrics.json").read_text())
    assert metrics["quality"]["tests"]["regressions"] == 1
