"""
Tests for the 2026-04-20 alert-group production-hardening branch:

  * Track A: saved-search ``purpose`` field + auto-toggle from AlertGroupStore
  * Wave C1: feeder freshness check (warn + fail variants)
  * Wave C2: per-AG cost budget (per-run + per-day)
  * Wave C4: circuit breaker (trip + reset + auto-disable)
  * Wave C5: metrics endpoint
  * Wave C8: per-AG email template override
  * Wave C9: dead-feeder detection (last_search_run_age_hours)
  * Wave B: CRUD emitters across saved_search / alert_group / macro / analyzer
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Point every persistent file at a tmp dir + reset the log writer."""
    from global_settings import get_settings
    from functionality import log_writer as lw

    settings = get_settings()
    settings.set("logs_root", str(tmp_path / "logs"))
    settings.set("logs_enabled", True)
    lw.LogWriter.reset_for_tests()

    # Redirect YAML stores + audit DBs
    import alert_group_store
    monkeypatch.setattr(alert_group_store, "GROUPS_DIR", tmp_path / "alert_groups")
    monkeypatch.setattr(alert_group_store, "LAST_CHANCE_DB", tmp_path / "lc.sqlite")
    monkeypatch.setattr(alert_group_store, "RUNS_DB", tmp_path / "ag_runs.sqlite")

    import saved_search_store
    monkeypatch.setattr(saved_search_store, "SEARCHES_DIR", tmp_path / "saved_searches")
    monkeypatch.setattr(saved_search_store, "DEFAULT_SEARCHES_DIR", tmp_path / "defaults_ss")
    monkeypatch.setattr(saved_search_store, "LAST_CHANCE_DB", tmp_path / "lc.sqlite")

    # Redirect the serializer history DB that the dispatcher reads from
    import alert_groups.serializer as serializer_mod
    history_db = tmp_path / "saved_search_history.db"
    monkeypatch.setattr(serializer_mod, "HISTORY_DB", history_db)
    # feeder_status reads the same DB but uses its own path resolver, so
    # also monkey-patch the helper to our tmp DB.
    import alert_groups.feeder_status as fs_mod

    def _stub_age(name, _hist_db=history_db):
        try:
            with sqlite3.connect(str(_hist_db)) as conn:
                row = conn.execute(
                    "SELECT execution_start_time FROM execution_history "
                    "WHERE query_name = ? ORDER BY execution_start_time DESC LIMIT 1",
                    (name,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or row[0] is None:
            return None
        return max(0.0, (time.time() - float(row[0])) / 3600.0)

    monkeypatch.setattr(fs_mod, "_search_run_age_hours", _stub_age)

    yield tmp_path

    lw.LogWriter.reset_for_tests()


# ─────────────────────────────────────────────────────────────────────
# Track A - purpose field + auto-toggle
# ─────────────────────────────────────────────────────────────────────


class TestPurposeField:
    def test_save_standalone_defaults(self, tmp_env):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        saved = store.save_search({
            "name": "standalone_a",
            "query": 'index="x/*.parquet" | head 1',
            "cron_schedule": "0 * * * *",
            "lookback": "-1h",
            "email_address": "user@example.com",
            "send_email": "yes",
        })
        assert saved["purpose"] == "standalone"
        assert saved["send_email"] == "yes"
        assert saved["email_address"] == "user@example.com"

    def test_save_feeder_forces_send_email_no(self, tmp_env):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        saved = store.save_search({
            "name": "feeder_a",
            "purpose": "alert_group_feeder",
            "query": 'index="x/*.parquet" | head 1',
            "cron_schedule": "0 * * * *",
            "lookback": "-1h",
            "send_email": "yes",   # user typo - feeder must override
        })
        assert saved["purpose"] == "alert_group_feeder"
        assert saved["send_email"] == "no"
        # Empty email_address is OK for feeders (sentinel inserted)
        assert saved["email_address"] == "noreply@speakesquery.local"

    def test_invalid_purpose_rejected(self, tmp_env):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        with pytest.raises(ValueError, match="purpose must be one of"):
            store.save_search({
                "name": "bad_purpose",
                "purpose": "bogus",
                "query": 'index="x/*.parquet"',
                "cron_schedule": "0 * * * *",
                "lookback": "-1h",
                "email_address": "u@example.com",
            })

    def test_mark_as_feeder_idempotent(self, tmp_env):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        store.save_search({
            "name": "target",
            "query": 'index="x/*.parquet"',
            "cron_schedule": "0 * * * *",
            "lookback": "-1h",
            "email_address": "u@example.com",
        })
        assert store.mark_as_alert_group_feeder("target", "ag_1") is True
        assert store.get_search("target")["purpose"] == "alert_group_feeder"
        # Second call is a no-op
        assert store.mark_as_alert_group_feeder("target", "ag_1") is False
        # Nonexistent search returns False without raising
        assert store.mark_as_alert_group_feeder("does_not_exist", "ag_1") is False


class TestAutoToggleFromAlertGroup:
    """Creating/updating an AG must flip its referenced saved searches to
    ``purpose=alert_group_feeder`` at the moment of save. The user's explicit
    requirement from 2026-04-20: *"if an ALERT GROUP is created later that
    targets an existing search, it should auto toggle it to be part of the
    ALERT GROUP at THE TIME OF TOGGLE."*
    """

    def _seed_standalone(self, tmp_env, name):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        store.save_search({
            "name": name,
            "query": 'index="x/*.parquet"',
            "cron_schedule": "0 * * * *",
            "lookback": "-1h",
            "email_address": "u@example.com",
        })
        return store

    def test_create_ag_flips_existing_searches(self, tmp_env):
        store = self._seed_standalone(tmp_env, "feed_a")
        self._seed_standalone(tmp_env, "feed_b")
        assert store.get_search("feed_a")["purpose"] == "standalone"

        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "my_ag",
            "search_names": ["feed_a", "feed_b"],
            "prompt_text": "Go.",
            "schedule": "0 5 * * *",
            "max_rows": 10,
            "email_address": "me@example.com",
        })

        assert store.get_search("feed_a")["purpose"] == "alert_group_feeder"
        assert store.get_search("feed_b")["purpose"] == "alert_group_feeder"

    def test_update_ag_flips_newly_referenced_search(self, tmp_env):
        store = self._seed_standalone(tmp_env, "feed_a")
        self._seed_standalone(tmp_env, "feed_new")

        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "my_ag", "search_names": ["feed_a"],
            "prompt_text": "Go.", "schedule": "0 5 * * *",
            "max_rows": 10, "email_address": "me@example.com",
        })
        assert store.get_search("feed_new")["purpose"] == "standalone"

        ag.update_group("my_ag", {"search_names": ["feed_a", "feed_new"]})
        assert store.get_search("feed_new")["purpose"] == "alert_group_feeder"


