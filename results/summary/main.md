### Main comparison

Subset `verified_50`, 50 tasks; same model and config for both (config 6ea20eaecc69) (run ids `M0`, `B0`).

| System | Resolve rate (95% CI) | $/task mean (P50) | $/resolved | Tokens in/out (mean) | Cache hit | P50 / P95 time | Steps (mean) |
|---|---|---|---|---|---|---|---|
| harness-lab (M0) | 37/50 = 74.0% (60.4%–84.1%) | $0.206 ($0.116) | $0.278 | 1910k / 20k | 98.2% | 224 s / 944 s | 53.4 |
| mini-SWE-agent (B0) | 39/50 = 78.0% (64.8%–87.2%) | $0.161 ($0.108) | $0.206 | 1293k / 17k | 97.8% | 187 s / 769 s | 41.7 |

Task by task, M0 against B0 (50 tasks; median of the per-task ratio with a bootstrap 95% interval):

| Measure | Median ratio M0/B0 (95% CI) | Tasks where M0 is higher |
|---|---|---|
| Cost | 1.25 (1.18–1.38) | 39/50 |
| Steps | 1.32 (1.21–1.42) | 42/50 |
| Wall time | 1.32 (1.18–1.44) | 39/50 |

Resolved by both: 36; only M0: 1; only B0: 3; exact sign test p = 0.625.

Notes: workers per run: M0 1/2, B0 1. Wall time is only comparable between runs with the same worker count made in the same period; B0's wall time runs from its first to its last model response, so it misses the wait for the first one.
