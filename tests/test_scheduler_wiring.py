"""Wiring tests for the APScheduler instances SpeakesQuery uses.

Pins the 2026-04-21 H-CE-2 fix: the saved-search AsyncIOScheduler (in
query_engine/QueryEngine.py) must carry explicit ``job_defaults`` so misfires
do not silently drop. The ScheduledInputEngine's BackgroundScheduler already
carried the same defaults; these tests make the parity load-bearing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so ``query_engine`` and
# ``scheduled_input_engine`` import cleanly in tests.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ----------------------------------------------------------------------
# Saved-search AsyncIOScheduler (query_engine/QueryEngine.py)
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def saved_search_scheduler():
    # Importing the module constructs the module-level scheduler with the
    # job_defaults passed at init. The scheduler is not started here.
    from query_engine import QueryEngine
    return QueryEngine.scheduler


class TestSavedSearchSchedulerDefaults:

    def test_coalesce_true(self, saved_search_scheduler):
        assert saved_search_scheduler._job_defaults["coalesce"] is True, (
            "coalesce must be True: a saved search that misses multiple cron "
            "fires during a restart should fire ONCE on recovery, not N times."
        )

    def test_max_instances_one(self, saved_search_scheduler):
        assert saved_search_scheduler._job_defaults["max_instances"] == 1, (
            "max_instances must be 1: a long-running query cannot overlap "
            "itself at the next cron tick."
        )

    def test_misfire_grace_time_300(self, saved_search_scheduler):
        assert saved_search_scheduler._job_defaults["misfire_grace_time"] == 300, (
            "misfire_grace_time must be 300 (5 min): without it APScheduler "
            "drops missed fires silently."
        )


# ----------------------------------------------------------------------
# ScheduledInputEngine BackgroundScheduler - parity check
# ----------------------------------------------------------------------
# The ingestion scheduler has carried these defaults since 2026-04-21. We
# keep a parity test so any future refactor that drops them trips both.

class TestIngestionSchedulerDefaults:

    def test_ingestion_scheduler_has_matching_defaults(self):
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()
        try:
            sched = engine._scheduler
            defaults = sched._job_defaults
            assert defaults["coalesce"] is True
            assert defaults["max_instances"] == 1
            assert defaults["misfire_grace_time"] == 300
            # Also UTC per reference_ag_scheduler_utc_and_reregister.md.
            assert str(sched.timezone) == "UTC"
        finally:
            # Avoid leaking scheduler threads across tests.
            if getattr(engine, "_scheduler", None) is not None:
                try:
                    engine._scheduler.shutdown(wait=False)
                except Exception:
                    pass