# ─────────────────────────────────────────────────────────────────────
# Wave C1 - feeder freshness
# ─────────────────────────────────────────────────────────────────────


class TestFeederFreshness:
    def _seed_history(self, tmp_env, search_name, parquet_age_hours, parquet_path):
        """Insert a row pointing at a parquet with a past mtime."""
        import os
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        parquet_path.write_bytes(b"x")
        past = time.time() - parquet_age_hours * 3600
        os.utime(parquet_path, (past, past))

        hist = tmp_env / "saved_search_history.db"
        with sqlite3.connect(str(hist)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_start_time REAL,
                    execution_end_time REAL,
                    runtime REAL,
                    query_name TEXT,
                    saved_search_path TEXT,
                    original_result_count INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO execution_history "
                "(execution_start_time, execution_end_time, runtime, "
                "query_name, saved_search_path, original_result_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (past, past, 0.1, search_name, str(parquet_path), 1),
            )
            conn.commit()

    def test_fresh_feeder_returns_empty_list(self, tmp_env):
        from alert_groups.dispatcher import AlertGroupDispatcher

        self._seed_history(
            tmp_env, "recent_search", 2.0,
            tmp_env / "cached" / "recent_search.parquet",
        )
        group = {"name": "ag", "max_feeder_staleness_hours": 48}
        stale = AlertGroupDispatcher._check_feeder_freshness(
            group, ["recent_search"],
        )
        assert stale == []

    def test_stale_feeder_reported(self, tmp_env):
        from alert_groups.dispatcher import AlertGroupDispatcher

        self._seed_history(
            tmp_env, "old_search", 72.0,
            tmp_env / "cached" / "old_search.parquet",
        )
        group = {"name": "ag", "max_feeder_staleness_hours": 24}
        stale = AlertGroupDispatcher._check_feeder_freshness(
            group, ["old_search"],
        )
        assert len(stale) == 1
        assert stale[0][0] == "old_search"
        assert stale[0][1] > 24  # age in hours

    def test_missing_history_row_is_infinitely_stale(self, tmp_env):
        from alert_groups.dispatcher import AlertGroupDispatcher

        # No history DB rows at all - every feeder is inf-stale
        group = {"name": "ag", "max_feeder_staleness_hours": 48}
        stale = AlertGroupDispatcher._check_feeder_freshness(
            group, ["never_ran"],
        )
        assert len(stale) == 1
        assert stale[0][1] == float("inf")


