### Ablations (one factor changed at a time)

Each ablation against `M0-r2` (the main configuration rerun on the same commit with the same worker count) on the ablation's own tasks; ratios are medians of the per-task ratio with a bootstrap 95% interval. "Only abl / only base" counts the tasks only one side resolved, with an exact sign test.

| ID | Change vs. M0 | Tasks | Resolved | Only abl / only base | $/task | Cost ratio | Steps ratio | Overflow rate |
|---|---|---|---|---|---|---|---|---|
| A2 | action space = bash only | 25 (`verified_25`) | 23/25 (base 24) | 1 / 2 (p = 1) | $0.211 | 1.29 (1.05–1.41) | 1.11 (1.04–1.29) | 0.0% |
| A5 | planning = off | 25 (`verified_25`) | 22/25 (base 24) | 0 / 2 (p = 0.5) | $0.168 | 1.05 (0.84–1.27) | 1.07 (0.81–1.26) | 0.0% |
| A6 | context budget = 32k (hybrid) | 25 (`verified_25`) | 22/25 (base 24) | 0 / 2 (p = 0.5) | $0.250 | 1.33 (0.99–1.60) | 1.13 (1.00–1.36) | 0.0% |
| A6xA1 | context budget = 32k, context = none | 25 (`verified_25`) | 15/25 (base 24) | 0 / 9 (p = 0.0039) | $0.080 | 0.71 (0.53–0.86) | 0.71 (0.56–0.86) | 60.0% |
| (noise) | none: `M0` against `M0-r2`, same config | 25 | — | 0 / 2 (p = 0.5) | — | 1.04 (0.88–1.30) | 1.00 (0.93–1.21) | — |
| (noise) | none: `M0-r2` against `M0`, same config | 25 | — | 2 / 0 (p = 0.5) | — | 0.96 (0.77–1.13) | 1.00 (0.83–1.08) | — |

Not run: A1, A3, A4. `M0-r2` compacted in 0 of its tasks at the 128k budget, so at that budget the context strategy never acts.
Context pressure (32k): A6 (compaction on) against A6xA1 (off) on the same tasks: overflow 0.0% vs 60.0%; resolved only with compaction / only without: 7 / 0 (p = 0.016); cost ratio 2.03 (1.24–2.86), steps ratio 1.59 (1.14–2.12) (runs that overflow stop early, so they are cheaper).
The two noise rows are the same pair of runs of the main configuration, each way round; the direction of a ratio is arbitrary, so both are shown. One run per configuration.
