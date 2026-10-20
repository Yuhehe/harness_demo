"""eval/report/plots.py: the numbers behind the figures (rendering is only smoke-tested)."""

import json

import pytest
from plots import CLASSES, ending, main, run_points


def run_end(fail=None, cost=0.1):
    return {
        "type": "run_end", "steps": 5, "cost_usd": cost, "fail_reason": fail,
        "submitted": fail is None, "wall_seconds": 1.0, "tokens_in": 10,
        "tokens_cached": 5, "tokens_out": 1,
    }  # fmt: skip


def make_run(root, run_id, ends, resolved, tests=None):
    d = root / run_id / "trajectories"
    d.mkdir(parents=True)
    for iid, end in ends.items():
        (d / f"{iid}.jsonl").write_text(json.dumps(end) if end else "")
    (root / run_id / "resolved.json").write_text(json.dumps(sorted(resolved)))
    (root / run_id / "score.json").write_text(json.dumps({"tests": tests or {}}))


T = {"f2p_pass": 0, "f2p_total": 1, "p2p_fail": 0, "p2p_total": 3}


@pytest.mark.parametrize(
    ("iid", "end", "tests", "want"),
    [
        ("a", run_end(), None, "resolved"),
        ("b", run_end(), T, "submitted, not fixed"),
        ("b", run_end(), {**T, "p2p_fail": 1}, "partial or regression"),
        ("b", run_end(), {**T, "f2p_pass": 1, "f2p_total": 2}, "partial or regression"),
        ("b", run_end("context_overflow"), None, "stopped early"),
        ("a", run_end("step_limit"), None, "resolved"),  # its patch passed anyway
    ],
)
def test_ending(iid, end, tests, want):
    score = {"tests": {iid: tests}} if tests else None
    assert ending(iid, end, {"a"}, score) == want


def test_an_empty_patch_is_submitted_not_stopped_early():
    score = {"empty_patch": ["b"], "tests": {}}
    assert ending("b", run_end(), {"a"}, score) == "submitted, not fixed"


def test_points_are_cut_to_the_subset(tmp_path):
    make_run(tmp_path, "R", {"a": run_end(cost=0.2), "b": run_end(cost=0.4),
                             "x": run_end(cost=9.0)}, {"a", "x"}, {"b": T})  # fmt: skip
    p = run_points("R", tmp_path, {"a", "b"})
    assert (p["n"], p["resolved"], p["cost_mean"]) == (2, 1, pytest.approx(0.3))
    assert p["endings"] == dict(zip(CLASSES, (1, 1, 0, 0), strict=True))
    assert run_points("missing", tmp_path, {"a"}) is None


def test_unfinished_or_incomplete_runs_are_refused(tmp_path):
    make_run(tmp_path, "R", {"a": run_end(), "b": None}, {"a"})
    with pytest.raises(SystemExit, match="unfinished"):
        run_points("R", tmp_path, {"a", "b"})
    with pytest.raises(SystemExit, match="missing"):
        run_points("R", tmp_path, {"a", "zz"})


def test_main_writes_both_figures(tmp_path):
    pytest.importorskip("matplotlib")
    runs = tmp_path / "runs"
    make_run(runs, "M0", {"a": run_end(), "b": run_end()}, {"a"}, {"b": T})
    make_run(runs, "B0", {"a": run_end(), "b": run_end()}, {"a", "b"})
    sub = tmp_path / "s.txt"
    sub.write_text("a\nb\n")
    out = tmp_path / "img"
    assert (
        main(["--subset", str(sub), "--results-dir", str(runs), "--out-dir", str(out)])
        == 0
    )
    for name in ("cost_vs_resolve.png", "failure_taxonomy.png"):
        assert (out / name).read_bytes()[:4] == b"\x89PNG"
