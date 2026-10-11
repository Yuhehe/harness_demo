"""eval/report/summary_probe.py with a scripted summarizer (no network)."""

import json

from ctx_helpers import add_turn, new_state, reply
from fakes import ScriptedLLM
from summary_probe import PatchSandbox, load_messages, probe

PATCH = "diff --git a/pkg/ops.py b/pkg/ops.py\n--- a/pkg/ops.py\n+++ b/pkg/ops.py\n+x\n"


def trajectory(tmp_path, n_turns=10, with_steps=True, edit_at=3):
    s = new_state()
    for i in range(1, n_turns + 1):
        if i == edit_at:
            add_turn(
                s,
                i,
                tool="str_replace",
                path="pkg/ops.py",
                old_str="a",
                new_str="b",
                output="ok " + "x" * 3000,
            )
        else:
            add_turn(s, i, chars=3000)
    spans, step = [], 0
    for m in s.messages:
        step += m["role"] == "assistant"
        span = {"type": "message", "ts": 1.0, "run_id": "r", "instance_id": "i", **m}
        if with_steps:
            span["step"] = step
        spans.append(span)
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(json.dumps(x) for x in spans) + "\n" + json.dumps({"type": "run_end"})
    )
    return path


def test_load_messages_numbers_steps_when_the_trajectory_has_none(tmp_path):
    old = load_messages(trajectory(tmp_path, 3, with_steps=False))
    new = load_messages(trajectory(tmp_path, 3, with_steps=True))
    assert (
        [m["step"] for m in old] == [m["step"] for m in new] == [0, 0, 1, 1, 2, 2, 3, 3]
    )
    assert all("run_id" not in m and "ts" not in m and "type" not in m for m in old)


async def test_a_probe_compacts_and_scores_the_summary_by_the_files_it_names(tmp_path):
    llm = ScriptedLLM([reply("## Files modified\n- pkg/ops.py: fixed add")])
    out = await probe(load_messages(trajectory(tmp_path)), PATCH, llm, at_step=8)
    assert out["compacted"] and out["at_step"] == 8
    assert out["tokens_after"] < out["tokens_before"] and out["cost_usd"] > 0
    assert out["edited_before"] == ["pkg/ops.py"] and out["summary_file_recall"] == 1.0
    # the summarizer's own text, without the framing and without the generated block
    assert out["summary"] == "## Files modified\n- pkg/ops.py: fixed add"
    assert (
        "(generated from the sandbox" in out["state_block"]
        and "pkg/ops.py | +1 -0" in out["state_block"]
    )
    assert (
        llm.calls[0]["messages"][1]["content"].count("OUT") >= 3
    )  # it was shown the history


async def test_a_summary_that_does_not_name_the_edited_file_scores_zero(tmp_path):
    llm = ScriptedLLM([reply("## Tried so far\nlooked around")])
    out = await probe(load_messages(trajectory(tmp_path)), PATCH, llm, at_step=8)
    assert out["summary_file_recall"] == 0.0


async def test_edits_after_the_chosen_step_are_not_expected_in_the_summary(tmp_path):
    llm = ScriptedLLM([reply("## Tried so far\nlooked around")])
    out = await probe(
        load_messages(trajectory(tmp_path, edit_at=9)), PATCH, llm, at_step=8
    )
    assert out["edited_before"] == [] and out["summary_file_recall"] is None


async def test_a_history_that_fits_comfortably_does_not_compact(tmp_path):
    out = await probe(
        load_messages(trajectory(tmp_path, n_turns=1)),
        PATCH,
        ScriptedLLM([]),
        at_step=1,
    )
    assert out["compacted"] is False


async def test_the_probe_sandbox_serves_the_patch_and_nothing_else():
    sb = PatchSandbox(PATCH)
    assert await sb.git_diff() == PATCH
    await sb.write_file("/tmp/x", "y")
    assert await sb.read_file("/tmp/x") == "y"
