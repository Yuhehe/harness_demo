"""Pre-pull the SWE-bench instance images listed in the subset files.

Why: Docker SDK / swebench pull missing images on demand, which would put download time
inside a timed task (latency metrics, time_limit) and mini-swe-agent's pull_timeout is
only 120 s. See docs/notes.md §2 and §8.

Image names come from the dataset's `image` field (pinned to the revision recorded in
subsets_meta.json), never hand-built; the naming rule is only asserted against it.
Resumable: images already present are skipped, state is kept per instance_id.

    uv run python eval/swebench/pull_images.py --dry-run
    mkdir -p results/runs/pull_images && nohup uv run python eval/swebench/pull_images.py \
        --yes --verify --workers 2 --min-free-gb 35 > results/runs/pull_images/pull.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from requests.exceptions import RequestException
from select_subset import META_NAME, SUBSET_DIR, load_rows, read_ids

PLATFORM = "linux/amd64"
STATUS_DIR = Path("results/runs/pull_images")
MIN_FREE_GB = 20.0
RETRIES = 3
BACKOFF_S = 5
# Fixed verification command (not model output). The image's HEAD is an empty "SWE-bench"
# commit on top of base_commit (docs/notes.md §2), so base_commit may be HEAD or HEAD^.
VERIFY_CMD = [
    "bash",
    "-lc",
    (
        "cd /testbed && echo head=$(git rev-parse HEAD)"
        " && echo parent=$(git rev-parse HEAD^ 2>/dev/null || echo none)"
        " && echo dirty=$(git status --porcelain | wc -l)"
        " && (command -v rg >/dev/null && echo rg=yes || echo rg=no)"
    ),
]

log = logging.getLogger("pull_images")


def expected_image(instance_id: str) -> str:
    """The naming rule from swebench/image_builder/image_spec.py (docs/notes.md §2)."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def is_present(client, image: str) -> bool:
    try:
        client.images.get(image)
        return True
    except ImageNotFound:
        return False


def pull_one(
    client_factory, image: str, retries=RETRIES, backoff=BACKOFF_S, sleep=time.sleep
):
    """Pull with retries on transient errors. Returns a status fragment."""
    repo, tag = image.rsplit(":", 1)
    start, last = time.time(), None
    for attempt in range(1, retries + 1):
        try:
            client_factory().images.pull(repo, tag=tag, platform=PLATFORM)
            return {
                "state": "done",
                "attempts": attempt,
                "seconds": round(time.time() - start, 1),
            }
        except (ImageNotFound, NotFound) as e:
            return {
                "state": "failed",
                "attempts": attempt,
                "error": f"not found: {e}"[:300],
            }
        except (APIError, RequestException) as e:
            last = e
            if (
                getattr(e, "status_code", None) == 429
                or "toomanyrequests" in str(e).lower()
            ):
                return {
                    "state": "rate_limited",
                    "attempts": attempt,
                    "error": str(e)[:300],
                }
            if attempt < retries:
                sleep(backoff * 2 ** (attempt - 1))
    return {"state": "failed", "attempts": retries, "error": str(last)[:300]}


def verify_one(client, image: str, base_commit: str) -> dict:
    try:
        out = client.containers.run(
            image,
            VERIFY_CMD,
            network_mode="none",
            remove=True,
            mem_limit="512m",
            pids_limit=64,
        )
        info = dict(line.split("=", 1) for line in out.decode().split())
        return {
            "base_commit_ok": base_commit in (info["head"], info["parent"]),
            "clean": info["dirty"] == "0",
            "has_rg": info["rg"] == "yes",
        }
    except Exception as e:  # noqa: BLE001 - record and keep going
        return {"verify_error": str(e)[:300]}


def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1e9


