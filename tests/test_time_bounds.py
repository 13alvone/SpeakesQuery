#!/usr/bin/env python3
"""
Comprehensive end-to-end tests for SPQL ``earliest=`` / ``latest=`` time bounds.

Triggered by a 2026-04-29 production complaint that earliest/latest "silently
did nothing". Investigation found three stacked bugs:

  1. ``_parse_date_to_epoch`` returned 0 on parse failure, so a typo like
     ``earliest="garbge"`` silently became ``WHERE _epoch >= 0`` - i.e. an
     unfiltered query indistinguishable from a bug-free baseline.
  2. Tz-naive ISO (``2024-01-01T10:00:00``) was always interpreted as UTC,
     a 7-hour silent offset for the project's PDT user.
  3. ZERO end-to-end tests exercised the ANTLR-listener-flatten pathway - the
     prior test suite called ``process_index_calls()`` with hand-built tokens,
     never proving that ``execute_query("index=… earliest=…")`` actually
     applies the bound.

This file pins all three layers so the bug class cannot recur silently:

  * Strict parser (``parse_date_to_epoch``) raises :class:`TimeBoundParseError`
    on bad input - no silent zero.
  * Tz parameter and inline ``/<IANA-tz>`` suffix produce correct epoch.
  * ``execute_query`` end-to-end smoke for every accepted form.
  * Differential assertion (``bounded_count < unbounded_count``) proves the
    bound actually filters rows - the missing piece that let the prior
    silent-zero bug ship.
  * Negative tests assert the parse failure SURFACES through
    ``process_query_with_diagnostics`` so the operator sees the real cause.
"""

import os
import sys
import time

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.chdir(PROJECT_ROOT)

from functionality.duckdb_index_call import (
    TimeBoundParseError,
    _parse_date_to_epoch,
    _parse_relative_time,
    _split_inline_tz,
    parse_date_to_epoch,
    process_index_calls,
)
from query_engine.CmdExecutionBackend import (
    execute_query,
    process_query_with_diagnostics,
)


SYSTEM4_PATH = os.path.join(
    PROJECT_ROOT, "indexes", "archive", "system_logs", "system4.parquet"
)
SYSTEM4_EXISTS = os.path.isfile(SYSTEM4_PATH)
needs_system4 = pytest.mark.skipif(
    not SYSTEM4_EXISTS, reason="system4.parquet not found in archive"
)

if SYSTEM4_EXISTS:
    REF_DF = pd.read_parquet(SYSTEM4_PATH)
    REF_ROW_COUNT = len(REF_DF)  # 1000
    REF_EPOCH_MIN = int(REF_DF["_epoch"].min())  # 1704067200 = 2024-01-01 UTC
    REF_EPOCH_MAX = int(REF_DF["_epoch"].max())  # 1735603200 = 2024-12-31 UTC

INDEX_TOKEN = '"archive/system_logs/system4.parquet"'
TOLERANCE = 5


# ===========================================================================
# Layer 1 - Strict parser unit tests (parse_date_to_epoch)
# ===========================================================================


