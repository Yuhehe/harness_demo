import pytest

from harness.cli import build_parser, main


def test_repo_and_instance_are_mutually_exclusive_and_one_is_required():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["run"])
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--repo", "x", "--instance", "y"])
    assert p.parse_args(["run", "--repo", "x"]).repo is not None


def test_record_and_replay_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["run", "--repo", "x", "--record", "a", "--replay", "b"]
        )


def test_repo_mode_needs_a_task():
    with pytest.raises(SystemExit, match="--task is required"):
        main(["run", "--repo", "tests/fixtures/toy_repo"])