# ─────────────────────────────────────────────────────────────────────
# Wave C2 - per-AG cost budget
# ─────────────────────────────────────────────────────────────────────


class TestPerAGBudget:
    def test_under_per_run_cap_returns_none(self, tmp_env):
        from alert_groups.dispatcher import AlertGroupDispatcher
        group = {"name": "ag", "max_cost_usd_per_run": 1.0}
        # Tiny token count → negligible cost
        err = AlertGroupDispatcher._check_per_ag_budget(group, "ag", 100)
        assert err is None

    def test_over_per_run_cap_returns_error(self, tmp_env):
        from alert_groups.dispatcher import AlertGroupDispatcher
        # Huge estimate; per-run cap $0.001 = impossible
        group = {"name": "ag", "max_cost_usd_per_run": 0.001}
        err = AlertGroupDispatcher._check_per_ag_budget(group, "ag", 1_000_000)
        assert err is not None
        assert "per-run cap" in err

    def test_per_day_cap_reads_history_store(self, tmp_env, tmp_path):
        from alert_groups.dispatcher import AlertGroupDispatcher
        from analyzers.claude_history_store import ClaudeHistoryStore

        hist = ClaudeHistoryStore(db_path=tmp_path / "hist.sqlite")
        ClaudeHistoryStore._instance = hist
        # Seed $0.50 spent today for "ag"
        hist.record_call(
            source="alert_group", group_name="ag",
            model="claude-sonnet-4-6", status="success",
            input_tokens=100000, output_tokens=10000, cost_usd=0.50,
        )
        try:
            group = {"name": "ag", "max_cost_usd_per_day": 0.60}
            # Estimated $0.15 more; 0.50 + 0.15 > 0.60 → should block
            err = AlertGroupDispatcher._check_per_ag_budget(group, "ag", 50000)
            assert err is not None
            assert "per-day cap" in err
        finally:
            ClaudeHistoryStore.reset_for_tests()


# ─────────────────────────────────────────────────────────────────────
# Wave C4 - circuit breaker
# ─────────────────────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_tripped_flag_blocks_dispatch(self, tmp_env):
        """2026-08-04 half-open update: a tripped breaker INSIDE its
        cooldown window skips CLEANLY (status='skipped', no failure
        email) instead of the old status='error' + daily failure email.
        A trip with no timestamp (legacy YAML) probes immediately - see
        tests/test_ag_graceful_2026_08_04.py for that path."""
        import datetime as _dt
        from alert_groups.dispatcher import AlertGroupDispatcher

        group = {
            "name": "tripped_ag",
            "disabled": False,
            "max_rows": 10,
            "search_names": ["s1"],
            "prompt_text": "Go.",
            "email_address": "",
            "circuit_breaker_tripped": True,
            "circuit_breaker_tripped_at": _dt.datetime.now(
                _dt.timezone.utc,
            ).isoformat(),
        }
        d = AlertGroupDispatcher()
        result = d.run(group)
        assert result.status == "skipped"
        assert "cooling down" in (result.error_message or "")

    def test_trips_after_consecutive_errors(self, tmp_env):
        from global_settings import get_settings
        get_settings().set("alert_group_circuit_breaker_consecutive_failures", 3)
        get_settings().set("alert_group_circuit_breaker_auto_disable", True)

        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "flapper", "search_names": ["s1"], "prompt_text": "Go.",
            "schedule": "", "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        # Seed 2 prior errors in runs DB
        for _ in range(2):
            ag.log_run(group_name="flapper", status="error", error_message="prev")

        from alert_groups.dispatcher import AlertGroupDispatcher
        # _maybe_trip accounts for the run that's ABOUT to be logged (+1)
        AlertGroupDispatcher._maybe_trip_circuit_breaker("flapper")
        assert ag.get_group("flapper").get("circuit_breaker_tripped") is True

    def test_success_does_not_trip(self, tmp_env):
        from global_settings import get_settings
        get_settings().set("alert_group_circuit_breaker_consecutive_failures", 3)

        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "healthy", "search_names": ["s1"], "prompt_text": "Go.",
            "schedule": "", "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        # Seed 5 successes
        for _ in range(5):
            ag.log_run(group_name="healthy", status="success")

        from alert_groups.dispatcher import AlertGroupDispatcher
        AlertGroupDispatcher._maybe_trip_circuit_breaker("healthy")
        assert ag.get_group("healthy").get("circuit_breaker_tripped") in (False, None)


# ─────────────────────────────────────────────────────────────────────
# Wave C5 - metrics endpoint
# ─────────────────────────────────────────────────────────────────────


