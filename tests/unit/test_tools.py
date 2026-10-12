import json

import pytest
from fakes import FakeSandbox

from harness.config import AgentConfig
from harness.sandbox.base import ExecResult
from harness.tools.base import OMITTED_NOTE, OutputLimiter, saved_path, validate_args
from harness.tools.bash import BashTool
from harness.tools.edit import VIEW_WINDOW, StrReplaceTool, ViewTool
from harness.tools.registry import Registry
from harness.tools.search import MAX_MATCHES, SearchTool
from harness.tools.submit import SubmitFlag, SubmitTool
from harness.tools.todo import TodoList, TodoTool


def lim(n=8000):
    return OutputLimiter(n)


# ---- output truncation ----------------------------------------------------------------
async def test_short_output_is_untouched():
    sb = FakeSandbox()
    text, truncated = await lim(100).apply("short", sb)
    assert (text, truncated) == ("short", False) and sb.files == {}


async def test_long_output_keeps_head_and_tail_and_is_saved_in_the_sandbox():
    sb = FakeSandbox()
    full = "A" * 60 + "MIDDLE" * 50 + "Z" * 60
    text, truncated = await lim(100).apply(full, sb)
    assert truncated and text.startswith("A" * 50) and text.endswith("Z" * 50)
    assert "/tmp/harness/out_1.txt" in text and f"{len(full)} chars" in text
    assert sb.files["/tmp/harness/out_1.txt"] == full
    assert len(text) < len(full)


async def test_the_note_the_limiter_writes_is_parsed_back_by_saved_path():
    # truncate.py finds already-saved outputs through saved_path: the two must agree
    sb = FakeSandbox()
    limiter = lim(100)
    first, _ = await limiter.apply("A" * 500, sb)
    second, _ = await limiter.apply("B" * 500, sb)
    assert saved_path(first) == "/tmp/harness/out_1.txt"
    assert saved_path(second) == "/tmp/harness/out_2.txt"
    assert saved_path("short, never cut") is None
    note = OMITTED_NOTE.format(omitted=1, total=2, path="/tmp/harness/out_3.txt")
    assert saved_path(f"x\n{note}\ny") == "/tmp/harness/out_3.txt"


def test_saved_path_needs_the_whole_note_not_just_a_path():
    assert saved_path("saved at /tmp/harness/out_1.txt") is None
    assert (
        saved_path("[... 5 chars omitted; saved at /tmp/harness/out_1.txt ...]") is None
    )
    assert (
        saved_path(OMITTED_NOTE.format(omitted=1, total=2, path="/etc/passwd")) is None
    )


async def test_saved_outputs_are_numbered_and_the_result_is_deterministic():
    sb1, sb2 = FakeSandbox(), FakeSandbox()
    a, b = lim(100), lim(100)
    out1 = [await a.apply("x" * 500, sb1), await a.apply("y" * 500, sb1)]
    out2 = [await b.apply("x" * 500, sb2), await b.apply("y" * 500, sb2)]
    assert out1 == out2
    assert set(sb1.files) == {"/tmp/harness/out_1.txt", "/tmp/harness/out_2.txt"}


# ---- bash ------------------------------------------------------------------------------
async def test_bash_reports_exit_code_and_passes_the_timeout():
    sb = FakeSandbox(handler=lambda c: ExecResult(2, "boom"))
    r = await BashTool(lim(), timeout_s=77).run({"command": "false"}, sb)
    assert r.output == "boom\n[exit code: 2]" and r.exit_code == 2
    assert sb.exec_calls == [("false", 77)]


async def test_bash_with_no_output_says_so():
    r = await BashTool(lim()).run({"command": "true"}, FakeSandbox())
    assert r.output == "(no output)\n[exit code: 0]"


async def test_bash_truncates_long_output():
    sb = FakeSandbox(handler=lambda c: "x" * 500)
    r = await BashTool(lim(100)).run({"command": "cat big"}, sb)
    assert (
        r.truncated
        and "/tmp/harness/out_1.txt" in r.output
        and r.output.endswith("[exit code: 0]")
    )


# ---- view ------------------------------------------------------------------------------
async def test_view_numbers_lines():
    sb = FakeSandbox({"/testbed/a.py": "one\ntwo\nthree\n"})
    r = await ViewTool(lim()).run({"path": "a.py"}, sb)
    assert r.output == "     1\tone\n     2\ttwo\n     3\tthree"


