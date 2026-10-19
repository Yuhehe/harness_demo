### Permission guard

Prompt-injection suite: not run (optional, plan.md).

| Run | Tasks | Guard checks (bash) | Blocks | Reviewed: false / true / unreviewed | False-block rate (all calls) | False-block rate (bash) |
|---|---|---|---|---|---|---|
| M0 | 50 | 2793 (1916) | 7 | 7 / 0 / 0 | 0.25% | 0.37% |

Every block was reviewed by hand (docs/failure_analysis.md); the rule matches commands by text, so it is defence in depth, not a proof. The sandbox (no network, resource caps) is the boundary.
