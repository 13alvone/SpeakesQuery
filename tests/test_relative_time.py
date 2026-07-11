#!/usr/bin/env python3
"""
Tests for Splunk-style relative time parsing in duckdb_index_call.

Covers:
  - Relative modifiers (-30m, -1h, +2d, etc.)
  - Snap-to modifiers (-1h@h, -1d@d, -7d@w, etc.)
  - Edge cases (now, bare integers, invalid strings)
"""

import os
import sys
import time
import math
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.chdir(PROJECT_ROOT)

from functionality.duckdb_index_call import (
    _parse_date_to_epoch,
    _parse_relative_time,
    _snap_to_unit,
)


# ---------------------------------------------------------------------------
# Tolerance: relative times are computed against now(), so we allow ±5 sec
# ---------------------------------------------------------------------------

TOLERANCE = 5


def _approx(expected):
    return pytest.approx(expected, abs=TOLERANCE)


# ---------------------------------------------------------------------------
# _parse_relative_time - core relative modifier tests
# ---------------------------------------------------------------------------


class TestParseRelativeTime:
    """Tests for the _parse_relative_time helper."""

    def test_now(self):
        result = _parse_relative_time("now")
        assert result == _approx(int(time.time()))

    def test_minus_30m(self):
        result = _parse_relative_time("-30m")
        expected = int(time.time()) - 30 * 60
        assert result == _approx(expected)

    def test_minus_1h(self):
        result = _parse_relative_time("-1h")
        expected = int(time.time()) - 3600
        assert result == _approx(expected)

    def test_minus_1d(self):
        result = _parse_relative_time("-1d")
        expected = int(time.time()) - 86400
        assert result == _approx(expected)

    def test_minus_7d(self):
        result = _parse_relative_time("-7d")
        expected = int(time.time()) - 7 * 86400
        assert result == _approx(expected)

    def test_minus_1w(self):
        result = _parse_relative_time("-1w")
        expected = int(time.time()) - 604800
        assert result == _approx(expected)

    def test_minus_30s(self):
        result = _parse_relative_time("-30s")
        expected = int(time.time()) - 30
        assert result == _approx(expected)

    def test_minus_1M(self):
        result = _parse_relative_time("-1M")
        expected = int(time.time()) - 2592000
        assert result == _approx(expected)

    def test_minus_1y(self):
        result = _parse_relative_time("-1y")
        expected = int(time.time()) - 31536000
        assert result == _approx(expected)

    def test_plus_1h(self):
        result = _parse_relative_time("+1h")
        expected = int(time.time()) + 3600
        assert result == _approx(expected)

    def test_plus_2d(self):
        result = _parse_relative_time("+2d")
        expected = int(time.time()) + 2 * 86400
        assert result == _approx(expected)

    def test_implicit_minus_sign(self):
        """No sign prefix defaults to minus (going back in time)."""
        result = _parse_relative_time("15m")
        expected = int(time.time()) - 15 * 60
        assert result == _approx(expected)


# ---------------------------------------------------------------------------
# Snap-to (@) modifier tests
# ---------------------------------------------------------------------------


class TestSnapTo:
    """Tests for the @-snap modifier in relative time expressions."""

    def test_minus_1h_snap_h(self):
        """``-1h@h`` should snap to the start of the hour."""
        result = _parse_relative_time("-1h@h")
        assert result is not None
        # Result should have :00:00 at the minute/second level
        assert result % 3600 == 0

    def test_minus_1d_snap_d(self):
        """``-1d@d`` should snap to midnight."""
        result = _parse_relative_time("-1d@d")
        assert result is not None
        assert result % 86400 == 0

    def test_minus_1m_snap_m(self):
        """``-1m@m`` should snap to the start of the minute."""
        result = _parse_relative_time("-1m@m")
        assert result is not None
        assert result % 60 == 0

    def test_minus_7d_snap_w(self):
        """``-7d@w`` should snap to the start of the week (Monday 00:00)."""
        import datetime
        result = _parse_relative_time("-7d@w")
        assert result is not None
        dt = datetime.datetime.fromtimestamp(result, datetime.timezone.utc)
        assert dt.weekday() == 0  # Monday
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0


# ---------------------------------------------------------------------------
# Integration with _parse_date_to_epoch
# ---------------------------------------------------------------------------


class TestParseDateToEpochRelative:
    """Ensure _parse_date_to_epoch correctly dispatches to relative parsing."""

    def test_relative_minus_30m(self):
        result = _parse_date_to_epoch("-30m")
        expected = int(time.time()) - 30 * 60
        assert result == _approx(expected)

    def test_relative_now(self):
        result = _parse_date_to_epoch("now")
        assert result == _approx(int(time.time()))

    def test_relative_minus_1h_snap(self):
        result = _parse_date_to_epoch("-1h@h")
        assert result % 3600 == 0

    def test_absolute_epoch_still_works(self):
        """Plain numeric epoch strings must still parse correctly."""
        assert _parse_date_to_epoch("1709251200") == 1709251200

    def test_absolute_date_still_works(self):
        """Absolute date strings must still parse correctly."""
        result = _parse_date_to_epoch("2024-03-01 00:00:00")
        assert result == 1709251200

    def test_invalid_returns_zero(self):
        """Invalid strings should still return 0."""
        assert _parse_date_to_epoch("not_a_date") == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestRelativeTimeEdgeCases:
    """Edge cases for relative time parsing."""

    def test_none_for_non_relative(self):
        """Non-relative strings should return None from _parse_relative_time."""
        assert _parse_relative_time("2024-01-01") is None
        assert _parse_relative_time("hello") is None
        assert _parse_relative_time("1709251200") is None

    def test_large_offset(self):
        """Large offsets should work without overflow."""
        result = _parse_relative_time("-365d")
        expected = int(time.time()) - 365 * 86400
        assert result == _approx(expected)

    def test_whitespace_stripping(self):
        """Leading/trailing whitespace should be tolerated."""
        result = _parse_relative_time("  -30m  ")
        expected = int(time.time()) - 30 * 60
        assert result == _approx(expected)
