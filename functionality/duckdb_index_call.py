#!/usr/bin/env python3
"""
Module: duckdb_index_call.py
Purpose: DuckDB-based replacement for the C++ process_index_calls function.

         Provides the same interface - accepts a list of token strings and
         returns a pandas DataFrame - but uses DuckDB's native Parquet reader
         for predicate pushdown, projection pushdown, and parallel I/O.

         Drop-in replacement: swap the import in speakesQueryListener.py and
         the rest of the pipeline is unchanged.
"""

import glob
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project root discovery (mirrors the C++ get_project_root logic)
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """Walk up from CWD until a directory containing 'indexes/' is found."""
    candidate = Path.cwd().resolve()
    while True:
        if (candidate / "indexes").is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return Path.cwd().resolve()


PROJECT_ROOT = _find_project_root()
INDEXES_DIR = PROJECT_ROOT / "indexes"

logger.info("[i] duckdb_index_call project root: %s", PROJECT_ROOT)
logger.info("[i] duckdb_index_call indexes dir:   %s", INDEXES_DIR)
if not INDEXES_DIR.is_dir():
    logger.warning("[!] Indexes directory does NOT exist: %s", INDEXES_DIR)
elif not os.access(INDEXES_DIR, os.R_OK):
    logger.warning("[!] Indexes directory is NOT readable (permission denied): %s", INDEXES_DIR)
else:
    _parquet_count = sum(1 for _ in INDEXES_DIR.rglob("*.parquet"))
    logger.info("[i] Indexes directory accessible - %d .parquet file(s) found", _parquet_count)


# ---------------------------------------------------------------------------
# Date parsing (replaces the C++ parse_date_to_epoch_single)
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m-%d-%Y %H:%M:%S",
    "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y",
    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
    "%B %d, %Y %H:%M:%S", "%d %B %Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p", "%m-%d-%Y %I:%M:%S %p",
    "%Y%m%d%H%M%S",
]

import calendar
import time as _time
import re as _re
import datetime as _dt

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore[no-redef]


class TimeBoundParseError(ValueError):
    """Raised when an ``earliest=``/``latest=`` value cannot be parsed.

    The user-query flow (``execute_query`` → ``process_index_calls``)
    propagates this so that ``process_query_with_diagnostics`` surfaces
    the failure to the operator, instead of silently defaulting to epoch
    0 and returning a meaningless dataset (the legacy behaviour).
    """


# Regex for Splunk-style relative time: e.g. -30m, +1h, -1d@d, now, -1h@h
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
    'M': 2592000,   # 30 days
    'y': 31536000,  # 365 days
}