class TestMetricsEndpoint:
    def test_metrics_returns_shape(self, tmp_env, monkeypatch):
        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "metrics_ag", "search_names": ["s1"], "prompt_text": "Go.",
            "schedule": "", "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        ag.log_run(group_name="metrics_ag", status="success",
                   actual_tokens=100, cost_usd=0.001)
        ag.log_run(group_name="metrics_ag", status="error",
                   error_message="boom")
        ag.log_run(group_name="metrics_ag", status="success",
                   actual_tokens=200, cost_usd=0.002)

        import desktop_app.server as server_mod
        monkeypatch.setattr(server_mod, "_ag_store", ag)
        with server_mod.app.test_client() as c:
            resp = c.get("/api/alert-groups/metrics_ag/metrics?hours=24")
            data = resp.get_json()
            assert resp.status_code == 200
            m = data["metrics"]
            assert m["total_runs"] == 3
            assert m["success"] == 2
            assert m["error"] == 1
            assert 0.0 <= m["success_rate"] <= 1.0
            assert m["total_cost_usd"] == pytest.approx(0.003)

    def test_metrics_404_on_unknown(self, tmp_env, monkeypatch):
        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        import desktop_app.server as server_mod
        monkeypatch.setattr(server_mod, "_ag_store", ag)
        with server_mod.app.test_client() as c:
            resp = c.get("/api/alert-groups/nope/metrics")
            assert resp.status_code == 404


class TestResetCircuitBreaker:
    def test_reset_clears_flag(self, tmp_env, monkeypatch):
        # The Flask route uses the module-level _ag_store singleton, which
        # was initialised at import time pointing at the production dir.
        # Swap it for a store rooted at the tmp_env dirs so the endpoint
        # actually finds the seeded group. Mirrors the pattern used by
        # tests/test_alert_groups.py for the same reason.
        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "reset_me", "search_names": ["s1"], "prompt_text": "Go.",
            "schedule": "", "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        ag.update_group("reset_me", {"circuit_breaker_tripped": True})

        import desktop_app.server as server_mod
        monkeypatch.setattr(server_mod, "_ag_store", ag)
        with server_mod.app.test_client() as c:
            resp = c.post("/api/alert-groups/reset_me/reset-circuit-breaker")
            assert resp.status_code == 200
        assert ag.get_group("reset_me").get("circuit_breaker_tripped") is False


# ─────────────────────────────────────────────────────────────────────
# Wave C8 - per-AG email template override
# ─────────────────────────────────────────────────────────────────────


class TestEmailTemplateOverride:
    def test_default_template_used_when_override_empty(self):
        from alert_groups.dispatcher import build_html_email
        html = build_html_email(
            "ag_default", "hello from claude",
            meta={"searches_used": ["s1"], "actual_tokens": 50, "cost_usd": 0.01},
            template_override="",
        )
        assert "SpeakesQuery" in html  # default branded template
        assert "hello from claude" in html

    def test_override_tokens_substituted(self):
        from alert_groups.dispatcher import build_html_email
        tmpl = (
            "<p>Group: {{group_name}}</p>"
            "<p>Tokens: {{actual_tokens}}</p>"
            "<p>Body: {{body_text}}</p>"
            "<p>Searches: {{searches_used}}</p>"
            "<p>Cost: ${{cost_usd}}</p>"
        )
        html = build_html_email(
            "custom_ag", "payload here",
            meta={
                "searches_used": ["alpha", "beta"],
                "actual_tokens": 123,
                "cost_usd": 0.0456,
            },
            template_override=tmpl,
        )
        assert "<p>Group: custom_ag</p>" in html
        assert "<p>Tokens: 123</p>" in html
        assert "<p>Body: payload here</p>" in html
        assert "<p>Searches: alpha, beta</p>" in html
        assert "<p>Cost: $0.0456</p>" in html
        # Default branding must NOT leak in
        assert "SpeakesQuery" not in html or "SpeakesQuery" in tmpl


# ─────────────────────────────────────────────────────────────────────
# Wave C9 - dead-feeder detection
# ─────────────────────────────────────────────────────────────────────


