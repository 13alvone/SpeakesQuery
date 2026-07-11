"""
Datetime parsing helpers - single source of truth for the SpeakesQuery
date format whitelist + epoch conversion.

Used by:
  - SPQL eval functions ``strptime()``, ``strftime()``, ``relative_time()``,
    ``now()`` (handlers/EvalHandler.py)
  - DuckDB index time-range parsing (functionality/duckdb_index_call.py)
  - Anywhere else that needs to parse a user-supplied date string

The 28-format whitelist below is the canonical list - add new formats
here, never inline.  The order matters: more-specific formats (with
microseconds, with time components) come before their less-specific
prefixes so ``strptime("2023-10-23 14:20:30.123456")`` matches the
microsecond format, not the bare date.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import time as _time
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Canonical format whitelist (28 entries)
# ---------------------------------------------------------------------------
# Order: most-specific first.  ``strptime`` returns at the first match.
DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S.%f",   # YYYY-MM-DD HH:MM:SS.mmmmmm
    "%Y-%m-%d %H:%M:%S",      # YYYY-MM-DD HH:MM:SS
    "%m/%d/%Y %H:%M:%S.%f",   # MM/DD/YYYY HH:MM:SS.mmmmmm
    "%m/%d/%Y %H:%M:%S",      # MM/DD/YYYY HH:MM:SS
    "%m-%d-%Y %H:%M:%S.%f",   # MM-DD-YYYY HH:MM:SS.mmmmmm
    "%m-%d-%Y %H:%M:%S",      # MM-DD-YYYY HH:MM:SS
    "%d-%m-%Y %H:%M:%S.%f",   # DD-MM-YYYY HH:MM:SS.mmmmmm
    "%d-%m-%Y %H:%M:%S",      # DD-MM-YYYY HH:MM:SS
    "%d/%m/%Y %H:%M:%S.%f",   # DD/MM/YYYY HH:MM:SS.mmmmmm
    "%d/%m/%Y %H:%M:%S",      # DD/MM/YYYY HH:MM:SS
    "%Y/%m/%d %H:%M:%S.%f",   # YYYY/MM/DD HH:MM:SS.mmmmmm
    "%Y/%m/%d %H:%M:%S",      # YYYY/MM/DD HH:MM:SS
    "%Y-%m-%dT%H:%M:%S.%f",   # YYYY-MM-DDTHH:MM:SS.mmmmmm
    "%Y-%m-%dT%H:%M:%S",      # YYYY-MM-DDTHH:MM:SS
    "%B %d, %Y %H:%M:%S.%f",  # October 23, 2023 14:20:30.123456
    "%B %d, %Y %H:%M:%S",     # October 23, 2023 14:20:30
    "%d %B %Y %H:%M:%S.%f",   # 23 October 2023 14:20:30.123456
    "%d %B %Y %H:%M:%S",      # 23 October 2023 14:20:30
    "%m/%d/%Y %I:%M:%S %p",   # 10/23/2023 02:20:30 PM
    "%m-%d-%Y %I:%M:%S %p",   # 10-23-2023 02:20:30 PM
    "%m/%d/%Y",               # MM/DD/YYYY
    "%m-%d-%Y",               # MM-DD-YYYY
    "%m/%d/%y",               # MM/DD/YY
    "%m-%d-%y",               # MM-DD-YY
    "%Y-%m-%d",               # YYYY-MM-DD
    "%Y%m%d%H%M%S",           # 20231023142030
    "%Y-W%W-%w %H:%M:%S.%f",  # 2023-W43-1 14:20:30.123456 (Monday-first ISO week)
    "%Y-W%U-%w %H:%M:%S.%f",  # 2023-W42-7 14:20:30.123456 (Sunday-first US week)
)


# ---------------------------------------------------------------------------
# Splunk-style relative time (now, -30m, -1h@h, +1d, -7d@w, etc.)
# ---------------------------------------------------------------------------

import re as _re

_RELATIVE_RE = _re.compile(
    r'^(?P<sign>[+-]?)(?P<num>\d+)(?P<unit>[smhdwMy])'
    r'(?:@(?P<snap>[smhdwMy]))?$'
)

_UNIT_SECONDS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800,
    'M': 2592000,    # 30 days
    'y': 31536000,   # 365 days
}


def _snap_to_unit(dt_obj: _dt.datetime, unit: str) -> _dt.datetime:
    """Truncate ``dt_obj`` down to the boundary of ``unit`` (Splunk @-snap)."""
    if unit == 's':
        return dt_obj.replace(microsecond=0)
    if unit == 'm':
        return dt_obj.replace(second=0, microsecond=0)
    if unit == 'h':
        return dt_obj.replace(minute=0, second=0, microsecond=0)
    if unit == 'd':
        return dt_obj.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == 'w':
        days_since_monday = dt_obj.weekday()
        return (dt_obj - _dt.timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if unit == 'M':
        return dt_obj.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == 'y':
        return dt_obj.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt_obj


def parse_relative_time(rel_str: str) -> Optional[int]:
    """Parse Splunk-style relative time strings into a UTC epoch second.

    Returns ``None`` if ``rel_str`` is not a recognised relative-time form
    (so callers can fall back to other parsing strategies).

    Supported syntax::

        now             current epoch
        -30m            30 minutes ago
        +1h             1 hour from now
        -1d@d           1 day ago, snapped to start of day
        -7d@w           7 days ago, snapped to start of week (Monday 00:00)
    """
    if not isinstance(rel_str, str):
        return None
    stripped = rel_str.strip()
    if stripped.lower() == 'now':
        return int(_time.time())

    m = _RELATIVE_RE.match(stripped)
    if not m:
        return None

    sign = -1 if m.group('sign') != '+' else 1
    num = int(m.group('num'))
    unit = m.group('unit')
    snap = m.group('snap')

    if unit not in _UNIT_SECONDS:
        return None

    # Aware UTC now - utcnow() is deprecated on 3.12+. timetuple() on an
    # aware-UTC datetime yields UTC wall-clock fields, so timegm() below
    # keeps returning the same epoch.
    now_dt = _dt.datetime.now(_dt.timezone.utc)
    delta = _dt.timedelta(seconds=sign * num * _UNIT_SECONDS[unit])
    result = now_dt + delta
    if snap and snap in _UNIT_SECONDS:
        result = _snap_to_unit(result, snap)
    return int(calendar.timegm(result.timetuple()))


# ---------------------------------------------------------------------------
# Single-value parsing
# ---------------------------------------------------------------------------


def parse_to_epoch(date_str: str, fmt: Optional[str] = None) -> Optional[float]:
    """Convert a single date string to a UTC epoch (float seconds, possibly
    fractional for microsecond formats).

    Resolution order:
      1. If ``date_str`` is already numeric (digits only or float-like), it
         is returned unchanged as a float - assumed to already be epoch.
      2. Splunk-style relative times (``now``, ``-30m``, ``-1h@h`` …).
      3. If ``fmt`` is given, that format only.
      4. Otherwise, try every entry in :data:`DATE_FORMATS` in order.

    Returns ``None`` on failure so callers can decide whether to raise,
    log, or substitute a default.
    """
    if date_str is None:
        return None
    if isinstance(date_str, (int, float)):
        # Already an epoch (or numeric stand-in) - pass through.
        return float(date_str)

    s = str(date_str).strip()
    if not s:
        return None

    # Numeric epoch fast path
    try:
        return float(s)
    except ValueError:
        pass

    # Relative time
    rel = parse_relative_time(s)
    if rel is not None:
        return float(rel)

    formats_to_try = (fmt,) if fmt else DATE_FORMATS
    for f in formats_to_try:
        try:
            dt = _dt.datetime.strptime(s, f)
            # ``strptime`` returns naive datetime in local civil time; we
            # treat the input as UTC for consistency with other SpeakesQuery
            # epoch handling (no timezone heuristics in strptime).
            return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Series fast path - column-homogeneous detection
# ---------------------------------------------------------------------------


def parse_series_to_epoch(series: pd.Series) -> pd.Series:
    """Convert a Series of date strings to a Series of epoch floats.

    Optimised for the common case where every value in the column shares
    one format: detects the format from the first non-null sample, then
    bulk-applies via ``pd.to_datetime(format=...)``.  Cells whose format
    differs (or that look like relative times / numeric epochs) fall back
    to row-by-row ``parse_to_epoch``.

    NaT / unparseable cells become ``NaN`` in the result.
    """
    if not isinstance(series, pd.Series):
        return pd.Series([parse_to_epoch(series)])

    # Sample the first non-null value to pick a format
    sample = next((v for v in series if pd.notna(v) and str(v).strip()), None)
    if sample is None:
        return series.apply(parse_to_epoch)

    sample_str = str(sample).strip()

    # Numeric-epoch fast path - if the sample parses as a float, treat the
    # whole column as already-epoch values.  Done BEFORE format detection
    # so that strings like "1705329000" don't get misparsed by Python's
    # permissive strptime against %Y%m%d%H%M%S (which happily accepts
    # variable-width digit groups).
    try:
        float(sample_str)
        return series.apply(parse_to_epoch)
    except (ValueError, TypeError):
        pass

    detected_fmt: Optional[str] = None
    if isinstance(sample, str):
        for f in DATE_FORMATS:
            try:
                _dt.datetime.strptime(sample_str, f)
                detected_fmt = f
                break
            except ValueError:
                continue

    if detected_fmt is not None:
        try:
            parsed = pd.to_datetime(series, format=detected_fmt, errors="coerce", utc=True)
            # ``astype("int64")`` on datetime64[ns, UTC] yields nanoseconds
            # since epoch (NaT becomes the int-min sentinel - masked below).
            epochs = parsed.astype("int64") / 1_000_000_000
            mask = parsed.isna()
            if mask.any():
                # NaT → NaN in the result Series first
                epochs = epochs.astype(float).mask(mask, other=float("nan"))
                # Retry rows where the source had a value but the bulk parse
                # failed (e.g. mid-column format change).
                retry_mask = mask & series.notna()
                if retry_mask.any():
                    fallback = series[retry_mask].apply(parse_to_epoch)
                    epochs.loc[retry_mask] = fallback
            return epochs
        except Exception:
            pass  # fall through to per-row

    return series.apply(parse_to_epoch)