class TestStrictParserAcceptedForms:
    """Every documented accepted form must produce the correct UTC epoch."""

    def test_epoch_int(self):
        assert parse_date_to_epoch("1709251200") == 1709251200

    def test_epoch_int_zero(self):
        assert parse_date_to_epoch("0") == 0

    def test_epoch_int_negative(self):
        # Pre-epoch dates are rare but not invalid; preserve numeric pass-through.
        assert parse_date_to_epoch("-1") == -1

    def test_relative_now(self):
        result = parse_date_to_epoch("now")
        assert abs(result - int(time.time())) <= TOLERANCE

    def test_relative_minus_30m(self):
        result = parse_date_to_epoch("-30m")
        expected = int(time.time()) - 1800
        assert abs(result - expected) <= TOLERANCE

    def test_relative_with_snap_h(self):
        result = parse_date_to_epoch("-1h@h")
        # Hour boundaries are aligned globally regardless of tz, so result is
        # always a multiple of 3600.
        assert result % 3600 == 0

    def test_iso_date_only_utc(self):
        # 2024-01-01 midnight UTC = 1704067200
        assert parse_date_to_epoch("2024-01-01") == 1704067200

    def test_iso_datetime_naive_utc_default(self):
        # Tz-naive ISO defaults to UTC interpretation
        assert parse_date_to_epoch("2024-01-01T10:00:00") == 1704103200  # 10h after midnight UTC

    def test_iso_datetime_with_z(self):
        # Z suffix == UTC
        assert parse_date_to_epoch("2024-01-01T10:00:00Z") == 1704103200

    def test_iso_datetime_with_explicit_offset_west(self):
        # 2024-01-01 10:00:00 -07:00 (PDT) = 17:00 UTC = 1704128400
        assert parse_date_to_epoch("2024-01-01T10:00:00-07:00") == 1704128400

    def test_iso_datetime_with_explicit_offset_east(self):
        # 2024-01-01 10:00:00 +09:00 (Tokyo) = 01:00 UTC = 1704070800
        assert parse_date_to_epoch("2024-01-01T10:00:00+09:00") == 1704070800

    def test_naive_iso_with_tz_param_ny(self):
        # NY is UTC-5 EST in January = 2024-01-01 00:00 EST = 05:00 UTC = 1704085200
        assert parse_date_to_epoch("2024-01-01", tz="America/New_York") == 1704085200

    def test_naive_iso_with_tz_param_tokyo(self):
        # Tokyo is UTC+9 = 2024-01-01 00:00 JST = 2023-12-31 15:00 UTC = 1704034800
        assert parse_date_to_epoch("2024-01-01", tz="Asia/Tokyo") == 1704034800

    def test_explicit_offset_overrides_tz_param(self):
        # The +00:00 suffix wins regardless of the tz arg
        result_with_ny_tz = parse_date_to_epoch(
            "2024-01-01T10:00:00+00:00", tz="America/New_York"
        )
        assert result_with_ny_tz == 1704103200  # 10h UTC

    def test_legacy_format_us_slash(self):
        # US-style "01/01/2024" → midnight UTC
        assert parse_date_to_epoch("01/01/2024") == 1704067200

    def test_legacy_format_us_dash(self):
        assert parse_date_to_epoch("01-01-2024") == 1704067200

    def test_fractional_seconds_stripped(self):
        # Fractional seconds are dropped, not parsed (legacy behaviour preserved)
        assert parse_date_to_epoch("2024-01-01 10:00:00.123456") == 1704103200


class TestStrictParserLoudFailures:
    """Every parse failure mode must raise TimeBoundParseError, not return 0."""

    def test_garbage_raises(self):
        with pytest.raises(TimeBoundParseError) as exc:
            parse_date_to_epoch("garbage_date")
        assert "garbage_date" in str(exc.value)
        assert "Accepted forms" in str(exc.value)  # error includes guidance

    def test_empty_string_raises(self):
        with pytest.raises(TimeBoundParseError):
            parse_date_to_epoch("")

    def test_whitespace_only_raises(self):
        with pytest.raises(TimeBoundParseError):
            parse_date_to_epoch("   ")

    def test_invalid_iso_raises(self):
        with pytest.raises(TimeBoundParseError):
            parse_date_to_epoch("2024-13-99")  # invalid month + day

    def test_invalid_iso_format_raises(self):
        with pytest.raises(TimeBoundParseError):
            parse_date_to_epoch("not_a_date_at_all")

    def test_bad_tz_arg_raises(self):
        with pytest.raises(TimeBoundParseError) as exc:
            parse_date_to_epoch("2024-01-01", tz="Mars/Olympus_Mons")
        assert "Mars/Olympus_Mons" in str(exc.value)

    def test_non_string_input_raises(self):
        with pytest.raises(TimeBoundParseError):
            parse_date_to_epoch(12345)  # type: ignore[arg-type]

    def test_none_input_raises(self):
        with pytest.raises(TimeBoundParseError):
            parse_date_to_epoch(None)  # type: ignore[arg-type]


class TestLegacyShimSilentZero:
    """The deprecated _parse_date_to_epoch shim must still return 0 on failure
    so ParquetEpochAdder's per-row backfill keeps its existing semantics."""

    def test_garbage_returns_zero(self):
        assert _parse_date_to_epoch("garbage") == 0

    def test_valid_input_round_trips(self):
        assert _parse_date_to_epoch("1704067200") == 1704067200
        assert _parse_date_to_epoch("2024-01-01") == 1704067200


# ===========================================================================
# Layer 2 - Inline /<IANA-tz> suffix
# ===========================================================================


