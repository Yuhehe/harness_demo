"""The permission policy: loading configs/policy.yaml and matching it (CLAUDE.md 5.6)."""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

Scope = Literal["bash", "path_read", "path_write"]
_SEGMENT = re.compile(r";|&&|\|\||\||&|\n")
_SUBSTITUTION = re.compile(r"[<>`]|\$\(")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleSpec(_Strict):
    id: str
    scope: list[Scope]
    regex: str = ""
    use_test_path: bool = False  # match the policy's `test_path` instead of `regex`
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
                        r.id, action, frozenset(r.scope), re.compile(pattern), r.reason
                    )
                )
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

    def first_hit(self, scope: str, text: str) -> Rule | None:
        """The first matching rule; deny rules are listed before ask rules."""
        return next((r for r in self.rules if r.hits(scope, text)), None)
