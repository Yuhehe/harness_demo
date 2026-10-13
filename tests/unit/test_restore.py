"""The state block of a summary (context/restore.py) and the formats it shares with the
limiter and the truncate strategy."""

import json

import pytest
from ctx_helpers import MODEL, TOOLS, add_turn, manager, new_state, reply, turns
from fakes import FakeSandbox, ScriptedLLM

from harness.config import ContextConfig
from harness.context.restore import (
    STATE_MARKER,
    IndexEntry,
    RestoreIndex,
    build_block,
    diff_stat,
)
from harness.context.strategies.summarize import (
    SUMMARY_PREFIX,
    is_summary,
    summary_message,
    summary_text_of,
)
from harness.context.strategies.truncate import cleared_output, cleared_stub
from harness.context.tokens import TokenEstimator
from harness.llm.types import LLMError, tool_message
from harness.tools.base import OMITTED_NOTE, OutputLimiter, saved_output
from harness.tracing.tracer import MemoryTracer

DIFF = """\
diff --git a/calc/ops.py b/calc/ops.py
index 1111111..2222222 100644
--- a/calc/ops.py
+++ b/calc/ops.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a - b
+    return a + b
 
diff --git a/calc/new.py b/calc/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/calc/new.py
@@ -0,0 +1,2 @@
+X = 1
+Y = 2
"""


# ---- the shared formats --------------------------------------------------------------------
def test_the_stub_and_its_parser_agree_and_only_a_whole_stub_counts():
    stub = cleared_stub(812, "/tmp/harness/ctx_3.txt")
    assert stub == "[output cleared: 812 chars, saved at /tmp/harness/ctx_3.txt]"
    assert cleared_output(stub) == ("/tmp/harness/ctx_3.txt", 812)
    assert cleared_output(cleared_stub(5, "/tmp/harness/out_2.txt")) == (
        "/tmp/harness/out_2.txt",
        5,
    )
    assert cleared_output("see " + stub) is None  # a stub quoted inside other text
    assert cleared_output(cleared_stub(5, "/etc/passwd")) is None


async def test_the_limiter_note_gives_the_path_and_the_full_length():
    text, cut = await OutputLimiter(100).apply("A" * 500, FakeSandbox())
    assert cut and saved_output(text) == ("/tmp/harness/out_1.txt", 500)
    note = OMITTED_NOTE.format(omitted=1, total=77, path="/tmp/harness/out_9.txt")
    assert saved_output(note) == ("/tmp/harness/out_9.txt", 77)
    assert saved_output("saved at /tmp/harness/out_1.txt") is None


# ---- the patch ----------------------------------------------------------------------------------
def test_diff_stat_counts_files_and_lines():
    out = diff_stat(DIFF)
    assert " calc/ops.py | +1 -1" in out and " calc/new.py | +2 -0" in out
    assert out.endswith("2 file(s) changed, +3 -1")


def test_diff_stat_with_nothing_or_something_else():
    assert diff_stat("") == "no changes yet"
    assert diff_stat("DIFF") == "(the patch is not in git format)"


def test_diff_stat_lists_at_most_forty_files():
    many = "".join(f"diff --git a/f{i}.py b/f{i}.py\n+x\n" for i in range(55))
    out = diff_stat(many)
    assert "(+15 more files)" in out and "55 file(s) changed" in out


# ---- the index ------------------------------------------------------------------------------------
def _call(i, name, **args):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"c{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def test_the_index_reads_limiter_notes_from_the_original_and_stubs_from_the_view():
    note = OMITTED_NOTE.format(omitted=900, total=1200, path="/tmp/harness/out_1.txt")
    original = [
        _call(1, "bash", command="pytest   tests/test_ops.py -x"),
        tool_message("c1", "head\n" + note + "\ntail"),
        _call(2, "view", path="calc/ops.py"),
        tool_message("c2", "x" * 900),
    ]
    shown = [
        original[0],
        original[1],
        original[2],
        tool_message("c2", cleared_stub(900, "/tmp/harness/ctx_1.txt")),
    ]
    idx = RestoreIndex().with_range(original, shown)
    assert [(e.path, e.chars, e.label) for e in idx.entries.values()] == [
        ("/tmp/harness/out_1.txt", 1200, "bash: pytest tests/test_ops.py -x"),
        ("/tmp/harness/ctx_1.txt", 900, "view calc/ops.py"),
    ]
    assert list(idx.viewed) == ["calc/ops.py"]


