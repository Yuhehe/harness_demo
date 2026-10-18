"""Prompt text. Deterministic: no timestamps, nothing that changes between runs (CLAUDE.md 5.2).

The step note is the "dynamic information goes last" rule: it is written once, when the
message is created, and stays in the history. It is built from the step number and the cost
computed from recorded usage, never from the wall clock, so replay and caching both hold.
"""

from __future__ import annotations

from harness.config import AgentConfig

_SYSTEM = """\
You are an expert software engineer working autonomously in a sandbox. A repository is checked \
out in the current directory ({workdir}). Your job is to resolve the issue you are given by \
changing the repository's source code.

How to work:
- Explore first: find the code the issue is about before changing anything.
- If it helps, reproduce the problem with a small script or the project's own tests.
- Fix the root cause with the smallest change that does the job. Do not refactor unrelated code.
- Do NOT edit existing test files: your fix is judged by tests you cannot see.
- Run the relevant tests after changing code, and read the output.
- There is no network access. Each bash call starts a fresh shell, so `cd` does not persist.
- Keep outputs small (use head, grep, line ranges). Long outputs are cut and saved to a file.
- Any file you create stays in the repository; remove scratch files you no longer need.
- When the fix is done and checked, call the submit tool. Your answer is the state of the \
repository at that moment.
{planning}
Every tool result ends with a note like [step 3/50 | cost $0.0123 of $0.50]. It only tells you \
how much budget is left."""

_PLANNING = (
    "- Before acting, write a short plan with the todo tool, and keep it up to date.\n"
)


# `lite` (prompt_variant): the v1 text with three edits, fixed before the experiment that
# tests them (plan.md, "M0 与 B0 差距的验证"). It drops the two instructions the trace analysis
# points at (docs/notes.md section 25.1), keeps the fact that long outputs are cut, and asks for
# independent commands in one response, as mini-swe-agent's prompt does.
_LITE_EDITS = (
    ("- Run the relevant tests after changing code, and read the output.\n", ""),
    (
        (
            "- Keep outputs small (use head, grep, line ranges). Long outputs are cut and saved "
            "to a file.\n"
        ),
        "- Long outputs are cut and saved to a file.\n",
    ),
    (
        "- There is no network access.",
        (
            "- When commands do not depend on each other, issue them as several tool calls in "
            "one response.\n- There is no network access."
        ),
    ),
)


def system_prompt(cfg: AgentConfig, workdir: str = "/testbed") -> str:
    planning = _PLANNING if cfg.planning == "on" and cfg.action_space == "tools" else ""
    text = _SYSTEM.format(workdir=workdir, planning=planning)
    if cfg.prompt_variant == "lite":
        for old, new in _LITE_EDITS:
            if text.count(old) != 1:
                raise ValueError(f"lite prompt edit does not apply: {old!r}")
            text = text.replace(old, new)
    return text


def task_message(problem_statement: str) -> str:
    return f"Resolve this issue:\n\n<issue>\n{problem_statement.strip()}\n</issue>"


def step_note(step: int, max_steps: int, cost_usd: float, max_cost_usd: float) -> str:
    return f"[step {step}/{max_steps} | cost ${cost_usd:.4f} of ${max_cost_usd:.2f}]"


NO_TOOL_CALL_NUDGE = (
    "Your last reply contained no tool call. Call a tool to continue, or call submit if the "
    "task is finished."
)


def truncation_feedback(max_tokens: int) -> str:
    return (
        f"Your previous response hit the output length limit ({max_tokens} tokens) and was cut "
        "off, so it was discarded and nothing was executed. Make smaller steps: edit one place "
        "at a time, avoid writing or printing very long content, and keep each tool call short."
    )
