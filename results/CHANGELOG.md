# results/CHANGELOG.md

Every comparison configuration runs on the same frozen tag (CLAUDE.md §2.4). When a harness bug
is fixed after a freeze, the affected configurations are rerun and the reason is recorded here.

## v0.1-frozen (2026-10-15)

First freeze, before the main experiments (M0, B0, ablations A1–A6).

- Harness code and experiment configs are those accepted by A3 (`M0-dev-v2` on dev_5, 5/5,
  commit `ed6625a`); later commits up to the tag only add docs, the `verified_25` subset and
  `select_subset.py derive`.
- Subsets: `verified_25` (half of `verified_50`, stratified by repo, seed 42) runs first; the
  remaining 25 of `verified_50` follow on the same tag.
- Summary space: option A (docs/notes.md §20); summary size calibration §19; trigger hysteresis
  §18.4 (`hysteresis_cap` 0.5).

## v0.1.1 (2026-10-16)

Runner and analysis changes only. `git diff v0.1-frozen v0.1.1 -- src configs pyproject.toml
uv.lock` is empty: the harness, the experiment configs and the dependencies are the same, so
M0 and B0 on verified_25 (run on `v0.1-frozen`) need no rerun.

- `eval/swebench/run_agent.py --workers N`: tasks run side by side (one process, asyncio); the
  budget check counts the expected cost of the tasks in flight; the worker count is recorded
  in every task's `run_start` and status row. **Latency is only comparable between runs with
  the same worker count** (tasks running side by side compete for cpu and disk).
