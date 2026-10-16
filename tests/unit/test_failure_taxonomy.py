"""eval/report/failure_taxonomy.py on synthetic runs with known answers."""

import json

import pytest
from aggregate import Task, _taxonomy, end_category
from failure_taxonomy import (
    LOOP_RUN,
    LOOP_WINDOW,
    classify,
    load_labels,
    loop_signal,
    main,
    outcome_class,
)


def run_end(steps=10, fail=None, submitted=True, detail=None):
    return {
        "type": "run_end", "steps": steps, "cost_usd": 0.1, "fail_reason": fail,
        "fail_detail": detail, "submitted": submitted, "wall_seconds": 5.0,
        "tokens_in": 100, "tokens_cached": 50, "tokens_out": 10,
    }  # fmt: skip


def call(step, tool="bash", h="A"):
    return {"type": "tool_call", "step": step, "tool": tool, "args_hash": h}


def deny():
    return {"type": "guard_decision", "action": "deny", "rule_id": "deny.x"}


def allow():
    return {"type": "guard_decision", "action": "allow", "rule_id": None}


def task(spans, name="t1"):
    by = {}
    for s in spans:
        by.setdefault(s["type"], []).append(s)
    return Task(
        name,
        (by.get("run_end") or [None])[-1],
        tool_calls=by.get("tool_call", []),
        spans=spans,
    )


def outcome(f2p_pass, f2p_total, p2p_fail):
    return {
        "f2p_pass": f2p_pass,
        "f2p_total": f2p_total,
        "p2p_fail": p2p_fail,
        "p2p_total": 9,
    }


# ---- tests class -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("t", "want"),
    [
        (outcome(0, 2, 0), "target_fail"),
        (outcome(1, 2, 0), "partial_fix"),
        (outcome(1, 2, 1), "regression"),  # a regression wins over a partial fix
        (outcome(0, 2, 3), "regression"),
        (outcome(2, 2, 0), "unknown"),  # all pass, nothing broken, yet unresolved
        (None, "unknown"),
    ],
)
def test_tests_class(t, want):
    assert outcome_class(t) == want


# ---- end category (shared with aggregate) -----------------------------------------------------------
def test_end_category_matches_the_taxonomy_counts():
    score = {"empty_patch": ["e"], "error": ["b"]}
    resolved = {"ok"}
    rows = [
        {"instance_id": "ok", "fail_reason": None},
        {"instance_id": "bad", "fail_reason": None},
        {"instance_id": "e", "fail_reason": None},
        {"instance_id": "b", "fail_reason": None},
        {"instance_id": "s", "fail_reason": "step_limit"},
    ]
    assert [
        end_category(r["instance_id"], r["fail_reason"], resolved, score) for r in rows
    ] == [
        "submitted",
        "submitted_but_failed",
        "submitted_empty_patch",
        "scoring_error",
        "step_limit",
    ]
    assert _taxonomy(rows, resolved, score) == {
        "submitted": 1, "submitted_but_failed": 1, "submitted_empty_patch": 1,
        "scoring_error": 1, "step_limit": 1,
    }  # fmt: skip


def test_without_scores_a_submitted_run_stays_submitted():
    assert end_category("x", None, None) == "submitted"


# ---- loop signal -------------------------------------------------------------------------------------
def test_identical_consecutive_calls_reach_the_threshold():
    t = task([call(i) for i in range(LOOP_RUN)] + [run_end()])
    sig = loop_signal(t)
    assert sig["max_consecutive_identical"] == LOOP_RUN and sig["loop_suspect"]


def test_one_short_of_the_threshold_is_not_a_suspect():
    t = task([call(i) for i in range(LOOP_RUN - 1)] + [call(9, h="B"), run_end()])
    sig = loop_signal(t)
    assert sig["max_consecutive_identical"] == LOOP_RUN - 1 and not sig["loop_suspect"]


def test_interleaved_repeats_count_in_the_window_but_not_as_a_run():
    # the same test command after each edit: repeated, but not stuck
    spans = []
    for i in range(5):
        spans += [call(2 * i, "str_replace", f"E{i}"), call(2 * i + 1, "bash", "T")]
    sig = loop_signal(task(spans + [run_end()]))
    assert sig["max_consecutive_identical"] == 1 and not sig["loop_suspect"]
    assert sig["max_same_in_window"] == 5


def test_the_window_only_counts_calls_within_its_width():
    far = [call(0, h="X")] + [call(i, h=f"F{i}") for i in range(1, LOOP_WINDOW)]
    sig = loop_signal(task(far + [call(LOOP_WINDOW, h="X"), run_end()]))
    assert sig["max_same_in_window"] == 1  # the two X calls are LOOP_WINDOW apart
    near = far[:-1] + [call(LOOP_WINDOW - 1, h="X")]
    assert loop_signal(task(near + [run_end()]))["max_same_in_window"] == 2