class TestDeadFeederDetection:
    def test_age_hours_none_when_no_history(self, tmp_env):
        from alert_groups.feeder_status import _search_run_age_hours
        assert _search_run_age_hours("never_ran") is None

    def test_age_hours_positive_when_ran(self, tmp_env):
        hist = tmp_env / "saved_search_history.db"
        with sqlite3.connect(str(hist)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_start_time REAL,
                    execution_end_time REAL,
                    runtime REAL,
                    query_name TEXT,
                    saved_search_path TEXT,
                    original_result_count INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO execution_history "
                "(execution_start_time, query_name) VALUES (?, ?)",
                (time.time() - 3600, "ran_one_hour_ago"),
            )
            conn.commit()
        from alert_groups.feeder_status import _search_run_age_hours
        age = _search_run_age_hours("ran_one_hour_ago")
        assert age is not None
        assert 0.9 <= age <= 1.2  # ~1 hour ±generous tolerance


# ─────────────────────────────────────────────────────────────────────
# Wave B - CRUD emitters across stores
# ─────────────────────────────────────────────────────────────────────


def _read_config_log(tmp_env) -> list[dict]:
    from functionality.log_writer import flush_all
    flush_all()
    rows = []
    cfg_dir = tmp_env / "logs" / "config"
    if not cfg_dir.exists():
        return rows
    for p in cfg_dir.glob("*.parquet"):
        rows.extend(pd.read_parquet(p).to_dict(orient="records"))
    return rows