def run(
    ids,
    rows,
    client_factory,
    *,
    status_path: Path,
    workers=3,
    min_free_gb=MIN_FREE_GB,
    verify=False,
    disk_path="/",
    free_gb_fn=free_gb,
    pull=pull_one,
):
    """Pull (and optionally verify) everything not yet done. Returns the status dict."""
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    lock, stop = threading.Lock(), threading.Event()
    counter = {"n": 0}

    def save():
        status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
        tmp.replace(status_path)

    def work(iid):
        image = rows[iid]["image"]
        client = client_factory()
        entry = dict(status.get(iid, {}), image=image)
        if not is_present(client, image):
            if stop.is_set():
                return
            if free_gb_fn(disk_path) < min_free_gb:
                log.warning(
                    "free space below %.0f GB, leaving %s pending", min_free_gb, iid
                )
                stop.set()
                return
            entry.pop("error", None)
            entry.update(pull(client_factory, image))
            if entry["state"] == "rate_limited":
                stop.set()
        else:
            entry.update(state="done")
            entry.setdefault("attempts", 0)
        if entry["state"] == "done" and verify and "base_commit_ok" not in entry:
            entry.update(verify_one(client, image, rows[iid]["base_commit"]))
        entry["free_gb"] = round(free_gb_fn(disk_path), 1)
        with lock:
            status[iid] = entry
            save()
            counter["n"] += 1
            log.info(
                "[%d/%d] %s %s",
                counter["n"],
                len(ids),
                iid,
                {k: v for k, v in entry.items() if k not in ("image",)},
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, ids))
    return status


def summarize(ids, status, verify) -> int:
    states = [status.get(i, {}).get("state", "pending") for i in ids]
    log.info(
        "done=%d failed=%d rate_limited=%d pending=%d",
        states.count("done"),
        states.count("failed"),
        states.count("rate_limited"),
        states.count("pending"),
    )
    bad = [
        i for i in ids if verify and status.get(i, {}).get("base_commit_ok") is not True
    ]
    if verify and bad:
        log.warning("verification not ok for: %s", bad)
    return 0 if states.count("done") == len(ids) and not bad else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--subsets",
        nargs="+",
        type=Path,
        default=[SUBSET_DIR / "verified_50.txt", SUBSET_DIR / "dev_5.txt"],
    )
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB)
    p.add_argument("--status", type=Path, default=STATUS_DIR / "status.json")
    p.add_argument(
        "--verify",
        action="store_true",
        help="check /testbed HEAD == base_commit in a network-less throwaway container",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--yes", action="store_true", help="skip the confirmation for >10 images"
    )
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # HF client chatter

    ids = list(dict.fromkeys(i for path in args.subsets for i in read_ids(path)))
    meta = json.loads((SUBSET_DIR / META_NAME).read_text())
    all_rows, revision = load_rows(meta["dataset_id"], meta["split"], meta["revision"])
    rows = {r["instance_id"]: r for r in all_rows}
    unknown = [i for i in ids if i not in rows]
    if unknown:
        raise SystemExit(f"not in dataset @ {revision}: {unknown[:3]}")
    for i in ids:
        if rows[i]["image"] != expected_image(i):
            raise SystemExit(f"image name rule mismatch for {i}: {rows[i]['image']}")

    client = docker.from_env()
    root = client.info().get("DockerRootDir", "/")
    present = [i for i in ids if is_present(client, rows[i]["image"])]
    todo = [i for i in ids if i not in present]
    log.info(
        "%d images: %d present, %d to pull; free %.1f GB at %s (floor %.0f GB)",
        len(ids),
        len(present),
        len(todo),
        free_gb(root),
        root,
        args.min_free_gb,
    )
    log.info(
        "anonymous Docker Hub pulls are rate-limited; stops on 429. Real disk cost: docs/notes.md §7"
    )
    if args.dry_run:
        return 0
    if len(todo) > 10 and not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("more than 10 images: pass --yes (or run interactively)")
        if input(f"pull {len(todo)} images? [y/N] ").strip().lower() != "y":
            return 2

    status = run(
        ids,
        rows,
        docker.from_env,
        status_path=args.status,
        workers=args.workers,
        min_free_gb=args.min_free_gb,
        verify=args.verify,
        disk_path=root,
    )
    return summarize(ids, status, args.verify)


if __name__ == "__main__":
    sys.exit(main())
