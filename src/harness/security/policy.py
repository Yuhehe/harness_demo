"""The permission policy: loading configs/policy.yaml and matching it (CLAUDE.md 5.6)."""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

Scope = Literal["bash", "path_read", "path_write"]
_SEGMENT = re.compile(r";|&&|\|\||\||&|\n")
_SUBSTITUTION = re.compile(r"[<>`]|\$\(")
# write targets of a command (see Policy.write_targets)
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_UNKNOWN = re.compile(r"[$*?`{~\[]")
_SEPARATORS = set(";&|()\n")
# a command that may write, for text shlex cannot split
_WRITE_HINT = re.compile(r">|\btee\b|\b(sed|perl)\b[^\n]*\s-\w*i|\b(cp|mv|install)\b")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleSpec(_Strict):
    id: str
    scope: list[Scope]
    regex: str = ""
    use_test_path: bool = False  # match the policy's `test_path` instead of `regex`
    # `tracked`: the rule only holds when a file it would write is one the repository tracked
    # when the task started (Policy.protected_targets); without that list it holds as matched
    protect: Literal["tracked"] | None = None
    reason: str


class LevelSpec(_Strict):
    tools: list[str]
    bash_commands: list[str] = []
    roots: list[str] = []


class PolicySpec(_Strict):
    version: int
    allowed_levels: list[Literal["read", "write", "exec"]]
    levels: dict[Literal["read", "write", "exec"], LevelSpec]
    test_path: str
    deny: list[RuleSpec]
    ask: list[RuleSpec] = []


@dataclass(frozen=True)
class Rule:
    id: str
    action: str  # deny | ask
    scopes: frozenset[str]
    pattern: re.Pattern[str]
    reason: str
    protect: str | None = None

    def hits(self, scope: str, text: str) -> bool:
        return scope in self.scopes and self.pattern.search(text) is not None


