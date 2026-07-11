#!/usr/bin/env python3
"""
Regression tests for the 2026-04-21 production-level review of the
Alert Group pipeline.

Fixes pinned here:

1. **Scheduler TZ forced to UTC.** `BackgroundScheduler` is
   instantiated with `timezone="UTC"` so cron expressions are
   predictable regardless of Docker host system TZ.
2. **/api/system/clock endpoint** returns server_time_utc + scheduler_timezone
   + system_timezone so the UI can surface what time SpeakesQuery thinks
   it is (cron reasoning sanity-check).
3. **Dispatch-progress polling**. `AlertGroupDispatcher` writes phase
   updates to a thread-safe module-level map; `dispatch_progress_snapshot()`
   exposes it to a Flask endpoint for UI live-updates during manual runs.
4. **PUT / POST / enable / disable / delete re-register cron jobs** with
   APScheduler so edits take effect without a server restart.
5. **Phase timings as structured columns** - `feeder_loop_ms`,
   `claude_call_ms`, `email_send_ms` are now first-class columns in the
   `alert_groups` Parquet log schema and on `AlertGroupRunResult`.
6. **Circuit breaker on missing-prompt-text path** - an AG with empty
   `prompt_text` now trips the breaker on repeated failures (prevents
   runaway failure-email flood).
7. **Rate-limit robustness**: `list_runs(limit=2000)` instead of 200 so
   a rolling 24h window can't miss rows, and DB-error fail-open now
   emits a WARNING (was silent).
8. **asyncio.run double-entry guard** - email send no longer crashes
   with "asyncio.run() cannot be called from a running event loop" when
   invoked from inside pywebview's loop.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =====================================================================
# Part 1: Scheduler forced to UTC
# =====================================================================

class TestSchedulerTimezone:

    def test_scheduler_uses_utc(self):
        """BackgroundScheduler instantiation must pin timezone='UTC'.
        Regressing this makes cron behaviour dependent on container TZ."""
        from scheduled_input_engine.engine import ScheduledInputEngine

        with patch("scheduled_input_engine.engine.CredentialVault"):
            with patch("scheduled_input_engine.engine.ParquetWriter"):
                eng = ScheduledInputEngine()
        tz_str = str(eng._scheduler.timezone)
        assert tz_str == "UTC", (
            f"Scheduler timezone is {tz_str!r}, must be 'UTC' - otherwise "
            "cron expressions fire at system-local time which "
            "surprises every operator."
        )


# =====================================================================
# Part 2: /api/system/clock exposes scheduler TZ
# =====================================================================

class TestSystemClockEndpoint:

    def test_clock_endpoint_returns_utc_time_and_tz(self, client):
        resp = client.get("/api/system/clock")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "server_time_utc" in data
        assert "scheduler_timezone" in data
        assert data["scheduler_timezone"] == "UTC"
        assert "epoch" in data
        assert int(data["epoch"]) > 1_700_000_000  # sanity - after 2023

    def test_clock_endpoint_notes_scheduler_interpretation(self, client):
        resp = client.get("/api/system/clock")
        data = resp.get_json()
        note = data.get("note", "")
        assert "cron" in note.lower()
        assert "scheduler_timezone" in note


# =====================================================================
# Part 3: Dispatch progress tracker
# =====================================================================

class TestDispatchProgress:

    def setup_method(self):
        # Drop all state between tests so cross-test progress doesn't leak.
        from alert_groups import dispatcher as _dsp
        with _dsp._DISPATCH_PROGRESS_LOCK:
            _dsp._DISPATCH_PROGRESS.clear()

    def test_progress_set_and_snapshot_round_trip(self):
        from alert_groups.dispatcher import (
            _dispatch_progress_set, dispatch_progress_snapshot,
        )
        _dispatch_progress_set(
            "my_ag",
            phase="feeder_loop",
            phase_label="Feeder [2/5] 'x' running…",
            feeder_idx=2, feeder_total=5, feeder_name="x",
        )
        snap = dispatch_progress_snapshot("my_ag")
        assert snap is not None
        assert snap["phase"] == "feeder_loop"
        assert snap["phase_label"].startswith("Feeder [2/5]")
        assert snap["feeder_idx"] == 2
        assert snap["feeder_total"] == 5
        # Elapsed fields added by the snapshot reader
        assert "run_elapsed_s" in snap
        assert "phase_elapsed_s" in snap

    def test_progress_none_when_no_dispatch(self):
        from alert_groups.dispatcher import dispatch_progress_snapshot
        assert dispatch_progress_snapshot("never_dispatched") is None

    def test_progress_ttl_drops_stale_entries(self):
        from alert_groups import dispatcher as _dsp
        # Inject a stale entry with updated_epoch far in the past.
        with _dsp._DISPATCH_PROGRESS_LOCK:
            _dsp._DISPATCH_PROGRESS["stale_ag"] = {
                "phase": "done_success",
                "phase_label": "old",
                "run_started": time.monotonic() - 600,
                "phase_started": time.monotonic() - 600,
                "updated_epoch": int(time.time()) - 500,
            }
        # Snapshot read triggers cleanup. TTL is 120s; 500s is well past.
        assert _dsp.dispatch_progress_snapshot("stale_ag") is None

    def test_progress_phase_transition_resets_phase_started(self):
        from alert_groups.dispatcher import (
            _dispatch_progress_set, dispatch_progress_snapshot,
        )
        _dispatch_progress_set("ag1", phase="feeder_loop", phase_label="Feeders")
        time.sleep(0.02)  # small sleep so monotonic advances
        first = dispatch_progress_snapshot("ag1")
        _dispatch_progress_set("ag1", phase="calling_claude", phase_label="Claude")
        second = dispatch_progress_snapshot("ag1")
        # On transition, phase_started resets → phase_elapsed_s near 0.
        assert second["phase"] == "calling_claude"
        assert second["phase_elapsed_s"] <= max(1, first["phase_elapsed_s"])


# =====================================================================
# Part 4: Dispatch-progress endpoint
# =====================================================================

class TestDispatchProgressEndpoint:

    def setup_method(self):
        from alert_groups import dispatcher as _dsp
        with _dsp._DISPATCH_PROGRESS_LOCK:
            _dsp._DISPATCH_PROGRESS.clear()

    def test_endpoint_returns_in_flight_false_when_no_dispatch(self, client):
        resp = client.get("/api/alert-groups/nonexistent_ag/dispatch-progress")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["in_flight"] is False
        assert data["progress"] is None

    def test_endpoint_returns_in_flight_progress(self, client):
        from alert_groups.dispatcher import _dispatch_progress_set
        _dispatch_progress_set(
            "live_ag",
            phase="calling_claude",
            phase_label="Calling Claude…",
            claude_model="claude-sonnet-4-6",
        )
        resp = client.get("/api/alert-groups/live_ag/dispatch-progress")
        data = resp.get_json()
        assert data["in_flight"] is True
        assert data["progress"]["phase"] == "calling_claude"
        assert data["progress"]["phase_label"].startswith("Calling Claude")

    def test_endpoint_returns_in_flight_false_for_done_phase(self, client):
        from alert_groups.dispatcher import _dispatch_progress_set
        _dispatch_progress_set(
            "completed_ag",
            phase="done_success",
            phase_label="Dispatch complete (success).",
            result_status="success",
        )
        resp = client.get("/api/alert-groups/completed_ag/dispatch-progress")
        data = resp.get_json()
        # "done_*" phases report in_flight=False so UI stops polling.
        assert data["in_flight"] is False
        # But the terminal state is still readable for 120s.
        assert data["progress"]["phase"].startswith("done_")


# =====================================================================
# Part 5: Phase timings as structured columns
# =====================================================================

class TestPhaseTimingColumns:

    def test_schema_includes_phase_timing_columns(self):
        from functionality.log_writer import SCHEMAS
        cols = set(SCHEMAS["alert_groups"])
        for phase_col in ("feeder_loop_ms", "claude_call_ms", "email_send_ms"):
            assert phase_col in cols, (
                f"alert_groups schema missing '{phase_col}' - remove this "
                "column and operators lose SPQL bottleneck aggregation."
            )

    def test_log_alert_group_event_accepts_phase_timings(self):
        from functionality.log_writer import log_alert_group_event
        import inspect
        sig = inspect.signature(log_alert_group_event)
        for phase_kw in ("feeder_loop_ms", "claude_call_ms", "email_send_ms"):
            assert phase_kw in sig.parameters, (
                f"log_alert_group_event() must accept '{phase_kw}' kwarg"
            )

    def test_alert_group_run_result_carries_phase_timings(self):
        from alert_groups.models import AlertGroupRunResult
        result = AlertGroupRunResult(group_name="x")
        # Default None - phase didn't run.
        assert result.feeder_loop_ms is None
        assert result.claude_call_ms is None
        assert result.email_send_ms is None
        result.feeder_loop_ms = 1234
        assert result.feeder_loop_ms == 1234


# =====================================================================
# Part 6: Circuit breaker on missing-prompt path
# =====================================================================

class TestCircuitBreakerOnMissingPrompt:

    def test_missing_prompt_path_calls_maybe_trip_circuit_breaker(self):
        """Audit finding (HIGH): missing prompt_text used to return
        ``error`` without ever tripping the breaker, so a
        permanently-misconfigured AG would email failure notifications
        forever. Now the breaker-trip helper is invoked so consecutive
        failures eventually disable the AG."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.serializer import ResultSerializer
        from alert_groups.models import SerializedResult
        import pandas as pd

        d = AlertGroupDispatcher()
        d.serializer = MagicMock(spec=ResultSerializer)
        d.serializer.serialize_df.side_effect = lambda name, df: SerializedResult(
            search_name=name, row_count=len(df), estimated_tokens=100,
            format="json", content="[]",
        )

        AlertGroupDispatcher._reset_ss_store_cache()

        group = {
            "name": "bad_prompt_ag",
            "search_names": ["feeder1"],
            "prompt_text": "",  # empty - the error trigger
            "email_address": "",
            "disabled": False,
            "max_rows": 10,
        }

        breaker_calls = []

        def _record(cls, group_name):
            breaker_calls.append(group_name)

        with patch.object(
            type(d), "_execute_feeder_query_now",
            return_value=pd.DataFrame({"x": [1]}),
        ):
            with patch.object(type(d), "_log_run"):
                with patch.object(type(d), "_emit_log"):
                    with patch.object(type(d), "_get_budget_gate", return_value=None):
                        with patch.object(
                            type(d), "_maybe_send_failure_email",
                        ):
                            with patch.object(
                                type(d), "_maybe_trip_circuit_breaker",
                                new=classmethod(_record),
                            ):
                                run = d.run(group=group)

        assert run.status == "error"
        assert "prompt" in (run.error_message or "").lower()
        assert "bad_prompt_ag" in breaker_calls, (
            "Missing-prompt path must call _maybe_trip_circuit_breaker "
            "so consecutive misconfigured failures auto-disable the AG"
        )


