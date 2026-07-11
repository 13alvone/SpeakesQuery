#!/usr/bin/env python3
"""
Alert-group end-to-end diagnostic.
==================================

When an alert group "returns empty" from the UI, the root cause could be
any of four distinct problems:

  A.  The library script hasn't been deployed as a scheduled ingestion
      task yet (so nothing is fetching the data).
  B.  The task exists but has never fired (first run pending; cron hasn't
      ticked yet).
  C.  The task fired but hit errors (missing credentials, API change,
      rate-limited, etc.) - parquet files exist but are empty or stale.
  D.  Data ingested fine, but the saved search's query filter excluded
      everything (threshold too aggressive, or data naturally below it).

Each failure class needs a different fix. This tool walks the full
pipeline per feeder and emits a traffic-light matrix so the operator can
see at a glance which class of problem each feeder is in.

Usage (run from project root, inside the venv or Docker container):

    python -m tools.diagnose_alert_group global_macro_risk_brief
    python -m tools.diagnose_alert_group daily_opportunity_brief
    python -m tools.diagnose_alert_group --all
    python -m tools.diagnose_alert_group global_macro_risk_brief --json

The ``--json`` flag emits a JSON dict instead of the terminal report so
the tool is scriptable (CI health checks, ops dashboards, etc.). Default
is a colored terminal report.

Exit code is 0 on success (all feeders either live or expected-sparse),
1 when any feeder is in a state that needs attention (undeployed,
missing credentials, ingestion failed, filter excluded everything).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path when invoked as a module via
# ``python -m tools.diagnose_alert_group``. Makes ad-hoc execution
# from the Docker container / ssh session "just work" regardless of
# CWD.
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# Feeders in these sets legitimately return zero rows most of the time.
# The diagnostic marks them "sparse OK" rather than raising a red flag
# when their parquet is present but the filter returns no rows.
#
# Rationale:
#   * tropical cyclones: Atlantic basin is inactive Dec-May.
#   * volcanic alerts: elevated volcanoes are globally rare (<30 active
#     at any time; the filter keeps only MODERATE+).
#   * kalshi_poly_arb: cross-platform arbitrage worth >=3% is genuinely
#     rare on a typical day.
#   * gov_contracts: mega/very-large contracts cluster around fiscal
#     quarter ends; sparse between.
#   * reserved_picks: empty on day 1 by construction (no prior picks).
EXPECTED_SPARSE = {
    "gmrb_tropical_cyclones",
    "gmrb_volcanic_activity",
    "dob_kalshi_poly_arb",
    "dob_gov_contracts",
    "dob_reserved_picks",
    "gmrb_reserved_picks",
}


# ANSI terminal colors - suppressed when stdout isn't a TTY (piped to
# a file / CI log capture / JSON consumer).
def _ansi_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_GREY = "\033[90m"


def _c(text: str, *codes: str) -> str:
    if not _ansi_enabled():
        return text
    return "".join(codes) + text + _RESET


def _fmt_age(seconds_ago: float | None) -> str:
    if seconds_ago is None:
        return "never"
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s ago"
    if seconds_ago < 3600:
        return f"{int(seconds_ago / 60)}m ago"
    if seconds_ago < 86400:
        return f"{seconds_ago / 3600:.1f}h ago"
    return f"{seconds_ago / 86400:.1f}d ago"


def _load_group(group_name: str) -> dict:
    """Load an AG YAML by name. Imports inside the function so
    ``--help`` works even when optional deps are missing."""
    from alert_group_store import AlertGroupStore
    store = AlertGroupStore()
    store.initialize()
    group = store.get_group(group_name)
    if group is None:
        available = [g.get("name") for g in store.list_groups()]
        raise SystemExit(
            f"Alert group {group_name!r} not found. "
            f"Available: {', '.join(available) if available else '(none)'}"
        )
    return group


def _resolve_feeders(group: dict) -> list[dict]:
    """Run the existing resolver to get the baseline per-feeder state
    (task deployed? creds present? parquet files present?)."""
    from alert_groups.feeder_status import resolve_alert_group
    from saved_search_store import SavedSearchStore
    from script_library import list_scripts as list_library_scripts
    from scheduled_input_engine.engine import ScheduledInputEngine

    ss_store = SavedSearchStore()
    ss_store.initialize()
    engine = ScheduledInputEngine()
    engine.store.initialize_databases()

    library_scripts = list_library_scripts()
    scheduled_tasks = engine.store.list_scheduled_inputs()

    def saved_search_loader(name: str):
        # Resolver contract: return a dict on success, raise
        # FileNotFoundError when the saved search is missing. The
        # SavedSearchStore raises FileNotFoundError itself, so propagate.
        return ss_store.get_search(name)

    def credentials_lister(task_id: int):
        # Must include globals - a FRED_API_KEY set once in the global
        # vault satisfies every FRED-backed task, and the resolver
        # should reflect that (matches decrypt_for_script semantics).
        return engine._vault.list_keys(task_id, include_global=True)

    default_names = [
        p.stem for p in (_PROJECT_ROOT / "default_saved_searches").glob("*.yaml")
    ]

    indexes_root = _PROJECT_ROOT / "indexes"

    result = resolve_alert_group(
        group,
        saved_search_loader=saved_search_loader,
        library_scripts=library_scripts,
        scheduled_tasks=scheduled_tasks,
        credentials_lister=credentials_lister,
        indexes_root=str(indexes_root),
        default_search_names=default_names,
        template_drift_checker=lambda n: ss_store.template_drift(n),
    )
    return result["feeders"]


def _count_parquet_rows(subdirectory: str | None) -> int:
    """Return the total row count across all parquet files under the
    given subdirectory. Cheap - uses pyarrow metadata, no full scan."""
    if not subdirectory:
        return 0
    root = _PROJECT_ROOT / "indexes" / subdirectory
    if not root.exists():
        return 0
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return -1  # caller can render "?" when pyarrow is unavailable
    total = 0
    for p in sorted(root.glob("*.parquet")):
        try:
            total += pq.ParquetFile(str(p)).metadata.num_rows
        except Exception:
            continue
    return total


def _run_saved_search(search_name: str) -> tuple[int, str]:
    """Run the saved search query and return (row_count, error_or_empty).

    Uses process_query_with_diagnostics so empty results are distinguished
    from load errors.
    """
    from saved_search_store import SavedSearchStore
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics

    store = SavedSearchStore()
    store.initialize()
    try:
        spec = store.get_search(search_name)
    except FileNotFoundError:
        return -1, "saved_search_missing"
    if not spec:
        return -1, "saved_search_missing"
    query = spec.get("query") or ""
    if not query.strip():
        return -1, "empty_query"
    df, _job_id, diagnostic = process_query_with_diagnostics(query)
    if df is None:
        return -1, diagnostic or "error"
    return len(df.index), diagnostic or ""


def _classify_feeder(feeder: dict, raw_rows: int, filtered_rows: int) -> tuple[str, str]:
    """Return (status_label, explanation) based on pipeline signals.

    Classes:
      OK - deployed + ingested + filter returning rows
      SPARSE_OK - sparse by design, zero rows is fine
      FILTER - parquet has raw rows but filter returns zero
      NEVER_RAN - task deployed but never fired (no parquet yet)
      UNDEPLOYED - library script exists but task not deployed
      MISSING - no library script / no saved search match
      CREDS - needed credentials aren't populated
      STALE - parquet old; task not running
      ERROR - saved search threw an exception
    """
    state = feeder.get("state", "")
    search_name = feeder.get("search_name", "")
    sparse = search_name in EXPECTED_SPARSE

    if state == "needs_creds":
        return "CREDS", "Missing credential(s): " + ", ".join(
            feeder.get("missing_credentials") or []
        )
    if state == "needs_deploy":
        return "UNDEPLOYED", (
            "Library script exists; not deployed as a scheduled task. "
            "Use the UI's 'Deploy Missing Feeders' button, or POST "
            "/api/si/deploy-library/" + (feeder.get("library_script_id") or "?")
        )
    if state == "missing_search":
        return "MISSING", "Saved search YAML not present."
    if state == "no_library_script":
        return "MISSING", (
            "No matching library script for this subdirectory - user "
            "must author an ingestion script or remove this feeder."
        )
    if state == "unknown_index":
        return "MISSING", "Saved search has no index=\"...\" clause."
    if state == "disabled":
        return "UNDEPLOYED", "Task is deployed but disabled."

    # state == "live" or "pending"
    if feeder.get("data_file_count", 0) == 0:
        return "NEVER_RAN", (
            "Task deployed but no parquet files yet. Trigger manually "
            "via POST /api/si/run/" + str(feeder.get("task_id") or "?") +
            " or wait for the next cron tick."
        )

    if raw_rows == 0:
        if sparse:
            return "SPARSE_OK", "Expected-sparse feeder; zero raw rows is fine."
        return "STALE", (
            "Parquet files present but every file is empty. Task may be "
            "failing silently - check execution history."
        )

    if filtered_rows == 0:
        if sparse:
            return "SPARSE_OK", (
                f"Expected-sparse feeder; {raw_rows} raw rows ingested but "
                "filter correctly finds nothing today."
            )
        return "FILTER", (
            f"Parquet has {raw_rows} raw rows but the saved-search filter "
            "returns zero. Filter may be too aggressive or data is below "
            "thresholds today. Check the query and try relaxing the "
            "threshold to sanity-check."
        )

    return "OK", f"{filtered_rows} rows (of {raw_rows} raw)"


_STATUS_STYLE = {
    "OK":          ("[OK]     ", (_GREEN, _BOLD)),
    "SPARSE_OK":   ("[sparse] ", (_GREY,)),
    "FILTER":      ("[FILTER] ", (_YELLOW, _BOLD)),
    "NEVER_RAN":   ("[no-run] ", (_YELLOW,)),
    "STALE":       ("[STALE]  ", (_YELLOW, _BOLD)),
    "UNDEPLOYED":  ("[deploy] ", (_RED, _BOLD)),
    "CREDS":       ("[creds]  ", (_RED, _BOLD)),
    "MISSING":     ("[MISSING]", (_RED, _BOLD)),
    "ERROR":       ("[ERROR]  ", (_RED, _BOLD)),
}


def _render_group(group: dict, feeders_augmented: list[dict]) -> str:
    name = group.get("name") or "(unnamed)"
    schedule = group.get("schedule") or "(manual)"
    lines: list[str] = []
    lines.append("")
    lines.append(_c(f"━━━ Alert Group: {name} ━━━", _BOLD, _CYAN))
    lines.append(_c(f"schedule: {schedule}   feeders: {len(feeders_augmented)}", _DIM))
    lines.append("")

    # Column widths
    name_width = max((len(f["feeder"]["search_name"]) for f in feeders_augmented), default=20)
    name_width = max(name_width, 20)

    now = time.time()
    for entry in feeders_augmented:
        fs = entry["feeder"]
        status = entry["status"]
        raw = entry["raw_rows"]
        filtered = entry["filtered_rows"]
        message = entry["explanation"]

        label, codes = _STATUS_STYLE.get(status, ("[?]      ", (_GREY,)))
        colored_label = _c(label, *codes)

        name_col = fs["search_name"].ljust(name_width)
        raw_col = (
            f"raw={raw}".rjust(10)
            if raw >= 0
            else "raw=?     "
        )
        filt_col = (
            f"filt={filtered}".rjust(9)
            if filtered >= 0
            else "filt=?   "
        )
        age = fs.get("last_data_epoch")
        age_col = _fmt_age(now - age if age else None).rjust(10)
        lines.append(
            f"  {colored_label} {name_col}  {raw_col}  {filt_col}  ingested {age_col}"
        )
        if status not in ("OK", "SPARSE_OK") or _ansi_enabled():
            lines.append(_c("           " + message, _DIM))

    # Summary line
    ok = sum(1 for e in feeders_augmented if e["status"] in ("OK",))
    sparse_ok = sum(1 for e in feeders_augmented if e["status"] == "SPARSE_OK")
    red = sum(1 for e in feeders_augmented if e["status"] in ("UNDEPLOYED", "CREDS", "MISSING", "ERROR"))
    yellow = sum(1 for e in feeders_augmented if e["status"] in ("FILTER", "NEVER_RAN", "STALE"))
    lines.append("")
    lines.append(
        _c(
            f"  SUMMARY: {ok} live, {sparse_ok} sparse-ok, "
            f"{yellow} warn, {red} blocked",
            _BOLD,
        )
    )
    lines.append("")
    return "\n".join(lines)


def diagnose(group_name: str) -> dict:
    """Return the diagnostic dict for one alert group (used by CLI + tests)."""
    group = _load_group(group_name)
    feeders = _resolve_feeders(group)
    augmented: list[dict] = []
    for fs in feeders:
        raw_rows = _count_parquet_rows(fs.get("subdirectory"))
        search_name = fs.get("search_name") or ""
        filtered_rows, diag = _run_saved_search(search_name)
        status, explanation = _classify_feeder(fs, raw_rows, filtered_rows)
        if diag and diag not in ("", "empty:no_rows"):
            # Surface hard errors (not just "empty result") in the tail
            if not explanation.startswith("(error)"):
                explanation = explanation + f"  (query diagnostic: {diag})"
        augmented.append({
            "feeder": fs,
            "raw_rows": raw_rows,
            "filtered_rows": filtered_rows,
            "status": status,
            "explanation": explanation,
        })
    return {
        "group_name": group.get("name"),
        "schedule": group.get("schedule"),
        "feeder_count": len(augmented),
        "feeders": augmented,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose alert-group end-to-end: deployment, "
                    "credentials, ingestion, data, filter.",
    )
    parser.add_argument(
        "group_name", nargs="?",
        help="Alert group name (e.g. global_macro_risk_brief). "
             "Omit when using --all.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Diagnose every alert group on the system.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the terminal report.",
    )
    args = parser.parse_args(argv)

    if not args.group_name and not args.all:
        parser.error("specify a group_name, or pass --all")

    from alert_group_store import AlertGroupStore
    store = AlertGroupStore()
    store.initialize()
    if args.all:
        names = [g.get("name") for g in store.list_groups() if g.get("name")]
    else:
        names = [args.group_name]

    all_results = []
    had_problem = False
    for name in names:
        try:
            result = diagnose(name)
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            return 1
        all_results.append(result)
        for entry in result["feeders"]:
            if entry["status"] in ("UNDEPLOYED", "CREDS", "MISSING",
                                    "FILTER", "NEVER_RAN", "STALE", "ERROR"):
                had_problem = True

    if args.json:
        # JSON output - strip dataclass fields that don't serialise cleanly.
        json.dump(all_results, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        for result in all_results:
            sys.stdout.write(_render_group(
                {"name": result["group_name"], "schedule": result["schedule"]},
                result["feeders"],
            ))

    return 1 if had_problem else 0


if __name__ == "__main__":
    sys.exit(main())
