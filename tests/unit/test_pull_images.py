import json

import pull_images as pi
import pytest
from docker.errors import APIError, ImageNotFound


class FakeImages:
    def __init__(self, present=(), pull_errors=None):
        self.present = set(present)
        self.pull_errors = (
            pull_errors or {}
        )  # repo -> list of exceptions to raise in order
        self.pulled = []

    def get(self, name):
        if name not in self.present:
            raise ImageNotFound("nope")
        return object()

    def pull(self, repo, tag="latest", platform=None):
        errs = self.pull_errors.get(repo, [])
        if errs:
            raise errs.pop(0)
        assert platform == pi.PLATFORM
        self.present.add(f"{repo}:{tag}")
        self.pulled.append(f"{repo}:{tag}")


class FakeContainers:
    def __init__(self, out=b""):
        self.out, self.kwargs = out, None

    def run(self, image, cmd, **kwargs):
        self.kwargs = kwargs
        return self.out


class FakeClient:
    def __init__(self, images, containers=None):
        self.images, self.containers = images, containers or FakeContainers()


def make_rows(n=4):
    rows = {}
    for i in range(n):
        iid = f"repo__repo-{i}"
        rows[iid] = {
            "instance_id": iid,
            "image": pi.expected_image(iid),
            "base_commit": "c" * 40,
        }
    return rows


def test_expected_image_rule():
    assert (
        pi.expected_image("astropy__astropy-12907")
        == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    )
    assert (
        pi.expected_image("Foo__Bar-1")
        == "swebench/sweb.eval.x86_64.foo_1776_bar-1:latest"
    )


def test_pull_one_retries_then_succeeds():
    images = FakeImages(pull_errors={"a/b": [APIError("boom"), APIError("boom")]})
    sleeps = []
    out = pi.pull_one(
        lambda: FakeClient(images), "a/b:latest", retries=3, sleep=sleeps.append
    )
    assert out["state"] == "done" and out["attempts"] == 3
    assert sleeps == [pi.BACKOFF_S, pi.BACKOFF_S * 2]


def test_pull_one_gives_up_after_retries():
    images = FakeImages(pull_errors={"a/b": [APIError("x")] * 5})
    out = pi.pull_one(
        lambda: FakeClient(images), "a/b:latest", retries=2, sleep=lambda s: None
    )
    assert out["state"] == "failed" and out["attempts"] == 2


def test_pull_one_not_found_is_not_retried():
    images = FakeImages(pull_errors={"a/b": [ImageNotFound("gone")]})
    out = pi.pull_one(lambda: FakeClient(images), "a/b:latest", sleep=lambda s: None)
    assert out["state"] == "failed" and out["attempts"] == 1


def test_pull_one_rate_limit_is_reported():
    images = FakeImages(pull_errors={"a/b": [APIError("toomanyrequests: slow down")]})
    out = pi.pull_one(lambda: FakeClient(images), "a/b:latest", sleep=lambda s: None)
    assert out["state"] == "rate_limited" and out["attempts"] == 1


def test_run_pulls_missing_and_resumes(tmp_path):
    rows = make_rows()
    ids = sorted(rows)
    images = FakeImages(present=[rows[ids[0]]["image"]])
    status_path = tmp_path / "s.json"
    kw = {"status_path": status_path, "workers": 2, "free_gb_fn": lambda p: 100.0}
    status = pi.run(ids, rows, lambda: FakeClient(images), **kw)
    assert sorted(images.pulled) == sorted(rows[i]["image"] for i in ids[1:])
    assert all(status[i]["state"] == "done" for i in ids)
    assert json.loads(status_path.read_text()) == status
    images.pulled.clear()
    pi.run(ids, rows, lambda: FakeClient(images), **kw)
    assert images.pulled == []  # everything present -> nothing to do


def test_run_stops_at_disk_floor(tmp_path):
    rows = make_rows()
    ids = sorted(rows)
    images = FakeImages()
    status = pi.run(
        ids,
        rows,
        lambda: FakeClient(images),
        status_path=tmp_path / "s.json",
        workers=1,
        min_free_gb=20,
        free_gb_fn=lambda p: 5.0,
    )
    assert images.pulled == [] and status == {}


def test_run_stops_on_rate_limit(tmp_path):
    rows = make_rows()
    ids = sorted(rows)
    images = FakeImages()

    def limited(client_factory, image):
        return {"state": "rate_limited", "attempts": 1, "error": "429"}

    status = pi.run(
        ids,
        rows,
        lambda: FakeClient(images),
        status_path=tmp_path / "s.json",
        workers=1,
        free_gb_fn=lambda p: 100.0,
        pull=limited,
    )
    assert [status[i]["state"] for i in status] == [
        "rate_limited"
    ]  # the rest never started


def verify_output(head, parent, dirty=0, rg="yes"):
    return f"head={head}\nparent={parent}\ndirty={dirty}\nrg={rg}\n".encode()


def test_verify_one_uses_safe_container_settings():
    # image HEAD is an empty commit on top of base_commit: base_commit is HEAD^
    good = FakeClient(FakeImages(), FakeContainers(verify_output("e" * 40, "c" * 40)))
    out = pi.verify_one(good, "img", "c" * 40)
    assert out == {"base_commit_ok": True, "clean": True, "has_rg": True}
    kw = good.containers.kwargs
    assert kw["network_mode"] == "none" and kw["remove"] is True and kw["pids_limit"]
    bad = FakeClient(
        FakeImages(),
        FakeContainers(verify_output("e" * 40, "d" * 40, dirty=2, rg="no")),
    )
    assert pi.verify_one(bad, "img", "c" * 40) == {
        "base_commit_ok": False,
        "clean": False,
        "has_rg": False,
    }
    at_head = FakeClient(FakeImages(), FakeContainers(verify_output("c" * 40, "none")))
    assert pi.verify_one(at_head, "img", "c" * 40)["base_commit_ok"] is True


def test_run_verify_records_result(tmp_path):
    rows = make_rows(1)
    iid = next(iter(rows))
    images = FakeImages(present=[rows[iid]["image"]])
    containers = FakeContainers(verify_output("e" * 40, "c" * 40, rg="no"))
    status = pi.run(
        [iid],
        rows,
        lambda: FakeClient(images, containers),
        status_path=tmp_path / "s.json",
        verify=True,
        free_gb_fn=lambda p: 100.0,
    )
    assert status[iid]["base_commit_ok"] is True and status[iid]["has_rg"] is False


@pytest.mark.parametrize(
    "states,code", [(["done", "done"], 0), (["done", "failed"], 1)]
)
def test_summarize_exit_code(states, code):
    ids = ["a", "b"]
    status = {i: {"state": s} for i, s in zip(ids, states)}
    assert pi.summarize(ids, status, verify=False) == code
