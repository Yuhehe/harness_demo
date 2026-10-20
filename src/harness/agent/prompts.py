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


# `mini` (prompt_variant): mini-swe-agent's SWE-bench prompt (2.4.6,
# config/benchmarks/swebench.yaml), for the diagnostic run of plan.md stage 3. The system
# message and everything up to "## Submission" are verbatim (tests compare them with the
# installed file; braces doubled for str.format); the submission section is replaced,
# because here the harness makes the patch with `git diff` and the agent calls the submit
# tool, and the step note this harness appends to every tool result is explained, as v1 does.
# no trailing newline: mini renders its templates with Jinja, which drops it (docs/notes.md
# §28; up to v0.2.1-exp this string ended in "\n")
_MINI_SYSTEM = (
    "You are a helpful assistant that can interact with a computer shell to solve "
    "programming tasks."
)
_MINI_HEAD = (
    "<pr_description>\n"
    "Consider the following PR description:\n"
    "{task}\n"
    "</pr_description>\n"
    "\n"
    "<instructions>\n"
    "# Task Instructions\n"
    "\n"
    "## Overview\n"
    "\n"
    "You're a software engineer interacting continuously with a computer by submitting commands.\n"
    "You'll be helping implement necessary changes to meet requirements in the PR description.\n"
    "Your task is specifically to make changes to non-test files in the current directory in order to fix the issue described in the PR description in a way that is general and consistent with the codebase.\n"
    "<IMPORTANT>This is an interactive process where you will think and issue AT LEAST ONE command, see the result, then think and issue your next command(s).</important>\n"
    "\n"
    "For each response:\n"
    "\n"
    "1. Include a THOUGHT section explaining your reasoning and what you're trying to accomplish\n"
    "2. Provide one or more bash tool calls to execute\n"
    "\n"
    "## Important Boundaries\n"
    "\n"
    "- MODIFY: Regular source code files in /testbed (this is the working directory for all your subsequent commands)\n"
    "- DO NOT MODIFY: Tests, configuration files (pyproject.toml, setup.cfg, etc.)\n"
    "\n"
    "## Recommended Workflow\n"
    "\n"
    "1. Analyze the codebase by finding and reading relevant files\n"
    "2. Create a script to reproduce the issue\n"
    "3. Edit the source code to resolve the issue\n"
    "4. Verify your fix works by running your script again\n"
    "5. Test edge cases to ensure your fix is robust\n"
    "\n"
    "## Command Execution Rules\n"
    "\n"
    "You are operating in an environment where\n"
    "\n"
    "1. You issue at least one command\n"
    "2. The system executes the command(s) in a subshell\n"
    "3. You see the result(s)\n"
    "4. You write your next command(s)\n"
    "\n"
    "Each response should include:\n"
    "\n"
    "1. **Reasoning text** where you explain your analysis and plan\n"
    "2. At least one tool call with your command\n"
    "\n"
    "**CRITICAL REQUIREMENTS:**\n"
    "\n"
    "- Your response SHOULD include reasoning text explaining what you're doing\n"
    "- Your response MUST include AT LEAST ONE bash tool call. You can make MULTIPLE tool calls in a single response when the commands are independent (e.g., searching multiple files, reading different parts of the codebase).\n"
    "- Directory or environment variable changes are not persistent. Every action is executed in a new subshell.\n"
    "- However, you can prefix any action with `MY_ENV_VAR=MY_VALUE cd /path/to/working/dir && ...` or write/load environment variables from files\n"
    "\n"
    "Example of a CORRECT response:\n"
    "<example_response>\n"
    "I need to understand the Builder-related code. Let me find relevant files and check the project structure.\n"
    "\n"
    '[Makes multiple bash tool calls: {{"command": "ls -la"}}, {{"command": "find src -name \'*.java\' | grep -i builder"}}, {{"command": "cat README.md | head -50"}}]\n'
    "</example_response>\n"
    "\n"
    "## Environment Details\n"
    "\n"
    "- You have a full Linux shell environment\n"
    "- Always use non-interactive flags (-y, -f) for commands\n"
    "- Avoid interactive tools like vi, nano, or any that require user input\n"
    "- You can use bash commands or invoke any tool that is available in the environment\n"
    "- You can also create new tools or scripts to help you with the task\n"
    "- If a tool isn't available, you can also install it\n"
    "\n"
    ""
)
_MINI_SUBMISSION = (
    "## Submission\n"
    "\n"
    "When you've completed your work, call the submit tool. What is submitted is the state of "
    "the repository at that moment (a `git diff` of the working directory), so first remove "
    "any reproduction scripts, helper scripts or other files you created that are not part "
    "of the fix.\n"
    "\n"
    "Every tool result ends with a note like [step 3/50 | cost $0.0123 of $0.50]. It only "
    "tells you how much budget is left.\n"
    "</instructions>"
)
# what explains the step note, removed with it (tool_output.step_note: false)
_NOTE_SENTENCES = (
    (
        "\nEvery tool result ends with a note like [step 3/50 | cost $0.0123 of $0.50]. It only "
        "tells you how much budget is left."
    ),
    (
        "\nEvery tool result ends with a note like [step 3/50 | cost $0.0123 of $0.50]. It only "
        "tells you how much budget is left.\n"
    ),
)


def _without_note(text: str, sentence: str) -> str:
    if text.count(sentence) != 1:
        raise ValueError("the step-note sentence is not where it should be")
    return text.replace(sentence, "")


def system_prompt(cfg: AgentConfig, workdir: str = "/testbed") -> str:
    planning = _PLANNING if cfg.planning == "on" and cfg.action_space == "tools" else ""
    if cfg.prompt_variant == "mini":
        return _MINI_SYSTEM
    text = _SYSTEM.format(workdir=workdir, planning=planning)
    if cfg.prompt_variant == "lite":
        for old, new in _LITE_EDITS:
            if text.count(old) != 1:
                raise ValueError(f"lite prompt edit does not apply: {old!r}")
            text = text.replace(old, new)
    if not cfg.tool_output.step_note:
        text = _without_note(text, _NOTE_SENTENCES[0])
    return text


def task_message(problem_statement: str, cfg: AgentConfig | None = None) -> str:
    if cfg is not None and cfg.prompt_variant == "mini":
        text = _MINI_HEAD.format(task=problem_statement) + _MINI_SUBMISSION
        if not cfg.tool_output.step_note:
            text = _without_note(text, _NOTE_SENTENCES[1])
        return text
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