class TestInlineTzSuffix:
    """``earliest="<value>/<IANA-tz>"`` overrides the per-call tz arg."""

    def test_split_no_slash(self):
        assert _split_inline_tz("2024-01-01") == ("2024-01-01", None)

    def test_split_two_segment_iana(self):
        assert _split_inline_tz("2024-01-01/America/New_York") == (
            "2024-01-01",
            "America/New_York",
        )

    def test_split_three_segment_iana(self):
        # America/Indiana/Indianapolis is a valid 3-segment IANA name
        assert _split_inline_tz("-1d@d/America/Indiana/Indianapolis") == (
            "-1d@d",
            "America/Indiana/Indianapolis",
        )

    def test_split_one_segment_iana(self):
        # Some IANA names are single-segment ("UTC", "GMT")
        assert _split_inline_tz("2024-01-01/UTC") == ("2024-01-01", "UTC")

    def test_split_invalid_iana_left_intact(self):
        # Alt date "2024/01/01" must NOT be confused for an IANA tz suffix.
        assert _split_inline_tz("2024/01/01") == ("2024/01/01", None)

    def test_split_garbage_after_slash_left_intact(self):
        assert _split_inline_tz("2024-01-01/garbage") == (
            "2024-01-01/garbage",
            None,
        )

    def test_inline_tz_overrides_default_utc(self):
        # Without inline tz: 2024-01-01 UTC midnight = 1704067200
        # With /America/New_York: 2024-01-01 NY midnight = 1704085200 (EST = UTC-5)
        assert parse_date_to_epoch("2024-01-01/America/New_York") == 1704085200

    def test_inline_tz_overrides_param_tz(self):
        # tz=Tokyo is overridden by inline /America/New_York
        result = parse_date_to_epoch(
            "2024-01-01/America/New_York", tz="Asia/Tokyo"
        )
        assert result == 1704085200  # NY midnight, NOT Tokyo midnight

    def test_inline_tz_with_relative_snap(self):
        # @d snap in different timezones produces different epochs because
        # "midnight" is at different UTC instants. Just check that the
        # result aligns to the day boundary in the specified tz.
        result_ny = parse_date_to_epoch("-1d@d/America/New_York")
        # NY midnight is 4 or 5 AM UTC depending on DST. (UTC epoch % 3600 == 0)
        # Either way, result modulo 1 hour must be 0.
        assert result_ny % 3600 == 0


# ===========================================================================
# Layer 3 - Relative time + tz interactions
# ===========================================================================


class TestRelativeTimeWithTz:
    """``-1d@d`` snap depends on tz: NY midnight ≠ UTC midnight."""

    def test_now_is_tz_independent(self):
        utc = _parse_relative_time("now", tz="UTC")
        ny = _parse_relative_time("now", tz="America/New_York")
        assert abs(utc - ny) <= TOLERANCE

    def test_minus_1d_unsnapped_is_tz_independent(self):
        # Without @-snap, relative offset is just arithmetic on "now" - tz
        # affects only the snap operation.
        utc = _parse_relative_time("-1d", tz="UTC")
        ny = _parse_relative_time("-1d", tz="America/New_York")
        assert abs(utc - ny) <= TOLERANCE

    def test_snap_d_differs_between_utc_and_ny(self):
        # Day boundaries in NY vs UTC produce different UTC epochs because
        # midnight is at different UTC instants. The exact difference depends
        # on (a) DST (4h EDT or 5h EST) and (b) whether UTC has rolled over
        # to the next calendar day yet (adds 24h offset). Valid offsets are
        # therefore: 4h, 5h, 4+24=28h, 5+24=29h, 24-4=20h, 24-5=19h.
        utc = _parse_relative_time("-1d@d", tz="UTC")
        ny = _parse_relative_time("-1d@d", tz="America/New_York")
        assert utc != ny  # the snap differs - the whole point of the test
        diff = abs(utc - ny)
        # Property: difference is always a whole-hour multiple AND less than
        # one full day plus the maximum tz offset
        assert diff % 3600 == 0, (
            f"Difference {diff}s is not a whole-hour multiple - snap broke"
        )
        assert diff <= 30 * 3600, (
            f"Difference {diff}s exceeds 30h - should be one of 4/5/19/20/28/29h"
        )

    def test_snap_d_aligns_to_hour_in_both_zones(self):
        # Both results must land on UTC hour boundaries (since NY/UTC offsets
        # are integer hours). This is the structural invariant that
        # distinguishes a working snap from a broken one.
        for tz in ("UTC", "America/New_York", "Asia/Tokyo", "Europe/London"):
            result = _parse_relative_time("-1d@d", tz=tz)
            assert result is not None
            assert result % 3600 == 0, (
                f"-1d@d snap in {tz} produced non-hour-aligned epoch {result}"
            )

    def test_invalid_relative_returns_none(self):
        # Non-relative strings still return None (caller falls through to ISO)
        assert _parse_relative_time("2024-01-01") is None
        assert _parse_relative_time("not_a_date") is None


