"""eval/report/aggregate.py on synthetic trajectories with known answers."""

import json

import pytest
from aggregate import (
    Task,
    aggregate,
    compare,
    edited_before,
    load_run,
    main,
    mentions,
    percentile,
    repeat_rate,
    steps_after_compaction,
    summary_file_recall,
    wilson,
)


# ---- builders ----------------------------------------------------------------------------------
def run_end(
    steps,
    cost=0.1,
    compaction_cost=0.0,
    fail=None,
    submitted=True,
    wall=10.0,
    tin=1000,
    cached=800,
):
    return {
        "type": "run_end", "steps": steps, "cost_usd": cost, "compaction_cost_usd": compaction_cost,
        "fail_reason": fail, "submitted": submitted, "wall_seconds": wall,
        "tokens_in": tin, "tokens_cached": cached, "tokens_out": 50,
    }  # fmt: skip


def edit_msg(step, path):
    return {
        "type": "message", "role": "assistant", "step": step, "content": "",
        "tool_calls": [{"id": f"e{step}", "type": "function", "function": {
            "name": "str_replace", "arguments": json.dumps({"path": path, "old_str": "a", "new_str": "b"})}}],
    }  # fmt: skip


def call(step, tool, h):
    return {"type": "tool_call", "step": step, "tool": tool, "args_hash": h}


def compaction(step, summary=None):
    return {
        "type": "compaction",
        "step": step,
        "summary": summary,
        "restored": {"stat_chars": 99},
    }


def write(tmp, run, instance, spans):
    d = tmp / run / "trajectories"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{instance}.jsonl").write_text("\n".join(json.dumps(s) for s in spans))


def task(spans, name="t1"):
    by = {}
    for s in spans:
        by.setdefault(s["type"], []).append(s)
    return Task(
        name,
        (by.get("run_end") or [None])[-1],
        by.get("compaction", []),
        by.get("tool_call", []),
        by.get("message", []),
    )


# ---- statistics ---------------------------------------------------------------------------------
def test_wilson_matches_known_values():
    lo, hi = wilson(35, 50)
    assert (round(lo, 3), round(hi, 3)) == (0.562, 0.809)
    assert wilson(0, 10)[0] == pytest.approx(0, abs=1e-12)
    assert wilson(10, 10)[1] == pytest.approx(1.0)
    assert wilson(0, 0) is None


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10], 0.95) == 10
    assert percentile([0, 100], 0.95) == pytest.approx(95)
    assert percentile([], 0.5) is None


# ---- compaction metrics -----------------------------------------------------------------------------
def test_steps_after_the_first_compaction():
    t = task([compaction(6), compaction(12), run_end(20)])
    assert steps_after_compaction(t) == 14
    assert steps_after_compaction(task([run_end(20)])) is None


def test_repeat_rate_counts_calls_after_the_boundary_that_were_made_before_it():
    t = task([
        call(1, "view", "A"), call(2, "bash", "B"), call(3, "bash", "C"),
        call(4, "view", "A"), call(5, "bash", "D"), call(6, "bash", "B"), run_end(6),
    ])  # fmt: skip
    assert repeat_rate(t, 3) == pytest.approx(2 / 3)  # A and B repeat; D is new
    assert repeat_rate(t, 6) is None  # nothing was called after the boundary


def test_a_call_made_on_the_boundary_step_belongs_to_the_part_before_it():
    # compaction(step=3) happens before the 4th model call; the 3rd response's calls were
    # made with the full history, so repeating one of them afterwards is a repeat
    t = task([call(3, "bash", "A"), call(4, "bash", "A"), run_end(4)])
    assert repeat_rate(t, 3) == 1.0


def test_the_same_tool_with_other_arguments_is_not_a_repeat():
    t = task([call(1, "view", "A"), call(2, "bash", "A"), run_end(2)])
    assert repeat_rate(t, 1) == 0.0


def test_mentions_matches_the_path_or_the_file_name_in_any_case():
    assert mentions("fixed Django/Core/Ops.py today", "django/core/ops.py")
    assert mentions("changed ops.py", "django/core/ops.py")
    assert not mentions("changed utils.py", "django/core/ops.py")


def test_edited_before_uses_the_step_of_the_edit():
    t = task(
        [edit_msg(2, "a.py"), edit_msg(5, "b.py"), edit_msg(9, "c.py"), run_end(10)]
    )
    assert edited_before(t, 5) == ["a.py", "b.py"] and edited_before(t, 1) == []


def test_file_recall_is_about_files_edited_before_the_summary():
    t = task([
        edit_msg(2, "pkg/ops.py"), edit_msg(3, "pkg/utils.py"), edit_msg(9, "pkg/late.py"),
        compaction(5, "## Files modified\n- pkg/ops.py: fixed add"), run_end(12),
    ])  # fmt: skip
    assert (
        summary_file_recall(t) == 0.5
    )  # ops.py yes, utils.py no; late.py is after the summary