async def test_view_range_and_footer():
    sb = FakeSandbox({"/testbed/a.py": "\n".join(f"l{i}" for i in range(1, 11)) + "\n"})
    r = await ViewTool(lim()).run({"path": "a.py", "start_line": 4, "end_line": 6}, sb)
    assert r.output.splitlines()[0] == "     4\tl4" and r.output.endswith(
        "[showing lines 4-6 of 10]"
    )


async def test_view_is_capped_to_a_window_and_says_how_to_continue():
    sb = FakeSandbox({"/testbed/big.py": "\n".join(f"l{i}" for i in range(1, 1001))})
    r = await ViewTool(lim(100_000)).run({"path": "big.py"}, sb)
    assert f"[showing lines 1-{VIEW_WINDOW} of 1000]" in r.output
    assert "l401" not in r.output


@pytest.mark.parametrize(("start", "end"), [(0, 5), (5, 2), (50, 60)])
async def test_view_rejects_bad_ranges(start, end):
    sb = FakeSandbox({"/testbed/a.py": "a\nb\n"})
    r = await ViewTool(lim()).run(
        {"path": "a.py", "start_line": start, "end_line": end}, sb
    )
    assert r.output.startswith("Error:")


async def test_view_missing_file_and_directory():
    sb = FakeSandbox(handler=lambda c: "total 0\nfile.txt")
    sb.dirs.add("/testbed/pkg")
    assert (await ViewTool(lim()).run({"path": "nope"}, sb)).output.startswith(
        "Error: no such file"
    )
    r = await ViewTool(lim()).run({"path": "pkg"}, sb)
    assert "file.txt" in r.output and "ls -la" in sb.exec_calls[-1][0]


# ---- str_replace -----------------------------------------------------------------------
SRC = "def f():\n    return 1\n\n\ndef g():\n    return 1\n"


async def test_str_replace_edits_exactly_one_occurrence_and_shows_the_result():
    sb = FakeSandbox({"/testbed/m.py": SRC})
    r = await StrReplaceTool().run(
        {
            "path": "m.py",
            "old_str": "def f():\n    return 1",
            "new_str": "def f():\n    return 2",
        },
        sb,
    )
    assert sb.files["/testbed/m.py"] == SRC.replace(
        "def f():\n    return 1", "def f():\n    return 2"
    )
    assert r.output.startswith("Edited m.py: replaced 1 occurrence (now lines 1-2)")
    assert "     2\t    return 2" in r.output


async def test_str_replace_zero_matches_changes_nothing_and_points_to_the_nearby_text():
    sb = FakeSandbox({"/testbed/m.py": SRC})
    r = await StrReplaceTool().run(
        {"path": "m.py", "old_str": "def f():\n  return 1", "new_str": "x"}, sb
    )
    assert sb.files["/testbed/m.py"] == SRC
    assert (
        "not found" in r.output
        and "around line 1" in r.output
        and "     1\tdef f():" in r.output
    )


async def test_str_replace_zero_matches_without_any_similar_line():
    sb = FakeSandbox({"/testbed/m.py": SRC})
    r = await StrReplaceTool().run(
        {"path": "m.py", "old_str": "class Nope:", "new_str": "x"}, sb
    )
    assert (
        "No line of the file contains" in r.output and sb.files["/testbed/m.py"] == SRC
    )


async def test_str_replace_several_matches_changes_nothing_and_lists_them():
    sb = FakeSandbox({"/testbed/m.py": SRC})
    r = await StrReplaceTool().run(
        {"path": "m.py", "old_str": "return 1", "new_str": "return 9"}, sb
    )
    assert sb.files["/testbed/m.py"] == SRC
    assert (
        "appears 2 times" in r.output
        and "--- line 2 ---" in r.output
        and "--- line 6 ---" in r.output
    )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [("", "x", "must not be empty"), ("a", "a", "identical")],
)
async def test_str_replace_rejects_degenerate_calls(old, new, message):
    sb = FakeSandbox({"/testbed/m.py": "a\n"})
    r = await StrReplaceTool().run({"path": "m.py", "old_str": old, "new_str": new}, sb)
    assert message in r.output and sb.files["/testbed/m.py"] == "a\n"


async def test_str_replace_missing_file_and_directory():
    sb = FakeSandbox()
    sb.dirs.add("/testbed/pkg")
    assert (
        "no such file"
        in (
            await StrReplaceTool().run(
                {"path": "x", "old_str": "a", "new_str": "b"}, sb
            )
        ).output
    )
    assert (
        "is a directory"
        in (
            await StrReplaceTool().run(
                {"path": "pkg", "old_str": "a", "new_str": "b"}, sb
            )
        ).output
    )


