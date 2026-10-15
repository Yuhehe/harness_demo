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
