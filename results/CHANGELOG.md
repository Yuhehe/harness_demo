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