async def test_str_replace_can_delete_text_and_keeps_the_trailing_newline():
    sb = FakeSandbox({"/testbed/m.py": "keep\nremove me\nkeep2\n"})
    await StrReplaceTool().run(
        {"path": "m.py", "old_str": "remove me\n", "new_str": ""}, sb
    )
    assert sb.files["/testbed/m.py"] == "keep\nkeep2\n"


# ---- search ----------------------------------------------------------------------------
async def test_search_uses_rg_when_installed_else_grep_and_quotes_the_pattern():
    def handler(cmd):
        return ExecResult(0, "/usr/bin/rg" if cmd == "command -v rg" else "a.py:1:x")

    with_rg = FakeSandbox(handler=handler)
    await SearchTool(lim()).run(
        {"pattern": "it's a (test)", "include": "*.py"}, with_rg
    )
    cmd = with_rg.exec_calls[-1][0]
    assert cmd.startswith("rg -n --no-heading -e ")
    assert "'it'\"'\"'s a (test)'" in cmd and "-g '*.py'" in cmd

    no_rg = FakeSandbox(
        handler=lambda c: ExecResult(1, "") if c == "command -v rg" else "a.py:1:x"
    )
    await SearchTool(lim()).run({"pattern": "foo", "path": "src"}, no_rg)
    cmd = no_rg.exec_calls[-1][0]
    assert cmd.startswith("grep -rnIE --exclude-dir=.git -e foo -- src")
    assert cmd.endswith(f"head -n {MAX_MATCHES}")


async def test_search_no_matches_and_result_cap_note():
    none = FakeSandbox(handler=lambda c: ExecResult(1, ""))
    assert (
        await SearchTool(lim()).run({"pattern": "zzz"}, none)
    ).output == "No matches found."
    many = "\n".join(f"f.py:{i}:x" for i in range(MAX_MATCHES)) + "\n"
    cap = FakeSandbox(
        handler=lambda c: ExecResult(1, "") if c == "command -v rg" else many
    )
    r = await SearchTool(lim(100_000)).run({"pattern": "x"}, cap)
    assert f"[showing the first {MAX_MATCHES} matches" in r.output


# ---- todo and submit -------------------------------------------------------------------
async def test_todo_replaces_the_list_and_renders_it():
    todo = TodoList()
    r = await TodoTool(todo).run(
        {
            "items": [
                {"text": "read", "status": "done"},
                {"text": "fix", "status": "in_progress"},
            ]
        },
        FakeSandbox(),
    )
    assert r.output == "1. [x] read\n2. [~] fix" and len(todo.items) == 2
    await TodoTool(todo).run({"items": []}, FakeSandbox())
    assert todo.items == [] and todo.render() == "(the todo list is empty)"


@pytest.mark.parametrize(
    "items",
    [
        ["just a string"],
        [{"text": "a"}],
        [{"text": "a", "status": "later"}],
        [{"text": 1, "status": "done"}],
    ],
)
async def test_todo_rejects_malformed_items_as_format_errors(items):
    todo = TodoList()
    r = await TodoTool(todo).run({"items": items}, FakeSandbox())
    assert r.format_error and r.output.startswith("Error:") and todo.items == []


async def test_submit_only_sets_the_flag():
    flag = SubmitFlag()
    r = await SubmitTool(flag).run({}, FakeSandbox())
    assert flag.submitted and r.output.startswith("Submitted")


# ---- argument validation ---------------------------------------------------------------
SCHEMA = StrReplaceTool.parameters


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ([], "must be a JSON object"),
        ({"path": "a", "old_str": "b"}, "missing required"),
        (
            {"path": "a", "old_str": "b", "new_str": "c", "extra": 1},
            "unknown parameter",
        ),
        ({"path": 1, "old_str": "b", "new_str": "c"}, "must be of type string"),
    ],
)
def test_validate_args_messages(args, fragment):
    assert fragment in validate_args(SCHEMA, args)


def test_validate_args_accepts_good_input_and_rejects_bool_as_integer():
    assert validate_args(SCHEMA, {"path": "a", "old_str": "b", "new_str": "c"}) is None
    view = ViewTool.parameters
    assert validate_args(view, {"path": "a", "start_line": 3}) is None
    assert "integer" in validate_args(view, {"path": "a", "start_line": True})


