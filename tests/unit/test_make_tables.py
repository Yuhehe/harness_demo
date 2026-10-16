"""eval/report/make_tables.py on synthetic runs."""

import json

import pytest
from aggregate import load_run
from make_tables import main, provenance, render, replace_block, subset_name

README = """# Title

intro stays

<!-- RESULTS:MAIN:START -->
old table
<!-- RESULTS:MAIN:END -->

<!-- RESULTS:ABLATION:START -->
untouched
<!-- RESULTS:ABLATION:END -->
"""


def run_start(commit="c1"):
    return {
        "type": "run_start",
        "git_commit": commit,
        "config_hash": "h1",
        "model": "m",
    }


def run_end(steps, cost, wall=100.0):
    return {
        "type": "run_end", "steps": steps, "cost_usd": cost, "fail_reason": None,
        "submitted": True, "wall_seconds": wall, "tokens_in": 20_000, "tokens_cached": 15_000,
        "tokens_out": 500,
    }  # fmt: skip


def make_run(root, run_id, tasks, resolved, *, meta=None, status=None):
    """tasks: {instance_id: (steps, cost) or None for a run without run_end}"""
    d = root / run_id
    (d / "trajectories").mkdir(parents=True)
    for iid, end in tasks.items():
        spans = [run_start()] + ([run_end(*end)] if end else [])
        (d / "trajectories" / f"{iid}.jsonl").write_text(
            "\n".join(json.dumps(s) for s in spans)
        )
    if resolved is not None:
        (d / "resolved.json").write_text(json.dumps(sorted(resolved)))
    (d / "run_meta.json").write_text(json.dumps(meta or {"config_hash": "h1"}))
    (d / "status.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (status or []))
    )
    return d


@pytest.fixture
def setup(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "M", {"a": (10, 0.2), "b": (20, 0.4)}, {"a"})
    make_run(runs, "B", {"a": (5, 0.1), "b": (10, 0.2)}, {"a", "b"})
    readme = tmp_path / "README.md"
    readme.write_text(README)
    args = [
        "--main", "M", "--baseline", "B", "--results-dir", str(runs),
        "--summary-dir", str(tmp_path / "summary"), "--readme", str(readme),
    ]  # fmt: skip
    return tmp_path, runs, readme, args


def test_writes_summary_and_readme_and_is_stable(setup):
    tmp, _, readme, args = setup
    assert main(args + ["--check"]) == 1  # nothing generated yet
    assert main(args) == 0
    text = readme.read_text()
    assert "old table" not in text and "intro stays" in text and "untouched" in text
    table = (tmp / "summary" / "main.md").read_text()
    assert table in text
    s = json.loads((tmp / "summary" / "main.json").read_text())
    assert s["tasks"] == 2 and s["subset"] == "custom"
    assert [x["run_id"] for x in s["systems"]] == ["M", "B"]
    assert s["paired"]["resolved"] == {
        "both": 1,
        "only_base": 1,
        "only_other": 0,
        "sign_test_p": 1.0,
    }
    assert main(args + ["--check"]) == 0  # regenerating changes nothing
    readme.write_text(text.replace("1/2 = 50.0%", "2/2 = 100.0%"))
    assert main(args + ["--check"]) == 1  # a hand edit is caught


def test_the_numbers_in_the_table(setup):
    tmp, _, _, args = setup
    main(args)
    table = (tmp / "summary" / "main.md").read_text()
    # M resolves 1 of 2: Wilson 95% interval of 1/2 is 9.5%-90.5%
    assert (
        "| harness-lab (M) | 1/2 = 50.0% (9.5%–90.5%) | $0.300 ($0.300) | $0.600 |"
        in table
    )
    assert (
        "| mini-SWE-agent (B) | 2/2 = 100.0% (34.2%–100.0%) | $0.150 ($0.150) | $0.150 |"
        in table
    )
    assert "20k / 0.5k | 75.0%" in table
    # every task costs twice as much and takes twice the steps in M
    assert "| Cost | 2.00 (2.00–2.00) | 2/2 |" in table
    assert "| Steps | 2.00 (2.00–2.00) | 2/2 |" in table
    assert "only M: 0; only B: 1" in table


