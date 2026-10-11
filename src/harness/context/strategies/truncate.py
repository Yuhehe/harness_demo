"""`truncate`: replace old tool OUTPUTS with a stub, keep every call record.

    [output cleared: N chars, saved at <path>]

The model sees what it did and can read the output again from the saved file. The file always
holds the full original output: an output the limiter already cut is pointed at the file the
limiter wrote (tools/base.py `saved_path`); any other output was never cut, so what is in the
view is the original and it is written to a new file.
"""

from __future__ import annotations

import re

from harness.context.strategies.types import HEAD, Env, Report
from harness.llm.types import Message
from harness.tools.base import OUT_DIR, saved_path

# The stub that replaces a cleared output, and its parser. The state block of a summary
# (context/restore.py) reads the saved path back from a stub, so both share this definition.
STUB_FORMAT = "[output cleared: {chars} chars, saved at {path}]"
STUB_PREFIX = STUB_FORMAT.split("{")[0].rstrip()  # "[output cleared:"
_STUB_RE = re.compile(
    re.escape(STUB_FORMAT)
    .replace(re.escape("{chars}"), r"(?P<chars>\d+)")
    .replace(
        re.escape("{path}"),
        r"(?P<path>" + re.escape(OUT_DIR) + r"/(?:out|ctx)_\d+\.txt)",
    )
)


def cleared_stub(chars: int, path: str) -> str:
    return STUB_FORMAT.format(chars=chars, path=path)


def cleared_output(text: str) -> tuple[str, int] | None:
    """(sandbox path, original length) if `text` is a stub made by this strategy."""
    match = _STUB_RE.fullmatch(text.strip())
    return (match["path"], int(match["chars"])) if match else None


def is_cleared(m: Message) -> bool:
    return str(m.get("content", "")).startswith(STUB_PREFIX)


async def run(env: Env, *, target: int | None = None) -> Report | None:
    """Clear the oldest eligible outputs until the estimate reaches `target` (default: the
    low water mark), or until nothing eligible is left."""
    target = env.low if target is None else target
    cfg = env.cfg
    view = list(env.view)
    end = env.cut
    eligible = [
        i
        for i in range(HEAD, end)
        if view[i].get("role") == "tool"
        and not is_cleared(view[i])
        and len(str(view[i].get("content", ""))) >= cfg.min_clear_chars
    ]
    if not eligible:
        return None

    sizes = await env.estimator.local_each([view[i] for i in eligible])
    ratio = env.estimator.ratio
    est, cleared, chars = env.est, 0, 0
    for i, size in zip(eligible, sizes, strict=True):
        if est <= target:
            break
        old = str(view[i]["content"])
        path = saved_path(old)  # the limiter already saved the full output
        if path is None:
            path = env.next_path()
            await env.sandbox.write_file(path, old)
        stub = {
            **view[i],
            "content": cleared_stub(len(old), path),
        }
        (stub_size,) = await env.estimator.local_each([stub])
        view[i] = stub
        est -= round(max(size - stub_size, 0) * ratio)
        cleared += 1
        chars += len(old)
    return Report(
        view, ["truncate"], {"cleared_outputs": cleared, "cleared_chars": chars}
    )
