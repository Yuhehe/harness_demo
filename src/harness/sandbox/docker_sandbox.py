"""Docker sandbox. Model-produced commands run only inside the container (CLAUDE.md rule 2).

The Docker SDK is blocking, so every call goes through asyncio.to_thread. The host never
builds a shell string out of model output: commands reach the container as an argv list.

Conventions the rest of the code relies on:
* Exit code 124 means the command timed out. It runs under the container's `timeout`, because
  the SDK's exec_run has no timeout of its own.
* The container is created with init=True, so PID 1 reaps the zombies a timed-out command
  leaves behind (they would otherwise use up pids_limit).
* git_diff rebuilds a temporary index with `git read-tree HEAD` every time, so it includes
  new files and is not fooled by git's stat cache (a same-size rewrite within one second).
* load_repo copies a local repository in with fixed file times and commit dates, so two loads
  are byte-identical and a recording replays.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any, Self

import docker
from docker.errors import NotFound

from harness.sandbox.base import ExecResult

DEFAULT_ENV = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "LESS": "-R",
    "PIP_PROGRESS_BAR": "off",
    "TQDM_DISABLE": "1",
}
LABEL = "harness-lab-sandbox"
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
GIT_IDENT = ["-c", "user.name=harness", "-c", "user.email=harness@example.invalid"]
# Fixed so that loading the same repository twice gives byte-identical state (same commit hash,
# same `ls -l` dates). Recorded runs are replayed against this, so it must not depend on today.
FIXED_EPOCH = 946_684_800  # 2000-01-01T00:00:00Z
FIXED_GIT_DATE = "2000-01-01T00:00:00+0000"


class SandboxError(RuntimeError):
    pass


class DockerSandbox:
    def __init__(
        self,
        image: str,
        *,
        workdir: str = "/testbed",
        network_mode: str = "none",
        mem_limit: str = "4g",
        cpus: float = 2.0,
        pids_limit: int = 512,
        env: dict[str, str] | None = None,
        docker_client: Any | None = None,
    ) -> None:
        self.image = image
        self.workdir = workdir
        self.network_mode = network_mode
        self.mem_limit = mem_limit
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.env = {**DEFAULT_ENV, **(env or {})}
        self._client = docker_client
        self._container: Any = None
        self.sandbox_id = uuid.uuid4().hex[:8]

    # ---- lifecycle -----------------------------------------------------------------------
    async def __aenter__(self) -> Self:
        try:
            await self.start()
        except BaseException:
            await self.close()  # never leak a half-started container
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = await asyncio.to_thread(docker.from_env)

        def run() -> Any:
            return self._client.containers.run(
                self.image,
                command=["tail", "-f", "/dev/null"],
                name=f"harness-{self.sandbox_id}",
                detach=True,
                working_dir=self.workdir,
                network_mode=self.network_mode,
                mem_limit=self.mem_limit,
                memswap_limit=self.mem_limit,  # no swap on top of the memory limit
                nano_cpus=int(self.cpus * 1e9),
                pids_limit=self.pids_limit,
                privileged=False,
                init=True,  # PID 1 reaps the orphans a timed-out command leaves behind
                security_opt=["no-new-privileges"],
                labels={LABEL: self.sandbox_id},
            )

        self._container = await asyncio.to_thread(run)
        # Files copied in belong to root and the repo is owned by whoever built the image:
        # git must not refuse to work because of "dubious ownership".
        await self._raw(["git", "config", "--global", "--add", "safe.directory", "*"])

    async def close(self) -> None:
        container, self._container = self._container, None
        if container is None:
            return

        def remove() -> None:
            try:
                container.remove(force=True)
            except NotFound:
                pass

        await asyncio.to_thread(remove)

    # ---- exec ----------------------------------------------------------------------------
    async def _raw(self, argv: list[str]) -> tuple[int, bytes]:
        if self._container is None:
            raise SandboxError("sandbox is not running")

        def run() -> tuple[int, bytes]:
            res = self._container.exec_run(
                argv, workdir=self.workdir, environment=self.env
            )
            return (
                res.exit_code if res.exit_code is not None else -1
            ), res.output or b""

        return await asyncio.to_thread(run)

    async def exec(self, cmd: str, timeout: int = 120) -> ExecResult:
        # The Docker API has no exec timeout, so the container's own `timeout` enforces it
        # (it signals the whole process group, then SIGKILLs 5 s later).
        code, out = await self._raw(
            ["timeout", "-k", "5", str(timeout), "bash", "-lc", cmd]
        )
        text = out.decode("utf-8", errors="replace")
        if code in (124, 137):
            text += f"\n[command timed out after {timeout}s and was killed]"
            code = 124
        return ExecResult(code, text)

    async def open_process(self, argv: list[str]) -> DockerProcessPipe:
        """`argv` in the container with stdin attached (an MCP server, tools/mcp.py): the
        Docker SDK's exec socket, not a host-side `docker exec`."""
        if self._container is None:
            raise SandboxError("sandbox is not running")
        api = self._client.api

        def start() -> Any:
            exec_id = api.exec_create(
                self._container.id,
                argv,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                workdir=self.workdir,
                environment=self.env,
            )["Id"]
            sock = api.exec_start(exec_id, socket=True)
            return getattr(sock, "_sock", sock)  # SocketIO on a Unix socket

        return DockerProcessPipe(await asyncio.to_thread(start))

    # ---- files ---------------------------------------------------------------------------
    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else posixpath.join(self.workdir, path)

    async def read_file(self, path: str) -> str:
        path = self._abs(path)

        def get() -> bytes:
            stream, _ = self._container.get_archive(path)
            return b"".join(stream)

        try:
            data = await asyncio.to_thread(get)
        except NotFound:
            raise FileNotFoundError(path) from None
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            member = tar.next()
            if member is None or not member.isfile():
                raise IsADirectoryError(path)
            fh = tar.extractfile(member)
            assert fh is not None
            return fh.read().decode("utf-8", errors="replace")

    async def _put(self, dest_dir: str, data: bytes) -> None:
        await asyncio.to_thread(self._container.put_archive, dest_dir, data)

    async def write_file(self, path: str, content: str) -> None:
        path = self._abs(path)
        parent, name = posixpath.split(path)
        await self._raw(["mkdir", "-p", parent])
        code, out = await self._raw(
            ["stat", "-c", "%a", path]
        )  # keep an existing file's mode
        mode = int(out.decode().strip(), 8) if code == 0 and out.strip() else 0o644
        payload = content.encode("utf-8")
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(payload), mode, int(time.time())
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.addfile(info, io.BytesIO(payload))
        await self._put(parent, buf.getvalue())

    # ---- git -----------------------------------------------------------------------------
    async def git_diff(self) -> str:
        # `git diff` alone omits files the agent created. Stage everything into a TEMPORARY
        # index so the repo's real index is left exactly as the agent left it.
        # The index is rebuilt from HEAD every time: a copied index carries stat data, and a
        # same-size edit within the same second as the last `git add` would look unchanged.
        script = (
            "export GIT_INDEX_FILE=/tmp/harness_index; rm -f $GIT_INDEX_FILE; "
            "git read-tree HEAD && git add -A >/dev/null 2>&1 && "
            "git diff --cached --no-color --no-ext-diff HEAD"
        )
        code, out = await self.exec(script, timeout=60)
        if code != 0:
            raise SandboxError(f"git diff failed ({code}): {out[:300]}")
        return out

    # ---- loading a local repository (toy repo and the CLI) -------------------------------
    async def load_repo(self, host_dir: str | Path) -> None:
        """Copy a host directory into the workdir and make it a git repo with one commit."""
        root = Path(host_dir)

        def pack() -> bytes:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for p in sorted(root.rglob("*")):
                    rel = p.relative_to(root)
                    if _SKIP_DIRS & set(rel.parts) or p.suffix == ".pyc":
                        continue
                    info = tar.gettarinfo(str(p), arcname=rel.as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = FIXED_EPOCH
                    if p.is_file():
                        with p.open("rb") as fh:
                            tar.addfile(info, fh)
                    else:
                        tar.addfile(info)
            return buf.getvalue()

        await self._raw(["mkdir", "-p", self.workdir])
        await self._put(self.workdir, await asyncio.to_thread(pack))
        dates = f"GIT_AUTHOR_DATE={FIXED_GIT_DATE} GIT_COMMITTER_DATE={FIXED_GIT_DATE}"
        script = (
            "git init -q && git add -A && "
            f"{dates} git {' '.join(GIT_IDENT)} commit -q -m base --allow-empty"
        )
        code, out = await self.exec(script, timeout=60)
        if code != 0:
            raise SandboxError(f"git init failed: {out[:300]}")


class FrameDemuxer:
    """Docker's multiplexed exec stream without a TTY: frames of an 8-byte header (stream id:
    1 stdout, 2 stderr; 3 bytes of zeros; a big-endian 4-byte length) and the payload. A
    socket read can end anywhere, inside a header or a payload, so partial frames wait here."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        frames = []
        while len(self._buf) >= 8:
            size = int.from_bytes(self._buf[4:8], "big")
            if len(self._buf) < 8 + size:
                break
            frames.append((self._buf[0], self._buf[8 : 8 + size]))
            self._buf = self._buf[8 + size :]
        return frames


class DockerProcessPipe:
    """sandbox.base.ProcessPipe over a Docker exec socket."""

    STDERR_KEEP = 4000

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._demux = FrameDemuxer()
        self._stdout: list[bytes] = []
        self._stderr = ""
        self._closed = False

    async def send(self, data: bytes) -> None:
        await asyncio.to_thread(self._sock.sendall, data)

    async def read(self) -> bytes:
        while not self._stdout:
            if self._closed:
                return b""
            try:
                data = await asyncio.to_thread(self._sock.recv, 65536)
            except OSError:
                data = b""
            if not data:
                return b""
            for stream, payload in self._demux.feed(data):
                if stream == 1:
                    self._stdout.append(payload)
                elif stream == 2:
                    text = self._stderr + payload.decode("utf-8", errors="replace")
                    self._stderr = text[-self.STDERR_KEEP :]
        return self._stdout.pop(0)

    def stderr_tail(self) -> str:
        return self._stderr

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        import socket

        try:
            # shutdown wakes a recv blocked in another thread; close alone does not
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