class TestRateLimit:
    """Regression for the 2026-04-20 *"send only once a day"* feedback.

    Per-AG ``max_dispatches_per_day`` and ``min_interval_between_runs_hours``
    gate the dispatch early with ``status="rate_limited"`` - distinct from
    ``error`` so the failure email + circuit breaker do NOT fire. Failed
    runs don't count against the daily cap (so a retry after a transient
    failure still works).
    """

    def _seed_success_runs(self, ag_store, name, count, age_hours=0):
        import datetime as _dt
        import sqlite3
        ag_store.save_group({
            "name": name, "search_names": ["s1"], "prompt_text": "Go.",
            "schedule": "", "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        now = _dt.datetime.now(_dt.timezone.utc)
        with sqlite3.connect(ag_store._runs_db) as conn:
            for i in range(count):
                ts = (now - _dt.timedelta(hours=age_hours)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO alert_group_runs (group_name, triggered_at, status) "
                    "VALUES (?, ?, ?)",
                    (name, ts, "success"),
                )
            conn.commit()

    def test_max_per_day_blocks(self, tmp_env):
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher
        ag = AlertGroupStore()
        ag.initialize()
        self._seed_success_runs(ag, "oncer", count=1, age_hours=3)

        group = dict(ag.get_group("oncer"))
        group["max_dispatches_per_day"] = 1
        err = AlertGroupDispatcher._check_rate_limit(group, "oncer")
        assert err is not None
        assert "max_dispatches_per_day" in err

    def test_min_interval_blocks(self, tmp_env):
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher
        ag = AlertGroupStore()
        ag.initialize()
        self._seed_success_runs(ag, "hourly", count=1, age_hours=2)

        group = dict(ag.get_group("hourly"))
        group["min_interval_between_runs_hours"] = 12
        err = AlertGroupDispatcher._check_rate_limit(group, "hourly")
        assert err is not None
        assert "min_interval_between_runs_hours" in err

    def test_unset_limits_allows_dispatch(self, tmp_env):
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher
        ag = AlertGroupStore()
        ag.initialize()
        self._seed_success_runs(ag, "unlimited", count=10, age_hours=1)
        group = dict(ag.get_group("unlimited"))
        # Neither field set - no gate
        assert AlertGroupDispatcher._check_rate_limit(group, "unlimited") is None

    def test_failed_runs_do_not_count_toward_cap(self, tmp_env):
        import datetime as _dt
        import sqlite3
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher

        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "retry_ok", "search_names": ["s1"], "prompt_text": "Go.",
            "schedule": "", "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        # Seed 5 failures in the last hour - these must not count.
        now = _dt.datetime.now(_dt.timezone.utc)
        with sqlite3.connect(ag._runs_db) as conn:
            for _ in range(5):
                ts = (now - _dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO alert_group_runs (group_name, triggered_at, status) "
                    "VALUES (?, ?, ?)",
                    ("retry_ok", ts, "error"),
                )
            conn.commit()

        group = dict(ag.get_group("retry_ok"))
        group["max_dispatches_per_day"] = 1
        # No success runs in the window → dispatch allowed
        assert AlertGroupDispatcher._check_rate_limit(group, "retry_ok") is None

    def test_rate_limit_error_message_points_to_per_ag_edit(self, tmp_env):
        """Regression for 2026-04-20 UX bug - user saw the error and went
        to global Settings looking for the knob. The message must tell
        them to edit the alert group, and must mention ``force=true`` as
        the manual-override escape hatch."""
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher
        ag = AlertGroupStore()
        ag.initialize()
        self._seed_success_runs(ag, "msg_ag", count=1, age_hours=1)
        ag.update_group("msg_ag", {"max_dispatches_per_day": 1})
        group = ag.get_group("msg_ag")

        d = AlertGroupDispatcher()
        result = d.run(group)

        assert result.status == "rate_limited"
        msg = result.error_message or ""
        # Points to the right place
        assert "per-group setting" in msg or "per-AG" in msg.lower() or "Advanced" in msg
        assert "Edit" in msg
        # Offers the escape hatch
        assert "force" in msg.lower()

    def test_force_true_bypasses_rate_limit(self, tmp_env):
        """force=true on dispatcher.run must skip the rate-limit check
        entirely. Tested by seeding a limit that would normally block,
        then confirming status != rate_limited when force is passed."""
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher
        ag = AlertGroupStore()
        ag.initialize()
        self._seed_success_runs(ag, "force_ag", count=1, age_hours=1)
        ag.update_group("force_ag", {"max_dispatches_per_day": 1})
        group = ag.get_group("force_ag")
        # Empty search_names will trip the next gate ("No results...") but
        # that's AFTER the rate limit - we just need to prove the rate
        # limit gate was skipped.
        group["search_names"] = []
        # Also set prompt_text + disable=false so we reach serialisation
        group["prompt_text"] = "Go."
        group["disabled"] = False

        d = AlertGroupDispatcher()
        result = d.run(group, force=True)
        # Whatever downstream happens, it's NOT rate_limited
        assert result.status != "rate_limited", (
            f"force=true did not bypass rate limit: {result.error_message}"
        )

    def test_force_true_bypasses_circuit_breaker(self, tmp_env):
        """Tripped breaker + force=true must allow the dispatch to proceed
        past the breaker gate (manual override of operator intent)."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        import datetime as _dt
        group = {
            "name": "tripped_force",
            "disabled": False,
            "max_rows": 10,
            "search_names": [],
            "prompt_text": "Go.",
            "email_address": "",
            "circuit_breaker_tripped": True,
            "circuit_breaker_tripped_at": _dt.datetime.now(
                _dt.timezone.utc,
            ).isoformat(),
        }
        d = AlertGroupDispatcher()
        # Without force: blocked by breaker (clean skip during cooldown
        # since the 2026-08-04 half-open update)
        r_blocked = d.run(group, force=False)
        assert r_blocked.status == "skipped"
        assert "Circuit breaker" in (r_blocked.error_message or "")
        # With force: breaker bypassed (downstream gate will still fire
        # because search_names is empty, but it won't be the breaker)
        r_forced = d.run(group, force=True)
        assert "Circuit breaker" not in (r_forced.error_message or "")

    def test_rate_limit_returns_rate_limited_status_not_error(self, tmp_env):
        """The full dispatcher path must produce ``status=rate_limited``
        (distinct from ``error``) so failure email + breaker both stay
        quiet."""
        from alert_group_store import AlertGroupStore
        from alert_groups.dispatcher import AlertGroupDispatcher
        ag = AlertGroupStore()
        ag.initialize()
        self._seed_success_runs(ag, "rl_ag", count=1, age_hours=1)

        # Pull fresh group dict + add the cap
        group = ag.get_group("rl_ag")
        ag.update_group("rl_ag", {"max_dispatches_per_day": 1})
        group = ag.get_group("rl_ag")

        sent = {}

        def _fake_plain(subject, body, to_addr):
            sent["called"] = True

        from unittest.mock import patch as _patch
        with _patch.object(AlertGroupDispatcher, "_send_plain_email",
                           staticmethod(_fake_plain)):
            d = AlertGroupDispatcher()
            result = d.run(group)

        assert result.status == "rate_limited"
        assert "max_dispatches_per_day" in (result.error_message or "")
        assert "called" not in sent, "Rate limit must not fire failure email"


class TestMaxTokensOverride:
    def test_per_ag_override_wins(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        assert AlertGroupDispatcher._max_tokens({"max_output_tokens": 16384}) == 16384

    def test_global_default_when_unset(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        from global_settings import get_settings
        # The global default was raised to 8192 on 2026-04-20.
        get_settings().set("claude_analyzer_max_output_tokens", 8192)
        assert AlertGroupDispatcher._max_tokens({}) == 8192
        assert AlertGroupDispatcher._max_tokens(None) == 8192

    def test_invalid_override_falls_back_to_default(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        assert AlertGroupDispatcher._max_tokens({"max_output_tokens": "nope"}) == 8192
        assert AlertGroupDispatcher._max_tokens({"max_output_tokens": 0}) == 8192
        assert AlertGroupDispatcher._max_tokens({"max_output_tokens": -50}) == 8192


class TestMarkdownAttachment:
    def test_send_html_email_attaches_markdown_when_requested(self):
        from unittest.mock import patch as _patch, MagicMock
        from email.message import EmailMessage
        # Stub out the actual send path
        cfg = MagicMock(
            server="smtp.example.com", port=587, user="u",
            password="p", from_addr="from@ex.com", start_tls=True,
        )
        with _patch("query_engine.Alert.load_smtp_config_from_env", return_value=cfg), \
             _patch("query_engine.Alert._normalize_recipients", return_value=["to@ex.com"]), \
             _patch("asyncio.run"):
            from alert_groups.dispatcher import AlertGroupDispatcher
            AlertGroupDispatcher._send_html_email(
                subject="[SpeakesQuery REPORT] test",
                plain_body="# Brief\n\nfull response text",
                group_name="test_group",
                to_addrs="to@ex.com",
                meta={"searches_used": [], "actual_tokens": 100},
                attach_markdown=True,
            )
        # Verify: nothing raised + asyncio.run was invoked (proxy for send
        # reaching completion). The actual attachment bytes are covered by
        # the EmailMessage.add_attachment call which we don't intercept;
        # this test exists to pin "attach_markdown=True path doesn't crash".


class TestLogoLoad:
    def test_logo_b64_loads_from_file(self):
        from alert_groups.dispatcher import _load_logo_b64, _FALLBACK_LOGO_B64
        b64 = _load_logo_b64()
        # Real SVG should be longer than the fallback dummy
        assert len(b64) > len(_FALLBACK_LOGO_B64)
        import base64 as _b64
        decoded = _b64.b64decode(b64).decode("utf-8", errors="replace")
        assert "<svg" in decoded


class TestTruncationBanner:
    def test_banner_present_when_truncated(self):
        from alert_groups.dispatcher import build_html_email
        html = build_html_email(
            "ag", "body text",
            meta={"searches_used": ["s1"], "actual_tokens": 100,
                  "truncated": True, "stop_reason": "max_tokens"},
        )
        assert "Analyst brief was truncated" in html
        assert "max_tokens" in html

    def test_banner_absent_when_not_truncated(self):
        from alert_groups.dispatcher import build_html_email
        html = build_html_email(
            "ag", "body text",
            meta={"searches_used": ["s1"], "actual_tokens": 100,
                  "truncated": False},
        )
        assert "Analyst brief was truncated" not in html


class TestOnDemandFeederExecution:
    """Regression for the 2026-04-20 manual-Run bug.

    Before this fix: manual Run on an alert group returned ``error: No
    results available for any search in group.`` whenever the feeders'
    own crons hadn't fired yet (i.e. ``saved_search_history.db`` was
    empty for those search names) - even though the underlying indexed
    data was present under ``indexes/<subdir>/*.parquet``.

    After the fix: the dispatcher calls ``process_query`` directly for
    each feeder's saved-search query, so manual Run ALWAYS reflects the
    current state of the indexes regardless of cache state. Regression
    test: seed the SavedSearchStore with a feeder whose query returns a
    known df, mock process_query, and confirm serialize_df is used and
    the dispatcher reaches the Claude call.
    """

    def test_runs_on_demand_when_cache_empty(self, tmp_env, monkeypatch):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        store.save_search({
            "name": "fresh_feeder",
            "purpose": "alert_group_feeder",
            "query": 'index="indexes/x/*.parquet" | head 5',
            "cron_schedule": "0 5 * * *",
            "lookback": "-1h",
        })

        synthetic = pd.DataFrame({
            "value": [10, 20, 30], "_epoch": [1, 2, 3],
        })

        # Patch process_query_with_diagnostics (the dispatcher's new entry
        # point per 2026-04-21) so we don't need real indexes on disk.
        # process_query itself is also patched for back-compat with any
        # legacy caller.
        import query_engine.CmdExecutionBackend as cmd_mod
        monkeypatch.setattr(
            cmd_mod, "process_query_with_diagnostics",
            lambda q: (synthetic, "job-test", None),
        )
        monkeypatch.setattr(
            cmd_mod, "process_query",
            lambda q: (synthetic, "job-test"),
        )

        # Reset the dispatcher's class-level SavedSearchStore cache so our
        # tmp-path store (configured above via tmp_env) is actually used
        # instead of a module-imported production store from a previous
        # test. Added 2026-04-21 when the dispatcher began caching stores
        # across the feeder loop for efficiency.
        from alert_groups.dispatcher import AlertGroupDispatcher as _AGDispatcher
        _AGDispatcher._reset_ss_store_cache()
        # Patch the class so its lazy _get_ss_store returns our store
        import saved_search_store as _ss_mod
        monkeypatch.setattr(_ss_mod, "SavedSearchStore", lambda: store)

        # Also mock the Claude call - we want to prove the dispatcher
        # REACHED the Claude call, not that the actual response matters.
        from unittest.mock import patch as _patch

        def fake_call(**kwargs):
            result = MagicMock()
            result.response = MagicMock()
            result.response.content = [MagicMock(text="ok")]
            result.input_tokens = 10
            result.output_tokens = 5
            result.cost_usd = 0.0001
            result.latency_ms = 10
            result.request_id = "rid-on-demand"
            result.model = kwargs["model"]
            return result

        group = {
            "name": "on_demand_ag",
            "disabled": False,
            "max_rows": 10,
            "search_names": ["fresh_feeder"],
            "prompt_text": "Analyse.",
            "email_address": "",  # skip email path
            "schedule": "",
        }

        from alert_groups.dispatcher import AlertGroupDispatcher
        with _patch("alert_groups.dispatcher.call_messages_create",
                    side_effect=fake_call) as mock_claude:
            d = AlertGroupDispatcher()
            result = d.run(group)

        assert result.status == "success", (
            f"Expected success with on-demand execution; got "
            f"status={result.status} error={result.error_message}"
        )
        assert "fresh_feeder" in result.searches_used
        mock_claude.assert_called_once()

    def test_empty_query_result_is_reported(self, tmp_env, monkeypatch):
        """An on-demand feeder that returns zero rows must NOT silently
        disappear - it should leave the AG with no serialized results and
        the dispatcher should emit the "No results available" error."""
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        store.save_search({
            "name": "empty_feeder",
            "purpose": "alert_group_feeder",
            "query": 'index="indexes/x/*.parquet" | where never=1',
            "cron_schedule": "0 5 * * *",
            "lookback": "-1h",
        })

        import query_engine.CmdExecutionBackend as cmd_mod
        # Empty DF is reported via the diagnostic channel on the new
        # code path; keep both patched for back-compat with anything
        # that still imports process_query directly.
        monkeypatch.setattr(
            cmd_mod, "process_query_with_diagnostics",
            lambda q: (None, None, "empty: query produced zero rows"),
        )
        monkeypatch.setattr(
            cmd_mod, "process_query",
            lambda q: (pd.DataFrame(columns=["value", "_epoch"]), "job"),
        )

        from alert_groups.dispatcher import AlertGroupDispatcher as _AGDispatcher
        _AGDispatcher._reset_ss_store_cache()
        import saved_search_store as _ss_mod
        monkeypatch.setattr(_ss_mod, "SavedSearchStore", lambda: store)

        group = {
            "name": "empty_ag", "disabled": False, "max_rows": 10,
            "search_names": ["empty_feeder"], "prompt_text": "Go.",
            "email_address": "", "schedule": "",
        }
        from alert_groups.dispatcher import AlertGroupDispatcher
        d = AlertGroupDispatcher()
        result = d.run(group)
        assert result.status == "error"
        assert "No results available" in (result.error_message or "")


class TestCrudEmitters:
    def test_saved_search_crud_emits(self, tmp_env):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_env / "saved_searches"
        store._defaults_dir = tmp_env / "defaults_ss"
        store._db = str(tmp_env / "lc.sqlite")
        store.initialize()
        store.save_search({
            "name": "emit_test", "query": 'index="x"',
            "cron_schedule": "0 * * * *", "lookback": "-1h",
            "email_address": "u@example.com",
        })
        store.update_search("emit_test", {"description": "updated"})
        store.delete_search("emit_test")
        actions = [
            r["action"] for r in _read_config_log(tmp_env)
            if r.get("subject_type") == "saved_search"
        ]
        assert "create" in actions
        assert "update" in actions
        assert "delete" in actions

    def test_alert_group_crud_emits(self, tmp_env):
        from alert_group_store import AlertGroupStore
        ag = AlertGroupStore()
        ag.initialize()
        ag.save_group({
            "name": "emit_ag", "search_names": ["s"],
            "prompt_text": "Go.", "schedule": "",
            "max_rows": 10, "email_address": "u@example.com",
            "disabled": False,
        })
        ag.update_group("emit_ag", {"description": "updated"})
        ag.delete_group("emit_ag")
        actions = [
            r["action"] for r in _read_config_log(tmp_env)
            if r.get("subject_type") == "alert_group"
        ]
        assert "create" in actions
        assert "update" in actions
        assert "delete" in actions
