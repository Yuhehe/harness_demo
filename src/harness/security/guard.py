"""Permission layer (CLAUDE.md 5.6): every tool call is checked against configs/policy.yaml
before it runs. The decision, its rule id and the level are traced, which is what the
false-block rate is computed from. In batch runs `ask` is treated as `deny`."""

from __future__ import annotations

import json
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

    def as_tool_result(self) -> ToolResult:
        if self.action == "ask":
            return ToolResult(
                f"Blocked by the permission layer ({self.rule_id}): {self.reason}. "
                "This needs the user's approval, which is not available in this run."
            )
        return ToolResult(
            f"Blocked by the permission layer ({self.rule_id}): {self.reason}"
        )


_PATH_ARGS = {  # tool -> (argument holding a path, scope)
    "view": ("path", "path_read"),
    "search": ("path", "path_read"),
    "str_replace": ("path", "path_write"),
}


class Guard:
    def __init__(self, policy: Policy | None, enabled: bool = True) -> None:
        if enabled and policy is None:
            raise ValueError("an enabled guard needs a policy")
        self.policy, self.enabled = policy, enabled

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
            rule = policy.first_hit("bash", command)
        elif call.name in _PATH_ARGS:
            key, scope = _PATH_ARGS[call.name]
            path = args.get(key)
            if not isinstance(path, str):
                return Decision(level=level)
            rule = policy.first_hit(scope, policy.resolve(path))
            if (
                rule is None
                and scope == "path_write"
                and not policy.inside_write_roots(path)
            ):
                return Decision(
                    "deny",
                    "deny.write_outside_roots",
                    "writing outside the repository and /tmp is not allowed",
                    level,
                )
        else:
            rule = None

        if rule is not None:
            return Decision(rule.action, rule.id, rule.reason, level)
        if level is not None and level not in policy.spec.allowed_levels:
            return Decision(
                "deny", f"deny.level_{level}", f"{level} actions are disabled", level
            )
        return Decision("allow", f"allow.{level}" if level else None, "", level)