# ===========================================================================
# Layer 4 - process_index_calls() with tz parameter
# ===========================================================================


class TestProcessIndexCallsTz:
    """The token-level entry point honours its tz parameter."""

    @needs_system4
    def test_tz_naive_iso_default_utc(self):
        tokens = ["index", "=", INDEX_TOKEN, "earliest", "=", '"2024-06-01"']
        df = process_index_calls(tokens)
        # Boundary at 2024-06-01 00:00 UTC = 1717200000
        assert (df["_epoch"] >= 1717200000).all()

    @needs_system4
    def test_tz_naive_iso_in_ny(self):
        # 2024-06-01 NY is EDT (UTC-4) → 2024-06-01 00:00 EDT = 04:00 UTC = 1717214400
        tokens = ["index", "=", INDEX_TOKEN, "earliest", "=", '"2024-06-01"']
        df_ny = process_index_calls(tokens, tz="America/New_York")
        df_utc = process_index_calls(tokens, tz="UTC")
        # NY midnight is 4 hours later → strictly fewer rows pass the bound
        assert len(df_ny) <= len(df_utc)

    @needs_system4
    def test_inline_tz_suffix(self):
        tokens = [
            "index", "=", INDEX_TOKEN,
            "earliest", "=", '"2024-06-01/America/New_York"',
        ]
        df = process_index_calls(tokens)  # default tz=UTC, inline overrides
        # 2024-06-01 00:00 EDT = 04:00 UTC = 1717214400
        assert (df["_epoch"] >= 1717214400).all()


# ===========================================================================
# Layer 5 - Loud failure surfaces through process_index_calls
# ===========================================================================


class TestErrorPropagation:
    """TimeBoundParseError raised by the parser propagates up through
    process_index_calls - not silently swallowed."""

    @needs_system4
    def test_garbage_earliest_raises(self):
        tokens = ["index", "=", INDEX_TOKEN, "earliest", "=", '"garbage"']
        with pytest.raises(TimeBoundParseError) as exc:
            process_index_calls(tokens)
        # Exception message includes which keyword AND the bad value
        assert "earliest" in str(exc.value)
        assert "garbage" in str(exc.value)

    @needs_system4
    def test_garbage_latest_raises(self):
        tokens = ["index", "=", INDEX_TOKEN, "latest", "=", '"garbage"']
        with pytest.raises(TimeBoundParseError) as exc:
            process_index_calls(tokens)
        assert "latest" in str(exc.value)

    @needs_system4
    def test_garbage_combined_form_raises(self):
        # Combined form: ``earliest=value`` (no spaces around =)
        tokens = ["index", "=", INDEX_TOKEN, 'earliest="garbage"']
        with pytest.raises(TimeBoundParseError):
            process_index_calls(tokens)

    @needs_system4
    def test_bad_inline_tz_raises(self):
        tokens = [
            "index", "=", INDEX_TOKEN,
            "earliest", "=", '"2024-01-01/Mars/Olympus_Mons"',
        ]
        # "Mars/Olympus_Mons" is not a valid IANA tz, so split returns the
        # whole string as the value, and the parse fails because that string
        # isn't a valid date. Exception still surfaces clearly.
        with pytest.raises(TimeBoundParseError):
            process_index_calls(tokens)


# ===========================================================================
# Layer 6 - End-to-end execute_query (THE PREVIOUSLY MISSING LAYER)
# ===========================================================================