def test_the_index_accumulates_dedupes_and_does_not_change_the_old_one():
    first = RestoreIndex().with_range(
        [_call(1, "view", path="a.py"), tool_message("c1", "x")],
        [
            _call(1, "view", path="a.py"),
            tool_message("c1", cleared_stub(5, "/tmp/harness/ctx_1.txt")),
        ],
    )
    second = first.with_range(
        [
            _call(2, "view", path="b.py"),
            tool_message("c2", "x"),
            _call(3, "str_replace", path="a.py", old_str="1", new_str="2"),
            tool_message("c3", "ok"),
        ],
        [
            _call(2, "view", path="b.py"),
            tool_message("c2", cleared_stub(7, "/tmp/harness/ctx_2.txt")),
            _call(3, "str_replace", path="a.py", old_str="1", new_str="2"),
            tool_message("c3", "ok"),
        ],
    )
    assert list(first.entries) == ["/tmp/harness/ctx_1.txt"] and list(first.viewed) == [
        "a.py"
    ]
    assert list(second.entries) == ["/tmp/harness/ctx_1.txt", "/tmp/harness/ctx_2.txt"]
    assert list(second.viewed) == [
        "b.py",
        "a.py",
    ]  # a.py was touched again: it moves to the end


def test_the_index_ignores_malformed_arguments_and_unknown_calls():
    bad = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "view", "arguments": "{not json"},
            }
        ],
    }
    idx = RestoreIndex().with_range(
        [bad, tool_message("c1", "x"), tool_message("zz", "y")],
        [bad, tool_message("c1", "x"), tool_message("zz", "y")],
    )
    assert not idx.entries and not idx.viewed


# ---- the block -------------------------------------------------------------------------------------
def cfg(**kw):
    kw.setdefault("budget_tokens", 20000)
    kw.setdefault("high_water", 0.5)
    kw.setdefault("low_water", 0.3)
    return ContextConfig(strategy="summarize", **kw)


def index_of(n_entries=3, n_files=3):
    idx = RestoreIndex()
    for i in range(n_entries):
        idx.entries[f"/tmp/harness/out_{i}.txt"] = IndexEntry(
            f"/tmp/harness/out_{i}.txt", 100 + i, f"bash: cmd {i}"
        )
    for i in range(n_files):
        idx.viewed[f"pkg/mod{i}.py"] = None
    return idx


async def block(
    c=None,
    *,
    diff=DIFF,
    todo="1. [~] fix add",
    index=None,
    minimal=False,
    step=7,
    sandbox=None,
):
    return await build_block(
        step=step,
        sandbox=sandbox or FakeSandbox(diff=diff),
        todo=todo,
        index=index or index_of(),
        cfg=c or cfg(),
        estimator=TokenEstimator(MODEL, TOOLS),
        minimal=minimal,
    )


async def test_the_full_block_has_every_section_and_says_when_it_was_made():
    b = await block()
    assert b.text.startswith("## State as of step 7")
    assert "generated from the sandbox" in b.text and "may be out of date" in b.text
    for heading in (
        "### Changes so far (git diff)",
        "### Plan (todo)",
        "### Saved outputs",
        "### Files viewed",
    ):
        assert heading in b.text
    assert (
        "calc/ops.py | +1 -1" in b.text and "-    return a - b" in b.text
    )  # stat and patch text
    assert "- /tmp/harness/out_2.txt (102 chars) bash: cmd 2" in b.text
    assert "- pkg/mod1.py" in b.text and "1. [~] fix add" in b.text
    assert b.tokens <= cfg().restore_tokens_cap() and b.parts["shape"] == 0


async def test_the_marker_separates_the_block_from_the_summary_text():
    b = await block()
    msg = summary_message("## Tried so far\nlooked", b.text)
    assert is_summary(msg) and STATE_MARKER in msg["content"]
    assert summary_text_of(msg).endswith(
        "looked"
    ) and "State as of step" not in summary_text_of(msg)
    assert (
        summary_text_of(summary_message("only text"))
        == summary_message("only text")["content"]
    )


async def test_the_index_shows_the_newest_entries_and_folds_the_rest():
    b = await block(index=index_of(n_entries=50), c=cfg(summary_index_entries=20))
    lines = [x for x in b.text.splitlines() if x.startswith("- /tmp/harness/out_")]
    assert len(lines) == 20 and lines[-1].startswith("- /tmp/harness/out_49.txt")
    assert "(+30 older saved outputs: run `ls /tmp/harness`)" in b.text
    assert all(len(x) <= 120 for x in lines)
    assert b.parts["index_lines"] == 20 and b.parts["index_older"] == 30