class Policy:
    def __init__(self, spec: PolicySpec, workdir: str, text_hash: str) -> None:
        self.spec = spec
        self.workdir = workdir.rstrip("/") or "/"
        self.hash = text_hash
        escaped = re.escape(self.workdir)
        self.rules: list[Rule] = []
        for action, specs in (("deny", spec.deny), ("ask", spec.ask)):
            for r in specs:
                raw = spec.test_path if r.use_test_path else r.regex
                # Multi-line patterns are written as `|-` blocks: drop the line breaks and indentation
                # (they are not part of the pattern), then fill in {workdir}.
                pattern = re.sub(r"\s*\n\s*", "", raw).replace("{workdir}", escaped)
                self.rules.append(
                    Rule(
                        r.id,
                        action,
                        frozenset(r.scope),
                        re.compile(pattern),
                        r.reason,
                        r.protect,
                    )
                )
        self._test_path = re.compile(spec.test_path)
        self._tool_level = {
            tool: level for level, lv in spec.levels.items() for tool in lv.tools
        }
        self._read_cmds = tuple(spec.levels["read"].bash_commands)
        self._roots = [
            posixpath.normpath(r.replace("{workdir}", self.workdir))
            for r in spec.levels["write"].roots
        ]

    @classmethod
    def load(cls, path: str | Path, workdir: str) -> Policy:
        text = Path(path).read_text()
        spec = PolicySpec.model_validate(yaml.safe_load(text))
        return cls(spec, workdir, hashlib.sha256(text.encode()).hexdigest()[:12])

    def level_of_tool(self, tool: str) -> str | None:
        return self._tool_level.get(tool)

    def bash_level(self, command: str) -> str:
        """`read` for plain inspection commands, otherwise `exec`."""
        if _SUBSTITUTION.search(command):
            return "exec"
        for segment in _SEGMENT.split(command):
            seg = segment.strip()
            if seg and not any(
                seg == c or seg.startswith(c + " ") for c in self._read_cmds
            ):
                return "exec"
        return "read"

    def resolve(self, path: str) -> str:
        if path.startswith("~"):
            return path  # not expanded here; it is outside every root
        joined = path if path.startswith("/") else f"{self.workdir}/{path}"
        return posixpath.normpath(joined)

    def inside_write_roots(self, path: str) -> bool:
        p = self.resolve(path)
        return any(p == r or p.startswith(r.rstrip("/") + "/") for r in self._roots)

    @property
    def protects_tracked(self) -> bool:
        """Whether a rule needs the list of files the repository tracks (Guard.tracked)."""
        return any(r.protect == "tracked" for r in self.rules)

    def first_hit(self, scope: str, text: str) -> Rule | None:
        """The first matching rule; deny rules are listed before ask rules. A `protect:
        tracked` rule is matched by its pattern here; Guard decides whether it holds."""
        return next((r for r in self.rules if r.hits(scope, text)), None)

    def rel(self, path: str) -> str | None:
        """`path` relative to the repository, or None when it is outside it."""
        p = self.resolve(path)
        if p == self.workdir:
            return ""
        prefix = self.workdir.rstrip("/") + "/"
        return p[len(prefix) :] if p.startswith(prefix) else None

    def write_targets(self, command: str) -> list[str] | None:
        """The files a bash command writes: redirections, `tee`, `sed -i` / `perl -i`, and the
        destination of `cp` / `mv` / `install`; relative paths resolved against a preceding
        `cd <dir>`. Heredoc bodies are dropped first, then the command is split with shlex
        (quotes respected, operators as tokens). None when a target cannot be read from the
        text (a variable, a glob, or a write in a command shlex cannot split): the caller
        treats that as protected."""
        kept: list[str] = []
        body_end: str | None = None
        for line in command.split("\n"):
            if body_end is not None:
                if line.strip() == body_end:
                    body_end = None
                continue
            kept.append(line)
            heredoc = _HEREDOC.search(line)
            if heredoc:
                body_end = heredoc.group(2)
        text = "\n".join(kept)
        lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split = " \t\r", True
        try:
            tokens = list(lex)
        except ValueError:
            return None if _WRITE_HINT.search(text) else []
        targets: list[str] = []
        cwd = self.workdir
        segment: list[str] = []
        for tok in [*tokens, ";"]:
            if set(tok) <= _SEPARATORS:
                found, cwd = self._segment_targets(segment, cwd)
                if found is None:
                    return None
                targets += found
                segment = []
            else:
                segment.append(tok)
        return targets

    def _segment_targets(
        self, seg: list[str], cwd: str
    ) -> tuple[list[str] | None, str]:
        words: list[str] = []
        found: list[str] = []
        i = 0
        while i < len(seg):
            tok = seg[i]
            if set(tok) <= set("<>&|") and ("<" in tok or ">" in tok):
                if words and words[-1].isdigit():
                    words.pop()  # the file descriptor of `2>`
                if tok in (">", ">>", ">|", "&>", "&>>") and i + 1 < len(seg):
                    found.append(seg[i + 1])
                i += 2  # the target, the descriptor of `>&`, or the input of `<` / `<<`
                continue
            words.append(tok)
            i += 1
        if words[:1] == ["cd"] and len(words) == 2:
            return [], self._join(cwd, words[1])
        if words and words[0] in ("tee", "sed", "perl"):
            in_place = words[0] == "tee" or any(
                w.startswith("-") and not w.startswith("--") and "i" in w[1:]
                for w in words[1:]
            )
            if in_place:
                args = [w for w in words[1:] if not w.startswith("-")]
                if words[0] in ("sed", "perl") and not any(
                    w in ("-e", "--expression") for w in words
                ):
                    args = args[1:]  # the script
                found += args
        elif words and words[0] in ("cp", "mv", "install"):
            args = [w for w in words[1:] if not w.startswith("-")]
            found += args[-1:] if len(args) >= 2 else []
        if any(_UNKNOWN.search(t) for t in found):
            return None, cwd
        return [self._join(cwd, t) for t in found], cwd

    def _join(self, cwd: str, path: str) -> str:
        return posixpath.normpath(path if path.startswith("/") else f"{cwd}/{path}")

    def protected_targets(
        self, rule: Rule, scope: str, text: str, tracked: frozenset[str]
    ) -> list[str] | None:
        """For a `protect: tracked` rule that matched: the protected files the call would
        write (empty: the rule does not hold), or None when the targets cannot be read.
        Protected: a test file the repository tracked, and any `conftest.py` in the
        repository, new or not (pytest loads it by itself, so a new one could skip or pass
        the hidden tests)."""
        if scope == "bash":
            targets = self.write_targets(text)
            if targets is None:
                return None
        else:
            targets = [self.resolve(text)]
        out = []
        for t in targets:
            rel = self.rel(t)
            if rel is None:
                continue
            if posixpath.basename(rel) == "conftest.py" or (
                rel in tracked and self._test_path.search(rel)
            ):
                out.append(rel)
        return out