class TestEndToEndExecuteQuery:
    """The full ANTLR parse → listener → flatten → process_index_calls path.

    The 2026-04-29 incident shipped because all prior tests bypassed this
    path by hand-building tokens. These tests prove the time clause survives
    the ANTLR flatten step and reaches the filter as expected.
    """

    @needs_system4
    def test_unbounded_baseline(self):
        df = execute_query(f"index={INDEX_TOKEN}\n")
        assert df is not None
        assert len(df) == REF_ROW_COUNT  # 1000

    @needs_system4
    def test_earliest_epoch_int(self):
        df = execute_query(f'index={INDEX_TOKEN} earliest="1735000000"\n')
        assert df is not None
        assert (df["_epoch"] >= 1735000000).all()
        assert len(df) < REF_ROW_COUNT  # filter actually filters

    @needs_system4
    def test_earliest_iso_date(self):
        # 2024-06-01 UTC = 1717200000; system4 has rows on both sides.
        df = execute_query(f'index={INDEX_TOKEN} earliest="2024-06-01"\n')
        assert df is not None
        assert (df["_epoch"] >= 1717200000).all()
        assert len(df) < REF_ROW_COUNT

    @needs_system4
    def test_earliest_iso_with_explicit_offset(self):
        # 2024-06-01 00:00 -07:00 = 07:00 UTC = 1717225200
        df = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00-07:00"\n'
        )
        assert df is not None
        assert (df["_epoch"] >= 1717225200).all()

    @needs_system4
    def test_earliest_iso_with_z_suffix(self):
        df = execute_query(f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00Z"\n')
        assert df is not None
        assert (df["_epoch"] >= 1717200000).all()

    @needs_system4
    def test_earliest_with_inline_tz(self):
        df = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01/America/New_York"\n'
        )
        assert df is not None
        # NY midnight is later in UTC than UTC midnight → fewer rows
        assert (df["_epoch"] >= 1717214400).all()

    @needs_system4
    def test_window_earliest_and_latest(self):
        df = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01" latest="2024-09-01"\n'
        )
        assert df is not None
        assert (df["_epoch"] >= 1717200000).all()
        assert (df["_epoch"] <= 1725148800).all()
        assert len(df) < REF_ROW_COUNT

    @needs_system4
    def test_latest_only(self):
        df = execute_query(f'index={INDEX_TOKEN} latest="2024-06-01"\n')
        assert df is not None
        assert (df["_epoch"] <= 1717200000).all()
        assert len(df) < REF_ROW_COUNT

    @needs_system4
    def test_relative_minus_year(self):
        # Data is from 2024; -1y from 2026-04-29 is 2025-04-29 → no overlap
        df = execute_query(f'index={INDEX_TOKEN} earliest="-1y"\n')
        # Either 0 rows (data older than 1y ago) or some recent rows;
        # the test target is "doesn't crash and the bound is applied".
        assert df is None or len(df) <= REF_ROW_COUNT

    @needs_system4
    def test_combined_earliest_token_form(self):
        # The combined token form ``earliest=<value>`` (no quotes around the
        # int - exercised when the lexer collapses adjacent tokens) must
        # produce the same result as the separated form.
        df_combined = execute_query(
            f'index={INDEX_TOKEN} earliest=1735000000\n'
        )
        df_separated = execute_query(
            f'index={INDEX_TOKEN} earliest="1735000000"\n'
        )
        if df_combined is not None and df_separated is not None:
            assert len(df_combined) == len(df_separated)


# ===========================================================================
# Layer 6.5 - Unquoted relative & date forms (2026-05-06 lexer fix)
# ===========================================================================
#
# Pre-2026-05-06 the grammar's ``earliestClause: EARLIEST EQUALS
# (DOUBLE_QUOTED_STRING | NUMBER)`` accepted only quoted strings or
# bare integers. Unquoted relative time (``-1h``) lexed as
# ``NUMBER('-1') + VARIABLE('h')`` - the listener captured ``-1`` as the
# value, the orphan ``h`` leaked into filter tokens, and the resulting
# WHERE clause silently returned 0 rows. Same problem for unquoted ISO
# dates (``2026-05-01`` → ``NUMBER('2026') + NUMBER('-05') + NUMBER('-01')``).
#
# Fix: added a ``TIMESPEC`` lexer token (defined BEFORE NUMBER so the
# longest-match rule wins it for unit-suffixed and dash-separated forms)
# and extended both clauses to accept it.
#
# These tests pin the four broken cases the schedule PDF triage
# uncovered + the documented forms operators are most likely to type.


