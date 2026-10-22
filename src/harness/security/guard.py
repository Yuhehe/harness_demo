"""Permission layer (CLAUDE.md 5.6): every tool call is checked against configs/policy.yaml
before it runs. The decision, its rule id and the level are traced, which is what the
false-block rate is computed from. In batch runs `ask` is treated as `deny`.

Rules marked `protect: tracked` (policy v2: the two test-file rules) hold only when the call
would write a test file the repository tracked when the task started: creating a new scratch
test under `tests/` is allowed, changing an existing test is not. The list of tracked files is
taken once per task from the sandbox (`tracked_files`, called by the agent loop) and kept in
`Guard.tracked`; while it is None the rules hold as soon as their pattern matches (policy v1
behaviour)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from harness.config import GuardConfig
from harness.llm.types import ToolCall
from harness.security.policy import Policy
from harness.tools.base import ToolResult


@dataclass(frozen=True)
class Decision:
    action: str = "allow"  # allow | deny | ask
    rule_id: str | None = None
    reason: str = ""
    level: str | None = None  # read | write | exec

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    def as_tool_result(self, *, declined: bool = False) -> ToolResult:
        if self.action == "ask" and declined:
            return ToolResult(
                f"Blocked by the permission layer ({self.rule_id}): {self.reason}. "
                "The user declined it. The call was not run."
            )
        if self.action == "ask":
            return ToolResult(
                f"Blocked by the permission layer ({self.rule_id}): {self.reason}. "
                "This needs the user's approval, which is not available in this run. "
                "The call was not run."
            )
        return ToolResult(
            f"Blocked by the permission layer ({self.rule_id}): {self.reason}. "
            "The call was not run."
        )


# Asks a person whether an `ask` decision may run (`harness run` on a terminal). Batch runs and
# the service have none, so there `ask` stays a deny. Never consulted for `deny`.
Approver = Callable[[ToolCall, Decision], Awaitable[bool]]


_PATH_ARGS = {  # tool -> (argument holding a path, scope)
    "view": ("path", "path_read"),
    "search": ("path", "path_read"),
    "str_replace": ("path", "path_write"),
}


async def tracked_files(sandbox: Any) -> frozenset[str] | None:
    """The files git tracks in the sandbox's repository (relative paths), or None when that
    cannot be read. An empty list is treated as unreadable too, so that a wrong working
    directory leaves the test-file rules in force (policy v1 behaviour) instead of turning
    them off. A harness command, not a model one; it runs in the sandbox."""
    code, out = await sandbox.exec("git ls-files -z", timeout=60)
    files = frozenset(p for p in out.split("\0") if p.strip())
    return files if code == 0 and files else None


class Guard:
    def __init__(
        self,
        policy: Policy | None,
        enabled: bool = True,
        tracked: frozenset[str] | None = None,
    ) -> None:
        if enabled and policy is None:
            raise ValueError("an enabled guard needs a policy")
        self.policy, self.enabled = policy, enabled
        self.tracked = tracked

    @property
    def needs_tracked(self) -> bool:
        return bool(self.enabled and self.policy and self.policy.protects_tracked)

    @classmethod
    def from_config(cls, cfg: GuardConfig, workdir: str) -> Guard:
        if not cfg.enabled:
            return cls(None, enabled=False)
        return cls(Policy.load(cfg.policy_path, workdir))

    @property
    def policy_hash(self) -> str | None:
        return self.policy.hash if self.policy else None

    def check(self, call: ToolCall, state: object = None) -> Decision:
        if not self.enabled or self.policy is None:
            return Decision()
        policy = self.policy
        try:
            args: Any = json.loads(call.arguments) if call.arguments.strip() else {}
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            # a malformed call does not run: the registry reports the format error
            return Decision(level=policy.level_of_tool(call.name))

        level = policy.level_of_tool(call.name)
        if call.name == "bash":
            command = args.get("command")
            if not isinstance(command, str):
                return Decision(level=level)
            level = policy.bash_level(command)
            scope, text = "bash", command
        elif call.name in _PATH_ARGS:
            key, scope = _PATH_ARGS[call.name]
            path = args.get(key)
            if not isinstance(path, str):
                return Decision(level=level)
            text = policy.resolve(path)
        else:
            scope, text = None, ""

        rule = None
        reason = ""
        if scope is not None:
            for r in policy.rules:
                if not r.hits(scope, text):
                    continue
                if r.protect == "tracked" and self.tracked is not None:
                    files = policy.protected_targets(r, scope, text, self.tracked)
                    if files == []:
                        continue  # only new or untracked files: this rule does not hold
                    reason = r.reason + (f" ({', '.join(files)})" if files else "")
                else:
                    reason = r.reason
                rule = r
                break
        if (
            rule is None
            and scope == "path_write"
            and not policy.inside_write_roots(text)
        ):
            return Decision(
                "deny",
                "deny.write_outside_roots",
                "writing outside the repository and /tmp is not allowed",
                level,
            )

        if rule is not None:
            return Decision(rule.action, rule.id, reason, level)
        if level is not None and level not in policy.spec.allowed_levels:
            return Decision(
                "deny", f"deny.level_{level}", f"{level} actions are disabled", level
            )
        return Decision("allow", f"allow.{level}" if level else None, "", level)
