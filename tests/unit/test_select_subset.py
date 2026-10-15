import json

import pytest
import select_subset as ss

REPOS = {"a/a": 30, "b/b": 30, "c/c": 30, "d/d": 30, "e/e": 30}
DIFFS = ["<15 min fix", "15 min - 1 hour", "<15 min fix", "1-4 hours"]
QUOTAS = {"a/a": 20, "b/b": 15, "c/c": 10, "d/d": 10}


def make_rows():
    rows = []
    for repo, n in REPOS.items():
        short = repo.split("/")[0]
        for i in range(n):
            iid = f"{short}__{short}-{i:03d}"
            rows.append(
                {
                    "instance_id": iid,
                    "repo": repo,
                    "difficulty": DIFFS[i % len(DIFFS)],
                    "image": f"swebench/sweb.eval.x86_64.{short}_1776_{short}-{i:03d}:latest",
                    "base_commit": "0" * 40,
                }
            )
    return rows


def ids(rows):
    return [r["instance_id"] for r in rows]


def test_sizes_and_disjoint():
    verified, dev = ss.select_subsets(make_rows(), QUOTAS, seed=1)
    assert len(verified) == 50 and len(dev) == 5
    assert not set(ids(verified)) & set(ids(dev))


def test_quotas_respected_across_both_sets():
    verified, dev = ss.select_subsets(make_rows(), QUOTAS, seed=1)
    got = {}
    for r in verified + dev:
        got[r["repo"]] = got.get(r["repo"], 0) + 1
    assert got == QUOTAS


def test_deterministic_and_independent_of_row_order():
    rows = make_rows()
    a = ss.select_subsets(rows, QUOTAS, seed=7)
    b = ss.select_subsets(list(reversed(rows)), QUOTAS, seed=7)
    assert [ids(x) for x in a] == [ids(x) for x in b]


def test_different_seed_changes_selection():
    a, _ = ss.select_subsets(make_rows(), QUOTAS, seed=1)
    b, _ = ss.select_subsets(make_rows(), QUOTAS, seed=2)
    assert ids(a) != ids(b)


def test_dev_is_easy_and_spans_repos():
    _, dev = ss.select_subsets(make_rows(), QUOTAS, seed=3)
    assert {r["difficulty"] for r in dev} == {ss.DEV_DIFFICULTY}
    assert len({r["repo"] for r in dev}) >= ss.MIN_DEV_REPOS


def test_outputs_sorted():
    verified, dev = ss.select_subsets(make_rows(), QUOTAS, seed=1)
    assert ids(verified) == sorted(ids(verified))
    assert ids(dev) == sorted(ids(dev))


def test_quota_larger_than_pool_raises():
    rows = [r for r in make_rows() if r["repo"] != "d/d"] + [
        r for r in make_rows() if r["repo"] == "d/d"
    ][:5]
    with pytest.raises(ValueError, match="d/d"):
        ss.select_subsets(rows, QUOTAS, seed=1)


def test_wrong_quota_sum_raises():
    with pytest.raises(ValueError, match="quotas sum"):
        ss.select_subsets(make_rows(), {"a/a": 20}, seed=1)


def test_missing_image_raises():
    rows = make_rows()
    rows[0]["image"] = ""
    with pytest.raises(ValueError, match="image"):
        ss.select_subsets(rows, QUOTAS, seed=1)


def test_write_is_byte_identical_and_refuses_overwrite(tmp_path):
    verified, dev = ss.select_subsets(make_rows(), QUOTAS, seed=1)
    meta = ss.build_meta(
        verified,
        dev,
        dataset_id="x/y",
        split="test",
        revision="abc",
        seed=1,
        quotas=QUOTAS,
    )
    out1, out2 = tmp_path / "one", tmp_path / "two"
    ss.write_outputs(out1, verified, dev, meta)
    ss.write_outputs(out2, verified, dev, meta)
    for name in ("verified_50.txt", "dev_5.txt", ss.META_NAME):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes()
    assert ss.read_ids(out1 / "dev_5.txt") == ids(dev)
    with pytest.raises(FileExistsError):
        ss.write_outputs(out1, verified, dev, meta)
    ss.write_outputs(out1, verified, dev, meta, force=True)


def test_meta_records_revision_and_counts():
    verified, dev = ss.select_subsets(make_rows(), QUOTAS, seed=1)
    meta = ss.build_meta(
        verified,
        dev,
        dataset_id="x/y",
        split="test",
        revision="abc",
        seed=1,
        quotas=QUOTAS,
    )
    assert meta["revision"] == "abc" and meta["seed"] == 1
    assert meta["verified_50"]["count"] == 50 and meta["dev_5"]["count"] == 5
    json.dumps(meta, sort_keys=True)  # serializable


def test_parse_quotas():
    assert ss.parse_quotas(None) == ss.DEFAULT_QUOTAS
    assert ss.parse_quotas(["a/a=3", "b/b=4"]) == {"a/a": 3, "b/b": 4}


# ---- a subset derived from a frozen one --------------------------------------------------------
PARENT = (
    [f"django__django-{i}" for i in range(100, 118)]
    + [f"sympy__sympy-{i}" for i in range(200, 214)]
    + [f"scikit-learn__scikit-learn-{i}" for i in range(300, 309)]
)
HALF = {"django/django": 9, "sympy/sympy": 7, "scikit-learn/scikit-learn": 5}


def test_a_derived_subset_meets_the_quotas_and_stays_inside_the_parent():
    from select_subset import derive_subset, repo_of

    ids = derive_subset(PARENT, HALF, 42)
    assert set(ids) <= set(PARENT) and ids == sorted(ids) and len(set(ids)) == 21
    counts = {r: sum(repo_of(i) == r for i in ids) for r in HALF}
    assert counts == HALF


def test_a_derived_subset_is_reproducible_and_independent_of_the_parent_order():
    from select_subset import derive_subset

    assert derive_subset(PARENT, HALF, 42) == derive_subset(
        list(reversed(PARENT)), HALF, 42
    )
    assert derive_subset(PARENT, HALF, 42) != derive_subset(PARENT, HALF, 7)


def test_a_derived_subset_refuses_impossible_quotas():
    from select_subset import derive_subset

    with pytest.raises(ValueError, match="quota 30"):
        derive_subset(PARENT, {"django/django": 30}, 42)
    with pytest.raises(ValueError, match="not in the parent"):
        derive_subset(PARENT, {"astropy/astropy": 1}, 42)


def test_the_repo_of_an_instance_id():
    from select_subset import repo_of

    assert repo_of("scikit-learn__scikit-learn-13779") == "scikit-learn/scikit-learn"
    assert repo_of("sphinx-doc__sphinx-7889") == "sphinx-doc/sphinx"


def test_deriving_writes_the_list_and_its_own_meta_and_never_overwrites(tmp_path):
    from select_subset import main

    parent = tmp_path / "p.txt"
    parent.write_text("".join(f"{i}\n" for i in PARENT))
    argv = ["derive", "--name", "half", "--parent", str(parent), "--out-dir", str(tmp_path),
            "--quota", "django/django=9", "--quota", "sympy/sympy=7",
            "--quota", "scikit-learn/scikit-learn=5"]  # fmt: skip
    assert main(argv) == 0
    meta = json.loads((tmp_path / "half.meta.json").read_text())
    assert meta["count"] == 21 and meta["seed"] == 42 and meta["parent_count"] == 41
    first = (tmp_path / "half.txt").read_text()
    with pytest.raises(FileExistsError):
        main(argv)
    assert (tmp_path / "half.txt").read_text() == first