# IANA tz suffix attached to an earliest/latest value, e.g.
#   ``-1d@d/America/New_York``  → snap to NY-local midnight, then UTC epoch
#   ``2024-01-01/America/New_York`` → midnight NY local, then UTC epoch
# The suffix overrides the per-call ``tz`` arg. Validated against ZoneInfo.
def _split_inline_tz(date_str: str) -> Tuple[str, Optional[str]]:
    """If ``date_str`` ends with ``/<IANA-tz>``, split it off.

    Returns ``(value, tz_or_None)``. IANA names use ``Region[/Subregion]/City``
    form (one to three ``/``-separated segments). Validation is by
    ``ZoneInfo()`` - invalid suffixes are NOT split off so existing
    queries that incidentally contain ``/`` (e.g. paths) are unaffected.
    """
    if "/" not in date_str:
        return date_str, None
    parts = date_str.split("/")
    # Try the longest plausible IANA suffix first (3 segments), then 2, then 1.
    for n in range(min(3, len(parts) - 1), 0, -1):
        candidate_tz = "/".join(parts[-n:])
        try:
            ZoneInfo(candidate_tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
        value = "/".join(parts[:-n])
        if value:
            return value, candidate_tz
    return date_str, None


def _resolve_tz(tz: Optional[str]) -> ZoneInfo:
    """Return a ZoneInfo for *tz*, defaulting to UTC.

    Raises :class:`TimeBoundParseError` for invalid IANA names so the caller
    sees a clean diagnostic instead of a bare ``ZoneInfoNotFoundError``.
    """
    if not tz or tz.upper() == "UTC":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimeBoundParseError(
            f"Unknown IANA timezone {tz!r}: {exc}"
        ) from exc


def _snap_to_unit(dt_obj: _dt.datetime, unit: str) -> _dt.datetime:
    """Snap a datetime down to the boundary of the given unit (Splunk @-snap).

    Operates in the timezone of ``dt_obj`` - caller chooses the anchor.
    Splunk behaviour:
      @s - truncate to second (no-op for integer seconds)
      @m - truncate to start of minute
      @h - truncate to start of hour
      @d - truncate to start of day (midnight)
      @w - truncate to start of week (Monday 00:00)
      @M - truncate to start of month
      @y - truncate to start of year
    """
    if unit == 's':
        return dt_obj.replace(microsecond=0)
    elif unit == 'm':
        return dt_obj.replace(second=0, microsecond=0)
    elif unit == 'h':
        return dt_obj.replace(minute=0, second=0, microsecond=0)
    elif unit == 'd':
        return dt_obj.replace(hour=0, minute=0, second=0, microsecond=0)
    elif unit == 'w':
        # Monday = 0
        days_since_monday = dt_obj.weekday()
        return (dt_obj - _dt.timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif unit == 'M':
        return dt_obj.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif unit == 'y':
        return dt_obj.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt_obj


def _parse_relative_time(date_str: str, tz: str = "UTC") -> Optional[int]:
    """Parse Splunk-style relative time modifiers.

    Supports:
      now - current epoch (timezone-independent)
      -30m - 30 minutes ago
      +1h - 1 hour from now
      -1d@d - 1 day ago, snapped to start of day in *tz*
      -1h@h - 1 hour ago, snapped to start of hour in *tz*
      -7d@w - 7 days ago, snapped to start of week (Mon 00:00) in *tz*

    The ``tz`` argument matters ONLY for the @-snap operation: ``-1d@d``
    in ``America/New_York`` snaps to NY-local midnight (5 AM UTC EST or
    4 AM UTC EDT), while the same expression in UTC snaps to UTC midnight.

    Returns epoch int (UTC seconds), or ``None`` if the string is not a
    relative-time modifier.
    """
    stripped = date_str.strip()
    if stripped.lower() == 'now':
        return int(_time.time())

    m = _RELATIVE_RE.match(stripped)
    if not m:
        return None

    sign = -1 if m.group('sign') != '+' else 1
    num = int(m.group('num'))
    unit = m.group('unit')
    snap = m.group('snap')  # may be None

    if unit not in _UNIT_SECONDS:
        return None

    tzinfo = _resolve_tz(tz)
    now = _dt.datetime.now(tzinfo)
    delta = _dt.timedelta(seconds=sign * num * _UNIT_SECONDS[unit])
    result = now + delta

    if snap and snap in _UNIT_SECONDS:
        result = _snap_to_unit(result, snap)

    # ``timestamp()`` on a tz-aware datetime returns UTC epoch directly.
    return int(result.timestamp())


def parse_date_to_epoch(date_str: str, tz: str = "UTC") -> int:
    """Parse a date string into a UTC epoch timestamp - strict.

    Accepted forms:
      Epoch integer:        ``"1709251200"`` (always UTC)
      Splunk relative time: ``"-30m"``, ``"-1h@h"``, ``"-1d@d"``, ``"now"``
      Tz-aware ISO 8601:    ``"2024-01-01T10:00:00-07:00"``,
                            ``"2024-01-01T10:00:00Z"``
                            (offset honoured, *tz* ignored)
      Tz-naive ISO/date:    ``"2024-01-01"``, ``"2024-01-01 10:00:00"``,
                            ``"2024-01-01T10:00:00"``
                            (interpreted in *tz*)
      Inline tz suffix:     append ``/<IANA-tz>`` to ANY of the above
                            (overrides the *tz* argument):
                            ``"-1d@d/America/New_York"``,
                            ``"2024-01-01/Europe/London"``

    Args:
        date_str: The string to parse.
        tz: IANA timezone name (e.g. ``"America/New_York"``) used for
            tz-naive absolute dates and for relative-time @-snap. Defaults
            to ``"UTC"``. Inline ``/<tz>`` suffixes override this.

    Returns:
        Epoch seconds (int, UTC).

    Raises:
        TimeBoundParseError: if ``date_str`` cannot be parsed in any
            supported form, or if ``tz`` is not a valid IANA name.
    """
    if not isinstance(date_str, str):
        raise TimeBoundParseError(
            f"earliest/latest value must be a string, got {type(date_str).__name__}: {date_str!r}"
        )

    raw = date_str.strip()
    if not raw:
        raise TimeBoundParseError("earliest/latest value cannot be empty")

    # Inline /<IANA-tz> suffix overrides the per-call tz arg
    value, inline_tz = _split_inline_tz(raw)
    effective_tz = inline_tz or tz
    # Validate eagerly so a bad tz fails loud BEFORE format dispatch
    tzinfo = _resolve_tz(effective_tz)

    # 1) Bare integer → epoch seconds (UTC)
    if value.lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            pass  # fall through (shouldn't happen after isdigit check)

    # 2) Splunk-style relative time
    relative = _parse_relative_time(value, tz=effective_tz)
    if relative is not None:
        return relative

    # 3) ISO 8601 via fromisoformat (Python 3.11+ accepts the full grammar;
    #    earlier versions are tightened to a subset). Normalise ``Z`` to
    #    ``+00:00`` so the older parser still accepts it.
    iso_candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = _dt.datetime.fromisoformat(iso_candidate)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tzinfo)
        return int(dt.timestamp())

    # 4) strptime against legacy formats - strip fractional seconds first.
    #    Only strip if what follows the dot looks like fractional seconds
    #    (digit-run terminated by EOL, +/-, or whitespace).
    dot_pos = value.find(".")
    candidate = value
    if dot_pos != -1:
        suffix = value[dot_pos + 1:]
        end = 0
        while end < len(suffix) and suffix[end].isdigit():
            end += 1
        if end > 0 and (end == len(suffix) or suffix[end] in "+-Z "):
            candidate = value[:dot_pos] + suffix[end:]

    for fmt in _DATE_FORMATS:
        try:
            t = _dt.datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        # strptime returns a tz-naive datetime - apply effective tz
        t = t.replace(tzinfo=tzinfo)
        return int(t.timestamp())

    raise TimeBoundParseError(
        f"Could not parse earliest/latest value {date_str!r} (tz={effective_tz}). "
        f"Accepted forms: epoch int (1709251200), relative time (-1d, -1h@h, now), "
        f"ISO 8601 (2024-01-01, 2024-01-01T10:00:00-07:00), "
        f"or any of {len(_DATE_FORMATS)} strptime formats. "
        f"Append /<IANA-tz> (e.g. /America/New_York) to override the timezone."
    )


def _parse_date_to_epoch(date_str: str) -> int:
    """LEGACY shim - returns 0 on failure (silent).

    Retained for :class:`functionality.ParquetEpochAdder.ParquetEpochAdder`
    which calls this per-row when backfilling ``_epoch`` on a legacy
    Parquet from a string-typed timestamp column. New code should call
    :func:`parse_date_to_epoch` directly so parse failures are surfaced
    as :class:`TimeBoundParseError` instead of silently corrupting the
    dataset with epoch-0 rows.
    """
    try:
        return parse_date_to_epoch(date_str)
    except TimeBoundParseError as exc:
        logger.warning("[!] Legacy parse fallback returning 0 for %r: %s", date_str, exc)
        return 0


# ---------------------------------------------------------------------------
# Token parsing - extract index patterns, filters, earliest/latest
# ---------------------------------------------------------------------------

def _extract_index_and_filters(
    tokens: List[str],
    tz: str = "UTC",
) -> Tuple[List[str], List[str], Optional[int], Optional[int]]:
    """Separate index=... clauses, filter tokens, and earliest/latest.

    Args:
        tokens: Flat token list emitted by the listener (or hand-built by tests).
        tz: IANA timezone for tz-naive earliest/latest values and @-snap
            anchoring. Default UTC. Inline ``/<tz>`` suffixes on individual
            values override this per-value.

    Returns:
        ``(index_patterns, filter_tokens, earliest_epoch, latest_epoch)``.

    Raises:
        TimeBoundParseError: if any earliest/latest value cannot be parsed.
            The exception propagates from :func:`parse_date_to_epoch`.
    """
    index_patterns: List[str] = []
    filter_tokens: List[str] = []

    i = 0
    while i < len(tokens):
        if (
            i + 2 < len(tokens)
            and tokens[i] == "index"
            and tokens[i + 1] == "="
        ):
            index_patterns.append(tokens[i + 2])
            i += 3
        elif tokens[i].startswith("index="):
            # Handle combined token form: index=path (from shlex.split)
            index_patterns.append(tokens[i][len("index="):])
            i += 1
        else:
            filter_tokens.append(tokens[i])
            i += 1

    if not index_patterns:
        index_patterns.append('"system_logs/**"')

    # Extract earliest/latest from filter tokens. parse_date_to_epoch raises
    # TimeBoundParseError on bad values - we add per-keyword context and
    # re-raise so the operator sees which clause failed.
    earliest_epoch: Optional[int] = None
    latest_epoch: Optional[int] = None
    remaining: List[str] = []

    def _parse_or_reraise(keyword: str, raw_val: str) -> int:
        try:
            return parse_date_to_epoch(raw_val, tz=tz)
        except TimeBoundParseError as exc:
            raise TimeBoundParseError(
                f"{keyword}={raw_val!r}: {exc}"
            ) from exc

    i = 0
    while i < len(filter_tokens):
        if (
            filter_tokens[i] in ("earliest", "latest")
            and i + 2 < len(filter_tokens)
            and filter_tokens[i + 1] == "="
        ):
            keyword = filter_tokens[i]
            val = filter_tokens[i + 2].strip('"')
            ep = _parse_or_reraise(keyword, val)
            if keyword == "earliest":
                earliest_epoch = ep
            else:
                latest_epoch = ep
            i += 3
        elif filter_tokens[i].startswith("earliest="):
            val = filter_tokens[i][len("earliest="):].strip('"')
            earliest_epoch = _parse_or_reraise("earliest", val)
            i += 1
        elif filter_tokens[i].startswith("latest="):
            val = filter_tokens[i][len("latest="):].strip('"')
            latest_epoch = _parse_or_reraise("latest", val)
            i += 1
        else:
            remaining.append(filter_tokens[i])
            i += 1

    return index_patterns, remaining, earliest_epoch, latest_epoch


# ---------------------------------------------------------------------------
# AST for filter expressions → SQL WHERE clause
# ---------------------------------------------------------------------------

class _ASTNode:
    __slots__ = ("kind", "op", "ident", "values", "left", "right", "literal")

    def __init__(self, kind: str):
        self.kind = kind  # "comparison", "logical", "in", "ident", "literal"
        self.op: Optional[str] = None
        self.ident: Optional[str] = None
        self.values: List[str] = []
        self.left: Optional["_ASTNode"] = None
        self.right: Optional["_ASTNode"] = None
        self.literal: Optional[str] = None


class _TokenObj:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str):
        self.kind = kind
        self.value = value