def test_different_tasks_are_refused(setup):
    _, runs, _, args = setup
    traj = runs / "B" / "trajectories"
    (traj / "b.jsonl").rename(
        traj / "c.jsonl"
    )  # same number of tasks, not the same tasks
    with pytest.raises(SystemExit, match="different tasks"):
        main(args)
    (traj / "c.jsonl").unlink()
    with pytest.raises(SystemExit, match="different tasks"):
        main(args)


def test_an_unscored_run_is_refused(setup):
    _, runs, _, args = setup
    (runs / "M" / "resolved.json").unlink()
    with pytest.raises(SystemExit, match="score the run first"):
        main(args)


def test_an_unfinished_task_is_refused(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "M", {"a": (10, 0.2), "b": None}, {"a"})
    make_run(runs, "B", {"a": (5, 0.1), "b": (10, 0.2)}, {"a"})
    readme = tmp_path / "README.md"
    readme.write_text(README)
    with pytest.raises(SystemExit, match="no run_end"):
        main(["--main", "M", "--baseline", "B", "--results-dir", str(runs),
              "--summary-dir", str(tmp_path / "s"), "--readme", str(readme)])  # fmt: skip


def test_subset_name_matches_the_exact_set(tmp_path):
    (tmp_path / "small.txt").write_text("a\nb\n")
    (tmp_path / "big.txt").write_text("a\nb\nc\n")
    assert subset_name({"a", "b"}, tmp_path) == "small"
    assert subset_name({"a", "b", "c"}, tmp_path) == "big"
    assert subset_name({"a"}, tmp_path) == "custom"


@pytest.mark.parametrize(
    ("status", "meta", "want"),
    [
        ([{"workers": 3}, {"workers": 3}], {}, [3]),
        ([{"instance_id": "a"}], {}, [1]),  # runner before v0.1.1: one at a time
        ([{"workers": 3}, {"instance_id": "x"}], {}, [1, 3]),  # mixed halves are shown
        ([{"instance_id": "a"}], {"workers": 3}, [3]),  # B0: only run_meta knows
    ],
)
def test_worker_counts(tmp_path, status, meta, want):
    d = make_run(tmp_path, "R", {"a": (1, 0.1)}, {"a"}, meta=meta, status=status)
    assert provenance(d, load_run(d))["workers"] == want


def test_provenance_falls_back_to_run_meta_for_b0(tmp_path):
    d = make_run(
        tmp_path, "R", {"a": (1, 0.1)}, {"a"},
        meta={"git_commit": "g", "harness_config_hash": "hh"},
    )  # fmt: skip
    t = load_run(d)
    for task in t.values():
        task.spans[0] = {"type": "run_start", "model": "m"}  # B0's converted run_start
    p = provenance(d, t)
    assert p["git_commits"] == ["g"] and p["config_hashes"] == ["hh"]


def test_replace_block_needs_exactly_one_pair():
    assert replace_block(README, "MAIN", "new\n").count("new\n") == 1
    with pytest.raises(SystemExit):
        replace_block("no markers", "MAIN", "x")
    with pytest.raises(SystemExit):
        replace_block(README + README, "MAIN", "x")


def test_render_shows_missing_values_as_a_dash():
    system = {
        "name": "n", "run_id": "R",
        "resolve_rate": {"resolved": 0, "n": 1, "rate": 0.0, "wilson95": [0.0, 0.79]},
        "cost_per_task": {"mean": 0.1, "p50": 0.1}, "cost_per_resolved": None,
        "tokens_in_mean": 100.0, "tokens_out_mean": 10.0, "cache_hit_rate": None,
        "latency": {"p50": 1.0, "p95": 2.0}, "steps_mean": 3.0,
        "provenance": {"workers": [1], "config_hashes": ["h"], "models": ["m"]},
    }  # fmt: skip
    out = render(
        {"subset": "s", "tasks": 1, "systems": [system, system], "paired": {"tasks": 1}}
    )
    assert "| — | 0.1k / 0.0k | — |" in out
    assert "| Cost | — | — |" in out


def test_the_setup_line_says_whether_config_and_model_match(setup):
    tmp, runs, _, args = setup
    main(args)
    assert (
        "same model and config for both (config h1)"
        in (tmp / "summary" / "main.md").read_text()
    )
    for f in (runs / "B" / "trajectories").glob("*.jsonl"):
        f.write_text(
            f.read_text().replace('"config_hash": "h1"', '"config_hash": "h2"')
        )
    main(args)
    assert (
        "configs h1 vs h2, models m vs m" in (tmp / "summary" / "main.md").read_text()
    )