def test_the_generated_state_block_cannot_raise_the_recall():
    # the block lists the patch by design; only the summarizer's own text may count
    t = task(
        [
            edit_msg(2, "pkg/ops.py"),
            compaction(5, "## Tried so far\nnothing"),
            run_end(8),
        ]
    )
    t.compactions[0]["restored"] = {"stat_text": "pkg/ops.py | +1 -1"}
    assert summary_file_recall(t) == 0.0


def test_recall_ignores_compactions_without_a_summary_or_without_edits():
    assert (
        summary_file_recall(
            task([edit_msg(2, "a.py"), compaction(5, None), run_end(8)])
        )
        is None
    )
    assert summary_file_recall(task([compaction(5, "text"), run_end(8)])) is None


# ---- a run ---------------------------------------------------------------------------------------------
def test_aggregate_a_small_run(tmp_path):
    write(
        tmp_path,
        "R",
        "a",
        [compaction(4, "x"), run_end(10, cost=0.30, compaction_cost=0.03, wall=100)],
    )
    write(tmp_path, "R", "b", [run_end(6, cost=0.10, wall=20)])
    write(
        tmp_path,
        "R",
        "c",
        [run_end(30, cost=0.50, fail="context_overflow", submitted=False, wall=300)],
    )
    write(tmp_path, "R", "d", [{"type": "run_start"}])  # crashed before run_end
    tasks = load_run(tmp_path / "R")
    m = aggregate(tasks, resolved={"a"})

    assert (m["tasks"], m["incomplete"]) == (3, 1)
    assert m["cost_per_task"]["mean"] == pytest.approx(0.30) and m["cost_per_task"][
        "p50"
    ] == pytest.approx(0.30)
    assert m["cache_hit_rate"] == pytest.approx(0.8)
    assert m["overflow_rate"] == pytest.approx(1 / 3)
    assert m["steps_mean"] == pytest.approx(46 / 3)
    assert m["latency"]["p50"] == 100
    # "a" was resolved; "b" submitted but was not; "c" overflowed
    assert m["fail_taxonomy"] == {
        "submitted": 1,
        "context_overflow": 1,
        "submitted_but_failed": 1,
    }
    assert m["resolve_rate"]["resolved"] == 1 and m["resolve_rate"]["n"] == 3
    assert m["resolve_rate"]["wilson95"][0] < 1 / 3 < m["resolve_rate"]["wilson95"][1]
    c = m["compaction"]
    assert c["tasks_with_compaction"] == 1 and c["steps_after_compaction_mean"] == 6
    assert c["compaction_cost_share"] == pytest.approx(0.10)  # 0.03 of 0.30


def test_without_a_score_the_resolve_fields_are_null_and_nothing_is_called_failed(
    tmp_path,
):
    write(tmp_path, "R", "a", [run_end(5)])
    m = aggregate(load_run(tmp_path / "R"))
    assert m["resolve_rate"] is None and m["fail_taxonomy"] == {"submitted": 1}


def test_an_empty_run_gives_an_empty_result():
    assert aggregate({}) == {"tasks": 0, "incomplete": 0}


# ---- two runs -----------------------------------------------------------------------------------------------
def test_compare_looks_only_at_compacted_tasks_in_both_runs(tmp_path):
    for inst in "abc":
        write(
            tmp_path,
            "A1",
            inst,
            [
                call(1, "view", "X"),
                call(5, "view", "X"),
                call(6, "bash", "Y"),
                run_end(8),
            ],
        )
    write(
        tmp_path,
        "M0",
        "a",
        [
            call(1, "view", "X"),
            call(5, "bash", "Z"),
            call(6, "bash", "Y"),
            compaction(3, "s"),
            run_end(12),
        ],
    )
    write(
        tmp_path, "M0", "b", [call(1, "view", "X"), run_end(8)]
    )  # never compacted: left out
    write(
        tmp_path, "M0", "x", [compaction(2, "s"), run_end(9)]
    )  # not in the baseline: left out
    out = compare(
        load_run(tmp_path / "A1"), load_run(tmp_path / "M0"),
        base_resolved={"a", "b"}, other_resolved={"a", "c"},
    )  # fmt: skip
    assert out["paired_tasks"] == 1  # only "a"
    assert out["steps_delta_mean"] == 4  # 12 - 8
    # after step 3: M0 made [bash Z, bash Y] (none seen before); A1 made [view X, bash Y] (X seen)
    assert (
        out["repeat_rate_other"] == 0.0 and out["repeat_rate_base_at_same_step"] == 0.5
    )
    assert out["resolved_pairs"] == {
        "both": 1,
        "only_base": 0,
        "only_other": 0,
        "neither": 0,
    }


