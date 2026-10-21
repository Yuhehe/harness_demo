"""Policy v1 against v2 on the calls the agent actually made (counterfactual, no LLM call).

    uv run python eval/report/guard_replay.py

Every tool call in the experiment runs that had the guard on (RUNS, all with the frozen v1
policy) is checked again by a v1 guard (the policy file at V1_REF, which must reproduce the
recorded decisions) and by a v2 guard (configs/policy.yaml) holding the task's tracked files.
The tracked files are read from each task's SWE-bench image with `git ls-files` (a harness
command in a throwaway container, no network), cached under results/runs/_analysis/tracked/.

This is a call-by-call counterfactual: once v2 lets a call through, the real run would have
gone on differently, so it says how these calls would be judged, not how the runs would go.

Writes results/runs/_analysis/guard_replay.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from aggregate import load_run

from harness.llm.types import ToolCall
from harness.security.guard import Guard
from harness.security.policy import Policy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval" / "swebench"))
from pull_images import expected_image

RUNS = (
    "M0", "M0-r2", "A2", "A2-v021", "A5", "A6", "A6xA1", "D-v021", "D-v03", "M0-v02",
    "lite-v02",
)  # fmt: skip
V1_REF = "v0.1-frozen"
WORKDIR = "/testbed"


def v1_policy() -> Policy:
    text = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{V1_REF}:configs/policy.yaml"],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
    return Policy.load(f.name, WORKDIR)


def tracked(instance_id: str, cache: Path, client: Any) -> frozenset[str]:
    path = cache / f"{instance_id}.txt"
    if not path.exists():
        out = client.containers.run(
            expected_image(instance_id),
            ["git", "-C", WORKDIR, "ls-files", "-z"],
            remove=True,
            network_disabled=True,
        )
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(out)
    return frozenset(p for p in path.read_text().split("\0") if p.strip())


def checked_calls(t: Any) -> list[ToolCall]:
    """The calls the guard saw, in order: every call in the history until a submit succeeded
    (later calls are answered without being checked). A submit that failed (e.g. a bad
    argument) does not end the task, so the calls after it were checked."""
    replies = {
        m.get("tool_call_id"): str(m.get("content", ""))
        for m in t.messages
        if m.get("role") == "tool"
    }
    out: list[ToolCall] = []
    submitted = False
    for m in t.messages:
        if m.get("role") != "assistant":
            continue
        for c in m.get("tool_calls") or []:
            if submitted:
                continue
            fn = c.get("function", {})
            out.append(
                ToolCall(c.get("id", ""), fn.get("name", ""), fn.get("arguments") or "")
            )
            if fn.get("name") == "submit" and replies.get(c.get("id"), "").startswith(
                "Submitted"
            ):
                submitted = True
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results-dir", type=Path, default=ROOT / "results/runs")
    ap.add_argument(
        "--out", type=Path, default=ROOT / "results/runs/_analysis/guard_replay.json"
    )
    args = ap.parse_args(argv)

    import docker

    client = docker.from_env()
    p1 = v1_policy()
    p2 = Policy.load(ROOT / "configs/policy.yaml", WORKDIR)
    cache = args.results_dir / "_analysis" / "tracked"
    report: dict[str, Any] = {"v1_hash": p1.hash, "v2_hash": p2.hash, "runs": {}}
    for run in RUNS:
        tasks = load_run(args.results_dir / run)
        r = {"tasks": len(tasks), "calls": 0, "recorded_not_allowed": 0, "v1_not_allowed": 0,
             "v2_not_allowed": 0, "v1_matches_recorded": True, "unmatched_tasks": [],
             "now_allowed": [], "now_blocked": [], "still_blocked": []}  # fmt: skip
        for iid, t in sorted(tasks.items()):
            start = next((s for s in t.spans if s["type"] == "run_start"), {})
            if start.get("policy_hash") != p1.hash:
                raise SystemExit(
                    f"{run}/{iid}: policy {start.get('policy_hash')} is not {V1_REF}'s"
                )
            calls = checked_calls(t)
            g1 = Guard(p1)
            g2 = Guard(p2, tracked=tracked(iid, cache, client))
            d1 = [g1.check(c) for c in calls]
            d2 = [g2.check(c) for c in calls]
            recorded = [
                (s["action"], s["rule_id"])
                for s in t.spans
                if s["type"] == "guard_decision"
            ]
            if recorded != [(d.action, d.rule_id) for d in d1]:
                r["v1_matches_recorded"] = False
                r["unmatched_tasks"].append(iid)
            r["calls"] += len(calls)
            r["recorded_not_allowed"] += sum(a != "allow" for a, _ in recorded)
            r["v1_not_allowed"] += sum(not d.allowed for d in d1)
            r["v2_not_allowed"] += sum(not d.allowed for d in d2)
            for c, a, b in zip(calls, d1, d2, strict=True):
                if a.allowed and b.allowed:
                    continue
                try:
                    text = json.loads(c.arguments).get("command") or c.arguments
                except (json.JSONDecodeError, AttributeError):
                    text = c.arguments
                row = {"task": iid, "tool": c.name, "v1": a.rule_id, "v2": b.rule_id,
                       "call": str(text)[:200]}  # fmt: skip
                key = "still_blocked" if not (a.allowed or b.allowed) else (
                    "now_allowed" if b.allowed else "now_blocked")  # fmt: skip
                r[key].append(row)
        report["runs"][run] = r
        print(f"{run:9} calls {r['calls']:5}  recorded {r['recorded_not_allowed']}  "
              f"v1 {r['v1_not_allowed']} (matches recorded: {r['v1_matches_recorded']})  "
              f"v2 {r['v2_not_allowed']}  now allowed {len(r['now_allowed'])}  "
              f"now blocked {len(r['now_blocked'])}  still blocked {len(r['still_blocked'])}")  # fmt: skip
    for run, r in report["runs"].items():
        for key in ("now_blocked", "still_blocked", "now_allowed"):
            for row in r[key]:
                print(f"  {key:13} {run:8} {row['task']:32} {row['v1']} -> {row['v2']}: "
                      f"{row['call'][:110]!r}")  # fmt: skip
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
