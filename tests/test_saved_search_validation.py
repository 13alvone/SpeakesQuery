"""
Regression tests for ``validation.SavedSearchValidation``.

Specifically pins the 2026-05-05 fix that allowed empty / None
``cron_schedule`` values to pass validation. ``alert_group_feeder``-purpose
SSes (like the ``*_reserved_picks`` set) are NEVER scheduled - they're
invoked on demand by the AG dispatcher. Their YAMLs have always been
seeded with an empty cron field, but the PUT path's validator was
rejecting empty strings as invalid syntax. Caught when re-PUTting 4
reserved_picks SSes to fix a live drift on the index path; all 4 PUTs
failed with ``Invalid cron schedule format: ''``.

The fix also brings ``SavedSearchValidation`` in line with
``AlertGroupValidation.validate_cron_schedule``, which already accepted
empty crons.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from validation.SavedSearchValidation import SavedSearchValidation  # noqa: E402


class TestValidateCronSchedule:
    def test_empty_string_accepted(self):
        """Empty-string cron is valid (= no schedule). Reserved-picks
        feeders are the canonical use case."""
        assert SavedSearchValidation.validate_cron_schedule("") == ""

    def test_whitespace_string_accepted(self):
        """Whitespace-only cron also normalizes to no-schedule."""
        assert SavedSearchValidation.validate_cron_schedule("   ") == ""

    def test_none_accepted(self):
        """None is treated as no-schedule, just like empty string."""
        assert SavedSearchValidation.validate_cron_schedule(None) == ""

    def test_valid_cron_unchanged(self):
        """Real cron strings pass through untouched."""
        assert (
            SavedSearchValidation.validate_cron_schedule("0 14,19 * * mon-fri")
            == "0 14,19 * * mon-fri"
        )

    def test_invalid_cron_rejected(self):
        """Malformed cron strings still raise."""
        with pytest.raises(ValueError, match="Invalid cron schedule"):
            SavedSearchValidation.validate_cron_schedule("not a cron")

    def test_too_few_fields_rejected(self):
        with pytest.raises(ValueError, match="Invalid cron schedule"):
            SavedSearchValidation.validate_cron_schedule("0 14")

    def test_consistent_with_alert_group_validator(self):
        """Both validators must accept empty crons identically - the
        2026-05-05 mismatch was the source of the PUT-time bug."""
        from validation.AlertGroupValidation import AlertGroupValidation

        for empty_form in ["", "   ", None]:
            ss_result = SavedSearchValidation.validate_cron_schedule(empty_form)
            ag_result = AlertGroupValidation.validate_schedule(empty_form)
            assert ss_result == ag_result == "", (
                f"Empty-cron mismatch on {empty_form!r}: "
                f"SS={ss_result!r}, AG={ag_result!r}"
            )