- `eval/baselines/run_b0.py --workers N` (mini-swe-agent's `-w`); refuses to start past 80% of
  the budget.
- `eval/swebench/score.py` keeps each instance's test outcome; `eval/report/aggregate.py` adds
  the metrics of docs/notes.md §22.7 that the runs already have, and `--paired`.
- Real check before tagging: dev run `dev-parallel` (2 tasks, 2 workers) both submitted, both
  started at the same moment, no sandbox left behind.

## Provider change: deepseek-flash replaced (reported 2026-10-18)

DeepSeek replaced the model behind `deepseek-flash` (reported by the user; our records cannot
confirm it, because no flash fingerprint was ever stored). Runs that called flash are not
comparable with later runs that do: A6 (17 summary calls), the compaction smoke runs, the
summary-size calibration (docs/notes.md §19) and the summary probes. Runs that called only
`deepseek-v4-pro` are unaffected: M0, M0-r2, A2 (its two compactions only cleared outputs),
A6xA1; B0 does not go through our client. `deepseek-v4-pro` itself looks unchanged: the
`system_fingerprint` of a call made on 2026-10-18 equals the one in all 1017 responses
mini-swe-agent stored for B0 (2026-10-15) and B0-dev (2026-10-12). From v0.2-exp on, every
`llm_call` span records `served_model` and `system_fingerprint`.

## v0.2-exp (2026-10-18)

Exploratory tag for the M0 vs B0 gap (plan.md, "M0 与 B0 差距的验证"); the v0.1 results stay the
results of record.

- New config key `prompt_variant` (`v1` default, `lite`), so every config hash changes. `v1`
  renders byte for byte the v0.1 system prompt (pinned by `tests/unit/test_prompts.py`; the
  replay end-to-end test still matches the recorded requests), so the harness behaves as in
  v0.1 under every existing config. M0's comparison runs for `lite` are made on this tag, at
  the same time, not taken from v0.1.
- New config `configs/ablations/prompt_lite.yaml` (`prompt_variant: lite` only).
- `llm_call` spans add `served_model` and `system_fingerprint` (trace only).
- `aggregate.py`: `quality.call_mix` (calls by class from the command text, single-edit tasks,
  share of responses with several calls).

## v0.2.1-exp (2026-10-18)

Adds the `mini` prompt variant for the diagnostic run of plan.md stage 3 (mini-swe-agent's
SWE-bench prompt, verbatim up to its submission section, which is replaced: the agent calls the
submit tool and the harness makes the patch). New config `configs/ablations/prompt_mini_bash.yaml`
(bash only + `mini`, a deliberate pair). `task_message` takes the config. The `v1` and `lite`
texts are unchanged (system prompt and task message pinned by hash in
`tests/unit/test_prompts.py`), and so are the config hashes of `default.yaml` (`af1871001292`)
and `prompt_lite.yaml` (`74d112cfe5ab`), so M0-v02 and lite-v02 (run on v0.2-exp) stay
comparable with runs on this tag.

## v0.3-exp (2026-10-20)

Mechanism switches for plan.md "M0 比 B0 效率差的原因拆解", stage 1 (request-level differences
from mini-swe-agent: docs/notes.md §28). Every new key defaults to the old behaviour.

- New config keys: `bash_tool` (`v1` | `mini`: mini-swe-agent's `BASH_TOOL` description and
  schema), `tool_output.format` (`v1` | `mini`: mini's `<returncode>` / `<output>` rendering,
  and its warning plus head / tail cut at `tool_output.max_chars`, nothing saved in the
  sandbox), `tool_output.step_note` (the `[step i/N | cost ...]` note and the prompt sentences
  that announce it) and `llm.parallel_tool_calls` (sent only when set). **Every config hash
  changes** (`default.yaml` `af1871001292` → `fff4b25214bf`; `prompt_lite.yaml`
  `74d112cfe5ab` → `1d7c45329897`; `prompt_mini_bash.yaml` → `82faaa793580`), so comparisons
  with runs on earlier tags are across versions.
- Default requests are unchanged byte for byte: the v1 system prompts and task message keep
  their pinned hashes, the v1 bash schema and result rendering are pinned
  (`tests/unit/test_mini_mechanics.py`), the replay key adds `parallel_tool_calls` only when it
  is set, and the replay end-to-end test still finds every recorded request.
- **Fix that changes D:** the `mini` system prompt ended in a newline that mini-swe-agent does
  not send (its Jinja rendering drops the template's trailing newline). `prompt_mini_bash`
  (D) on this tag differs from D on v0.2.1-exp by that one character, so stage 2 reruns D
  instead of reusing it.
- New config `configs/ablations/mech_aligned.yaml` (D\*, `d2c12544b369`): D plus every switch
  set to mini's side, the guard off and the cut at 10000 characters. Remaining differences
  that cannot be aligned: the `submit` tool and submission section, `bash -lc` vs `bash -c`,
  and timeouts (exit code 124 and the output here; mini renders its `exception_info` instead).

## v0.4-exp (2026-10-21)

Fix of the permission guard's false blocks (plan.md, "权限层误拦的修复"; docs/notes.md §30).
**M0 is not rerun** (the user's decision): the reported false-block rate (README, résumé) is
policy v1's, measured on M0; v2 is checked offline against the calls the runs recorded.

- `configs/policy.yaml` version 2 (policy hash changes): the two test-file rules carry
  `protect: tracked` and hold only when the call writes a test file the repository tracked
  when the task started; a new scratch test under `tests/` is allowed. Write targets are read
  with a quote-aware split of the command (redirections, `tee`, `sed -i`, `perl -i`, and new:
  the destination of `cp` / `mv` / `install`); a target that cannot be read (a variable, a
  glob, unparsable text) keeps the rule in force. Any `conftest.py` in the repository stays
  protected, new or not (pytest loads it by itself, so a new one could skip or pass the hidden
  tests). Without a tracked list, or with an empty one, the rules behave as in v1.
- The tag was first set on `5010c87` (local, not pushed, no run made with it) and moved to the
  commit that adds the `conftest.py` and empty-list cases, found in review before any use.
- The agent loop takes the tracked list once per task (`git ls-files -z` in the sandbox, a
  harness command) and records its size in `run_start` (`guard_tracked_files`).
- Block messages end with "The call was not run."; the test-file rules name the protected file
  and say that new files are allowed.
- No config key changed, so config hashes are those of v0.3-exp; runs differ by
  `policy_hash` in `run_start`.
- Offline check (`eval/report/guard_replay.py`): a v1 guard rebuilt from `v0.1-frozen`
  reproduces every recorded decision of the 11 experiment runs that had the guard on; a v2
  guard on the same calls blocks only the one `env` call, which the policy means to block.
