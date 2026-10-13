"""B0: the mini-swe-agent command is built from the harness config, what mini-swe-agent then
sends is checked at the server, and its output is converted for the rest of the pipeline."""

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_b0 as b0
from aggregate import aggregate, load_run
from fakes import FakeLLMServer, Step, ok_body, tool_call
from minisweagent.config import _key_value_spec_to_nested_dict
from minisweagent.models.litellm_model import LitellmModel

from harness.config import AgentConfig, LLMConfig, load_config
from harness.llm import cost
from harness.llm.types import Usage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "b0"
MAIN = load_config("configs/default.yaml")


def merged(cfg):
    """The settings mini-swe-agent ends up with: its own parser, merged the way it merges."""
    out: dict = {}

    def merge(a, b):
        for k, v in b.items():
            a[k] = (
                merge(a.get(k, {}), v)
                if isinstance(v, dict) and isinstance(a.get(k), dict)
                else v
            )
        return a

    for spec in b0.overrides(cfg):
        merge(out, _key_value_spec_to_nested_dict(spec))
    return out


# ---- the limits come from the harness config -----------------------------------------------------------
def test_b0_gets_the_limits_and_sandbox_settings_of_the_main_config():
    got = merged(MAIN)
    assert got["agent"] == {
        "step_limit": MAIN.limits.max_steps,
        "cost_limit": MAIN.limits.max_cost_per_task_usd,
        "wall_time_limit_seconds": int(MAIN.limits.max_wall_seconds),
    }
    assert got["environment"]["timeout"] == MAIN.bash_timeout_s
    run_args = got["environment"]["run_args"]
    assert run_args[0] == "--rm" and run_args[run_args.index("--network") + 1] == "none"
    assert run_args[run_args.index("--memory") + 1] == MAIN.sandbox.mem_limit
    assert run_args[run_args.index("--pids-limit") + 1] == str(MAIN.sandbox.pids_limit)


def test_changing_the_config_changes_the_command():
    cfg = AgentConfig.model_validate(
        {
            "llm": {"model": "deepseek/deepseek-v4-pro"},
            "limits": {"max_steps": 7, "max_cost_per_task_usd": 0.3},
        }
    )
    got = merged(cfg)
    assert got["agent"]["step_limit"] == 7 and got["agent"]["cost_limit"] == 0.3


def test_thinking_on_sends_thinking_and_effort_and_the_paired_limits():
    mk = merged(MAIN)["model"]["model_kwargs"]
    assert mk["thinking"] == {"type": "enabled"} and mk["extra_body"] == {
        "reasoning_effort": MAIN.llm.reasoning_effort
    }
    assert (mk["max_tokens"], mk["timeout"]) == (
        MAIN.llm.max_tokens,
        MAIN.llm.timeout_s,
    )


def test_thinking_off_has_no_effort_and_the_smaller_pair():
    cfg = AgentConfig(
        llm=LLMConfig(model="deepseek/deepseek-flash", thinking="disabled")
    )
    mk = merged(cfg)["model"]["model_kwargs"]
    assert mk["thinking"] == {"type": "disabled"} and "extra_body" not in mk
    assert (mk["max_tokens"], mk["timeout"]) == (16384, 300.0)


