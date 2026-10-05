"""Select the SWE-bench Verified subsets used by every experiment.

Writes configs/subsets/verified_50.txt, dev_5.txt (disjoint, one instance_id per line,
sorted) and subsets_meta.json (seed, dataset revision, quotas, actual counts).
The result is a pure function of (dataset revision, seed, quotas), so a rerun is
byte-identical. Existing files are never overwritten without --force: once the
experiments start, these files are frozen (CLAUDE.md §2.4).

    uv run python eval/swebench/select_subset.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATASET_ID = "SWE-bench/SWE-bench_Verified"
SPLIT = "test"
SEED = 42
VERIFIED_SIZE = 50
DEV_SIZE = 5
# Quotas are justified by task counts only (django 231, sympy 75, sphinx 44, sklearn 32).
DEFAULT_QUOTAS = {
    "django/django": 20,
    "sympy/sympy": 15,
    "sphinx-doc/sphinx": 10,
    "scikit-learn/scikit-learn": 10,
}
DEV_DIFFICULTY = "<15 min fix"  # dev_5 is for smoke tests, so take the easy ones
MIN_DEV_REPOS = 3
ALGORITHM = (
    "v1: per repo (sorted) rng.sample over id-sorted rows; dev_5 = round-robin over "
    "repos among chosen rows with the dev difficulty, remainder = verified_50"
)
COLUMNS = ("instance_id", "repo", "difficulty", "image", "base_commit")
SUBSET_DIR = Path("configs/subsets")
META_NAME = "subsets_meta.json"


def resolve_revision(dataset_id: str, revision: str | None) -> str:
    if revision:
        return revision
    from huggingface_hub import HfApi

    return HfApi().dataset_info(dataset_id).sha


def load_rows(
    dataset_id: str = DATASET_ID, split: str = SPLIT, revision: str | None = None
) -> tuple[list[dict], str]:
    """Load the needed columns at a pinned revision. Returns (rows, revision)."""
    from datasets import load_dataset

    revision = resolve_revision(dataset_id, revision)
    ds = load_dataset(dataset_id, split=split, revision=revision)
    ds = ds.select_columns(list(COLUMNS))
    return [dict(row) for row in ds], revision


def read_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def select_subsets(
    rows: list[dict],
    quotas: dict[str, int],
    seed: int,
    dev_size: int = DEV_SIZE,
    verified_size: int = VERIFIED_SIZE,
    dev_difficulty: str = DEV_DIFFICULTY,
) -> tuple[list[dict], list[dict]]:
    """Pure selection. Returns (verified_rows, dev_rows), each sorted by instance_id."""
    if sum(quotas.values()) != verified_size + dev_size:
        raise ValueError(
            f"quotas sum to {sum(quotas.values())}, expected {verified_size + dev_size}"
        )
    ids = [r["instance_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate instance_id in dataset rows")
    no_image = [r["instance_id"] for r in rows if not r.get("image")]
    if no_image:
        raise ValueError(f"rows without image field: {no_image[:3]}...")

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["instance_id"]):
        by_repo[r["repo"]].append(r)

    rng = random.Random(seed)  # one RNG, fixed call order
    chosen: list[dict] = []
    for repo in sorted(quotas):
        pool = by_repo.get(repo, [])
        if quotas[repo] > len(pool):
            raise ValueError(f"{repo}: quota {quotas[repo]} > {len(pool)} available")
        chosen += rng.sample(pool, quotas[repo])

    cands: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(chosen, key=lambda r: r["instance_id"]):
        if r["difficulty"] == dev_difficulty:
            cands[r["repo"]].append(r)
    repos = sorted(cands)
    for repo in repos:
        rng.shuffle(cands[repo])
    dev: list[dict] = []
    while len(dev) < dev_size and any(cands[r] for r in repos):
        for repo in repos:
            if cands[repo] and len(dev) < dev_size:
                dev.append(cands[repo].pop())
    if len(dev) < dev_size:
        raise ValueError(
            f"only {len(dev)} '{dev_difficulty}' tasks available for dev set"
        )
    if len({r["repo"] for r in dev}) < MIN_DEV_REPOS:
        raise ValueError(f"dev set spans fewer than {MIN_DEV_REPOS} repos")

    dev_ids = {r["instance_id"] for r in dev}
    verified = [r for r in chosen if r["instance_id"] not in dev_ids]
    return (
        sorted(verified, key=lambda r: r["instance_id"]),
        sorted(dev, key=lambda r: r["instance_id"]),
    )


def _describe(rows: list[dict]) -> dict:
    return {
        "count": len(rows),
        "repos": dict(sorted(Counter(r["repo"] for r in rows).items())),
        "difficulty": dict(sorted(Counter(r["difficulty"] for r in rows).items())),
    }


def build_meta(verified, dev, *, dataset_id, split, revision, seed, quotas) -> dict:
    return {
        "algorithm": ALGORITHM,
        "dataset_id": dataset_id,
        "split": split,
        "revision": revision,
        "seed": seed,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "quotas": dict(sorted(quotas.items())),
        "dev_difficulty": DEV_DIFFICULTY,
        "verified_50": _describe(verified),
        "dev_5": _describe(dev),
    }


def write_outputs(
    out_dir: Path, verified, dev, meta: dict, force: bool = False
) -> list[Path]:
    paths = {
        "verified": out_dir / "verified_50.txt",
        "dev": out_dir / "dev_5.txt",
        "meta": out_dir / META_NAME,
    }
    existing = [p for p in paths.values() if p.exists()]
    if existing and not force:
        raise FileExistsError(
            f"refusing to overwrite (use --force): {[str(p) for p in existing]}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    paths["verified"].write_text("".join(f"{r['instance_id']}\n" for r in verified))
    paths["dev"].write_text("".join(f"{r['instance_id']}\n" for r in dev))
    paths["meta"].write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return list(paths.values())


def parse_quotas(items: list[str] | None) -> dict[str, int]:
    if not items:
        return dict(DEFAULT_QUOTAS)
    quotas = {}
    for item in items:
        repo, _, n = item.partition("=")
        quotas[repo] = int(n)
    return quotas


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument(
        "--revision",
        help="HF dataset revision; default: resolve the latest and record it",
    )
    p.add_argument(
        "--quota",
        action="append",
        metavar="REPO=N",
        help="repeatable; default: built-in quotas",
    )
    p.add_argument("--out-dir", type=Path, default=SUBSET_DIR)
    p.add_argument(
        "--force", action="store_true", help="overwrite existing subset files"
    )
    args = p.parse_args(argv)

    quotas = parse_quotas(args.quota)
    rows, revision = load_rows(revision=args.revision)
    verified, dev = select_subsets(rows, quotas, args.seed)
    meta = build_meta(
        verified,
        dev,
        dataset_id=DATASET_ID,
        split=SPLIT,
        revision=revision,
        seed=args.seed,
        quotas=quotas,
    )
    paths = write_outputs(args.out_dir, verified, dev, meta, force=args.force)

    print(f"dataset {DATASET_ID} @ {revision} ({len(rows)} rows), seed {args.seed}")
    print(f"verified_50: {meta['verified_50']['repos']}")
    print(f"dev_5:       {[r['instance_id'] for r in dev]}")
    print("wrote " + ", ".join(str(x) for x in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
