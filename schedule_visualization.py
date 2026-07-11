"""
Schedule Visualization Aggregator
─────────────────────────────────
Builds a unified view of every scheduled job in the system (ingestion
tasks, saved searches, alert groups), expands each cron schedule to its
next-N firings, and joins with recent run history so the UI can show:

  - which UTC hours are busiest (count of firings)
  - which UTC hours expect the most data (avg rows × runs)
  - per-job averages (last 5 runs by default) for row_count and runtime

Used by the ``/api/schedule/heatmap`` endpoint and any future scheduler-
debugging tooling. Pure-Python, side-effect-free aside from reading
existing stores + log parquet - safe to import anywhere.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Job kinds we surface in the heatmap ─────────────────────────────
KIND_INGESTION = "ingestion"
KIND_SAVED_SEARCH = "saved_search"
KIND_ALERT_GROUP = "alert_group"

ALL_KINDS = (KIND_INGESTION, KIND_SAVED_SEARCH, KIND_ALERT_GROUP)


# ── Data-volume estimation knobs ────────────────────────────────────
# Lookahead window for expanding cron schedules. 7 days × 24 hours =
# 168 cells. Longer windows produce smoother averages for cron specs
# that don't fire every day; shorter windows produce a tighter "what
# does next week look like" view. 7 days is the operator-friendly
# default - the UI grid is "day of week" × "hour".
DEFAULT_LOOKAHEAD_DAYS = 7

# Number of recent runs to average per job for the data-volume column.
# User asked for "past 5 last runs or whatever count is available".
DEFAULT_HISTORY_LOOKBACK_RUNS = 5

# How far back to look when reading the parquet log streams. Keeps the
# read budget bounded. Most jobs run at least daily so 30 days is
# plenty for the 5-run average.
DEFAULT_HISTORY_LOOKBACK_DAYS = 30


# ──────────────────────────────────────────────────────────────────
# Job collection
# ──────────────────────────────────────────────────────────────────


def _collect_ingestion_jobs() -> list[dict]:
    """Read scheduled inputs from the SQLite store; return job dicts.

    Falls back to an empty list on any exception so the visualization
    page renders even if one source is broken.
    """
    try:
        from scheduled_input_engine.store import ScheduledInputStore
        store = ScheduledInputStore()
        store.initialize_databases()
        rows = store.list_scheduled_inputs(enabled_only=False)
    except Exception as exc:
        logger.warning("[!] schedule_viz: ingestion source failed: %s", exc)
        return []

    jobs = []
    for row in rows or []:
        cron = (row.get("cron_schedule") or "").strip()
        if not cron:
            continue
        jobs.append({
            "kind": KIND_INGESTION,
            "name": row.get("title") or f'task-{row.get("id")}',
            "task_id": str(row.get("id")) if row.get("id") is not None else "",
            "cron": cron,
            "disabled": bool(row.get("disabled", 0)),
            "subdirectory": row.get("output_subdir", "") or "",
        })
    return jobs


def _collect_saved_search_jobs() -> list[dict]:
    """Read saved searches via the YAML store."""
    try:
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store.initialize()
        rows = store.list_searches()
    except Exception as exc:
        logger.warning("[!] schedule_viz: saved-search source failed: %s", exc)
        return []

    jobs = []
    for row in rows or []:
        cron = (row.get("cron_schedule") or "").strip()
        if not cron:
            continue
        jobs.append({
            "kind": KIND_SAVED_SEARCH,
            "name": row.get("name") or "(unnamed)",
            "cron": cron,
            "disabled": bool(row.get("disabled", False)),
            "purpose": row.get("purpose", "standalone"),
        })
    return jobs


def _collect_alert_group_jobs() -> list[dict]:
    """Read alert groups via the YAML store."""
    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        rows = store.list_groups()
    except Exception as exc:
        logger.warning("[!] schedule_viz: alert-group source failed: %s", exc)
        return []

    jobs = []
    for row in rows or []:
        # Alert groups use ``schedule`` (not ``cron_schedule``)
        cron = (row.get("schedule") or row.get("cron_schedule") or "").strip()
        if not cron:
            continue
        jobs.append({
            "kind": KIND_ALERT_GROUP,
            "name": row.get("name") or "(unnamed)",
            "cron": cron,
            "disabled": bool(row.get("disabled", False)),
            "feeder_count": len(row.get("search_names", []) or []),
        })
    return jobs


def collect_all_jobs() -> list[dict]:
    """Return every scheduled job across all three kinds."""
    return (
        _collect_ingestion_jobs()
        + _collect_saved_search_jobs()
        + _collect_alert_group_jobs()
    )


# ──────────────────────────────────────────────────────────────────
# Cron expansion
# ──────────────────────────────────────────────────────────────────


def expand_cron_to_firings(
    cron: str,
    *,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    base_dt: datetime | None = None,
) -> list[datetime]:
    """Expand a cron expression to its UTC firing times over the next
    ``lookahead_days`` window.

    Returns an empty list (with a WARNING log) if the cron is invalid -
    the visualization should not crash on a single bad row.
    """
    if not cron:
        return []
    try:
        from croniter import croniter
    except ImportError:
        logger.warning("[!] schedule_viz: croniter not available")
        return []

    if base_dt is None:
        base_dt = datetime.now(timezone.utc)
    end_dt = base_dt + timedelta(days=lookahead_days)

    try:
        it = croniter(cron, base_dt)
    except (ValueError, KeyError, Exception) as exc:  # croniter is strict
        logger.warning("[!] schedule_viz: invalid cron %r: %s", cron, exc)
        return []

    firings = []
    # Hard ceiling on iterations to prevent runaway expansion on a
    # malformed cron that croniter happens to accept.
    max_firings = lookahead_days * 24 * 60 + 100  # generous
    while True:
        try:
            nxt = it.get_next(datetime)
        except Exception:
            break
        # croniter returns naive in some configurations; normalise.
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        if nxt >= end_dt:
            break
        firings.append(nxt)
        if len(firings) >= max_firings:
            break
    return firings


def compute_hour_distribution(
    jobs: list[dict],
    *,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    base_dt: datetime | None = None,
    include_disabled: bool = False,
) -> dict:
    """Compute per-(day-of-week, hour) firing counts.

    Returns a dict shaped like::

        {
            "lookahead_days": 7,
            "base_epoch": <int>,
            "by_dow_hour": {  # Monday=0 .. Sunday=6
                0: [hour-0-count, hour-1-count, ..., hour-23-count],
                1: [...],
                ...
                6: [...],
            },
            "by_hour_total": [hour-0-total, ..., hour-23-total],
            "total_firings": <int>,
        }

    Each cell holds the COUNT of distinct job firings during that hour
    summed across all matching jobs and across all matching weekdays in
    the lookahead window. The UI typically shows a 7×24 grid.

    Set ``include_disabled=True`` to count disabled jobs too (useful
    when planning to enable them).
    """
    if base_dt is None:
        base_dt = datetime.now(timezone.utc)

    # 7 weekdays × 24 hours
    by_dow_hour = {dow: [0] * 24 for dow in range(7)}
    by_hour_total = [0] * 24
    total = 0

    for job in jobs:
        if not include_disabled and job.get("disabled"):
            continue
        firings = expand_cron_to_firings(
            job["cron"], lookahead_days=lookahead_days, base_dt=base_dt
        )
        for f in firings:
            dow = f.weekday()  # Mon=0..Sun=6
            hour = f.hour
            by_dow_hour[dow][hour] += 1
            by_hour_total[hour] += 1
            total += 1

    return {
        "lookahead_days": lookahead_days,
        "base_epoch": int(base_dt.timestamp()),
        "by_dow_hour": by_dow_hour,
        "by_hour_total": by_hour_total,
        "total_firings": total,
    }


# ──────────────────────────────────────────────────────────────────
# Recent-run history (row_count + duration averages)
# ──────────────────────────────────────────────────────────────────


def _read_recent_log_parquet(
    category: str,
    *,
    project_root: Path | None = None,
    lookback_days: int = DEFAULT_HISTORY_LOOKBACK_DAYS,
):
    """Read recent rows from ``indexes/logs/<category>/*.parquet``.

    Returns a pandas DataFrame or None if no rows are available. Filters
    by ``_epoch >= now - lookback_days * 86400``.
    """
    try:
        import pandas as pd
        import pyarrow.parquet as pq
    except ImportError:
        return None

    if project_root is None:
        project_root = Path(__file__).parent.resolve()

    logs_dir = project_root / "indexes" / "logs" / category
    if not logs_dir.exists():
        return None

    parquet_files = sorted(logs_dir.glob("*.parquet"))
    if not parquet_files:
        return None

    cutoff_epoch = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).timestamp()

    frames = []
    for path in parquet_files:
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            logger.debug(
                "[i] schedule_viz: skipping unreadable parquet %s: %s",
                path.name, exc,
            )
            continue
        if df is None or df.empty:
            continue
        if "_epoch" in df.columns:
            df = df[df["_epoch"] >= cutoff_epoch]
        if df.empty:
            continue
        frames.append(df)

    if not frames:
        return None
    try:
        import pandas as pd
        return pd.concat(frames, ignore_index=True)
    except Exception:
        return None



def _count_errors(top) -> int:
    """Count status=='error' rows in a recent-runs DataFrame slice."""
    if "status" not in top.columns:
        return 0
    try:
        return int((top["status"].astype(str) == "error").sum())
    except Exception:
        return 0


def gather_run_history(
    *,
    project_root: Path | None = None,
    history_lookback_runs: int = DEFAULT_HISTORY_LOOKBACK_RUNS,
    history_lookback_days: int = DEFAULT_HISTORY_LOOKBACK_DAYS,
) -> dict:
    """Aggregate per-job recent run history from the parquet log streams.

    Returns a dict like::

        {
            "ingestion::<task_id>": {
                "name": "<title>",
                "kind": "ingestion",
                "run_count": 5,
                "avg_row_count": 142.0,
                "avg_duration_ms": 1850.0,
                "error_count": 0,
            },
            "saved_search::<name>": {...},
            "alert_group::<name>": {...},
        }

    Keys are scoped by kind so a saved-search and an ingestion task with
    the same name don't collide.

    ``error_count`` is how many of the last-N runs logged
    ``status == "error"`` (all three log streams carry a ``status``
    column). A job erroring on every recent run previously showed up as
    avg_row_count None - rendered " - " - and escaped every anomaly
    bucket. Caught 2026-07-01 via the schedule report.
    """
    out = {}

    # ── Ingestion runs (indexes/logs/ingestion/*.parquet)
    ing = _read_recent_log_parquet(
        "ingestion",
        project_root=project_root,
        lookback_days=history_lookback_days,
    )
    if ing is not None and len(ing) > 0:
        # Sort newest first, take last-N per task_id
        try:
            ing = ing.sort_values("_epoch", ascending=False)
            for task_id, group in ing.groupby("task_id"):
                top = group.head(history_lookback_runs)
                key = f"{KIND_INGESTION}::{task_id}"
                avg_rc = (
                    float(top["row_count"].dropna().mean())
                    if "row_count" in top.columns and not top["row_count"].dropna().empty
                    else None
                )
                avg_dur = (
                    float(top["duration_ms"].dropna().mean())
                    if "duration_ms" in top.columns and not top["duration_ms"].dropna().empty
                    else None
                )
                title = ""
                if "title" in top.columns and not top["title"].empty:
                    try:
                        title = str(top["title"].iloc[0])
                    except Exception:
                        title = ""
                out[key] = {
                    "name": title or str(task_id),
                    "kind": KIND_INGESTION,
                    "run_count": int(len(top)),
                    "avg_row_count": avg_rc,
                    "avg_duration_ms": avg_dur,
                    "error_count": _count_errors(top),
                }
        except Exception as exc:
            logger.debug("[i] schedule_viz: ingestion history aggregation failed: %s", exc)

    # ── Saved-search runs (indexes/logs/search_runs/*.parquet)
    ss = _read_recent_log_parquet(
        "search_runs",
        project_root=project_root,
        lookback_days=history_lookback_days,
    )
    if ss is not None and len(ss) > 0:
        try:
            ss = ss.sort_values("_epoch", ascending=False)
            for name, group in ss.groupby("search_name"):
                top = group.head(history_lookback_runs)
                key = f"{KIND_SAVED_SEARCH}::{name}"
                avg_rc = (
                    float(top["row_count"].dropna().mean())
                    if "row_count" in top.columns and not top["row_count"].dropna().empty
                    else None
                )
                avg_dur = (
                    float(top["duration_ms"].dropna().mean())
                    if "duration_ms" in top.columns and not top["duration_ms"].dropna().empty
                    else None
                )
                out[key] = {
                    "name": str(name),
                    "kind": KIND_SAVED_SEARCH,
                    "run_count": int(len(top)),
                    "avg_row_count": avg_rc,
                    "avg_duration_ms": avg_dur,
                    "error_count": _count_errors(top),
                }
        except Exception as exc:
            logger.debug(
                "[i] schedule_viz: saved-search history aggregation failed: %s", exc,
            )

    # ── Alert-group runs (indexes/logs/alert_groups/*.parquet)
    ag = _read_recent_log_parquet(
        "alert_groups",
        project_root=project_root,
        lookback_days=history_lookback_days,
    )
    if ag is not None and len(ag) > 0:
        try:
            ag = ag.sort_values("_epoch", ascending=False)
            for name, group in ag.groupby("group_name"):
                top = group.head(history_lookback_runs)
                key = f"{KIND_ALERT_GROUP}::{name}"
                avg_dur = (
                    float(top["duration_ms"].dropna().mean())
                    if "duration_ms" in top.columns and not top["duration_ms"].dropna().empty
                    else None
                )
                # AGs don't emit row_count directly; sum feeder rows isn't
                # tracked here either. Leave avg_row_count None - the UI
                # treats None as "no data yet".
                out[key] = {
                    "name": str(name),
                    "kind": KIND_ALERT_GROUP,
                    "run_count": int(len(top)),
                    "avg_row_count": None,
                    "avg_duration_ms": avg_dur,
                    "error_count": _count_errors(top),
                }
        except Exception as exc:
            logger.debug(
                "[i] schedule_viz: alert-group history aggregation failed: %s", exc,
            )

    return out


# ──────────────────────────────────────────────────────────────────
# Top-level summary builder
# ──────────────────────────────────────────────────────────────────


def _job_history_key(job: dict) -> str:
    """Return the key used to look up a job in the history dict."""
    if job["kind"] == KIND_INGESTION:
        return f"{KIND_INGESTION}::{job.get('task_id', '')}"
    if job["kind"] == KIND_SAVED_SEARCH:
        return f"{KIND_SAVED_SEARCH}::{job['name']}"
    return f"{KIND_ALERT_GROUP}::{job['name']}"


def compute_data_distribution(
    jobs: list[dict],
    history: dict,
    *,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    base_dt: datetime | None = None,
    include_disabled: bool = False,
) -> dict:
    """Estimate expected per-(dow, hour) data volume in rows.

    For each job, multiply its expected firings in a cell by its
    ``avg_row_count`` (from history). Sum across all jobs in that cell.
    Cells with no historical data for any of their firing jobs return
    None to distinguish "no data" from "literal zero rows".
    """
    if base_dt is None:
        base_dt = datetime.now(timezone.utc)

    by_dow_hour = {dow: [0.0] * 24 for dow in range(7)}
    by_dow_hour_has_data = {dow: [False] * 24 for dow in range(7)}
    by_hour_total = [0.0] * 24

    for job in jobs:
        if not include_disabled and job.get("disabled"):
            continue
        hist = history.get(_job_history_key(job))
        avg_rc = hist.get("avg_row_count") if hist else None
        if avg_rc is None:
            continue
        firings = expand_cron_to_firings(
            job["cron"], lookahead_days=lookahead_days, base_dt=base_dt
        )
        for f in firings:
            dow = f.weekday()
            hour = f.hour
            by_dow_hour[dow][hour] += float(avg_rc)
            by_dow_hour_has_data[dow][hour] = True
            by_hour_total[hour] += float(avg_rc)

    return {
        "lookahead_days": lookahead_days,
        "base_epoch": int(base_dt.timestamp()),
        "by_dow_hour": by_dow_hour,
        "by_dow_hour_has_data": by_dow_hour_has_data,
        "by_hour_total": by_hour_total,
    }


def build_schedule_summary(
    *,
    project_root: Path | None = None,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    history_lookback_runs: int = DEFAULT_HISTORY_LOOKBACK_RUNS,
    history_lookback_days: int = DEFAULT_HISTORY_LOOKBACK_DAYS,
    include_disabled: bool = False,
    base_dt: datetime | None = None,
) -> dict:
    """Top-level: gather jobs + history, compute distributions, return a
    JSON-serialisable summary structure for the UI / API.

    The returned dict is the contract for ``/api/schedule/heatmap``::

        {
            "generated_at_epoch": int,
            "lookahead_days": int,
            "history_lookback_runs": int,
            "jobs": [
                {
                    "name": str,
                    "kind": "ingestion"|"saved_search"|"alert_group",
                    "cron": str,
                    "disabled": bool,
                    "next_firing_epoch": int|None,
                    "next_firing_iso": str|None,
                    "firings_in_lookahead": int,
                    "run_count": int,
                    "avg_row_count": float|None,
                    "avg_duration_ms": float|None,
                    "error_count": int,
                    # plus kind-specific fields (task_id, subdirectory,
                    # purpose, feeder_count)
                },
                ...
            ],
            "hour_distribution": {  # firing counts
                "by_dow_hour": {0..6: [24 ints]},
                "by_hour_total": [24 ints],
                "total_firings": int,
            },
            "data_distribution": {  # row-count estimates
                "by_dow_hour": {0..6: [24 floats]},
                "by_dow_hour_has_data": {0..6: [24 bools]},
                "by_hour_total": [24 floats],
            },
            "summary": {
                "total_jobs": int,
                "total_jobs_disabled": int,
                "by_kind": {kind: count, ...},
                "busiest_hour_utc": int|None,
                "busiest_hour_count": int,
                "biggest_data_hour_utc": int|None,
            },
        }
    """
    if base_dt is None:
        base_dt = datetime.now(timezone.utc)

    jobs = collect_all_jobs()
    history = gather_run_history(
        project_root=project_root,
        history_lookback_runs=history_lookback_runs,
        history_lookback_days=history_lookback_days,
    )

    # Enrich each job with history + next-firing info
    enriched = []
    for job in jobs:
        firings = expand_cron_to_firings(
            job["cron"], lookahead_days=lookahead_days, base_dt=base_dt,
        )
        next_f = firings[0] if firings else None
        h = history.get(_job_history_key(job)) or {}
        enriched.append({
            **job,
            "next_firing_epoch": int(next_f.timestamp()) if next_f else None,
            "next_firing_iso": next_f.isoformat() if next_f else None,
            "firings_in_lookahead": len(firings),
            "run_count": h.get("run_count", 0),
            "avg_row_count": h.get("avg_row_count"),
            "avg_duration_ms": h.get("avg_duration_ms"),
            "error_count": h.get("error_count", 0),
        })

    hour_dist = compute_hour_distribution(
        jobs, lookahead_days=lookahead_days, base_dt=base_dt,
        include_disabled=include_disabled,
    )
    data_dist = compute_data_distribution(
        jobs, history, lookahead_days=lookahead_days, base_dt=base_dt,
        include_disabled=include_disabled,
    )

    # ── Summary stats
    by_kind = {k: 0 for k in ALL_KINDS}
    disabled_count = 0
    for job in enriched:
        by_kind[job["kind"]] = by_kind.get(job["kind"], 0) + 1
        if job.get("disabled"):
            disabled_count += 1

    busiest_hour = None
    busiest_count = 0
    for h in range(24):
        if hour_dist["by_hour_total"][h] > busiest_count:
            busiest_count = hour_dist["by_hour_total"][h]
            busiest_hour = h

    biggest_hour = None
    biggest_total = 0.0
    for h in range(24):
        if data_dist["by_hour_total"][h] > biggest_total:
            biggest_total = data_dist["by_hour_total"][h]
            biggest_hour = h

    return {
        "generated_at_epoch": int(base_dt.timestamp()),
        "lookahead_days": lookahead_days,
        "history_lookback_runs": history_lookback_runs,
        "jobs": enriched,
        "hour_distribution": hour_dist,
        "data_distribution": data_dist,
        "summary": {
            "total_jobs": len(enriched),
            "total_jobs_disabled": disabled_count,
            "by_kind": by_kind,
            "busiest_hour_utc": busiest_hour,
            "busiest_hour_count": busiest_count,
            "biggest_data_hour_utc": biggest_hour,
            "biggest_data_hour_total": biggest_total,
        },
    }


# ──────────────────────────────────────────────────────────────────
# Daily volume aggregator (Wave 6, 2026-04-26)
# ──────────────────────────────────────────────────────────────────
# Powers the bar chart (events per day grouped by kind) and line chart
# (rows ingested per day) added to the Schedule page in Wave 6.
# Reads the same parquet log streams the heatmap reads, but aggregates
# by UTC date instead of day-of-week × hour.


def compute_daily_volume(
    *,
    project_root: Path | None = None,
    days: int = 14,
    base_dt: datetime | None = None,
) -> list[dict]:
    """Aggregate the last ``days`` days of activity into per-day buckets.

    Reads ``indexes/logs/{ingestion,search_runs,alert_groups}/*.parquet``
    and returns one dict per UTC day spanning the window:

        {
            "date":            "2026-04-12",  # ISO UTC date
            "ingestion_runs":  35,            # count of ingestion task runs
            "search_runs":     200,           # count of saved-search executions
            "ag_dispatches":   11,            # count of alert-group dispatches
            "rows_ingested":   1542,          # sum of row_count from ingestion
        }

    Buckets always cover every day in the window - empty days are
    returned with zero counts so the bar/line chart x-axis is uniform.
    Future-dated days are skipped (the window walks backward from
    today's UTC date inclusive).

    No-op friendly: if any log category is missing, that source's
    counts are zero rather than raising - the chart degrades gracefully
    on a fresh install.
    """
    try:
        import pandas as pd
    except ImportError:
        return []

    if days <= 0:
        return []
    days = min(int(days), 365)  # bound the read budget

    if project_root is None:
        project_root = Path(__file__).parent.resolve()

    if base_dt is None:
        base_dt = datetime.now(timezone.utc)
    base_date = base_dt.date()

    # Build the bucket frame inclusive of today + the last (days - 1)
    # full days. So days=14 → today + 13 prior days = 14 buckets.
    dates: list = []
    for offset in range(days):
        d = base_date - timedelta(days=days - 1 - offset)
        dates.append(d)

    # Pre-zero every bucket so empty-day rows render in the chart.
    by_date: dict = {
        d.isoformat(): {
            "date": d.isoformat(),
            "ingestion_runs": 0,
            "search_runs": 0,
            "ag_dispatches": 0,
            "rows_ingested": 0,
        }
        for d in dates
    }

    # Read each log category and accumulate. _read_recent_log_parquet
    # already filters by lookback_days, so we just pass a slightly
    # generous window (days + 1) to absorb timezone edge cases.
    lookback = days + 1

    def _agg(category: str, value_col: str | None) -> None:
        df = _read_recent_log_parquet(
            category, project_root=project_root, lookback_days=lookback,
        )
        if df is None or df.empty or "_epoch" not in df.columns:
            return
        try:
            ts = pd.to_datetime(df["_epoch"], unit="s", utc=True)
        except Exception:
            return
        df = df.copy()
        df["_date"] = ts.dt.date.astype(str)
        if category == "ingestion":
            counts = df.groupby("_date").size()
            for d, n in counts.items():
                if d in by_date:
                    by_date[d]["ingestion_runs"] += int(n)
            if value_col and value_col in df.columns:
                # row_count may be NaN/None for failed runs - coerce.
                rows = df.groupby("_date")[value_col].apply(
                    lambda s: int(pd.to_numeric(s, errors="coerce")
                                  .fillna(0).sum())
                )
                for d, total in rows.items():
                    if d in by_date:
                        by_date[d]["rows_ingested"] += int(total)
        elif category == "search_runs":
            counts = df.groupby("_date").size()
            for d, n in counts.items():
                if d in by_date:
                    by_date[d]["search_runs"] += int(n)
        elif category == "alert_groups":
            counts = df.groupby("_date").size()
            for d, n in counts.items():
                if d in by_date:
                    by_date[d]["ag_dispatches"] += int(n)

    _agg("ingestion", "row_count")
    _agg("search_runs", None)
    _agg("alert_groups", None)

    # Return chronologically ordered (oldest → newest) for chart x-axis.
    return [by_date[d.isoformat()] for d in dates]
