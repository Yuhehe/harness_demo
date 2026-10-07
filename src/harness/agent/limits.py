"""Step, cost and wall-clock limits, checked before every LLM call."""

from __future__ import annotations

from harness.agent.state import AgentState, FailReason
from harness.config import LimitsConfig


def check(
    state: AgentState, limits: LimitsConfig, *, wasted_cost_usd: float, now: float
) -> tuple[FailReason, str] | None:
    """Returns (reason, detail) when the run must stop. `wasted_cost_usd` is the estimated cost
    of failed attempts: it is not in the reported cost, but the budget guard counts it."""
    if state.steps >= limits.max_steps:
        return FailReason.step_limit, f"{state.steps} steps (limit {limits.max_steps})"
    spent = state.cost_usd + wasted_cost_usd
    if spent >= limits.max_cost_per_task_usd:
        return (
            FailReason.cost_limit,
            f"${spent:.4f} spent incl. estimated waste (limit ${limits.max_cost_per_task_usd})",
        )
    elapsed = now - state.started_at
    if elapsed >= limits.max_wall_seconds:
        return (
            FailReason.time_limit,
            f"{elapsed:.0f}s (limit {limits.max_wall_seconds:.0f}s)",
        )
    return None
