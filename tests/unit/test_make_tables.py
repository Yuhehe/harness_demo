"""eval/report/make_tables.py on synthetic runs."""

import json

import pytest
from aggregate import load_run
from make_tables import main, provenance, render, replace_block, subset_name

README = """# Title

<!-- RESULTS:TLDR:START -->
<!-- RESULTS:TLDR:END -->

intro stays

<!-- RESULTS:SETUP:START -->
<!-- RESULTS:SETUP:END -->

<!-- RESULTS:MAIN:START -->
old table
<!-- RESULTS:MAIN:END -->

<!-- RESULTS:ABLATION:START -->
old ablations
<!-- RESULTS:ABLATION:END -->

untouched

<!-- RESULTS:COST:START -->
<!-- RESULTS:COST:END -->

<!-- RESULTS:SECURITY:START -->
<!-- RESULTS:SECURITY:END -->

<!-- RESULTS:CAVEAT:START -->
<!-- RESULTS:CAVEAT:END -->

<!-- RESULTS:FAILURES:START -->
<!-- RESULTS:FAILURES:END -->
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
    make_run(runs, "R2", {"a": (12, 0.25), "b": (18, 0.35)}, {"a"})
    readme = tmp_path / "README.md"
    readme.write_text(README)
    args = [
        "--main", "M", "--baseline", "B", "--results-dir", str(runs),
        "--summary-dir", str(tmp_path / "summary"), "--readme", str(readme),
        "--ablation-base", "R2", "--review", str(tmp_path / "review.md"),
    ]  # fmt: skip
    return tmp_path, runs, readme, args


def test_writes_summary_and_readme_and_is_stable(setup):
    tmp, _, readme, args = setup
    assert main(args + ["--check"]) == 1  # nothing generated yet
    assert main(args) == 0
    text = readme.read_text()
    assert "old table" not in text and "intro stays" in text and "untouched" in text
    assert (
        "old ablations" not in text
        and "### Cost" in text
        and "### Permission guard" in text
    )
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
    make_run(runs, "R2", {"a": (5, 0.1), "b": (10, 0.2)}, {"a"})
    with pytest.raises(SystemExit, match="no run_end"):
        main(["--main", "M", "--baseline", "B", "--results-dir", str(runs),
              "--summary-dir", str(tmp_path / "s"), "--readme", str(readme),
              "--ablation-base", "R2"])  # fmt: skip


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


# ---- ablations, cost, security ------------------------------------------------------------------
def test_ablations_pair_with_the_base_on_their_own_tasks(setup):
    tmp, runs, _, args = setup
    make_run(runs, "A2", {"a": (24, 0.5)}, {"a"})  # one task of the base's two
    make_run(runs, "A6", {"a": (12, 0.25), "b": (18, 0.35)}, {"a", "b"})
    make_run(runs, "A6xA1", {"a": (6, 0.1), "b": (9, 0.2)}, set())
    main(args)
    a = json.loads((tmp / "summary" / "ablation.json").read_text())
    rows = {r["id"]: r for r in a["rows"]}
    assert set(rows) == {"A2", "A6", "A6xA1"} and a["base"] == "R2"
    assert rows["A2"]["paired"]["tasks"] == 1  # paired on A2's tasks only
    assert rows["A2"]["paired"]["steps"]["median_ratio"] == 2.0
    assert set(a["not_run"]) == {"A1", "A3", "A4", "A5"}
    assert a["run_to_run"]["other"] == "M" and a["run_to_run"]["tasks"] == 2
    rev = a["run_to_run_reverse"]
    assert (rev["base"], rev["other"]) == ("M", "R2")
    # 2 tasks: medians are means of two ratios, so only check the direction flips
    assert (rev["steps"]["median_ratio"] < 1) == (
        a["run_to_run"]["steps"]["median_ratio"] > 1
    )
    pr = a["pressure"]
    assert (pr["with"], pr["without"]) == ("A6", "A6xA1")
    assert pr["paired"]["resolved"]["only_other"] == 2  # A6 resolved both, A6xA1 none
    md = (tmp / "summary" / "ablation.md").read_text()
    assert "| A2 | action space = bash only | 1 (`custom`) | 1/1 (base 1) |" in md
    assert "Not run: A1, A3, A4, A5." in md and "| (noise) |" in md


def test_an_ablation_with_tasks_the_base_lacks_is_refused(setup):
    _, runs, _, args = setup
    make_run(runs, "A5", {"a": (1, 0.1), "zzz": (1, 0.1)}, set())
    with pytest.raises(SystemExit, match="lacks"):
        main(args)


def test_cost_lists_main_baseline_base_then_ablations(setup):
    tmp, runs, _, args = setup
    make_run(runs, "A5", {"a": (3, 0.3)}, {"a"})
    main(args)
    c = json.loads((tmp / "summary" / "cost.json").read_text())
    assert [r["run_id"] for r in c["rows"]] == ["M", "B", "R2", "A5"]
    assert c["rows"][0]["total_usd"] == pytest.approx(0.6)


def deny(rule="deny.test_edit_bash"):
    return {"type": "guard_decision", "action": "deny", "tool": "bash", "rule_id": rule}


def allow(tool="bash"):
    return {"type": "guard_decision", "action": "allow", "tool": tool, "rule_id": None}


def test_false_block_rate_from_spans_and_reviews(tmp_path):
    runs = tmp_path / "runs"
    d = make_run(runs, "M", {"a": (5, 0.2), "b": (5, 0.2)}, {"a"})
    make_run(runs, "B", {"a": (5, 0.1), "b": (5, 0.1)}, {"a"})
    make_run(runs, "R2", {"a": (5, 0.1), "b": (5, 0.1)}, {"a"})
    spans = {"a": [allow(), allow("view"), deny(), deny()], "b": [allow(), deny()]}
    for iid, extra in spans.items():
        f = d / "trajectories" / f"{iid}.jsonl"
        lines = f.read_text().splitlines()
        f.write_text("\n".join(lines[:1] + [json.dumps(s) for s in extra] + lines[1:]))
    review = tmp_path / "review.md"
    review.write_text(
        "## 权限层拦截的人工复核\n\n| M | a | deny.test_edit_bash | false_block | x |\n"
        "| M | a | deny.test_edit_bash | true_block | y |\n| B | b | r | false_block | z |\n"
        "\n## 之后的一节\n\n| M | b | r | false_block | outside the section |\n"
    )
    readme = tmp_path / "README.md"
    readme.write_text(README)
    main(["--main", "M", "--baseline", "B", "--results-dir", str(runs),
          "--summary-dir", str(tmp_path / "s"), "--readme", str(readme),
          "--ablation-base", "R2", "--review", str(review)])  # fmt: skip
    s = json.loads((tmp_path / "s" / "security.json").read_text())
    assert (s["decisions"], s["bash_decisions"], s["denies"]) == (6, 5, 3)
    # b's block has no review row for run M (the B row belongs to another run)
    assert (s["false_blocks"], s["true_blocks"], s["unreviewed"]) == (1, 1, 1)
    assert s["false_block_rate"] == pytest.approx(1 / 6)
    assert s["false_block_rate_bash"] == pytest.approx(1 / 5)


def test_an_unknown_review_verdict_is_an_error(tmp_path):
    from make_tables import load_reviews

    p = tmp_path / "r.md"
    p.write_text("## 权限层拦截的人工复核\n| M | a | r | maybe | x |\n")
    with pytest.raises(SystemExit, match="maybe"):
        load_reviews(p, "M")


# ---- rule B and its calibration (docs/notes.md section 22.9) ---------------------------------------
def test_differs_reads_the_interval_against_1():
    from make_tables import differs

    assert differs({"ci95": [1.05, 1.4]}) == "higher"
    assert differs({"ci95": [0.6, 0.95]}) == "lower"
    assert differs({"ci95": [0.9, 1.2]}) == "no"
    assert differs({"ci95": [1.0, 1.2]}) == "no"  # touching 1 is not excluding it
    assert differs(None) is None


def test_calibration_passes_only_if_no_noise_interval_excludes_1():
    from make_tables import _calibration_note, calibration

    ok = {"ci95": [0.9, 1.1]}
    a = {
        "run_to_run": {"other": "M", "base": "R", "cost_usd": ok, "steps": ok},
        "run_to_run_reverse": {"other": "R", "base": "M", "cost_usd": ok, "steps": ok},
    }
    assert calibration(a)["passed"] and "does not differ" in _calibration_note(
        calibration(a)
    )
    a["run_to_run"]["steps"] = {"ci95": [1.02, 1.3]}
    c = calibration(a)
    assert (
        not c["passed"]
        and "FAILED" in _calibration_note(c)
        and "M/R steps" in _calibration_note(c)
    )
    assert calibration({}) is None and "No rerun" in _calibration_note(None)


def test_tables_state_the_rule_and_the_check(setup):
    tmp, runs, _, args = setup
    make_run(
        runs, "A2", {"a": (24, 0.5), "b": (36, 0.7)}, {"a"}
    )  # twice the base everywhere
    main(args)
    abl = (tmp / "summary" / "ablation.md").read_text()
    main_md = (tmp / "summary" / "main.md").read_text()
    assert "| higher / higher |" in abl.split("| A2 |")[1].splitlines()[0]
    assert "Rule: a cost or steps ratio counts as a difference" in abl
    assert "Rule: a cost or steps ratio counts as a difference" in main_md
    a = json.loads((tmp / "summary" / "ablation.json").read_text())
    assert "calibration" in a and "verdicts" in a["calibration"]
    assert "| Cost | 2.00 (2.00–2.00) | 2/2 | higher |" in main_md
    # the rerun (M against R2) goes both ways across tasks, so the check passes
    assert "Check: the main configuration run twice does not differ" in main_md


# ---- TL;DR, setup, failures, caveat -------------------------------------------------------------
def test_tldr_wording_follows_the_data(setup):
    tmp, _, _, args = setup
    main(args)
    t = (tmp / "summary" / "tldr.md").read_text()
    # M resolved 1/2, B 2/2: one discordant task, sign test p = 1 -> indistinguishable
    assert (
        "harness-lab resolves **1/2 (50.0%" in t
        and "indistinguishable (exact sign test p = 1)" in t
    )
    assert "**$0.300** per task against **$0.150**" in t
    assert "rules that fired: none" in t


def test_failures_block_lists_the_unresolved_tasks_with_owners(setup, tmp_path):
    tmp, _, _, args = setup
    labels = tmp_path / "labels.md"
    labels.write_text("## 归属标注\n| M | b | model | why |\n")
    main(args + ["--labels", str(labels)])
    f = json.loads((tmp / "summary" / "failures.json").read_text())
    assert f["failures"]["n"] == 1 and f["failures"]["by_owner"] == {"model": 1}
    assert f["by_class"] == {"submitted_but_failed / unknown": ["b"]}
    assert (
        "Owner after manual review: model 1"
        in (tmp / "summary" / "failures.md").read_text()
    )


def test_caveat_counts_the_rerun_flips(setup):
    tmp, _, _, args = setup
    main(args)
    c = (tmp / "summary" / "caveat.md").read_text()
    # M (main) resolved a; R2 resolved a: no flips on the two tasks
    assert "resolved 1 and 1; 0 tasks flipped" in c


def test_setup_names_limits_commits_and_cost(setup):
    tmp, _, _, args = setup
    main(args)
    s = json.loads((tmp / "summary" / "setup.json").read_text())
    assert s["tasks"] == 2 and s["commits"][0]["commit"] == "c1"
    assert s["cost_of_tables"] == pytest.approx(0.6 + 0.3 + 0.6)  # M, B, R2
    assert s["harness_lines"] > 1000


def test_caveat_counts_flips_both_ways(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "M", {"a": (5, 0.2), "b": (5, 0.2)}, {"a"})
    make_run(runs, "B", {"a": (5, 0.1), "b": (5, 0.1)}, {"a"})
    make_run(runs, "R2", {"a": (5, 0.2), "b": (5, 0.2)}, {"b"})  # the other task
    readme = tmp_path / "README.md"
    readme.write_text(README)
    main(["--main", "M", "--baseline", "B", "--results-dir", str(runs),
          "--summary-dir", str(tmp_path / "s"), "--readme", str(readme),
          "--ablation-base", "R2", "--review", str(tmp_path / "none.md")])  # fmt: skip
    assert (
        "resolved 1 and 1; 2 tasks flipped"
        in (tmp_path / "s" / "caveat.md").read_text()
    )
