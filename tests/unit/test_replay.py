import json

import pytest
from fakes import ScriptedLLM

from harness.config import LLMConfig
from harness.llm.replay import (
    RecordingClient,
    ReplayClient,
    ReplayMiss,
    fingerprint,
    normalize_text,
)
from harness.llm.types import LLMResponse, ToolCall, Usage

CFG = LLMConfig(model="deepseek/deepseek-flash", thinking="enabled")
TOOLS = [
    {"type": "function", "function": {"name": "b", "parameters": {}}},
    {"type": "function", "function": {"name": "a", "parameters": {}}},
]


def resp(text="ok", calls=()):
    return LLMResponse(
        content=text,
        reasoning_content=None,
        tool_calls=tuple(calls),
        finish_reason="stop",
        usage=Usage(10, 5, 0, 0),
        model="deepseek-flash",
    )


def convo(tool_output="Ran 1 test in 0.003s\n\nOK"):
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": tool_output},
    ]


def key(messages, cfg=CFG, tools=TOOLS):
    return fingerprint(cfg, messages, tools)[0]


def digests(messages, cfg=CFG, tools=TOOLS):
    return fingerprint(cfg, messages, tools)[1]


# ---- normalization -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Ran 1 test in 0.003s", "Ran 1 test in 1.250s"),
        ("1 passed in 0.01s", "1 passed in 12.5s"),
        ("real 0m0.003s", "real 0m1.200s"),
        ("at 0x7f3a2c1d4e50", "at 0x7e09e9e40bf0"),
        ("/tmp/pytest-of-yuhe/pytest-3/x/a.py", "/tmp/pytest-of-root/pytest-9/y/a.py"),
        ("2026-10-06 12:00:01", "2026-10-07 09:15:59.123"),
        (
            "drwxr-xr-x 2 root root 4096 Oct  6 07:11 pkg",
            "drwxr-xr-x 2 root root 4096 Nov 12 23:59 pkg",
        ),
        (
            "-rw-r--r-- 1 root root 12 Oct  6  2025 a.py",
            "-rw-r--r-- 1 root root 12 Jan  1  2000 a.py",
        ),
        ("Tue Oct  6 07:11:03 UTC 2026", "Wed Nov 11 12:00:59 UTC 2027"),
    ],
)
def test_volatile_parts_do_not_change_the_key(a, b):
    assert normalize_text(a) == normalize_text(b)
    assert digests(convo(a)) == digests(convo(b))


def test_everything_else_in_a_tool_output_still_matters_to_the_digest():
    assert digests(convo("OK")) != digests(convo("FAILED (failures=1)"))
    assert digests(convo("2 passed")) != digests(convo("3 passed"))


def test_the_key_ignores_tool_output_content_because_its_order_is_host_dependent():
    # `find`/`ls` list files in an order that depends on the host file system
    assert key(convo("a.py\nb.py")) == key(convo("b.py\na.py"))
    assert key(convo("x")) != key(
        convo(
            "x",
        )[:3]
    )  # but the conversation path still counts


def test_our_own_stable_paths_are_not_normalized():
    assert (
        normalize_text("saved at /tmp/harness/out_3.txt")
        == "saved at /tmp/harness/out_3.txt"
    )


def test_only_tool_messages_are_normalized():
    a, b = convo(), convo()
    a[1]["content"] = "run in 5s"
    b[1]["content"] = "run in 9s"
    assert key(a) != key(b) and digests(a) != digests(b)


def test_key_depends_on_model_params_and_messages_but_not_tool_order():
    base = key(convo())
    assert base == key(convo(), tools=list(reversed(TOOLS)))
    assert base != key(convo(), cfg=LLMConfig(model="deepseek/deepseek-v4-pro"))
    assert base != key(convo(), cfg=LLMConfig(model=CFG.model, thinking="disabled"))
    assert base != key(convo(), cfg=LLMConfig(model=CFG.model, max_tokens=123))
    changed = convo()
    changed[2] = {"role": "assistant", "content": "different"}
    assert base != key(changed)