_OPERATORS = {"=", "!=", "<", ">", "<=", ">=", "AND", "OR", "IN"}
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


def _tokenize(raw_tokens: List[str]) -> List[_TokenObj]:
    result = []
    for tok in raw_tokens:
        if tok in ("(", ")"):
            result.append(_TokenObj("paren", tok))
        elif tok.upper() in _OPERATORS:
            result.append(_TokenObj("op", tok.upper()))
        elif tok == ",":
            result.append(_TokenObj("comma", tok))
        elif tok in ("True", "False"):
            result.append(_TokenObj("literal", tok))
        elif tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            result.append(_TokenObj("string", tok[1:-1]))
        else:
            # Try numeric
            try:
                float(tok)
                result.append(_TokenObj("number", tok))
            except ValueError:
                result.append(_TokenObj("ident", tok))
    return result


def _sql_quote(value) -> str:
    """Render *value* as a single-quoted SQL string literal, escaping embedded quotes.

    Before H-CE-1 (2026-04-21 production review) the parser wrapped filter
    values with ``f"'{t.value}'"``. A user query like ``| search title="O'Brien"``
    produced invalid SQL (``(title = 'O'Brien')``); DuckDB raised a parse
    error, which the ``_load_and_filter`` catch-all swallowed and returned an
    empty DataFrame. The user saw "no results" with no indication of cause.

    Doubling the single quote (``''``) is the standard ANSI SQL escape and
    what DuckDB's own string-literal lexer expects.
    """
    return "'" + str(value).replace("'", "''") + "'"