def test_same_tool_other_arguments_is_not_identical():
    t = task([call(i, h=str(i)) for i in range(6)] + [run_end()])
    assert loop_signal(t)["max_consecutive_identical"] == 1


def test_no_tool_calls_gives_no_signal():
    assert loop_signal(task([run_end()])) is None


# ---- labels ------------------------------------------------------------------------------------------
LABELS_MD = """# 失败分析

| M0 | not-in-the-label-section | harness | ignored |

## 归属标注（机器可读）

| run_id | instance_id | 归属 | 依据 |
|---|---|---|---|
| M0 | `a` | model | read the issue wrongly |
| B0 | a | task | other run |
| M0 | b | harness | |

## 之后的一节

| M0 | c | task | after the section ends |
"""


def test_labels_come_only_from_the_label_section_and_the_run(tmp_path):
    p = tmp_path / "fa.md"
    p.write_text(LABELS_MD)
    labels = load_labels(p, "M0")
    assert labels == {
        "a": {"owner": "model", "basis": "read the issue wrongly"},
        "b": {"owner": "harness", "basis": ""},
    }
    assert load_labels(p, "B0") == {"a": {"owner": "task", "basis": "other run"}}
    assert load_labels(tmp_path / "missing.md", "M0") == {}


def test_an_unknown_owner_is_an_error(tmp_path):
    p = tmp_path / "fa.md"
    p.write_text("## 归属标注\n\n| M0 | a | the model | x |\n")
    with pytest.raises(ValueError, match="the model"):
        load_labels(p, "M0")


# ---- classify ----------------------------------------------------------------------------------------
def test_classify_labels_only_the_failures():
    tasks = {
        "ok": task([call(1), run_end()], "ok"),
        "bad": task(
            [allow(), deny(), call(1), allow(), call(2), run_end(steps=30)], "bad"
        ),
        "limit": task([run_end(fail="step_limit", submitted=False)], "limit"),
        "half": task([call(1)], "half"),  # no run_end: incomplete
    }
    score = {"tests": {"bad": outcome(1, 2, 0)}}
    out = classify(tasks, {"ok"}, score, {"bad": {"owner": "model", "basis": "why"}})
    assert out["tasks"] == 3 and out["incomplete"] == 1 and out["resolved"] == 1
    assert out["failures"] == {
        "n": 2,
        "by_end": {"step_limit": 1, "submitted_but_failed": 1},
        "by_tests": {"partial_fix": 1, "unknown": 1},
        "by_owner": {"model": 1, "unlabelled": 1},
    }
    rows = {r["instance_id"]: r for r in out["per_task"]}
    assert "owner" not in rows["ok"] and "tests" not in rows["ok"]
    assert rows["bad"]["guard_denials"] == 1 and rows["bad"]["owner_basis"] == "why"
    assert rows["limit"]["loop"] is None  # no calls


def test_a_resolved_run_that_hit_a_limit_is_not_a_failure():
    tasks = {"x": task([run_end(fail="step_limit", submitted=False)], "x")}
    out = classify(tasks, {"x"}, None, {})
    assert out["failures"]["n"] == 0 and out["per_task"][0]["end"] == "step_limit"


def test_labels_for_tasks_that_did_not_fail_are_reported():
    tasks = {"ok": task([run_end()], "ok")}
    out = classify(tasks, {"ok"}, None, {"ok": {"owner": "model", "basis": ""}})
    assert out["unknown_labels"] == ["ok"]


def test_loop_suspects_are_listed_with_and_without_resolution():
    loop = [call(i) for i in range(LOOP_RUN)]
    tasks = {"a": task(loop + [run_end()], "a"), "b": task(loop + [run_end()], "b")}
    sig = classify(tasks, {"a"}, None, {})["loop_signal"]
    assert sig["suspect"] == ["a", "b"] and sig["suspect_unresolved"] == ["b"]


# ---- command line ------------------------------------------------------------------------------------
def test_main_writes_the_file_and_needs_scores(tmp_path):
    run = tmp_path / "runs" / "R"
    (run / "trajectories").mkdir(parents=True)
    (run / "trajectories" / "a.jsonl").write_text(json.dumps(run_end()))
    args = ["--run-id", "R", "--results-dir", str(tmp_path / "runs")]
    with pytest.raises(SystemExit, match="score the run first"):
        main(args + ["--labels", str(tmp_path / "none.md")])
    (run / "resolved.json").write_text("[]")
    labels = tmp_path / "fa.md"
    labels.write_text("## 归属标注\n| R | a | task | spec conflict |\n")
    assert main(args + ["--labels", str(labels)]) == 0
    out = json.loads((run / "failure_taxonomy.json").read_text())
    assert out["run_id"] == "R" and out["failures"]["by_owner"] == {"task": 1}
