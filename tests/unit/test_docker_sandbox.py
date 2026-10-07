"""Integration tests: need Docker and the harness-toy image (see tests/conftest.py)."""

import time

import pytest
from conftest import TOY_IMAGE

from harness.sandbox.docker_sandbox import LABEL, DockerSandbox

pytestmark = pytest.mark.docker


@pytest.fixture
async def sb(docker_client):
    async with DockerSandbox(
        TOY_IMAGE, workdir="/testbed", docker_client=docker_client
    ) as s:
        yield s


async def test_exec_merges_streams_and_returns_exit_code(sb):
    r = await sb.exec("echo out; echo err >&2; exit 3")
    assert r.exit_code == 3
    assert "out" in r.output and "err" in r.output


async def test_exec_runs_in_workdir_with_pager_disabled(sb):
    r = await sb.exec("pwd; echo $PAGER")
    assert r.output.split() == ["/testbed", "cat"]


async def test_exec_timeout_kills_the_whole_process_group(sb):
    t0 = time.time()
    r = await sb.exec("sleep 30 & sleep 30", timeout=2)
    assert r.exit_code == 124 and "timed out" in r.output
    assert time.time() - t0 < 15
    left = await sb.exec("grep -l '^sleep$' /proc/[0-9]*/comm 2>/dev/null | wc -l")
    assert left.output.strip() == "0"


async def test_network_is_disabled(sb):
    code = "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"
    r = await sb.exec(f'python -c "{code}"')
    assert r.exit_code != 0


async def test_limits_and_privileges(sb):
    r = await sb.exec(
        "cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.max; grep NoNewPrivs /proc/self/status"
    )
    pids, mem, nnp = (
        r.output.split("\n")[0],
        r.output.split("\n")[1],
        r.output.split("\n")[2],
    )
    assert pids == "512" and mem == str(4 * 1024**3) and nnp.split()[-1] == "1"


async def test_write_then_read_roundtrip_with_unicode_and_nested_dirs(sb):
    await sb.write_file("a/b/c.txt", "héllo\n世界\n")
    assert await sb.read_file("/testbed/a/b/c.txt") == "héllo\n世界\n"
    r = await sb.exec("stat -c '%U %a' a/b/c.txt")
    assert r.output.strip() == "root 644"


async def test_write_preserves_an_existing_files_mode(sb):
    await sb.exec("printf '#!/bin/sh\\n' > run.sh && chmod 755 run.sh")
    await sb.write_file("run.sh", "#!/bin/sh\necho changed\n")
    assert (await sb.exec("stat -c %a run.sh")).output.strip() == "755"
    assert (await sb.exec("./run.sh")).output.strip() == "changed"


async def test_read_missing_file_and_directory(sb):
    with pytest.raises(FileNotFoundError):
        await sb.read_file("nope.txt")
    with pytest.raises(IsADirectoryError):
        await sb.read_file("/testbed")


async def _repo(sb, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    await sb.load_repo(tmp_path)


async def test_load_repo_makes_a_clean_git_repo(sb, tmp_path):
    await _repo(sb, tmp_path)
    assert (await sb.exec("git status --porcelain")).output == ""
    assert await sb.git_diff() == ""


async def test_git_diff_contains_modified_new_deleted_files_but_not_ignored(
    sb, tmp_path
):
    await _repo(sb, tmp_path)
    await sb.write_file("pkg/mod.py", "x = 2\n")  # modified
    await sb.write_file("pkg/new.py", "y = 1\n")  # new, untracked
    await sb.exec("rm keep.txt")  # deleted
    await sb.write_file("debug.log", "ignored\n")  # ignored by .gitignore
    diff = await sb.git_diff()
    assert "+x = 2" in diff and "-x = 1" in diff
    assert "pkg/new.py" in diff and "+y = 1" in diff
    assert "deleted file mode" in diff and "keep.txt" in diff
    assert "debug.log" not in diff


async def test_git_diff_does_not_touch_the_real_index(sb, tmp_path):
    await _repo(sb, tmp_path)
    await sb.write_file("pkg/new.py", "y = 1\n")
    await sb.git_diff()
    assert (await sb.exec("git diff --cached --name-only")).output == ""
    assert "pkg/new.py" in (await sb.exec("git status --porcelain")).output


async def test_diff_applies_to_a_fresh_checkout(sb, tmp_path):
    await _repo(sb, tmp_path)
    await sb.write_file("pkg/mod.py", "x = 2\n")
    await sb.write_file("pkg/new.py", "y = 1\n")
    await sb.write_file("/tmp/p.diff", await sb.git_diff())
    await sb.exec("git checkout -q -- . && git clean -fdq")
    r = await sb.exec(
        "git apply --check /tmp/p.diff && git apply /tmp/p.diff && cat pkg/mod.py pkg/new.py"
    )
    assert r.output.split() == ["x", "=", "2", "y", "=", "1"]


async def test_container_is_removed_on_exit_even_after_an_error(docker_client):
    sandbox = DockerSandbox(TOY_IMAGE, docker_client=docker_client)
    with pytest.raises(RuntimeError, match="boom"):
        async with sandbox:
            raise RuntimeError("boom")
    left = docker_client.containers.list(
        all=True, filters={"label": f"{LABEL}={sandbox.sandbox_id}"}
    )
    assert left == []


async def test_loading_the_same_repository_twice_gives_the_same_commit_and_dates(
    docker_client, tmp_path
):
    (tmp_path / "a.py").write_text("x = 1\n")
    seen = []
    for _ in range(2):
        async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as s:
            await s.load_repo(tmp_path)
            seen.append(
                (
                    await s.exec(
                        "git log -1 --format='%H %ad'; ls -l --time-style=long-iso a.py"
                    )
                ).output
            )
    assert (
        seen[0] == seen[1]
        and "Sat Jan 1 00:00:00 2000" in seen[0]
        and "2000-01-01" in seen[0]
    )
