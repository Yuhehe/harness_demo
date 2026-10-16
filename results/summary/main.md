### Main comparison

Subset `verified_25`, 25 tasks; same model and config for both (config 6ea20eaecc69) (run ids `M0`, `B0`).

| System | Resolve rate (95% CI) | $/task mean (P50) | $/resolved | Tokens in/out (mean) | Cache hit | P50 / P95 time | Steps (mean) |
|---|---|---|---|---|---|---|---|
| harness-lab (M0) | 22/25 = 88.0% (70.0%–95.8%) | $0.175 ($0.087) | $0.198 | 1561k / 18k | 98.2% | 216 s / 888 s | 48.5 |
| mini-SWE-agent (B0) | 22/25 = 88.0% (70.0%–95.8%) | $0.126 ($0.107) | $0.143 | 894k / 14k | 97.3% | 197 s / 623 s | 36.5 |

Task by task, M0 against B0 (25 tasks; median of the per-task ratio with a bootstrap 95% interval):

| Measure | Median ratio M0/B0 (95% CI) | Tasks where M0 is higher |
|---|---|---|
| Cost | 1.34 (1.19–1.53) | 20/25 |
| Steps | 1.42 (1.23–1.51) | 21/25 |
| Wall time | 1.50 (1.32–1.67) | 22/25 |

Resolved by both: 22; only M0: 0; only B0: 0; exact sign test p = 1.

Notes: workers per run: M0 1, B0 1. Wall time is only comparable between runs with the same worker count made in the same period; B0's wall time runs from its first to its last model response, so it misses the wait for the first one.