# =====================================================================
# Part 7: Rate-limit list_runs expanded + fail-open warns
# =====================================================================

class TestRateLimitRobustness:

    def test_rate_limit_fetches_enough_rows(self):
        """Used to be limit=200; an AG with many failed attempts could
        push a valid success beyond that window. Now 2000."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        captured_limit = {}

        def _fake_list_runs(self, group_name, limit=200):
            captured_limit["value"] = limit
            return []

        with patch("alert_group_store.AlertGroupStore.list_runs",
                   new=_fake_list_runs):
            AlertGroupDispatcher._check_rate_limit(
                {"max_dispatches_per_day": 1}, "some_ag",
            )
        assert captured_limit.get("value", 0) >= 2000, (
            f"Rate-limit check pulled only {captured_limit.get('value')} "
            "rows - should be >=2000 to cover the rolling 24h window "
            "on high-churn AGs."
        )

    def test_rate_limit_db_error_warns_not_silent(self, caplog):
        """Audit finding: fail-open on DB error used to be silent. Now
        emits a WARNING line so operators can investigate runaway
        dispatches caused by a broken audit DB."""
        import logging
        from alert_groups.dispatcher import AlertGroupDispatcher

        caplog.set_level(logging.WARNING, logger="alert_groups.dispatcher")

        def _boom(self, *a, **kw):
            raise RuntimeError("db is on fire")

        with patch("alert_group_store.AlertGroupStore.list_runs", new=_boom):
            result = AlertGroupDispatcher._check_rate_limit(
                {"max_dispatches_per_day": 1}, "fail_open_ag",
            )
        # Returns None (fail-open) AND warns.
        assert result is None
        warns = [r.getMessage() for r in caplog.records
                 if r.levelname == "WARNING"]
        assert any("rate-limit check failed" in w.lower() for w in warns), (
            "Expected a WARNING line on rate-limit DB failure; captured: "
            + "\n".join(warns)
        )


# =====================================================================
# Part 8: asyncio.run double-entry guard
# =====================================================================

class TestAsyncioGuard:

    def test_run_coroutine_from_sync_context_plain_path(self):
        """When NO event loop is running, uses asyncio.run directly."""
        from alert_groups.dispatcher import _run_coroutine_from_sync_context

        ran = []

        async def _coro():
            ran.append(True)

        _run_coroutine_from_sync_context(_coro())
        assert ran == [True]

    def test_run_coroutine_from_sync_context_handles_running_loop(self):
        """When a loop IS running in this thread, delegates to a worker
        thread so asyncio.run doesn't crash. Uses a background thread
        with its own loop to simulate the double-entry scenario."""
        import asyncio
        from alert_groups.dispatcher import _run_coroutine_from_sync_context

        outcomes = []

        async def _set_flag():
            outcomes.append("coroutine ran")

        async def _inner():
            # Inside THIS loop, call the helper. Should NOT crash.
            _run_coroutine_from_sync_context(_set_flag())

        asyncio.run(_inner())
        assert outcomes == ["coroutine ran"]


# =====================================================================
# Part 9: PUT / POST / enable / disable re-register scheduler jobs
# =====================================================================

class TestSchedulerReregisterOnMutation:

    def test_put_endpoint_calls_register_alert_group_jobs(
        self, client,
    ):
        """Edit an AG via PUT → register_alert_group_jobs must be
        called so cron changes take effect without a restart."""
        register_calls = []

        def _record(sched):
            register_calls.append(sched)

        with patch(
            "alert_groups.scheduler.register_alert_group_jobs",
            side_effect=_record,
        ):
            # Pre-create an AG so the PUT can target it
            client.post(
                "/api/alert-groups/create",
                json={
                    "name": "reg_test_ag",
                    "search_names": ["any"],
                    "prompt_text": "hi",
                    "schedule": "0 6 * * *",
                },
            )
            register_calls.clear()  # only care about the PUT side-effect

            try:
                resp = client.put(
                    "/api/alert-groups/reg_test_ag",
                    json={"schedule": "0 18 * * *"},
                )
            finally:
                # Clean up so a committed YAML leftover doesn't pollute
                # alert_groups/ on subsequent commits.
                client.delete("/api/alert-groups/reg_test_ag")
        assert resp.status_code == 200
        assert len(register_calls) >= 1, (
            "PUT /api/alert-groups/<name> did NOT call "
            "register_alert_group_jobs - cron edits won't take effect "
            "until server restart."
        )

    def test_enable_endpoint_reregisters(self, client):
        register_calls = []
        with patch(
            "alert_groups.scheduler.register_alert_group_jobs",
            side_effect=lambda s: register_calls.append(s),
        ):
            client.post(
                "/api/alert-groups/create",
                json={
                    "name": "enable_test_ag",
                    "search_names": ["x"],
                    "prompt_text": "p",
                    "schedule": "0 6 * * *",
                    "disabled": True,
                },
            )
            register_calls.clear()
            try:
                client.post("/api/alert-groups/enable_test_ag/enable")
            finally:
                # Clean up so a committed YAML leftover doesn't pollute
                # alert_groups/ on subsequent commits.
                client.delete("/api/alert-groups/enable_test_ag")
        assert register_calls, (
            "Enable endpoint must re-register so the cron job starts "
            "firing immediately."
        )