def test_on_the_generic_openai_route_thinking_travels_in_the_body():
    cfg = AgentConfig(llm=LLMConfig(model="openai/deepseek-flash"))
    mk = merged(cfg)["model"]["model_kwargs"]
    assert (
        "thinking" not in mk
    )  # the keyword would be rejected, or dropped by drop_params
    assert mk["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def test_the_instance_filter_matches_exactly_the_subset_and_nothing_longer():
    cmd = b0.command(
        MAIN,
        ["django__django-11066", "sympy__sympy-16886"],
        Path("out"),
        Path("mini-extra"),
    )
    pattern = cmd[cmd.index("--filter") + 1]
    assert re.match(pattern, "django__django-11066") and re.match(
        pattern, "sympy__sympy-16886"
    )
    assert not re.match(
        pattern, "django__django-110660"
    )  # mini-swe-agent uses re.match: it needs ^...$
    assert not re.match(pattern, "django__django-1106") and not re.match(
        pattern, "sympy__sympy-1688"
    )
    assert (
        cmd[cmd.index("-c") + 1] == "swebench.yaml"
    )  # a -c list replaces the defaults: keep the built-in file
    assert cmd[-2:] == ["-o", "out"]


def test_the_environment_pins_the_price_table_and_the_config_dir():
    env = b0.environment({"PATH": "/x"})
    assert (
        env["LITELLM_MODEL_REGISTRY_PATH"].endswith("litellm_registry.json")
        and env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    )
    assert env["MSWEA_GLOBAL_CONFIG_DIR"].endswith(".mswea") and env["PATH"] == "/x"


# ---- what mini-swe-agent really sends ---------------------------------------------------------------------------
def test_what_the_server_receives_is_what_the_config_says(monkeypatch):
    # the silent-drop test: build mini-swe-agent's own model from the generated settings and
    # look at the request body that arrives, not at the settings we meant to send
    import litellm

    seen = {}
    real = litellm.completion

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    body = ok_body(
        tool_calls=[tool_call("c1", "bash", '{"command": "ls"}')],
        finish_reason="tool_calls",
    )
    with FakeLLMServer([Step(body=body)]) as server:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("DEEPSEEK_API_BASE", server.url)
        monkeypatch.setattr(litellm, "completion", spy)
        # swebench.yaml's own defaults, which a -c list does not replace
        kwargs = {"drop_params": True, "parallel_tool_calls": True} | merged(MAIN)[
            "model"
        ]["model_kwargs"]
        model = LitellmModel(model_name="deepseek/deepseek-v4-pro", model_kwargs=kwargs)
        model.query([{"role": "user", "content": "hi"}])
        sent = server.requests[0]["body"]

    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == MAIN.llm.reasoning_effort
    assert sent["max_tokens"] == MAIN.llm.max_tokens
    assert (
        seen["timeout"] == MAIN.llm.timeout_s
    )  # the request timeout is not part of the body


# ---- converting the output --------------------------------------------------------------------------------------------
@pytest.fixture
def mini_output(tmp_path):
    iid = "django__django-11066"
    run = tmp_path / "B0"
    (run / "mini" / iid).mkdir(parents=True)
    shutil.copy(FIXTURES / f"{iid}.traj.json", run / "mini" / iid / f"{iid}.traj.json")
    (run / "mini" / "preds.json").write_text(
        json.dumps(
            {
                iid: {
                    "model_name_or_path": "deepseek/deepseek-v4-pro",
                    "instance_id": iid,
                    "model_patch": "diff --git a/x b/x\n",
                }
            }
        )
    )
    return run, iid


def test_the_conversion_rebuilds_tokens_cost_and_steps_from_the_trajectory(mini_output):
    run, iid = mini_output
    assert b0.convert(run, MAIN, "B0") == [iid]
    end = [
        json.loads(x)
        for x in (run / "trajectories" / f"{iid}.jsonl").read_text().splitlines()
    ][-1]
    traj = json.loads((run / "mini" / iid / f"{iid}.traj.json").read_text())
    usages = [
        m["extra"]["response"]["usage"]
        for m in traj["messages"]
        if m["role"] == "assistant"
    ]

    assert (
        end["steps"] == 25 and end["submitted"] is True and end["fail_reason"] is None
    )
    assert end["tokens_in"] == sum(u["prompt_tokens"] for u in usages) == 272239
    assert (
        end["tokens_cached"] == 260864
        and end["tokens_out"] == 9991
        and end["tokens_reasoning"] == 6275
    )
    # the cost is the harness's own fixed price on those tokens, not mini-swe-agent's figure
    expected = sum(
        cost.cost_usd(
            MAIN.llm.resolved_model(),
            Usage(
                u["prompt_tokens"],
                u["completion_tokens"],
                u["prompt_tokens_details"]["cached_tokens"],
                0,
            ),
        )
        for u in usages
    )
    assert end["cost_usd"] == pytest.approx(expected) and end[
        "b0_reported_cost"
    ] == pytest.approx(0.008483441999999999)
    assert end["wall_seconds"] == pytest.approx(
        81.9, abs=0.1
    )  # first to last response, a little short


def test_the_converted_run_is_readable_by_aggregate_and_by_the_scorer(mini_output):
    from swebench.harness.utils import get_predictions_from_file

    run, iid = mini_output
    b0.convert(run, MAIN, "B0")
    metrics = aggregate(load_run(run), {iid})
    assert (
        metrics["tasks"] == 1
        and metrics["steps_mean"] == 25
        and metrics["resolve_rate"]["resolved"] == 1
    )
    assert metrics["cache_hit_rate"] == pytest.approx(260864 / 272239)
    assert metrics["cost_per_task"]["mean"] > 0 and metrics["fail_taxonomy"] == {
        "submitted": 1
    }
    preds = get_predictions_from_file(
        str(run / "preds.jsonl"), "SWE-bench/SWE-bench_Verified", "test"
    )
    assert preds[0]["instance_id"] == iid and preds[0]["model_patch"].startswith(
        "diff --git"
    )
    status = [json.loads(x) for x in (run / "status.jsonl").read_text().splitlines()]
    assert status[0]["steps"] == 25 and status[0]["fail_reason"] is None


def test_converting_twice_gives_the_same_files(mini_output):
    run, _ = mini_output
    b0.convert(run, MAIN, "B0")
    first = {p.name: p.read_text() for p in [run / "preds.jsonl", run / "status.jsonl"]}
    b0.convert(run, MAIN, "B0")
    assert first == {
        p.name: p.read_text() for p in [run / "preds.jsonl", run / "status.jsonl"]
    }
    assert len(list((run / "trajectories").glob("*.jsonl"))) == 1


def test_a_task_without_a_trajectory_is_an_env_error_not_a_crash(tmp_path):
    run = tmp_path / "B0"
    (run / "mini").mkdir(parents=True)
    (run / "mini" / "preds.json").write_text(
        json.dumps({"a__a-1": {"model_patch": ""}})
    )
    b0.convert(run, MAIN, "B0")
    end = json.loads(
        (run / "trajectories" / "a__a-1.jsonl").read_text().splitlines()[-1]
    )
    assert end["fail_reason"] == "env_error" and end["steps"] == 0


@pytest.mark.parametrize(
    ("status", "steps", "cost", "expected"),
    [
        ("Submitted", 20, 0.1, None),
        ("LimitsExceeded", 100, 0.5, "step_limit"),
        ("LimitsExceeded", 40, 2.5, "cost_limit"),
        ("TimeExceeded", 30, 0.2, "time_limit"),
        ("RepeatedFormatError", 9, 0.1, "format_error"),
        ("SomethingElse", 9, 0.1, "env_error"),
        ("", 0, 0.0, "env_error"),
    ],
)
def test_mini_swe_agents_exit_status_maps_to_our_fail_reasons(
    status, steps, cost, expected
):
    assert b0.fail_reason(status, steps, cost, MAIN) == expected


# ---- the command line --------------------------------------------------------------------------------------------------------
def test_run_checks_the_dataset_converts_even_after_a_failure_and_passes_the_environment(
    tmp_path, mini_output
):
    run, _ = mini_output
    calls = []

    def fake_run(cmd, env, check):
        calls.append((cmd, env))
        return SimpleNamespace(returncode=1)

    argv = [
        "run",
        "--config",
        "configs/default.yaml",
        "--subset",
        "configs/subsets/dev_5.txt",
        "--run-id",
        "B0",
        "--results-dir",
        str(run.parent),
        "--yes",
    ]
    meta = json.loads(Path("configs/subsets/subsets_meta.json").read_text())
    code = b0.main(
        argv, run=fake_run, newest_revision=lambda *_: meta["revision"], git_meta=CLEAN
    )
    assert code == 1 and calls[0][1]["LITELLM_MODEL_REGISTRY_PATH"].endswith(
        "litellm_registry.json"
    )
    assert (
        run / "preds.jsonl"
    ).exists()  # what mini-swe-agent wrote before it failed was still converted

    calls.clear()
    with pytest.raises(SystemExit, match="has changed"):
        b0.main(argv, run=fake_run, newest_revision=lambda *_: "0" * 40, git_meta=CLEAN)
    assert not calls  # a moved dataset: mini-swe-agent is not started


# ---- the same guards as run_agent.py ------------------------------------------------------------
def CLEAN():
    return {"git_commit": "abc123", "git_dirty": False}


def DIRTY():
    return {"git_commit": "abc123", "git_dirty": True}


def _run_argv(results_dir, *extra):
    return [
        "run", "--config", "configs/default.yaml", "--subset", "configs/subsets/dev_5.txt",
        "--run-id", "B0", "--results-dir", str(results_dir), "--yes", *extra,
    ]  # fmt: skip


def _go(argv, calls, git):
    meta = json.loads(Path("configs/subsets/subsets_meta.json").read_text())

    def fake_run(cmd, env, check):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    return b0.main(
        argv, run=fake_run, newest_revision=lambda *_: meta["revision"], git_meta=git
    )


def test_a_dirty_tree_is_refused_unless_allowed_and_then_recorded(mini_output):
    run, _ = mini_output
    calls = []
    with pytest.raises(SystemExit, match="uncommitted changes"):
        _go(_run_argv(run.parent), calls, DIRTY)
    assert not calls and not (run / "run_meta.json").exists()
    _go(_run_argv(run.parent, "--allow-dirty"), calls, DIRTY)
    meta = json.loads((run / "run_meta.json").read_text())
    assert calls and meta["allow_dirty"] is True and meta["git_dirty"] is True


def test_a_run_id_belongs_to_one_config(mini_output, tmp_path):
    run, _ = mini_output
    calls = []
    _go(_run_argv(run.parent), calls, CLEAN)
    first = json.loads((run / "run_meta.json").read_text())
    assert first["harness_config_hash"] == MAIN.config_hash()
    assert first["git_commit"] == "abc123"

    other = tmp_path / "other.yaml"
    other.write_text(
        "extends: "
        + str(Path("configs/default.yaml").resolve())
        + "\nlimits:\n  max_steps: 99\n"
    )
    calls.clear()
    argv = _run_argv(run.parent)
    argv[argv.index("configs/default.yaml")] = str(other)
    with pytest.raises(b0.ConfigMismatch):
        _go(argv, calls, CLEAN)
    assert not calls  # mini-swe-agent is not started
    b0.convert(run, MAIN, "B0")
    (run / "preds.jsonl").unlink()
    with pytest.raises(b0.ConfigMismatch):
        b0.convert(run, b0_load(other), "B0")
    assert not (run / "preds.jsonl").exists()  # refused before writing anything


def test_converting_keeps_the_record_of_how_the_run_started(mini_output):
    run, _ = mini_output
    _go(_run_argv(run.parent), [], CLEAN)
    before = (run / "run_meta.json").read_text()
    b0.convert(run, MAIN, "B0")
    assert (
        run / "run_meta.json"
    ).read_text() == before  # not rewritten, git commit kept


def b0_load(path):
    from harness.config import load_config

    return load_config(path)