async def test_long_labels_are_cut_to_one_short_line():
    idx = RestoreIndex()
    idx.entries["/tmp/harness/out_1.txt"] = IndexEntry(
        "/tmp/harness/out_1.txt", 9, "bash: " + "x" * 400
    )
    b = await block(index=idx)
    (line,) = [x for x in b.text.splitlines() if x.startswith("- /tmp/harness/out_1")]
    assert len(line) == 120


async def test_the_patch_text_is_capped_and_says_how_to_get_the_rest():
    big = DIFF + "".join(f"+line {i}\n" for i in range(3000))
    b = await block(
        diff=big,
        c=cfg(
            summary_diff_chars=500,
            summary_restore_tokens=3000,
            summary_restore_ratio=0.2,
        ),
    )
    assert "more chars; run `git diff` for the rest" in b.text
    assert b.parts["diff_chars"] < 700


# ---- when the block does not fit its cap -------------------------------------------------------------
def tight(tokens, **kw):
    return cfg(summary_restore_tokens=tokens, summary_restore_ratio=1.0, **kw)


# (below ~90 tokens not even the header and the stat fit; real caps are never that small:
# the smallest one used is summary_restore_min_tokens = 200)
@pytest.mark.parametrize("cap", [400, 250, 150, 90])
async def test_a_block_that_does_not_fit_is_cut_down_in_a_fixed_order(cap):
    b = await block(tight(cap), index=index_of(30, 30), diff=DIFF + "+x\n" * 400)
    assert (
        "### Changes so far (git diff)" in b.text and "calc/ops.py | +1 -1" in b.text
    )  # the stat stays
    assert b.tokens <= cap or b.parts.get("clipped")
    has = {
        k: k in b.text
        for k in (
            "### Files viewed",
            "### Saved outputs",
            "-    return a - b",
            "### Plan",
        )
    }
    # a later part is never present while an earlier-dropped one is gone
    order = ["### Files viewed", "### Saved outputs", "-    return a - b", "### Plan"]
    present = [has[k] for k in order]
    assert present == sorted(
        present
    )  # False... then True...: paths go first, the plan last


async def test_dropping_order_is_paths_then_index_then_patch_text_then_plan():
    shapes = []
    for cap in range(3000, 40, -100):
        b = await block(tight(cap), index=index_of(30, 30), diff=DIFF + "+x\n" * 400)
        shapes.append(b.parts.get("shape", 99))
    assert shapes == sorted(shapes) and shapes[0] == 0 and shapes[-1] >= 5


async def test_minimal_mode_keeps_the_stat_and_at_most_the_plan():
    b = await block(minimal=True, index=index_of(30, 30))
    assert "calc/ops.py | +1 -1" in b.text
    assert (
        "### Saved outputs" not in b.text
        and "### Files viewed" not in b.text
        and "return a - b" not in b.text
    )
    assert b.parts["minimal"] and b.tokens <= 200


async def test_minimal_mode_leaves_the_index_out_even_when_it_would_fit():
    # one saved output and one file: the full block is small enough for the minimal cap, so
    # only the mode (not the size) can be the reason the index is absent
    full = await block(index=index_of(1, 1), diff="")
    small = await block(index=index_of(1, 1), diff="", minimal=True)
    assert "### Saved outputs" in full.text and full.tokens <= 200
    assert (
        "### Saved outputs" not in small.text and "### Files viewed" not in small.text
    )
    assert "no changes yet" in small.text


async def test_a_sandbox_that_cannot_diff_still_gives_a_block():
    class Broken(FakeSandbox):
        async def git_diff(self):
            raise RuntimeError("container gone")

    b = await block(sandbox=Broken())
    assert (
        "the patch is unavailable: RuntimeError" in b.text
        and "### Saved outputs" in b.text
    )


# ---- inside a compaction ---------------------------------------------------------------------------------
KW = {
    "budget_tokens": 20000,
    "high_water": 0.5,
    "low_water": 0.3,
    "keep_recent_ratio": 0.01,
}


def with_todo(s):
    s.todo.items = [{"text": "fix add", "status": "in_progress"}]
    return s


