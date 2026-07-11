#!/usr/bin/env python3
"""Audit deployed ingestion tasks for code drift from the script library.

Library updates (e.g. shipping a backlog fix) commit a new version to
``script_library/scripts/<name>.json``, but the running task in
``scheduled_inputs.db`` retains the OLD code from when it was first deployed.
Without an explicit redeploy, the running cron keeps firing the stale code.

Caught 2026-05-03: 14 of 57 deployed tasks had significant drift (>100 chars)
from the library, including every backlog #1-#11 fix + Backlog #6/#7 chain-
derive parity fix. The FDA Adverse Event Signals task had been firing 404 every
day for 7+ days because the deployed code was the pre-fix version.

Usage:
    # Read-only audit (default):
    python -m tools.audit_deployed_task_drift

    # Apply: PUT library code to every drifted task with diff >= threshold:
    python -m tools.audit_deployed_task_drift --apply

    # Tighter threshold:
    python -m tools.audit_deployed_task_drift --threshold 1 --apply

    # Different box:
    python -m tools.audit_deployed_task_drift --box http://localhost:5111
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--box", default="http://localhost:5111", help="API base URL")
    parser.add_argument("--threshold", type=int, default=100, help="Minimum char-diff to flag/apply (default: 100; set to 1 to catch trivia)")
    parser.add_argument("--apply", action="store_true", help="Push library code to drifted tasks (default: read-only)")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent), help="Project root containing script_library/")
    args = parser.parse_args()

    project_root = Path(args.project_root)
    scripts_dir = project_root / "script_library" / "scripts"
    if not scripts_dir.is_dir():
        print(f"ERROR: {scripts_dir} not found", file=sys.stderr)
        return 1

    # Index library by title (the same key used by the ingestion task store)
    library: dict[str, tuple[str, str]] = {}
    for path in scripts_dir.glob("*.json"):
        try:
            d = json.loads(path.read_text())
            title = d.get("title", "")
            if title:
                library[title] = (path.stem, d.get("code", ""))
        except Exception as exc:
            print(f"  WARN: cannot read {path.name}: {exc}", file=sys.stderr)

    print(f"Library: {len(library)} scripts indexed from {scripts_dir}")

    # Pull deployed tasks
    try:
        r = requests.get(f"{args.box}/api/si/list", timeout=10)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
    except Exception as exc:
        print(f"ERROR: cannot reach {args.box}/api/si/list: {exc}", file=sys.stderr)
        return 1

    print(f"Deployed: {len(tasks)} tasks (querying {args.box})")

    # Compare
    drift: list[tuple[int, str, str, int, int, int]] = []
    no_match: list[tuple[int, str]] = []
    matching = 0
    for t in tasks:
        if t.get("disabled"):
            continue
        title = t.get("title", "")
        deployed_code = t.get("code", "")
        if title not in library:
            no_match.append((t["id"], title))
            continue
        lib_name, lib_code = library[title]
        if deployed_code == lib_code:
            matching += 1
            continue
        size_diff = abs(len(deployed_code) - len(lib_code))
        drift.append((t["id"], title, lib_name, len(deployed_code), len(lib_code), size_diff))

    drift.sort(key=lambda x: -x[5])

    print()
    print(f"=== Audit results ===")
    print(f"  ✓ Match library: {matching}")
    print(f"  ⚠️  Drift from library: {len(drift)}")
    print(f"  ❓ No matching library entry: {len(no_match)}")

    significant = [d for d in drift if d[5] >= args.threshold]
    trivial = [d for d in drift if d[5] < args.threshold]

    if drift:
        print(f"\n  Drift breakdown (threshold={args.threshold}):")
        print(f"    Significant: {len(significant)}")
        print(f"    Trivial (likely whitespace): {len(trivial)}")
        print()
        print(f"  {'id':<5} {'title':<48} {'drift':<10}")
        for tid, title, lib_name, d_len, l_len, _ in drift:
            marker = "⚠️ " if abs(d_len - l_len) >= args.threshold else "·  "
            print(f"  {marker} {tid:<5} {title[:46]:<48} {l_len - d_len:+d}")

    if no_match:
        print(f"\n  Deployed tasks with no library match (orphan deploys):")
        for tid, title in sorted(no_match, key=lambda x: x[1]):
            print(f"    id={tid:<5} {title}")

    if not args.apply:
        if significant:
            print(f"\n💡 Re-run with --apply to push library code to {len(significant)} drifted tasks.")
        return 0 if not significant else 2

    # APPLY mode: PUT library code to each significantly-drifted task
    print(f"\nApplying - pushing library code to {len(significant)} task(s)...")
    ok, fail = 0, []
    for tid, title, lib_name, d_len, l_len, _ in significant:
        _, lib_code = library[title]
        try:
            r = requests.put(f"{args.box}/api/si/{tid}", json={"code": lib_code}, timeout=15)
            r.raise_for_status()
            body = r.json()
            if body.get("status") == "success":
                ok += 1
                print(f"  ✓ id={tid} {title[:46]}")
                continue
            fail.append((tid, title, body.get("message", "?")))
        except Exception as exc:
            fail.append((tid, title, str(exc)[:120]))
        print(f"  ✗ id={tid} {title}: {fail[-1][2]}")

    print(f"\nResult: {ok}/{len(significant)} updated")
    return 0 if ok == len(significant) else 1


if __name__ == "__main__":
    sys.exit(main())