# ---- record and replay -----------------------------------------------------------------
async def test_record_then_replay_returns_the_same_responses(tmp_path):
    path = tmp_path / "r.jsonl"
    calls = (ToolCall("c1", "a", '{"x": 1}'),)
    inner = ScriptedLLM([resp("first", calls), resp("second")])
    rec = RecordingClient(inner, CFG, path)
    m1 = convo()[:2]
    m2 = convo()
    r1 = await rec.complete(m1, TOOLS)
    r2 = await rec.complete(m2, TOOLS)
    assert [
        json.loads(x)["response"]["content"] for x in path.read_text().splitlines()
    ] == [
        "first",
        "second",
    ]
    assert rec.stats is inner.stats

    replay = ReplayClient(CFG, path)
    # a second run whose tool output differs only in its timing still replays, with no report
    assert await replay.complete(m1, TOOLS) == r1
    assert await replay.complete(convo("Ran 1 test in 9.9s\n\nOK"), TOOLS) == r2
    assert replay.stats.calls == 2 and replay.mismatches == []


async def test_repeated_identical_requests_replay_in_recorded_order(tmp_path):
    path = tmp_path / "r.jsonl"
    rec = RecordingClient(ScriptedLLM([resp("one"), resp("two")]), CFG, path)
    await rec.complete(convo()[:2], TOOLS)
    await rec.complete(convo()[:2], TOOLS)
    replay = ReplayClient(CFG, path)
    assert (await replay.complete(convo()[:2], TOOLS)).content == "one"
    assert (await replay.complete(convo()[:2], TOOLS)).content == "two"
    with pytest.raises(ReplayMiss, match="already used"):
        await replay.complete(convo()[:2], TOOLS)


async def test_a_miss_is_an_error_that_says_where_the_request_diverges(tmp_path):
    path = tmp_path / "r.jsonl"
    await RecordingClient(ScriptedLLM([resp()]), CFG, path).complete(convo(), TOOLS)
    replay = ReplayClient(CFG, path)
    changed = convo()
    changed[2] = {
        "role": "assistant",
        "content": "something else",
        "tool_calls": [{"id": "c1"}],
    }
    with pytest.raises(ReplayMiss) as ei:
        await replay.complete(changed, TOOLS)
    text = str(ei.value)
    assert "message 2 differs" in text and "something else" in text


async def test_a_different_tool_output_still_replays_but_is_reported(tmp_path):
    path = tmp_path / "r.jsonl"
    await RecordingClient(ScriptedLLM([resp("x")]), CFG, path).complete(
        convo("OK"), TOOLS
    )
    replay = ReplayClient(CFG, path)
    out = await replay.complete(convo("FAILED (failures=1)"), TOOLS)
    assert out.content == "x"
    assert [m["index"] for m in replay.mismatches] == [3]
    assert (
        "FAILED" in replay.mismatches[0]["now"]
        and "OK" in replay.mismatches[0]["recorded"]
    )


async def test_strict_mode_turns_a_tool_output_difference_into_an_error(tmp_path):
    path = tmp_path / "r.jsonl"
    await RecordingClient(ScriptedLLM([resp("x")]), CFG, path).complete(
        convo("OK"), TOOLS
    )
    strict = ReplayClient(CFG, path, strict_tool_outputs=True)
    with pytest.raises(ReplayMiss, match="message 3 differs from the recording"):
        await strict.complete(convo("FAILED"), TOOLS)
    # a timing-only difference is not a difference
    ok = ReplayClient(CFG, path, strict_tool_outputs=True)
    await ok.complete(convo("OK"), TOOLS)


async def test_a_miss_caused_by_other_parameters_says_so(tmp_path):
    path = tmp_path / "r.jsonl"
    await RecordingClient(ScriptedLLM([resp()]), CFG, path).complete(convo(), TOOLS)
    other = ReplayClient(LLMConfig(model=CFG.model, thinking="disabled"), path)
    with pytest.raises(
        ReplayMiss, match="thinking, reasoning_effort, max_tokens or tools"
    ):
        await other.complete(convo(), TOOLS)


async def test_an_empty_recording_is_reported(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text("")
    with pytest.raises(ReplayMiss, match="empty"):
        await ReplayClient(CFG, path).complete(convo(), TOOLS)