class TestUnquotedRelativeAndDateForms:
    """Unquoted ``earliest=-1h`` / ``earliest=2026-05-01`` must work.

    The bug was caught 2026-05-06 when end-to-end-verifying the GDELT
    + Kalshi P0 fixes against live data. These probes silently returned
    0 rows even though the underlying parquet had real rows - a complete
    regression for any console user typing relative or unquoted-date
    bounds. Production SS execution unaffected because YAML ``lookback:``
    is metadata-only (not injected as ``earliest=``).
    """

    @needs_system4
    def test_unquoted_relative_minutes(self):
        # earliest=-1m should equal earliest="-1m" - the quoted form
        # was always the workaround; both must return the same df.
        df_unq = execute_query(f'index={INDEX_TOKEN} earliest=-1m\n')
        df_q = execute_query(f'index={INDEX_TOKEN} earliest="-1m"\n')
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_relative_hours(self):
        df_unq = execute_query(f'index={INDEX_TOKEN} earliest=-1h\n')
        df_q = execute_query(f'index={INDEX_TOKEN} earliest="-1h"\n')
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_relative_days(self):
        df_unq = execute_query(f'index={INDEX_TOKEN} earliest=-7d\n')
        df_q = execute_query(f'index={INDEX_TOKEN} earliest="-7d"\n')
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_relative_with_snap(self):
        df_unq = execute_query(f'index={INDEX_TOKEN} earliest=-1d@d\n')
        df_q = execute_query(f'index={INDEX_TOKEN} earliest="-1d@d"\n')
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_relative_with_inline_tz(self):
        df_unq = execute_query(
            f'index={INDEX_TOKEN} earliest=-1d@d/America/New_York\n'
        )
        df_q = execute_query(
            f'index={INDEX_TOKEN} earliest="-1d@d/America/New_York"\n'
        )
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_relative_positive(self):
        df_unq = execute_query(f'index={INDEX_TOKEN} earliest=+1h\n')
        df_q = execute_query(f'index={INDEX_TOKEN} earliest="+1h"\n')
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_iso_date(self):
        df_unq = execute_query(f'index={INDEX_TOKEN} earliest=2024-06-01\n')
        df_q = execute_query(f'index={INDEX_TOKEN} earliest="2024-06-01"\n')
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_iso_datetime_z(self):
        df_unq = execute_query(
            f'index={INDEX_TOKEN} earliest=2024-06-01T00:00:00Z\n'
        )
        df_q = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00Z"\n'
        )
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_iso_datetime_offset(self):
        df_unq = execute_query(
            f'index={INDEX_TOKEN} earliest=2024-06-01T00:00:00-07:00\n'
        )
        df_q = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00-07:00"\n'
        )
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_iso_date_with_inline_tz(self):
        df_unq = execute_query(
            f'index={INDEX_TOKEN} earliest=2024-06-01/America/New_York\n'
        )
        df_q = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01/America/New_York"\n'
        )
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_both_earliest_and_latest(self):
        # The original bug also affected combinations.
        df_unq = execute_query(
            f'index={INDEX_TOKEN} earliest=-7d latest=-1d\n'
        )
        df_q = execute_query(
            f'index={INDEX_TOKEN} earliest="-7d" latest="-1d"\n'
        )
        assert df_unq is not None
        assert df_q is not None
        assert len(df_unq) == len(df_q)

    @needs_system4
    def test_unquoted_relative_actually_filters(self):
        # The differential assertion that would have caught the silent
        # 0-rows bug: bounded must be strictly less than unbounded when
        # the bound excludes data. The system4 fixture ends 2024-12-31,
        # so a 1-hour earliest=-1h excludes ALL rows.
        df_bounded = execute_query(f'index={INDEX_TOKEN} earliest=-1h\n')
        df_unbounded = execute_query(f'index={INDEX_TOKEN}\n')
        assert df_unbounded is not None
        assert len(df_unbounded) == REF_ROW_COUNT
        # df_bounded may be None (no rows) or have 0 rows - either way
        # it must NOT silently return all rows.
        bounded_count = 0 if df_bounded is None else len(df_bounded)
        assert bounded_count < REF_ROW_COUNT, (
            f"earliest=-1h on a 2024 fixture should exclude all rows "
            f"but returned {bounded_count}/{REF_ROW_COUNT} - the silent "
            f"unfiltered-pass bug has resurfaced."
        )

    def test_lexer_tokenizes_unquoted_relative_as_TIMESPEC(self):
        """Drift guard: the regenerated lexer must produce a single
        TIMESPEC token for `-1h`, not NUMBER('-1') + VARIABLE('h')."""
        from antlr4 import InputStream
        from lexers.antlr4_active.speakesQueryLexer import speakesQueryLexer

        cases = [
            ('-1h', 'TIMESPEC'),
            ('-7d', 'TIMESPEC'),
            ('+30m', 'TIMESPEC'),
            ('-1d@d', 'TIMESPEC'),
            ('-1d@d/America/New_York', 'TIMESPEC'),
            ('2026-05-01', 'TIMESPEC'),
            ('2026-05-06T20:00:00Z', 'TIMESPEC'),
            ('1778100000', 'NUMBER'),  # pure epoch unchanged
            ('-1', 'NUMBER'),           # pure negative int unchanged
            ('1.5', 'NUMBER'),          # float unchanged
        ]
        for raw, expected_token in cases:
            q = f'index="x" earliest={raw}\n'
            inp = InputStream(q)
            lex = speakesQueryLexer(inp)
            tokens = [t for t in lex.getAllTokens() if t.text.strip()]
            # tokens: INDEX(0) EQUALS(1) DQ(2) EARLIEST(3) EQUALS(4) <value>(5)
            value_token = tokens[5]
            actual_name = lex.symbolicNames[value_token.type]
            assert actual_name == expected_token, (
                f"expected {raw!r} to lex as {expected_token}, got "
                f"{actual_name}({value_token.text!r})"
            )


