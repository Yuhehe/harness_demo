"""Runs only where the dev_5 django image is already pulled (it is 4 GB, never pulled by tests)."""

import time

import pytest

from harness.sandbox.docker_sandbox import DockerSandbox

IMAGE = "swebench/sweb.eval.x86_64.django_1776_django-11066:latest"
pytestmark = [pytest.mark.docker, pytest.mark.swebench_image]


@pytest.fixture
async def sb(docker_client):
    try:
        docker_client.images.get(IMAGE)
    except Exception:  # noqa: BLE001
        pytest.skip(f"{IMAGE} is not pulled")
    async with DockerSandbox(IMAGE, docker_client=docker_client) as s:
        yield s


async def test_clean_checkout_has_an_empty_diff_and_the_conda_env_is_active(sb):
    assert await sb.git_diff() == ""
    r = await sb.exec("python --version")
    assert "3.6" in r.output  # the instance's own environment, not a system python


async def test_patch_has_new_and_modified_files_and_applies_to_a_fresh_checkout(sb):
    original = await sb.read_file("/testbed/django/__init__.py")
    await sb.write_file(
        "/testbed/django/__init__.py", original + "# touched by the agent\n"
    )
    await sb.write_file("/testbed/reproduce_issue.py", "print('repro')\n")
    t0 = time.time()
    diff = await sb.git_diff()
    elapsed = time.time() - t0
    assert "+# touched by the agent" in diff
    assert "reproduce_issue.py" in diff and "new file mode" in diff
    assert (
        elapsed < 30
    )  # rebuilding the index hashes the whole tree; stays cheap on django

    await sb.write_file("/tmp/agent.patch", diff)
    await sb.exec("git checkout -q -- . && git clean -fdq")
    r = await sb.exec(
        "git apply --check /tmp/agent.patch && git apply /tmp/agent.patch && git status --porcelain"
    )
    assert r.exit_code == 0
    assert "reproduce_issue.py" in r.output and "django/__init__.py" in r.output
