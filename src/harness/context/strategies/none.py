"""`none`: no compaction. A prompt over the budget ends the run (context_overflow)."""

from __future__ import annotations

from harness.context.strategies.types import Env, Report


async def run(env: Env) -> Report | None:
    return None