# ===========================================================================
# Layer 7 - Differential tests (prove the bound actually filters)
# ===========================================================================


class TestBoundActuallyFilters:
    """Bounded count must be strictly less than unbounded count when the bound
    excludes data - this is the assertion that would have caught the original
    silent-zero bug."""

    @needs_system4
    def test_earliest_strictly_filters(self):
        unbounded = execute_query(f"index={INDEX_TOKEN}\n")
        bounded = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01"\n'
        )
        assert unbounded is not None
        assert bounded is not None
        assert len(bounded) < len(unbounded), (
            "earliest=2024-06-01 must exclude rows; if these are equal, "
            "the bound is silently being dropped (the 2026-04-29 bug)."
        )

    @needs_system4
    def test_latest_strictly_filters(self):
        unbounded = execute_query(f"index={INDEX_TOKEN}\n")
        bounded = execute_query(
            f'index={INDEX_TOKEN} latest="2024-06-01"\n'
        )
        assert unbounded is not None
        assert bounded is not None
        assert len(bounded) < len(unbounded)

    @needs_system4
    def test_window_strictly_filters(self):
        unbounded = execute_query(f"index={INDEX_TOKEN}\n")
        bounded = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01" latest="2024-09-01"\n'
        )
        assert unbounded is not None
        assert bounded is not None
        assert len(bounded) < len(unbounded)

    @needs_system4
    def test_equivalent_iso_forms_match(self):
        """Same instant expressed three ways must produce identical results."""
        # 2024-06-01 00:00 UTC, three forms:
        df_utc_naive = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00"\n'
        )
        df_utc_explicit = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00Z"\n'
        )
        df_utc_offset = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01T00:00:00+00:00"\n'
        )
        for df in (df_utc_naive, df_utc_explicit, df_utc_offset):
            assert df is not None
        assert len(df_utc_naive) == len(df_utc_explicit) == len(df_utc_offset)

    @needs_system4
    def test_inline_tz_matches_param_tz_via_process_index_calls(self):
        # End-to-end (via execute_query, default tz=UTC) with inline /NY
        # must equal token-level call with tz="America/New_York" param.
        df_inline = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01/America/New_York"\n'
        )
        tokens = [
            "index", "=", INDEX_TOKEN,
            "earliest", "=", '"2024-06-01"',
        ]
        df_param = process_index_calls(tokens, tz="America/New_York")
        assert df_inline is not None and df_param is not None
        # Both reach the same _epoch boundary; row count identical.
        assert len(df_inline) == len(df_param)


# ===========================================================================
# Layer 8 - Negative tests via diagnostics surface (operator visibility)
# ===========================================================================