class _Parser:
    """Recursive descent parser that mirrors the C++ AST builder."""

    def __init__(self, tokens: List[_TokenObj]):
        self._tokens = list(tokens)
        self._pos = 0

    def _peek(self) -> Optional[_TokenObj]:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> _TokenObj:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def parse_expression(self) -> _ASTNode:
        return self._parse_or()

    def _parse_or(self) -> _ASTNode:
        node = self._parse_and()
        while True:
            t = self._peek()
            if t and t.kind == "op" and t.value == "OR":
                self._advance()
                right = self._parse_and()
                parent = _ASTNode("logical")
                parent.op = "OR"
                parent.left = node
                parent.right = right
                node = parent
            else:
                break
        return node

    def _parse_and(self) -> _ASTNode:
        node = self._parse_comparison()
        while True:
            t = self._peek()
            if t and t.kind == "op" and t.value == "AND":
                self._advance()
                right = self._parse_comparison()
                parent = _ASTNode("logical")
                parent.op = "AND"
                parent.left = node
                parent.right = right
                node = parent
            elif t and t.kind in ("ident", "string", "number") or (
                t and t.kind == "paren" and t.value == "("
            ):
                # Implicit AND
                right = self._parse_comparison()
                parent = _ASTNode("logical")
                parent.op = "AND"
                parent.left = node
                parent.right = right
                node = parent
            else:
                break
        return node

    def _parse_comparison(self) -> _ASTNode:
        t = self._peek()
        if t and t.kind == "paren" and t.value == "(":
            self._advance()
            node = self.parse_expression()
            t2 = self._peek()
            if t2 and t2.kind == "paren" and t2.value == ")":
                self._advance()
            return node

        left = self._parse_operand()
        t = self._peek()
        if t and t.kind == "op":
            op = t.value
            self._advance()
            if op == "IN":
                return self._parse_in_clause(left)
            else:
                right = self._parse_operand()
                node = _ASTNode("comparison")
                node.op = op
                node.left = left
                node.right = right
                return node
        return left

    def _parse_in_clause(self, left: _ASTNode) -> _ASTNode:
        # Expect (
        t = self._peek()
        if t and t.kind == "paren" and t.value == "(":
            self._advance()
        values = []
        while True:
            t = self._peek()
            if t is None:
                break
            if t.kind == "paren" and t.value == ")":
                self._advance()
                break
            if t.kind == "comma":
                self._advance()
                continue
            self._advance()
            if t.kind == "string":
                values.append(_sql_quote(t.value))
            elif t.kind == "number":
                values.append(t.value)
            elif t.kind == "ident":
                values.append(_sql_quote(t.value))
            elif t.kind == "literal":
                values.append(_sql_quote(t.value))
        node = _ASTNode("in")
        node.ident = left.literal
        node.values = values
        return node

    def _parse_operand(self) -> _ASTNode:
        t = self._advance()
        node = _ASTNode("ident" if t.kind == "ident" else "literal")
        if t.kind == "ident":
            node.literal = t.value
        elif t.kind == "string":
            node.literal = _sql_quote(t.value)
        elif t.kind == "number":
            node.literal = t.value
        elif t.kind == "literal":
            # True/False
            node.literal = t.value
        return node


