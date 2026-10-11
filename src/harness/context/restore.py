"""The state block: what a summary carries that the summarizer does not have to remember.

The summary text is written by a model from the history. Some things are better not left to
its memory: what the patch looks like right now, the plan, and where the full output of an
earlier tool call is saved. The harness knows these exactly, so it writes them itself, below
the summary and clearly marked as generated. (Claude Code re-reads recent files and git status
after compaction; here the sandbox's own `git diff` serves, because the thing being judged is
the patch.)

The block is never fed to the next summarizer: it is a snapshot of one moment and is rebuilt
from the sandbox every time (see `summary_text_of`).

Sections, in the order they are dropped when the block does not fit its cap:
file paths first, then the saved-output index, then the patch text, then the plan; the
`git diff --stat` is always kept.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from harness.config import ContextConfig
from harness.context.strategies.truncate import cleared_output
from harness.context.strategies.types import RESTORE_MIN_TOKENS
from harness.context.tokens import TokenEstimator
from harness.llm.types import Message
from harness.sandbox.base import Sandbox
from harness.tools.base import OUT_DIR, saved_output

STATE_MARKER = "\n\n## State as of step "
MAX_LINE = 120  # chars of one index line
MAX_STAT_FILES = 40
_READ_TOOLS = ("view", "str_replace")  # tools whose `path` argument is a file


# ---- the index of saved outputs and viewed files -------------------------------------------
@dataclass(frozen=True)
class IndexEntry:
    path: str
    chars: int
    label: str


def _args(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _label(name: str, args: dict[str, Any]) -> str:
    if name == "bash":
        return "bash: " + " ".join(str(args.get("command", "")).split())[:60]
    if name == "view":
        return f"view {args.get('path', '')}"
    if name == "search":
        return f"search {args.get('pattern', '')!r}"
    return name


@dataclass
class RestoreIndex:
    """Where the full outputs of dropped tool calls are saved, and which files were viewed.
    Kept across compactions (the dropped messages are gone for the next one) and rebuilt, not
    parsed back from a summary's text."""

    entries: dict[str, IndexEntry] = field(
        default_factory=dict
    )  # by path, oldest first
    viewed: dict[str, None] = field(default_factory=dict)  # ordered set, oldest first

    def with_range(self, original: list[Message], shown: list[Message]) -> RestoreIndex:
        """A new index that also covers the stretch about to be dropped. `original` and
        `shown` are the same messages as the agent saw them and as the model was sent them:
        a limiter note is in the first, a `truncate` stub only in the second."""
        out = RestoreIndex(dict(self.entries), dict(self.viewed))
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        for orig, view in zip(original, shown, strict=True):
            role = orig.get("role")
            if role == "assistant":
                for tc in orig.get("tool_calls") or []:
                    fn = tc["function"]
                    args = _args(fn["arguments"])
                    calls[tc["id"]] = (fn["name"], args)
                    path = args.get("path")
                    if fn["name"] in _READ_TOOLS and isinstance(path, str):
                        out.viewed.pop(path, None)  # a later view moves it to the end
                        out.viewed[path] = None
            elif role == "tool":
                label = _label(*calls.get(orig.get("tool_call_id"), ("tool", {})))
                found = saved_output(str(orig.get("content", ""))) or cleared_output(
                    str(view.get("content", ""))
                )
                if found:
                    path, chars = found
                    out.entries.pop(path, None)
                    out.entries[path] = IndexEntry(path, chars, label)
        return out


