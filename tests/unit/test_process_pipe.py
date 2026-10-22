"""sandbox/docker_sandbox.py: the stdin-attached process for MCP servers (FrameDemuxer,
DockerProcessPipe)."""

import asyncio

import pytest

from harness.sandbox.docker_sandbox import DockerSandbox, FrameDemuxer

TOY_IMAGE = "harness-toy:latest"


def frame(stream, payload):
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


STREAM = (
    frame(1, b'{"a":1}\n') + frame(2, b"log line\n") + frame(1, b'{"b":2}\n{"c":3}\n')
)
EXPECTED = [(1, b'{"a":1}\n'), (2, b"log line\n"), (1, b'{"b":2}\n{"c":3}\n')]


def test_whole_frames_in_one_read():
    assert FrameDemuxer().feed(STREAM) == EXPECTED


@pytest.mark.parametrize("cut", [1, 3, 7, 8, 9, 15, 16, 17, 30])
def test_a_read_can_end_anywhere_inside_a_header_or_a_payload(cut):
    d = FrameDemuxer()
    got = d.feed(STREAM[:cut]) + d.feed(STREAM[cut:])
    assert got == EXPECTED


def test_one_byte_at_a_time():
    d = FrameDemuxer()
    got = [f for i in range(len(STREAM)) for f in d.feed(STREAM[i : i + 1])]
    assert got == EXPECTED


def test_an_empty_payload_frame():
    assert FrameDemuxer().feed(frame(1, b"") + frame(1, b"x")) == [(1, b""), (1, b"x")]


# ---- a real container -----------------------------------------------------------------------------
async def read_until(pipe, needle, timeout=10):
    out = b""
    async with asyncio.timeout(timeout):
        while needle not in out:
            chunk = await pipe.read()
            if not chunk:
                break
            out += chunk
    return out


@pytest.mark.docker
async def test_stdin_and_stdout_reach_a_process_in_the_container(docker_client):
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        pipe = await sb.open_process(["sh", "-c", "echo started >&2; cat"])
        await pipe.send(b'{"a":1}\n')
        await pipe.send(b'{"b":2}\n')
        out = await read_until(pipe, b'{"b":2}\n')
        assert out == b'{"a":1}\n{"b":2}\n'  # stderr never mixes into stdout
        assert "started" in pipe.stderr_tail()
        await pipe.close()
        assert await pipe.read() == b""


@pytest.mark.docker
async def test_closing_wakes_a_pending_read(docker_client):
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        pipe = await sb.open_process(["cat"])
        reader = asyncio.create_task(pipe.read())
        await asyncio.sleep(0.2)
        await pipe.close()
        async with asyncio.timeout(5):
            assert await reader == b""


@pytest.mark.docker
async def test_a_process_that_exits_ends_the_stream(docker_client):
    async with DockerSandbox(TOY_IMAGE, docker_client=docker_client) as sb:
        pipe = await sb.open_process(["sh", "-c", "echo bye; exit 3"])
        assert await read_until(pipe, b"bye\n") == b"bye\n"
        async with asyncio.timeout(5):
            assert await pipe.read() == b""
        await pipe.close()
