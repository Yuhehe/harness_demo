"""eval/report/gap_breakdown.py on synthetic data: step profile, edit targets, pooled statistics."""

import pytest
from gap_breakdown import (
    CLASSES,
    difference,
    edit_targets,
    patch_files,
    profile,
    ratio,
)


def bash(cmd):
    return ("bash", {"command": cmd})


RUN = [
    [bash("grep -rn foo src")],
    [
        bash("cat a.py"),
        bash("sed -i 's/a/b/' src/a.py"),
    ],  # two calls: half explore, half edit
    [bash("python -m pytest tests")],
    [],  # text only
    [("str_replace", {"path": "/testbed/src/b.py", "old_str": "x", "new_str": "y"})],
    [bash("python -m pytest tests")],
    [("submit", {})],
]


def test_classes_split_a_multi_call_step_and_add_up_to_the_responses():
    p = profile(RUN)
    c = p["classes"]
    assert (
        c["explore"] == 1.5 and c["edit"] == 1.5 and c["test"] == 2 and c["text"] == 1
    )
    assert c["submit"] == 1
    assert sum(c.values()) == pytest.approx(len(RUN)) == p["responses"]
    assert set(c) == set(CLASSES)


def test_phases_are_cut_at_the_first_and_last_edit():
    p = profile(RUN)
    assert p["phases"] == {"locate": 1, "fix": 4, "verify": 2, "no_edit": 0}
    assert sum(p["phases"].values()) == len(RUN)
    assert p["steps_before_first_edit"] == 1
    assert p["tests_after_last_edit"] == 1


def test_a_run_without_an_edit_is_one_block():
    p = profile([[bash("ls")], [bash("ls")], [("submit", {})]])
    assert p["phases"] == {"locate": 0, "fix": 0, "verify": 0, "no_edit": 3}
    assert p["repeated_calls"] == 1  # the second `ls`
    assert p["steps_before_first_edit"] == 3


def test_edit_targets_and_patch_files():
    assert edit_targets("str_replace", {"path": "/testbed/django/db/x.py"}) == [
        "django/db/x.py"
    ]
    assert edit_targets(*bash("cd /testbed && sed -i 's/a/b/' ./django/x.py")) == [
        "django/x.py"
    ]
    # a Python heredoc: a literal path is found, a path in a variable is not
    heredoc = "python - <<'PY'\nfrom pathlib import Path\np = Path('django/x.py')\nPY"
    assert edit_targets(*bash(heredoc)) == ["django/x.py"]
    assert edit_targets(*bash("python - <<'EOF'\nopen(p,'w').write(s)\nEOF")) is None
    patch = "diff --git a/django/x.py b/django/x.py\n--- a\n+++ b\ndiff --git a/t.py b/t.py\n"
    assert patch_files(patch) == {"django/x.py", "t.py"}
    assert patch_files(None) == set()


def test_ratio_of_identical_pools_is_one_and_doubling_is_two():
    a = {f"t{i}": [10.0 + i, 12.0 + i] for i in range(8)}
    same = ratio(a, a, rounds=200)
    assert (
        same["median_ratio"] == pytest.approx(1.0)
        and same["n"] == 8
        and same["runs"] == [2, 2]
    )
    double = ratio({i: [2 * x for x in v] for i, v in a.items()}, a, rounds=200)
    assert double["median_ratio"] == pytest.approx(2.0)
    assert double["other_higher"] == 8


def test_ratio_uses_geometric_means_of_the_runs():
    # one task: a's runs 10 and 40 (geometric mean 20), b's run 10 -> 2.0
    r = ratio({"t": [10.0, 40.0]}, {"t": [10.0]}, rounds=50)
    assert r["median_ratio"] == pytest.approx(2.0)


def test_the_bootstrap_resamples_runs_inside_a_task():
    # with every task identical, only the inner level can move the statistic
    a = {f"t{i}": [10.0, 30.0] for i in range(10)}
    b = {f"t{i}": [10.0] for i in range(10)}
    lo, hi = ratio(a, b, rounds=2000)["ci95"]
    assert lo < ratio(a, b, rounds=10)["median_ratio"] < hi
    assert lo < 1.5 and hi > 2.0


def test_difference_mean_and_median():
    a = {"t1": [3.0, 5.0], "t2": [10.0], "t3": [2.0]}
    b = {"t1": [1.0], "t2": [1.0], "t3": [2.0]}
    d = difference(a, b, rounds=500)
    assert d["mean"] == pytest.approx((3 + 9 + 0) / 3)
    assert d["median"] == pytest.approx(3.0)
    assert d["mean_ci95"][0] <= d["mean"] <= d["mean_ci95"][1]


def test_the_bootstrap_is_reproducible():
    a = {f"t{i}": [float(i + 1), float(i + 3)] for i in range(6)}
    b = {f"t{i}": [float(i + 2)] for i in range(6)}
    assert ratio(a, b, rounds=300, seed=1) == ratio(a, b, rounds=300, seed=1)