# ---- the patch ---------------------------------------------------------------------------------
def diff_stat(diff: str) -> str:
    """A `git diff --stat` like summary, computed from the patch text itself."""
    files: list[list[Any]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/.* b/(.*)$", line)
            files.append([m[1] if m else line[11:], 0, 0])
        elif files and line.startswith("+") and not line.startswith("+++"):
            files[-1][1] += 1
        elif files and line.startswith("-") and not line.startswith("---"):
            files[-1][2] += 1
    if not files:
        return (
            "no changes yet" if not diff.strip() else "(the patch is not in git format)"
        )
    lines = [f" {p} | +{a} -{d}" for p, a, d in files[:MAX_STAT_FILES]]
    if len(files) > MAX_STAT_FILES:
        lines.append(f" (+{len(files) - MAX_STAT_FILES} more files)")
    adds, dels = sum(f[1] for f in files), sum(f[2] for f in files)
    lines.append(f"{len(files)} file(s) changed, +{adds} -{dels}")
    return "\n".join(lines)


def _clip_diff(diff: str, chars: int) -> str:
    if chars <= 0 or not diff.strip():
        return ""
    if len(diff) <= chars:
        return diff.rstrip()
    return f"{diff[:chars].rstrip()}\n[... {len(diff) - chars} more chars; run `git diff` for the rest]"


# ---- the block ---------------------------------------------------------------------------------
@dataclass
class Block:
    text: str = ""
    parts: dict[str, Any] = field(default_factory=dict)  # what went in, for the span
    tokens: int = 0


@dataclass(frozen=True)
class _Shape:
    viewed: int
    index: int
    diff: int
    todo: bool


def _shapes(cfg: ContextConfig, minimal: bool) -> list[_Shape]:
    full = _Shape(
        cfg.summary_viewed_files,
        cfg.summary_index_entries,
        cfg.summary_diff_chars,
        True,
    )
    ladder = [
        full,
        _Shape(0, full.index, full.diff, True),  # 1. drop the file paths
        _Shape(0, min(10, full.index), full.diff, True),  # 2. shorten the index
        _Shape(0, 0, full.diff, True),  # 3. drop the index
        _Shape(0, 0, full.diff // 2, True),  # 4. cut the patch text
        _Shape(0, 0, full.diff // 4, True),
        _Shape(0, 0, 0, True),  # 5. no patch text
        _Shape(0, 0, 0, False),  # 6. no plan: only the stat is left
    ]
    return ladder[5:] if minimal else ladder


def _line(entry: IndexEntry) -> str:
    return f"- {entry.path} ({entry.chars} chars) {entry.label}"[:MAX_LINE]


def _assemble(
    step: int,
    stat: str,
    diff_body: str,
    todo: str | None,
    index: RestoreIndex,
    shape: _Shape,
) -> tuple[str, dict[str, Any]]:
    parts: dict[str, Any] = {"stat_chars": len(stat), "diff_chars": len(diff_body)}
    header = (
        f"{STATE_MARKER.lstrip()}{step}\n"
        "(generated from the sandbox by the harness, not written by the summarizer; "
        "it is a snapshot and may be out of date)"
    )
    out = [
        header,
        "### Changes so far (git diff)",
        stat,
    ]
    if diff_body:
        out.append(diff_body)
    if shape.todo and todo:
        out += ["### Plan (todo)", todo]
        parts["todo_chars"] = len(todo)
    entries = list(index.entries.values())
    if shape.index and entries:
        shown = entries[-shape.index :]
        older = len(entries) - len(shown)
        out.append("### Saved outputs (full text; read with cat or view)")
        if older:
            out.append(f"(+{older} older saved outputs: run `ls {OUT_DIR}`)")
        out += [_line(e) for e in shown]
        parts["index_lines"], parts["index_older"] = len(shown), older
    paths = list(index.viewed)[-shape.viewed :] if shape.viewed else []
    if paths:
        out += ["### Files viewed or edited", *[f"- {p}" for p in paths]]
        parts["viewed_files"] = len(paths)
    return "\n".join(out), parts


async def build_block(
    *,
    step: int,
    sandbox: Sandbox,
    todo: str | None,
    index: RestoreIndex,
    cfg: ContextConfig,
    estimator: TokenEstimator,
    minimal: bool,
) -> Block:
    """The state block for a summary written at `step`, within its token cap. If the block
    does not fit, parts are dropped in a fixed order and the stat is kept."""
    cap = RESTORE_MIN_TOKENS if minimal else cfg.restore_tokens_cap()
    try:
        diff = await sandbox.git_diff()
    except Exception as e:  # noqa: BLE001 - a sandbox that cannot diff must not stop a summary
        diff, stat_text = "", f"(the patch is unavailable: {type(e).__name__})"
    else:
        stat_text = diff_stat(diff)
    text, parts, tokens = "", {}, 0
    for n, shape in enumerate(_shapes(cfg, minimal)):
        text, parts = _assemble(
            step, stat_text, _clip_diff(diff, shape.diff), todo, index, shape
        )
        tokens = await estimator.text_tokens(text)
        if tokens <= cap:
            parts["shape"] = n
            break
    else:  # even the stat is too long: cut it hard
        text, parts["clipped"] = text[: cap * 3], True
        tokens = await estimator.text_tokens(text)
    return Block(
        text, {**parts, "tokens": tokens, "cap_tokens": cap, "minimal": minimal}, tokens
    )