def test_compare_with_nothing_in_common(tmp_path):
    write(tmp_path, "A1", "a", [run_end(5)])
    write(tmp_path, "M0", "b", [compaction(1, "s"), run_end(5)])
    assert compare(load_run(tmp_path / "A1"), load_run(tmp_path / "M0")) == {
        "paired_tasks": 0
    }


# ---- the command line ---------------------------------------------------------------------------------------
def test_main_writes_metrics_json_and_reads_resolved_json(tmp_path, capsys):
    write(tmp_path, "M0", "a", [compaction(2, "s"), run_end(7)])
    write(tmp_path, "M0", "b", [run_end(4)])
    (tmp_path / "M0" / "resolved.json").write_text(json.dumps(["a"]))
    write(tmp_path, "A1", "a", [run_end(5)])
    assert (
        main(["--run-id", "M0", "--baseline", "A1", "--results-dir", str(tmp_path)])
        == 0
    )
    saved = json.loads((tmp_path / "M0" / "metrics.json").read_text())
    assert (
        saved["resolve_rate"]["resolved"] == 1
        and saved["vs_baseline"]["baseline"] == "A1"
    )
    assert saved["vs_baseline"]["paired_tasks"] == 1


def test_main_refuses_an_empty_run(tmp_path):
    (tmp_path / "R" / "trajectories").mkdir(parents=True)
    with pytest.raises(SystemExit):
        main(["--run-id", "R", "--results-dir", str(tmp_path)])


# ---- the size after a compaction: the provider's count when a request followed ---------------------
def sized(step, est, **extra):
    return {"type": "compaction", "step": step, "tokens_after": est, **extra}


def llm(prompt, outcome="ok", purpose="agent"):
    return {
        "type": "llm_call",
        "purpose": purpose,
        "outcome": outcome,
        "prompt_tokens": prompt,
    }


def test_the_size_after_a_compaction_is_the_next_agent_requests_count():
    from aggregate import sizes_after_compaction

    t = task([])
    t.spans = [
        sized(5, 6000),
        llm(None, outcome="retry"),  # a failed attempt has no usage
        llm(
            900, purpose="summary"
        ),  # the summarizer's own call is not the agent's prompt
        llm(4800),
        {"type": "message", "step": 6},
        sized(9, 7000),
        {
            "type": "message",
            "step": 9,
        },  # the history moved on before any request: no count
        llm(9999),
        sized(12, 5000),  # the run ended (context_overflow): nothing followed
    ]
    assert sizes_after_compaction(t) == [
        {
            "step": 5,
            "tokens_after_est": 6000,
            "tokens_after": 4800,
            "source": "provider",
        },
        {
            "step": 9,
            "tokens_after_est": 7000,
            "tokens_after": 7000,
            "source": "estimate",
        },
        {
            "step": 12,
            "tokens_after_est": 5000,
            "tokens_after": 5000,
            "source": "estimate",
        },
    ]


def test_estimated_sizes_are_counted_apart_and_kept_out_of_the_mean(tmp_path):
    spans = [
        sized(5, 6000), llm(4000), {"type": "message", "step": 6},
        sized(8, 9000),
        run_end(8, fail="context_overflow"),
    ]  # fmt: skip
    write(tmp_path, "R", "t1", spans)
    out = aggregate(load_run(tmp_path / "R"))["compaction"]["tokens_after"]
    assert out["tokens_after_source"] == {"provider": 1, "estimate": 1}
    assert out["provider_mean"] == 4000
    assert out["estimate_error_mean"] == pytest.approx(6000 / 4000 - 1)


def test_monitoring_counts_oversized_summaries_skips_and_unusual_ratios(tmp_path):
    spans = [
        sized(3, 5000, summary="s", summary_expected_tokens=1000, summary_tokens_out=1200,
              ratio_unusual=True),
        sized(7, 5000, summary="s", summary_expected_tokens=1000, summary_tokens_out=800),
        sized(9, 5000),  # truncate only: no summary to compare
        {"type": "compaction_skipped", "step": 11, "reason": "no_gain", "ratio_unusual": False},
        {"type": "compaction_skipped", "step": 14, "reason": "no_gain", "ratio_unusual": True},
        {"type": "compaction_skipped", "step": 15, "reason": "infeasible"},
        run_end(15),
    ]  # fmt: skip
    write(tmp_path, "R", "t1", spans)
    out = aggregate(load_run(tmp_path / "R"))
    mon = out["monitoring"]
    assert mon["summary_size"] == {
        "n": 2, "over_expected": 1, "actual_over_expected_max": pytest.approx(1.2)
    }  # fmt: skip
    assert mon["compactions_skipped"] == {"no_gain": 2, "infeasible": 1}
    assert mon["ratio_unusual"] == 2
    assert (
        out["compaction"]["compactions_total"] == 3
    )  # skipped attempts are not compactions