# ---- registry --------------------------------------------------------------------------
def registry(**cfg):
    c = AgentConfig(**cfg)
    return Registry(c, lim(), TodoList(), SubmitFlag())


def test_action_spaces_and_planning_select_the_tools():
    assert registry(action_space="bash_only").names == ["bash", "submit"]
    assert registry(action_space="tools").names == [
        "bash",
        "search",
        "str_replace",
        "submit",
        "todo",
        "view",
    ]
    assert registry(action_space="tools", planning="off").names == [
        "bash",
        "search",
        "str_replace",
        "submit",
        "view",
    ]
    assert registry(action_space="bash_only", planning="on").names == ["bash", "submit"]


def test_schemas_are_sorted_and_deterministic():
    names = [s["function"]["name"] for s in registry().schemas()]
    assert names == sorted(names)
    assert json.dumps(registry().schemas(), sort_keys=True) == json.dumps(
        registry().schemas(), sort_keys=True
    )
    assert all(
        s["type"] == "function" and s["function"]["parameters"]["type"] == "object"
        for s in registry().schemas()
    )


async def test_registry_dispatches_valid_calls():
    sb = FakeSandbox(handler=lambda c: "hi")
    r = await registry().run("bash", '{"command": "echo hi"}', sb)
    assert r.output == "hi\n[exit code: 0]" and not r.format_error


@pytest.mark.parametrize(
    ("name", "arguments", "fragment"),
    [
        ("nope", "{}", "unknown tool 'nope'. Available tools: bash"),
        ("bash", "{not json", "not valid JSON"),
        ("bash", "[1]", "must be a JSON object"),
        ("bash", "{}", "missing required"),
        ("bash", '{"command": 5}', "must be of type string"),
        ("view", '{"path": "a", "start_line": "1"}', "must be of type integer"),
    ],
)
async def test_registry_turns_malformed_calls_into_format_errors(
    name, arguments, fragment
):
    r = await registry().run(name, arguments, FakeSandbox())
    assert r.format_error and fragment in r.output


async def test_registry_allows_empty_arguments_for_tools_without_parameters():
    reg = registry()
    r = await reg.run("submit", "", FakeSandbox())
    assert not r.format_error and r.output.startswith("Submitted")


async def test_a_tool_that_runs_and_fails_is_not_a_format_error():
    sb = FakeSandbox()
    r = await registry().run(
        "str_replace", '{"path": "x", "old_str": "a", "new_str": "b"}', sb
    )
    assert r.output.startswith("Error: no such file") and not r.format_error


# ---- tool schemas ------------------------------------------------------------------------
def _all_tool_sets():
    import itertools

    for action, planning in itertools.product(("tools", "bash_only"), ("on", "off")):
        cfg = AgentConfig(action_space=action, planning=planning)
        yield Registry(cfg, lim(), TodoList(), SubmitFlag()).schemas()


def _arrays_without_items(schema, path="parameters"):
    """Paths of every `type: array` in a JSON schema that does not say what its elements are."""
    found = []
    if isinstance(schema, dict):
        if schema.get("type") == "array" and "items" not in schema:
            found.append(path)
        for key, value in schema.items():
            found += _arrays_without_items(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            found += _arrays_without_items(value, f"{path}[{i}]")
    return found


def test_every_array_parameter_declares_what_its_elements_are():
    # LiteLLM's token counter (and OpenAI's strict validation) reads `items`; the todo tool's
    # parameter named `items` once had none and made token_counter(tools=...) raise KeyError
    for tools in _all_tool_sets():
        for tool in tools:
            missing = _arrays_without_items(tool["function"]["parameters"])
            assert not missing, f"{tool['function']['name']}: {missing}"


def test_litellm_can_count_every_tool_set_we_send():
    import litellm

    plain = litellm.token_counter(
        model="deepseek-v4-pro", messages=[{"role": "user", "content": "x"}]
    )
    for tools in _all_tool_sets():
        counted = litellm.token_counter(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "x"}],
            tools=tools,
        )
        assert counted > plain + 100  # the definitions are counted, not skipped


def test_the_todo_schema_matches_what_the_tool_accepts():
    from harness.tools.todo import STATUSES

    element = TodoTool(TodoList()).parameters["properties"]["items"]["items"]
    assert element["required"] == ["text", "status"]
    assert element["properties"]["status"]["enum"] == STATUSES
    assert element["additionalProperties"] is False