def _ast_to_sql(node: _ASTNode) -> str:
    """Convert an AST node to a SQL WHERE clause fragment."""
    if node.kind == "comparison":
        left = _ast_to_sql(node.left)
        right = _ast_to_sql(node.right)
        # Map = to == for SQL? No - SQL uses single =
        op = node.op
        if op == "!=":
            op = "<>"
        return f"({left} {op} {right})"

    elif node.kind == "logical":
        left = _ast_to_sql(node.left)
        right = _ast_to_sql(node.right)
        return f"({left} {node.op} {right})"

    elif node.kind == "in":
        values_str = ", ".join(node.values)
        return f"({node.ident} IN ({values_str}))"

    elif node.kind in ("ident", "literal"):
        val = node.literal
        if val in ("True", "False"):
            return val
        return val

    return "TRUE"


def _parse_filter_tokens(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    """Parse filter tokens into a SQL WHERE clause and list of referenced columns.

    Returns (sql_where_fragment_or_None, list_of_column_names).
    """
    if not tokens:
        return None, []

    token_objs = _tokenize(tokens)
    if not token_objs:
        return None, []

    try:
        parser = _Parser(token_objs)
        ast = parser.parse_expression()
    except (IndexError, RuntimeError):
        return None, []

    columns: List[str] = []
    _collect_columns(ast, columns)
    sql = _ast_to_sql(ast)
    if not sql or sql == "TRUE":
        return None, columns
    return sql, columns


def _collect_columns(node: _ASTNode, columns: List[str]) -> None:
    """Walk the AST and collect column identifiers."""
    if node.kind == "comparison" or node.kind == "logical":
        if node.left:
            _collect_columns(node.left, columns)
        if node.right:
            _collect_columns(node.right, columns)
    elif node.kind == "in":
        if node.ident and node.ident not in columns:
            columns.append(node.ident)
    elif node.kind == "ident":
        if node.literal and node.literal not in columns:
            columns.append(node.literal)


# ---------------------------------------------------------------------------
# Pattern resolution (mirrors C++ adjust_pattern)
# ---------------------------------------------------------------------------

def _resolve_glob_pattern(raw_pattern: str) -> str:
    """Convert a user-facing index pattern into a filesystem glob path.

    Handles: stripping quotes/parens, stripping leading 'indexes/' prefix,
    detecting files vs directories, and appending /**/*.parquet as needed.

    Accepted user-facing forms (all in CLAUDE.md / docs examples):

    * ``indexes/foo/bar.parquet`` - single file
    * ``indexes/foo/`` - directory (recurse)
    * ``indexes/foo/*`` - files directly under foo/
    * ``indexes/foo/**`` - recurse under foo/
    * ``indexes/foo/*.parquet`` - files directly under foo/
    * ``indexes/foo/**/*.parquet`` - recurse under foo/ (verbatim)
    """
    pat = raw_pattern.strip('"').strip(",").strip("(").strip(")")

    # Strip leading "indexes/" - INDEXES_DIR already points there
    if pat.startswith("indexes/"):
        pat = pat[len("indexes/"):]

    # Patterns that already resolve to a glob (contain a filename-level
    # wildcard and end in .parquet) are used as-is - the default feeder
    # queries ship with forms like `indexes/<subdir>/*.parquet`, and the
    # old resolver broke them by appending `/**/*.parquet` on top, which
    # treated `*.parquet` as a directory component and matched zero files.
    basename = pat.rsplit("/", 1)[-1]
    if pat.endswith(".parquet") and "*" in basename:
        return str(INDEXES_DIR / pat)

    if pat.endswith("/**"):
        return str(INDEXES_DIR / pat[:-3] / "**" / "*.parquet")
    elif pat.endswith("/*"):
        return str(INDEXES_DIR / pat[:-2] / "*.parquet")
    else:
        possible = INDEXES_DIR / pat
        if possible.is_file():
            return str(possible)
        elif possible.is_dir():
            return str(possible / "**" / "*.parquet")
        else:
            return str(INDEXES_DIR / pat / "**" / "*.parquet")


def _resolve_files(pattern: str) -> List[str]:
    """Expand a glob pattern to a sorted list of .parquet file paths.

    Embedding sidecars (``*.embeddings.parquet``, written next to every
    source parquet by the embedding sweeper) are infrastructure, not data:
    a wildcard or directory index must never load them as rows, or every
    swept source double-counts. They are only returned when the pattern
    names a sidecar file explicitly (a deliberate inspection escape hatch).
    """
    explicit_sidecar = (
        pattern.endswith(".embeddings.parquet")
        and "*" not in os.path.basename(pattern)
    )
    files = sorted(glob.glob(pattern, recursive=True))
    result = [f for f in files if f.endswith(".parquet") and os.path.isfile(f)]
    if not explicit_sidecar:
        result = [f for f in result if not f.endswith(".embeddings.parquet")]
    if not result:
        # Diagnostic: help operators figure out why no files matched
        parent = os.path.dirname(pattern.split("*")[0].rstrip("/"))
        if not os.path.exists(parent):
            logger.warning(
                "[!] Glob matched 0 files - parent directory does not exist: %s",
                parent,
            )
        elif not os.access(parent, os.R_OK):
            logger.warning(
                "[!] Glob matched 0 files - permission denied on directory: %s "
                "(uid=%s)",
                parent,
                os.getuid() if hasattr(os, "getuid") else "n/a",
            )
        else:
            logger.info(
                "[i] Glob matched 0 files for pattern: %s (directory exists "
                "and is readable - no .parquet files present)",
                pattern,
            )
    return result


# ---------------------------------------------------------------------------
# Core: load and filter via DuckDB
# ---------------------------------------------------------------------------

def _load_and_filter(
    index_pattern: str,
    sql_where: Optional[str],
    earliest_epoch: Optional[int],
    latest_epoch: Optional[int],
    filter_columns: List[str],
) -> pd.DataFrame:
    """Load Parquet files matching *index_pattern* and apply filters via DuckDB.

    Returns a pandas DataFrame with a ``_source_file`` column (relative to
    indexes dir) - matching the C++ behaviour.
    """
    glob_pattern = _resolve_glob_pattern(index_pattern)
    files = _resolve_files(glob_pattern)

    logger.info("[i] Pattern '%s' resolved to %d file(s)", glob_pattern, len(files))

    if not files:
        return pd.DataFrame()

    need_epoch = earliest_epoch is not None or latest_epoch is not None

    dataframes: List[pd.DataFrame] = []

    for fpath in files:
        rel_path = os.path.relpath(fpath, str(INDEXES_DIR))

        # Peek at schema to validate required columns exist
        try:
            schema_df = duckdb.sql(
                f"SELECT name FROM parquet_schema('{fpath}')"
            ).df()
            available_cols = set(schema_df["name"].tolist())
        except Exception as e:
            logger.warning("[!] Cannot read schema of %s: %s", fpath, e)
            continue

        # Check filter columns exist
        missing = [c for c in filter_columns if c not in available_cols]
        if missing:
            logger.info(
                "[i] Skipping %s - missing columns: %s", fpath, missing
            )
            continue

        # Check timestamp column exists if we need epoch filtering
        if need_epoch and "timestamp" not in available_cols and "_epoch" not in available_cols:
            logger.info(
                "[i] Skipping %s - no 'timestamp' or '_epoch' for time filter",
                fpath,
            )
            continue

        # Build the SQL query
        where_parts: List[str] = []

        if need_epoch:
            has_epoch_col = "_epoch" in available_cols
            if has_epoch_col:
                # Use existing _epoch column directly
                epoch_expr = "_epoch"
            else:
                # Use DuckDB's native timestamp parsing - cast timestamp to epoch
                epoch_expr = "epoch(TRY_CAST(timestamp AS TIMESTAMP))"

            if earliest_epoch is not None:
                where_parts.append(f"{epoch_expr} >= {earliest_epoch}")
            if latest_epoch is not None:
                where_parts.append(f"{epoch_expr} <= {latest_epoch}")

        if sql_where:
            where_parts.append(sql_where)

        where_clause = ""
        if where_parts:
            where_clause = " WHERE " + " AND ".join(where_parts)

        sql = f"SELECT * FROM read_parquet('{fpath}'){where_clause}"
        logger.info("[i] DuckDB SQL: %s", sql)

        try:
            df = duckdb.sql(sql).df()
            # Normalize nullable integer types (Int64 → float64) to match
            # the behaviour of pd.read_parquet, which uses float64 for
            # integer columns that contain NaN.
            for col in df.columns:
                if pd.api.types.is_integer_dtype(df[col]) and df[col].isna().any():
                    df[col] = df[col].astype("float64")
                elif hasattr(df[col].dtype, "name") and df[col].dtype.name in (
                    "Int8", "Int16", "Int32", "Int64",
                    "UInt8", "UInt16", "UInt32", "UInt64",
                ):
                    df[col] = df[col].astype("float64")
        except Exception as e:
            logger.warning(
                "[!] DuckDB query failed on %s: %s - returning empty",
                fpath,
                e,
            )
            df = pd.DataFrame()

        if not df.empty:
            df["_source_file"] = rel_path
        dataframes.append(df)

    if not dataframes:
        return pd.DataFrame()
    elif len(dataframes) == 1:
        return dataframes[0]
    else:
        return pd.concat(dataframes, ignore_index=True)


# ---------------------------------------------------------------------------
# Public API - drop-in replacement for cpp_index_call.process_index_calls
# ---------------------------------------------------------------------------

def process_index_calls(tokens: List[str], tz: str = "UTC") -> pd.DataFrame:
    """Process index calls from a list of SPQL tokens.

    This is the public entry point - same signature as the C++ version,
    plus an optional ``tz`` for tz-naive earliest/latest interpretation.

    Args:
        tokens: List of token strings, e.g.
                ['index', '=', '"system_logs/**"', 'status', '=', '"ERROR"']
        tz: IANA timezone name used to interpret tz-naive ``earliest=``/
            ``latest=`` values and to anchor relative-time @-snap.
            Default UTC. Inline ``/<tz>`` suffixes on individual values
            override this per-value.

    Returns:
        A pandas DataFrame containing the filtered results.

    Raises:
        TimeBoundParseError: if any ``earliest=``/``latest=`` value cannot
            be parsed. Caught by ``process_query_with_diagnostics`` and
            surfaced to the operator instead of silently returning an
            unfiltered dataset (the legacy behaviour).
    """
    logger.info("[i] duckdb_index_call.process_index_calls called with %d tokens", len(tokens))

    index_patterns, filter_tokens, earliest_epoch, latest_epoch = (
        _extract_index_and_filters(tokens, tz=tz)
    )

    sql_where, filter_columns = _parse_filter_tokens(filter_tokens)

    results: List[pd.DataFrame] = []
    for pattern in index_patterns:
        logger.info("[i] Processing index pattern: %s", pattern)
        df = _load_and_filter(
            pattern, sql_where, earliest_epoch, latest_epoch, filter_columns
        )
        results.append(df)

    if not results:
        return pd.DataFrame()
    elif len(results) == 1:
        return results[0]
    else:
        return pd.concat(results, ignore_index=True)