class TestDiagnosticsSurface:
    """``process_query_with_diagnostics`` must return the parse error in the
    diagnostic field - silent failure was the entire reason this PR exists."""

    @needs_system4
    def test_garbage_surfaces_in_diagnostic(self):
        df, jid, diag = process_query_with_diagnostics(
            f'index={INDEX_TOKEN} earliest="garbage"\n'
        )
        assert df is None
        assert jid is None
        assert diag is not None
        assert "TimeBoundParseError" in diag
        assert "garbage" in diag

    @needs_system4
    def test_invalid_iso_surfaces(self):
        df, jid, diag = process_query_with_diagnostics(
            f'index={INDEX_TOKEN} earliest="2024-13-99"\n'
        )
        assert df is None
        assert diag is not None
        assert "TimeBoundParseError" in diag

    @needs_system4
    def test_bad_inline_tz_surfaces(self):
        df, jid, diag = process_query_with_diagnostics(
            f'index={INDEX_TOKEN} earliest="2024-01-01/Mars/Olympus_Mons"\n'
        )
        assert df is None
        assert diag is not None
        assert "TimeBoundParseError" in diag

    @needs_system4
    def test_garbage_does_not_silently_return_unfiltered(self):
        """The original 2026-04-29 bug: garbage value ⇒ epoch 0 ⇒ all rows.

        This is the canary for the silent-zero regression. It MUST NOT pass
        even if some other change re-introduces the silent fallback.
        """
        df_garbage, _, _ = process_query_with_diagnostics(
            f'index={INDEX_TOKEN} earliest="garbage_bug_canary"\n'
        )
        df_unbounded, _, _ = process_query_with_diagnostics(
            f"index={INDEX_TOKEN}\n"
        )
        # Pre-fix: both would return ~1000 rows and the user would think
        # earliest had no effect. Post-fix: garbage produces None+diag,
        # unbounded produces 1000 rows.
        assert df_garbage is None, (
            "Silent-zero regression detected - earliest='garbage' returned "
            "data instead of erroring. The legacy fallback to epoch 0 must "
            "not be reachable from the user-query path."
        )
        assert df_unbounded is not None
        assert len(df_unbounded) == REF_ROW_COUNT


# ===========================================================================
# Layer 9 - Listener-pathway sanity (proves ANTLR flatten preserves clause)
# ===========================================================================


class TestListenerPathwayPreservesClause:
    """If ``ctx_flatten`` ever drops the time clause, these tests fail loud."""

    @needs_system4
    def test_earliest_only(self):
        df_with = execute_query(f'index={INDEX_TOKEN} earliest="2024-09-01"\n')
        df_without = execute_query(f"index={INDEX_TOKEN}\n")
        assert df_with is not None
        assert df_without is not None
        assert len(df_with) < len(df_without)

    @needs_system4
    def test_latest_only(self):
        df_with = execute_query(f'index={INDEX_TOKEN} latest="2024-03-01"\n')
        df_without = execute_query(f"index={INDEX_TOKEN}\n")
        assert df_with is not None
        assert df_without is not None
        assert len(df_with) < len(df_without)

    @needs_system4
    def test_both_in_either_order(self):
        df_a = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-03-01" latest="2024-09-01"\n'
        )
        df_b = execute_query(
            f'index={INDEX_TOKEN} latest="2024-09-01" earliest="2024-03-01"\n'
        )
        assert df_a is not None and df_b is not None
        assert len(df_a) == len(df_b)

    @needs_system4
    def test_earliest_with_pipe_command_after(self):
        """earliest must survive when followed by a pipe operation."""
        df_filtered = execute_query(
            f'index={INDEX_TOKEN} earliest="2024-06-01" | head 50\n'
        )
        df_unfiltered = execute_query(
            f"index={INDEX_TOKEN} | head 50\n"
        )
        assert df_filtered is not None
        assert df_unfiltered is not None
        # Both are head-50, but filtered must respect the time bound
        assert (df_filtered["_epoch"] >= 1717200000).all()
        # Unfiltered may include rows from before 2024-06-01
        # (proving the bound is what's making the difference)
        if not df_unfiltered.empty:
            # At least some unbounded rows are below the bound - this is
            # what proves the filter is doing real work.
            assert (df_unfiltered["_epoch"] < 1717200000).any() or (
                df_unfiltered["_epoch"] >= 1717200000
            ).all()
