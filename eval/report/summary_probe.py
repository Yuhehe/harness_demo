"""Replay a recorded trajectory into the context manager and force one compaction, to see
what a summary keeps. A probe, not an experiment: one summary call (a few cents), one
trajectory, and its numbers do not go into the report.

    uv run python eval/report/summary_probe.py \\
        --trajectory results/runs/dev-pro-1/trajectories/django__django-11066.jsonl \\
        --patch results/runs/dev-pro-1/patches/django__django-11066.diff [--at-step 12]

Dev runs are short (at most about 13k prompt tokens), so they never compact on their own;
the probe picks a budget at which the history up to `--at-step` is just over the high water
mark. The summary model is the configured one ($MODEL_CHEAP).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from aggregate import Task, edited_before, mentions

from harness.agent.state import AgentState
from harness.agent.state import Task as AgentTask
from harness.config import AgentConfig, ContextConfig, LLMConfig, summary_llm_config
from harness.context.manager import ContextManager
from harness.context.strategies.summarize import is_summary
from harness.llm.client import LLMClient
from harness.llm.types import Message
from harness.tracing.tracer import MemoryTracer

_SPAN_KEYS = ("type", "ts", "run_id", "instance_id", "step")


class PatchSandbox:
    """Just enough sandbox for a replay: the state block asks it for the patch."""

    workdir = "/testbed"

    def __init__(self, patch: str) -> None:
        self.patch, self.files = patch, {}

    async def exec(self, cmd: str, timeout: int = 120):
        raise RuntimeError("the probe has no sandbox to run commands in")

    async def read_file(self, path: str) -> str:
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def git_diff(self) -> str:
        return self.patch


def load_messages(path: Path) -> list[Message]:
    """The conversation of a trajectory, with the `step` of every message filled in (runs
    from before the tracer wrote it are numbered by counting assistant messages)."""
    spans = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    out, step = [], 0
    for s in spans:
        if s["type"] != "message":
            continue
        step = s["step"] if "step" in s else step + (s["role"] == "assistant")
        out.append(
            {**{k: v for k, v in s.items() if k not in _SPAN_KEYS}, "step": step}
        )
    return out


async def probe(
    messages: list[Message],
    patch: str,
    summarizer: LLMClient,
    *,
    at_step: int | None = None,
    strategy: str = "summarize",
    model: str = "deepseek/deepseek-v4-pro",
    summary_model: str | None = None,
) -> dict[str, Any]:
    last = max(m["step"] for m in messages)
    at_step = last - 1 if at_step is None else at_step
    upto = [m for m in messages if m["step"] <= at_step]
    kept = [{k: v for k, v in m.items() if k != "step"} for m in upto]

    # a budget at which this history is 80% full: just over the 0.5 high water mark
    cfg = AgentConfig(
        llm=LLMConfig(model=model), context=ContextConfig(strategy=strategy)
    )
    state = AgentState(task=AgentTask("probe", ""), cfg=cfg, model=model)
    state.messages.extend(kept)
    state.steps = at_step
    sizer = ContextManager(
        cfg.context, model=model, tools=[], sandbox=PatchSandbox(patch), tracer=MemoryTracer(),
        summarizer=summarizer, summary_model=summary_model or "deepseek/deepseek-flash",
    )  # fmt: skip
    size = await sizer.estimator.estimate(kept)
    ctx = ContextConfig(
        strategy=strategy,
        budget_tokens=max(int(size / 0.8), 4000),
        high_water=0.5,
        low_water=0.25,
        keep_recent_ratio=0.01,
        summary_model=summary_model,
    )
    tracer = MemoryTracer()
    manager = ContextManager(
        ctx, model=model, tools=[], sandbox=PatchSandbox(patch), tracer=tracer,
        summarizer=summarizer, summary_model=summary_model or "deepseek/deepseek-flash",
    )  # fmt: skip
    view = await manager.build(state, deadline=None)

    spans = tracer.of("compaction")
    if not spans:
        return {
            "compacted": False,
            "history_tokens_est": size,
            "budget_tokens": ctx.budget_tokens,
        }
    span = spans[0]
    summary = span.get("summary")  # the summarizer's own text, without framing or block
    files = edited_before(Task("probe", None, [], [], upto), at_step)
    return {
        "compacted": True,
        "at_step": at_step,
        "budget_tokens": ctx.budget_tokens,
        "plan_level": span["plan_level"],
        "tokens_before": span["tokens_before"],
        "tokens_after": span["tokens_after"],
        "summarizer_tokens": [span["summary_tokens_in"], span["summary_tokens_out"]],
        "cost_usd": span["cost_usd"],
        "summary_input_chars": span.get("summary_input_chars"),
        "restored": span.get("restored"),
        "summary": summary,
        "edited_before": files,
        "summary_file_recall": (
            sum(mentions(summary or "", f) for f in files) / len(files)
            if files
            else None
        ),
        "state_block": next(
            (m["content"] for m in view if is_summary(m)), ""
        ).partition("\n\n## State as of")[2],
    }


async def _main(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from harness.llm.client import LiteLLMClient

    load_dotenv(".env")
    cfg = AgentConfig(context=ContextConfig(strategy=args.strategy))
    summ_cfg = summary_llm_config(cfg)
    summarizer = LiteLLMClient(summ_cfg, MemoryTracer(), purpose="summary")
    result = await probe(
        load_messages(args.trajectory),
        args.patch.read_text() if args.patch else "",
        summarizer,
        at_step=args.at_step,
        strategy=args.strategy,
        summary_model=summ_cfg.resolved_model(),
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if not result["compacted"]:
        print(f"no compaction: {result}")
        return 1
    r = result
    print(
        f"compaction at step {r['at_step']} (budget {r['budget_tokens']}, plan {r['plan_level']})"
    )
    print(
        f"  prompt {r['tokens_before']} -> {r['tokens_after']} tokens; summarizer in/out {r['summarizer_tokens']}; ${r['cost_usd']:.5f}"
    )
    print(
        f"  files edited before: {r['edited_before']}  -> summary recall {r['summary_file_recall']}"
    )
    print("---- summary (summarizer's own text) ----\n" + (r["summary"] or ""))
    print("---- state block (generated) ----\n## State as of" + r["state_block"])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trajectory", type=Path, required=True)
    ap.add_argument("--patch", type=Path, help="the run's patch, for the state block")
    ap.add_argument(
        "--at-step",
        type=int,
        help="compact the history as of this step (default: the second to last)",
    )
    ap.add_argument("--strategy", choices=["summarize", "hybrid"], default="summarize")
    ap.add_argument("--json", action="store_true")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
