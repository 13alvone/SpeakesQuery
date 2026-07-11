"""
Alert Group feeder status resolver.

Walks the dependency chain  Alert Group → saved searches → index paths →
library scripts → scheduled ingestion tasks → credentials → data  and
reports a per-feeder health verdict plus an aggregate summary.

Pure functions (no Flask), so it's straightforward to unit-test and to
re-use from both the UI API and any future CLI / scheduled health check.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Match index="..." (or index='...') inside an SPQL query string ──────
_INDEX_RX = re.compile(r'''index\s*=\s*["']([^"']+)["']''')

# Derivable terminal states, ranked from best to worst.  The aggregate
# "overall" verdict of an alert group is the worst state across its feeders.
STATE_RANK = {
    "live":              0,
    "pending":           1,
    "disabled":          2,
    "needs_creds":       3,
    "needs_deploy":      4,
    "no_library_script": 5,
    "missing_search":    6,
    "unknown_index":     7,
}


@dataclass
class FeederStatus:
    search_name: str
    state: str  # one of STATE_RANK keys
    index_paths: list[str] = field(default_factory=list)
    # Resolved subdirectory (normalized index path, e.g. "polymarket/high_probability_pro")
    subdirectory: str | None = None
    # Matched library script id (e.g. "polymarket_high_probability_pro"), if any
    library_script_id: str | None = None
    # Matched scheduled ingestion task id, if deployed
    task_id: int | None = None
    task_enabled: bool | None = None
    required_credentials: list[str] = field(default_factory=list)
    missing_credentials: list[str] = field(default_factory=list)
    data_file_count: int = 0
    last_data_epoch: float | None = None  # max mtime across parquet files
    # True if state==missing_search AND a project-shipped default exists that
    # the user can install with one click.
    installable: bool = False
    message: str = ""
    # Age of the most recent saved-search execution result, in hours.
    # Added 2026-04-20 so the UI can surface "dead feeder" warnings even
    # when the task is nominally `live` but hasn't run in days.
    last_search_run_age_hours: float | None = None
    is_dead_feeder: bool = False
    # Template drift: True when the user's installed saved_searches YAML
    # differs from the git-tracked default_saved_searches template.
    # Added 2026-04-21 after a Daily Brief dispatch produced an empty
    # brief because 4 of 10 installed feeder YAMLs had stale queries
    # (``sort -amount_usd`` after ``| table`` dropped the column, etc.)
    # while the template had already been fixed. The UI surfaces this
    # with a "Sync Template" button that POSTs to
    # /api/alert-groups/<name>/install-default-feeder/<search>?overwrite=true.
    template_drift: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _search_run_age_hours(search_name: str) -> float | None:
    """Return the age of the most recent saved-search execution in hours.

    Reads ``saved_search_history.db`` - the canonical history table
    populated by ``execute_query()``. Returns None when no history exists
    (saved search has never run) OR the file is missing. The age is
    computed from the ``execution_start_time`` column which is a Unix
    epoch seconds float.
    """
    import sqlite3
    import time as _time
    from pathlib import Path as _Path

    history_db = _Path(__file__).resolve().parent.parent / "saved_search_history.db"
    if not history_db.exists():
        return None
    try:
        with sqlite3.connect(str(history_db)) as conn:
            row = conn.execute(
                "SELECT execution_start_time FROM execution_history "
                "WHERE query_name = ? ORDER BY execution_start_time DESC "
                "LIMIT 1",
                (search_name,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return max(0.0, (_time.time() - float(row[0])) / 3600.0)
    except (TypeError, ValueError):
        return None


def _normalize_subdirectory(index_path: str) -> str:
    """
    Strip `indexes/` prefix and any trailing glob/extension tokens to get
    the logical subdirectory used by both library scripts and scheduled
    ingestion tasks.

    Examples:
        "indexes/polymarket/high_probability_pro/*.parquet"
            → "polymarket/high_probability_pro"
        "indexes/sec/major_filings/*"
            → "sec/major_filings"
        "indexes/github/public_events"
            → "github/public_events"
    """
    p = index_path.strip().strip('"').strip("'")
    if p.startswith("indexes/"):
        p = p[len("indexes/"):]
    # Drop the file-glob / extension tail if present
    p = re.sub(r"/\*(\.[\w]+)?$", "", p)
    p = p.rstrip("/")
    return p


def extract_index_paths(query: str) -> list[str]:
    """Return every `index="..."` value found in the raw SPQL query."""
    if not query:
        return []
    return list(_INDEX_RX.findall(query))


def derive_pre_cron(ag_cron: str, offset_minutes: int = 60) -> str | None:
    """
    Derive an ingestion cron that fires ``offset_minutes`` BEFORE each
    fire-time of ``ag_cron``.  The goal is that feeder ingestion runs
    far enough ahead of the alert-group dispatch for the Parquet indexes
    (and the saved-search result caches that depend on them) to be fresh.

    Handles simple expressions with literal minute / hour fields plus
    comma-lists; returns None for anything exotic (`*/5`, `1-8`, etc.)
    so the caller can safely fall back to the library script's
    ``suggested_cron``.

    Examples:
        "0 6,12 * * *", offset=60 -> "0 5,11 * * *"
        "30 8 * * *",  offset=60 -> "30 7 * * *"
        "0 0 * * *",   offset=60 -> None  (would cross into previous day)
        "*/30 * * * *",            -> None  (continuous; fallback)
    """
    if not ag_cron or not isinstance(ag_cron, str):
        return None
    parts = ag_cron.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts

    # Refuse to touch anything with ranges, steps, or wildcards in the
    # minute/hour positions - too easy to shift incorrectly.
    for _f in (minute, hour):
        if _f == "*" or any(c in _f for c in "*/-"):
            return None

    def _ints(fld: str) -> list[int] | None:
        try:
            return sorted({int(tok) for tok in fld.split(",")})
        except ValueError:
            return None

    minutes = _ints(minute)
    hours = _ints(hour)
    if minutes is None or hours is None:
        return None
    if not minutes or not hours:
        return None
    if any(m < 0 or m > 59 for m in minutes):
        return None
    if any(h < 0 or h > 23 for h in hours):
        return None

    # Shift back: first the minute, then any hour rollover.
    offset_h, offset_m = divmod(offset_minutes, 60)
    m0 = min(minutes)
    new_minute = m0 - offset_m
    hour_decrement = offset_h
    if new_minute < 0:
        new_minute += 60
        hour_decrement += 1

    new_hours = [h - hour_decrement for h in hours]
    if any(h < 0 for h in new_hours):
        # Crossing midnight boundary would require shifting dom/dow too,
        # which is not safe to do generically - fall back to the script.
        return None

    hour_str = ",".join(str(h) for h in sorted(set(new_hours)))
    return f"{new_minute} {hour_str} {dom} {month} {dow}"


def _find_library_script_for_subdir(
    subdir: str,
    library_scripts: list[dict],
) -> dict | None:
    """
    Given a normalized subdirectory, return the library script whose
    `suggested_subdirectory` matches exactly.  Returns None if nothing
    matches.
    """
    if not subdir:
        return None
    for s in library_scripts:
        if (s.get("suggested_subdirectory") or "").strip("/") == subdir:
            return s
    return None


def _find_scheduled_task_for_subdir(
    subdir: str,
    tasks: list[dict],
) -> dict | None:
    """
    Return the scheduled ingestion task whose `subdirectory` matches the
    normalized index subdirectory.  Returns the first match; duplicates
    are unusual and the first is the canonical one.
    """
    if not subdir:
        return None
    for t in tasks:
        if (t.get("subdirectory") or "").strip("/") == subdir:
            return t
    return None


def _count_parquet_data(
    indexes_root: str | os.PathLike,
    subdir: str,
) -> tuple[int, float | None]:
    """
    Count parquet files under `<indexes_root>/<subdir>/` and return
    (file_count, max_mtime).  Missing directory → (0, None).
    """
    if not subdir:
        return 0, None
    root = Path(indexes_root) / subdir
    if not root.is_dir():
        return 0, None
    try:
        files = [p for p in root.rglob("*.parquet") if p.is_file()]
    except OSError as exc:
        logger.warning("[!] feeder_status: could not stat %s: %s", root, exc)
        return 0, None
    if not files:
        return 0, None
    max_mtime = max(p.stat().st_mtime for p in files)
    return len(files), max_mtime


def _missing_credentials_for_task(
    task_id: int,
    required: list[str],
    credentials_lister,
) -> list[str]:
    """
    Given the library script's required credential keys, return the subset
    that are NOT present in the vault for this task.  Accepts a callable
    to stay decoupled from the credentials module (easier to test).
    """
    if not required:
        return []
    try:
        present = set(credentials_lister(task_id) or [])
    except Exception as exc:
        logger.warning(
            "[!] feeder_status: list_keys(%s) failed: %s", task_id, exc
        )
        return list(required)
    return [k for k in required if k not in present]


def resolve_feeder(
    search_name: str,
    *,
    saved_search_loader,
    library_scripts: list[dict],
    scheduled_tasks: list[dict],
    credentials_lister,
    indexes_root: str | os.PathLike,
    default_search_names: list[str] | None = None,
    template_drift_checker=None,
) -> FeederStatus:
    """
    Compute a FeederStatus for a single saved search referenced by an AG.

    Loaders are injected to keep this pure and testable:
        saved_search_loader(name) -> dict | raises FileNotFoundError
        credentials_lister(task_id) -> list[str] of stored key names
        default_search_names: list of names available as project-shipped
            templates - drives the `installable` flag when a feeder is
            missing but a default is on disk waiting to be installed.
        template_drift_checker(name) -> dict | None: optional callable
            returning a non-None value when the installed saved search
            differs from the current default template. When provided and
            the feeder is installed, sets ``fs.template_drift=True`` so
            the UI can surface a "Sync Template" action.
    """
    defaults = set(default_search_names or [])
    fs = FeederStatus(search_name=search_name, state="unknown_index")

    # Template-drift check runs regardless of feeder state so the UI can
    # show the "Sync Template" nudge even on healthy-looking feeders.
    if template_drift_checker is not None:
        try:
            drift = template_drift_checker(search_name)
            fs.template_drift = drift is not None
        except Exception:
            fs.template_drift = False

    _DRIFT_SUFFIX = (
        " ⚠️ installed query differs from the shipped default "
        "template - click Sync Template to re-install the current "
        "version."
    )

    def _attach_drift(_fs: FeederStatus) -> FeederStatus:
        if _fs.template_drift and _DRIFT_SUFFIX not in (_fs.message or ""):
            _fs.message = (_fs.message or "") + _DRIFT_SUFFIX
        return _fs

    try:
        search = saved_search_loader(search_name)
    except FileNotFoundError:
        fs.state = "missing_search"
        fs.installable = search_name in defaults
        fs.message = (
            f"Saved search '{search_name}' not found."
            + (" A default template is available - click Install."
               if fs.installable else "")
        )
        return _attach_drift(fs)
    indexes = extract_index_paths(search.get("query") or "")
    fs.index_paths = indexes
    if not indexes:
        fs.message = (
            "No index=\"…\" path found in the search query."
        )
        return _attach_drift(fs)
    # Prefer the first index path - feeders with multiple indexes are
    # unusual, and the first one is the authoritative primary source.
    subdir = _normalize_subdirectory(indexes[0])
    fs.subdirectory = subdir

    # Dispatcher-managed subdirectories: populated by the alert-group
    # dispatcher itself (or similar internal component), NOT by a
    # scheduled ingestion task. Show a clearer message than the generic
    # "no library script matches" so the operator knows nothing needs
    # to be deployed. Extend this set when adding new dispatcher-managed
    # log indexes.
    # Both legacy ``logs/ag_picks`` (pre-Wave-2 path) and the current
    # ``IMMUTABLE/ag_picks`` (Wave 2 of OEB, 2026-04-26) are recognized
    # so saved searches that reference either path display the correct
    # dispatcher-managed status. The legacy entry can be removed once
    # there are no remaining saved searches pointing at the old path.
    _DISPATCHER_MANAGED_SUBDIRS = ("logs/ag_picks", "IMMUTABLE/ag_picks")

    if subdir.startswith(_DISPATCHER_MANAGED_SUBDIRS):
        fs.data_file_count, fs.last_data_epoch = _count_parquet_data(
            indexes_root, subdir
        )
        if fs.data_file_count > 0:
            fs.state = "live"
            fs.message = (
                f"Dispatcher-managed index (populated by the alert "
                f"group dispatcher on every successful run). "
                f"{fs.data_file_count} parquet file(s) present."
            )
        else:
            fs.state = "pending"
            fs.message = (
                "Dispatcher-managed index (populated by the alert "
                "group dispatcher). No data yet - will populate on the "
                "first successful dispatch that extracts a JSON picks "
                "block. Day-1 empty is normal."
            )
        return _attach_drift(fs)

    script = _find_library_script_for_subdir(subdir, library_scripts)
    if script is None:
        fs.state = "no_library_script"
        fs.message = (
            f"No library script matches subdirectory '{subdir}'. "
            "The index may be user-managed (custom ingestion)."
        )
        # Still count data if the dir exists - a user-managed index can be live.
        fs.data_file_count, fs.last_data_epoch = _count_parquet_data(
            indexes_root, subdir
        )
        if fs.data_file_count > 0:
            # User-managed + live: override to "live" with an informational note.
            fs.state = "live"
            fs.message += " Index has data."
        return _attach_drift(fs)
    fs.library_script_id = script.get("id")
    fs.required_credentials = list(script.get("requires_credentials") or [])

    task = _find_scheduled_task_for_subdir(subdir, scheduled_tasks)
    if task is None:
        fs.state = "needs_deploy"
        fs.message = (
            f"Library script '{fs.library_script_id}' is not yet deployed "
            "as a scheduled ingestion task."
        )
        # Older/stale data may still exist from a prior deploy.
        fs.data_file_count, fs.last_data_epoch = _count_parquet_data(
            indexes_root, subdir
        )
        return _attach_drift(fs)
    fs.task_id = task.get("id")
    fs.task_enabled = not task.get("disabled", False)

    fs.missing_credentials = _missing_credentials_for_task(
        fs.task_id, fs.required_credentials, credentials_lister
    )

    fs.data_file_count, fs.last_data_epoch = _count_parquet_data(
        indexes_root, subdir
    )

    if not fs.task_enabled:
        fs.state = "disabled"
        fs.message = f"Scheduled task #{fs.task_id} is disabled."
        return _attach_drift(fs)
    if fs.missing_credentials:
        fs.state = "needs_creds"
        fs.message = (
            f"Task #{fs.task_id} is missing credentials: "
            f"{', '.join(fs.missing_credentials)}."
        )
        return _attach_drift(fs)
    if fs.data_file_count == 0:
        fs.state = "pending"
        fs.message = (
            f"Task #{fs.task_id} is deployed but no data has landed yet."
        )
        return _attach_drift(fs)
    fs.state = "live"
    fs.message = (
        f"{fs.data_file_count} parquet file(s) present under "
        f"indexes/{subdir}."
    )

    # Dead-feeder detection: the feeder is useful as long as EITHER
    # (a) the ingestion produced fresh parquet files (primary signal),
    # OR (b) the saved-search cron ran recently. Previously this check
    # only looked at saved_search_history.db - which misled the user on
    # 2026-04-21 because AG dispatchers run saved-search queries
    # on-demand (logging to indexes/logs/search_runs/*.parquet, NOT to
    # saved_search_history.db). Feeders with fresh data + AG-driven
    # executions were flagged "hasn't run in: never" even though they
    # return rows every dispatch. Data freshness is the honest signal.
    fs.last_search_run_age_hours = _search_run_age_hours(fs.search_name)
    try:
        from global_settings import get_settings
        threshold = float(
            get_settings().get("alert_group_max_feeder_staleness_hours") or 48
        )
    except Exception:
        threshold = 48.0

    import time as _time
    now = _time.time()
    data_age_hours = (
        (now - fs.last_data_epoch) / 3600.0
        if fs.last_data_epoch else float("inf")
    )
    search_age_hours = (
        fs.last_search_run_age_hours
        if fs.last_search_run_age_hours is not None else float("inf")
    )
    # Feeder is alive if EITHER its data is fresh OR the saved-search ran
    # recently. min() == infinity means both absent → dead.
    min_age = min(data_age_hours, search_age_hours)

    if min_age > threshold:
        fs.is_dead_feeder = True
        # Build a message that tells the operator which signal is stale.
        # Prefer the data-freshness framing since it's the one they can
        # actually act on (check ingestion task, rebuild index, etc.).
        if fs.last_data_epoch:
            data_str = f"{data_age_hours:.1f}h"
        else:
            data_str = "never"
        fs.message += (
            f" ⚠️ data is stale (last parquet: {data_str}, "
            f"threshold: {int(threshold)}h). Check the ingestion task."
        )

    return _attach_drift(fs)


def summarize(feeders: list[FeederStatus]) -> dict[str, Any]:
    """Roll a list of feeder statuses into counts + overall worst-state."""
    counts: dict[str, int] = {k: 0 for k in STATE_RANK}
    for f in feeders:
        counts[f.state] = counts.get(f.state, 0) + 1
    if not feeders:
        overall = "unknown_index"
    else:
        overall = max((f.state for f in feeders), key=lambda s: STATE_RANK.get(s, 99))
    return {"counts": counts, "overall": overall, "total": len(feeders)}


def resolve_alert_group(
    group: dict,
    *,
    saved_search_loader,
    library_scripts: list[dict],
    scheduled_tasks: list[dict],
    credentials_lister,
    indexes_root: str | os.PathLike,
    default_search_names: list[str] | None = None,
    template_drift_checker=None,
) -> dict[str, Any]:
    """
    High-level convenience: resolve every feeder in an alert group and
    return a dict ready to be jsonified by the Flask endpoint.
    """
    names = list(group.get("search_names") or [])
    feeders = [
        resolve_feeder(
            n,
            saved_search_loader=saved_search_loader,
            library_scripts=library_scripts,
            scheduled_tasks=scheduled_tasks,
            credentials_lister=credentials_lister,
            indexes_root=indexes_root,
            default_search_names=default_search_names,
            template_drift_checker=template_drift_checker,
        )
        for n in names
    ]
    drift_count = sum(1 for f in feeders if f.template_drift)
    return {
        "group_name": group.get("name"),
        "feeders": [f.to_dict() for f in feeders],
        "summary": {
            **summarize(feeders),
            "template_drift_count": drift_count,
        },
    }