async def test_the_applied_summary_carries_the_block_and_the_span_keeps_them_apart():
    llm, tracer = (
        ScriptedLLM([reply("## Tried so far\nlooked at ops.py")]),
        MemoryTracer(),
    )
    m = manager(
        "summarize", llm=llm, tracer=tracer, sandbox=FakeSandbox(diff=DIFF), **KW
    )
    s = with_todo(turns(30))
    view = await m.build(s)

    summary = view[2]
    assert is_summary(summary) and "looked at ops.py" in summary["content"]
    assert (
        f"## State as of step {s.steps}" in summary["content"]
        and "calc/ops.py | +1 -1" in summary["content"]
    )
    assert "1. [~] fix add" in summary["content"]
    (span,) = tracer.of("compaction")
    assert (
        span["summary"] == "## Tried so far\nlooked at ops.py"
    )  # the summarizer's text only
    assert "State as of" not in span["summary"] and "calc/ops.py" not in span["summary"]
    assert span["restored"]["tokens"] <= span["restored"]["cap_tokens"]
    assert span["restored"]["stat_chars"] > 0 and span["restored_error"] is None
    assert m._view[2] is m._original[2]  # the same summary on both sides


async def test_the_next_summary_is_not_given_the_old_state_block():
    llm = ScriptedLLM([reply("FIRST"), reply("SECOND")])
    m = manager("summarize", llm=llm, sandbox=FakeSandbox(diff=DIFF), **KW)
    s = turns(30)
    await m.build(s)
    for i in range(31, 70):
        add_turn(s, i)
    await m.build(s)
    assert len(llm.calls) == 2
    second_input = llm.calls[1]["messages"][1]["content"]
    assert "PREVIOUS SUMMARY" in second_input and "FIRST" in second_input
    assert "State as of step" not in second_input and "calc/ops.py" not in second_input


async def test_a_summary_that_was_not_applied_does_not_change_the_index():
    llm = ScriptedLLM(
        [LLMError("server_error", "boom")]
    )  # the summary fails: truncate takes over
    m = manager("summarize", llm=llm, sandbox=FakeSandbox(diff=DIFF), **KW)
    s = new_state()
    for i in range(1, 31):  # file views: a wrongly committed index would not be empty
        add_turn(s, i, tool="view", path=f"pkg/mod{i}.py")
    await m.build(s)
    assert len(llm.calls) == 1  # the summary was attempted, and failed
    assert not m._index.entries and not m._index.viewed


async def test_the_index_follows_the_applied_summary_across_compactions():
    llm = ScriptedLLM([reply("one"), reply("two")])
    m = manager("summarize", llm=llm, sandbox=FakeSandbox(diff=DIFF), **KW)
    s = new_state()
    for i in range(1, 31):
        add_turn(
            s, i, output=f"OUT{i} " + "word " * 800
        )  # short enough not to be cut by a limiter
    await m.build(s)
    first = set(m._index.viewed) | set(m._index.entries)
    for i in range(31, 70):
        add_turn(s, i, output=f"OUT{i} " + "word " * 800)
    await m.build(s)
    assert len(llm.calls) == 2
    assert first <= set(m._index.viewed) | set(
        m._index.entries
    )  # nothing from the first one is lost


async def test_in_minimal_mode_the_summary_carries_only_the_short_block():
    # a tail of two large turns leaves no room for a full summary and a full block
    llm, tracer = ScriptedLLM([reply()]), MemoryTracer()
    m = manager(
        "summarize", llm=llm, tracer=tracer, sandbox=FakeSandbox(diff=DIFF),
        budget_tokens=8000, high_water=0.6, low_water=0.3, keep_recent_ratio=0.01,
    )  # fmt: skip
    view = await m.build(turns(12))
    (span,) = tracer.of("compaction")
    assert (
        span["plan_level"] in ("minimal", "unreachable") and span["restored"]["minimal"]
    )
    assert (
        "### Saved outputs" not in view[2]["content"]
        and "calc/ops.py | +1 -1" in view[2]["content"]
    )
    assert "at most 200 words" in llm.calls[0]["messages"][1]["content"]


async def test_a_block_that_fails_to_build_does_not_stop_the_summary():
    class Broken(FakeSandbox):
        async def git_diff(self):
            raise RuntimeError("gone")

    llm, tracer = ScriptedLLM([reply("text")]), MemoryTracer()
    m = manager("summarize", llm=llm, tracer=tracer, sandbox=Broken(), **KW)
    view = await m.build(turns(30))
    assert is_summary(view[2]) and "the patch is unavailable" in view[2]["content"]


def test_the_prefix_is_still_the_marker_of_a_summary():
    assert summary_message("x")["content"].startswith(SUMMARY_PREFIX)
