# Résumé bullets

Every number below comes from a file in `results/summary/`, which `eval/report/make_tables.py` generates from `results/runs/` (CLAUDE.md §2.3). None is taken from `docs/dev_log.md`. Rounding is the only change: the numbers are rounded for display, and the sources give the exact values.

```text
harness-lab — Framework-free Coding Agent Harness | Python, LiteLLM, Docker, FastAPI, SWE-bench | 2026 | github.com/Yuhehe/harness_demo
- Built a framework-free coding-agent harness (~4.6k lines of Python; LiteLLM only for transport) with Docker-sandboxed execution and config-switchable components; on a 50-task SWE-bench Verified subset it resolved 74% (95% CI 60–84%) vs. 78% for mini-SWE-agent on the same model, a difference that is not statistically distinguishable (sign test p = 0.63), at $0.21 vs. $0.16 per task.
- Ran single-factor ablations with task-paired bootstrap intervals and a rerun calibration check: under a 32K context budget, rule-based output clearing plus LLM summarization cut context-overflow failures from 60% to 0% and resolved 7 more of 25 tasks (p = 0.016); in a single-run ablation, bash alone cost more than purpose-built edit tools (×1.29, 95% CI 1.05–1.41), though the gap did not hold up when repeated runs were pooled; an append-only, KV-cache-friendly context layout kept a 98.2% prompt-cache hit rate.
- Designed a tiered, regex-based permission guard over a network-isolated sandbox, with a 0.25% false-block rate across 2,793 tool calls (every block reviewed by hand); shipped the harness as a FastAPI + SSE service with Docker Compose, with replay-based end-to-end tests that run in GitHub Actions CI without an API key.
```

## Where each number comes from

| Bullet | Number | File | Field |
|---|---|---|---|
| 1 | ~4.6k lines | `results/summary/setup.json` | `harness_lines` (4644) |
| 1 | 50 tasks | `results/summary/tldr.json` | `tasks` |
| 1 | 74% (95% CI 60–84%) | `results/summary/tldr.json` | `main.resolve_rate.rate` (0.74), `.wilson95` (0.604, 0.841) |
| 1 | 78% | `results/summary/tldr.json` | `baseline.resolve_rate.rate` (0.78) |
| 1 | p = 0.63 | `results/summary/tldr.json` | `resolved_pairs.sign_test_p` (0.625) |
| 1 | $0.21 vs. $0.16 | `results/summary/tldr.json` | `main.cost_per_task.mean` (0.206), `baseline.cost_per_task.mean` (0.161) |
| 2 | 60% → 0% | `results/summary/tldr.json` | `pressure.overflow_without` (0.6), `pressure.overflow_with` (0.0) |
| 2 | 7 more of 25 tasks, p = 0.016 | `results/summary/tldr.json` | `pressure.resolved.only_other` (7), `.only_base` (0), `.sign_test_p` (0.015625), `pressure.tasks` (25) |
| 2 | ×1.29 (95% CI 1.05–1.41) | `results/summary/ablation.json` | `rows[id=A2].paired.cost_usd.median_ratio` and `.ci95` |
| 2 | did not hold up when pooled (no number) | `docs/notes.md` §29, 2026-10-21 supplement | qualitative; the pooled A2 / M0 cost interval contains 1 |
| 2 | 98.2% | `results/summary/tldr.json` | `main.cache_hit_rate` (0.982) |
| 3 | 0.25% | `results/summary/security.json` | `false_block_rate` (0.0025) |
| 3 | 2,793 tool calls | `results/summary/security.json` | `decisions` |
| 3 | every block reviewed | `results/summary/security.json` | `denies` (7), `false_blocks` (7), `unreviewed` (0) |

## Wording limits (do not drop them)

- **Bullet 1 is not a win.** The two resolve rates cannot be told apart, and harness-lab costs more per task. Keep "not statistically distinguishable", and do not write "outperforms" or "matches".
- **Bullet 2's tools clause is single-run.** The ×1.29 comes from the pre-registered A2 vs. M0-r2 comparison (one run each). Pooling the repeated runs of A2 and M0 gives an interval that contains 1 (`docs/notes.md` §29). Keep "single-run" and "did not hold up when pooled"; do not write "tools beat bash" or "tools save cost".
- **Bullet 2 is about context pressure.** The overflow result comes from an artificial 32K budget on 25 tasks. At the 128K budget of the main run, compaction almost never happens. The bash-only result also comes from 25 tasks and one run per configuration.
- **Bullet 3 makes no injection claim.** The prompt-injection suite was not run, so the bullet makes no claim about attack success. The false-block rate is all the guard was measured on.
- **CI passed on GitHub** (2026-10-21, run 39629927498 at `0698f91`: lint, the toy sandbox image, all tests including the replay end-to-end test). If a later push breaks it, drop "CI" until it passes again.
