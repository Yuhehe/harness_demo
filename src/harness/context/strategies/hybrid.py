"""`hybrid`: clear old outputs first; summarize only if that was not enough.

The two steps see different things on purpose. Clearing shrinks what the AGENT is sent (the
view). The summary is written from the original history, not from the cleared view, so the
summarizer reads the outputs and not their stubs (see strategies/summarize.py)."""

from __future__ import annotations

from dataclasses import replace

from harness.context.strategies import summarize, truncate
from harness.context.strategies.summarize import spent_by
from harness.context.strategies.types import Env, Report, Skip
from harness.llm.types import LLMError


async def run(env: Env) -> Report | Skip:
    first = await truncate.run(env)
    if isinstance(first, Skip):
        return await summarize.run(env)
    # What the cleared view would cost, by the same estimate the trigger used.
    est = await env.estimator.estimate(first.view)
    if est <= env.high:
        return first
    try:
        # `view` is the cleared one (its tail is what survives); `original` is untouched.
        # Over the budget is judged again: if the clearing got back under it, the summary is
        # no longer a last chance and the minimum-gain check applies.
        second = await summarize.run(
            replace(
                env,
                view=first.view,
                est=est,
                over_budget=est > env.cfg.budget_tokens,
            )
        )
    except (LLMError, ValueError) as e:  # the clearing already done still stands
        first.details["summary_failed"] = f"{type(e).__name__}: {e}"[:200]
        spent = spent_by(e)  # an unusable response was still paid for
        first.cost_usd += spent.cost_usd
        first.tokens_in += spent.tokens_in
        first.tokens_out += spent.tokens_out
        return first
    if isinstance(second, Skip):
        first.details["summary_skipped"] = second.reason
        first.details.update({f"summary_{k}": v for k, v in second.details.items()})
        return first
    return Report(
        second.view,
        [*first.actions, *second.actions],
        {**first.details, **second.details},
        cost_usd=second.cost_usd,
        tokens_in=second.tokens_in,
        tokens_out=second.tokens_out,
        original=second.original,
    )
