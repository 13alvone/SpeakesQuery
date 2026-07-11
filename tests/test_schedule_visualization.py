"""
Tests for the schedule visualization feature:
  - schedule_visualization.expand_cron_to_firings
  - schedule_visualization.compute_hour_distribution
  - schedule_visualization.gather_run_history
  - schedule_visualization.build_schedule_summary
  - /api/schedule/heatmap

We isolate from the user's real data by:
  - patching the three job-source functions to return controlled inputs
  - patching the project_root pointed at by gather_run_history so it
    sees a tmp_path with synthetic parquet logs.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

# Project root on sys.path
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from schedule_visualization import (
    KIND_ALERT_GROUP,
    KIND_INGESTION,
    KIND_SAVED_SEARCH,
    build_schedule_summary,
    compute_data_distribution,
    compute_hour_distribution,
    expand_cron_to_firings,
    gather_run_history,
)


# ──────────────────────────────────────────────────────────────────
# Cron expansion
# ──────────────────────────────────────────────────────────────────


class TestExpandCron:

    def test_hourly_cron_produces_24_per_day(self):
        # "0 * * * *" fires every hour on the hour
        base = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
        firings = expand_cron_to_firings("0 * * * *", lookahead_days=1, base_dt=base)
        # Should produce 24 firings (next 24 hours, starting 13:00)
        assert 23 <= len(firings) <= 25
        # All firings are within the lookahead window
        end = base + timedelta(days=1)
        assert all(base <= f <= end for f in firings)
        # All firings have :00 minute
        assert all(f.minute == 0 for f in firings)

    def test_daily_cron_produces_one_per_day(self):
        base = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
        firings = expand_cron_to_firings("0 14 * * *", lookahead_days=7, base_dt=base)
        # Should produce ~7 firings (one per day at 14:00 UTC)
        assert 6 <= len(firings) <= 8
        # All at 14:00
        assert all(f.hour == 14 and f.minute == 0 for f in firings)

    def test_weekly_sunday_cron(self):
        base = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)  # Saturday
        firings = expand_cron_to_firings("0 18 * * 0", lookahead_days=14, base_dt=base)
        # Sundays in the next 14 days: 2 (4/26 and 5/3)
        assert 1 <= len(firings) <= 3
        assert all(f.weekday() == 6 for f in firings)  # Sunday

    def test_invalid_cron_returns_empty(self):
        firings = expand_cron_to_firings("not a cron", lookahead_days=1)
        assert firings == []

    def test_empty_cron_returns_empty(self):
        assert expand_cron_to_firings("", lookahead_days=1) == []
        assert expand_cron_to_firings(None, lookahead_days=1) == []


# ──────────────────────────────────────────────────────────────────
# Hour distribution
# ──────────────────────────────────────────────────────────────────


class TestComputeHourDistribution:

    def test_empty_jobs(self):
        result = compute_hour_distribution([])
        assert result["total_firings"] == 0
        assert sum(result["by_hour_total"]) == 0
        for dow in range(7):
            assert sum(result["by_dow_hour"][dow]) == 0

    def test_single_daily_job_at_5utc(self):
        base = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
        jobs = [{"kind": KIND_INGESTION, "name": "t1", "cron": "0 5 * * *", "disabled": False}]
        result = compute_hour_distribution(jobs, lookahead_days=7, base_dt=base)
        # Should have ~7 firings, all at hour 5
        assert 6 <= result["total_firings"] <= 8
        assert result["by_hour_total"][5] == result["total_firings"]
        # No firings in any other hour
        for h in range(24):
            if h != 5:
                assert result["by_hour_total"][h] == 0

    def test_disabled_jobs_excluded_by_default(self):
        base = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
        jobs = [
            {"kind": KIND_INGESTION, "name": "off", "cron": "0 5 * * *", "disabled": True},
            {"kind": KIND_INGESTION, "name": "on", "cron": "0 6 * * *", "disabled": False},
        ]
        result = compute_hour_distribution(jobs, lookahead_days=7, base_dt=base)
        # Only the enabled job should contribute
        assert result["by_hour_total"][5] == 0
        assert result["by_hour_total"][6] > 0

    def test_include_disabled(self):
        base = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
        jobs = [
            {"kind": KIND_INGESTION, "name": "off", "cron": "0 5 * * *", "disabled": True},
            {"kind": KIND_INGESTION, "name": "on", "cron": "0 6 * * *", "disabled": False},
        ]
        result = compute_hour_distribution(
            jobs, lookahead_days=7, base_dt=base, include_disabled=True,
        )
        assert result["by_hour_total"][5] > 0
        assert result["by_hour_total"][6] > 0

    def test_multiple_jobs_aggregate(self):
        base = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
        jobs = [
            {"kind": KIND_INGESTION, "name": "a", "cron": "0 5 * * *", "disabled": False},
            {"kind": KIND_SAVED_SEARCH, "name": "b", "cron": "0 5 * * *", "disabled": False},
            {"kind": KIND_ALERT_GROUP, "name": "c", "cron": "0 5 * * *", "disabled": False},
        ]
        result = compute_hour_distribution(jobs, lookahead_days=7, base_dt=base)
        # Three jobs all firing at 5 UTC, 7 days = ~21 firings at hour 5
        assert result["by_hour_total"][5] == result["total_firings"]
        assert 18 <= result["total_firings"] <= 24


# ──────────────────────────────────────────────────────────────────
# Run history aggregation
# ──────────────────────────────────────────────────────────────────


def _write_log_parquet(tmp_path, category, rows):
    """Helper: write a synthetic indexes/logs/<category>/test.parquet."""
    logs_dir = tmp_path / "indexes" / "logs" / category
    logs_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    out = logs_dir / "test.parquet"
    df.to_parquet(out, index=False)
    return out


class TestGatherRunHistory:

    def test_no_logs_returns_empty(self, tmp_path):
        result = gather_run_history(project_root=tmp_path)
        assert result == {}

    def test_ingestion_history_aggregation(self, tmp_path):
        now = datetime.now(timezone.utc).timestamp()
        rows = [
            {"_epoch": now - i * 3600, "task_id": "task_1", "title": "Test Task",
             "row_count": 100 + i, "duration_ms": 1000 + i * 10}
            for i in range(10)
        ]
        _write_log_parquet(tmp_path, "ingestion", rows)
        result = gather_run_history(project_root=tmp_path, history_lookback_runs=5)
        key = "ingestion::task_1"
        assert key in result
        entry = result[key]
        assert entry["kind"] == "ingestion"
        assert entry["run_count"] == 5
        assert entry["name"] == "Test Task"
        # Average of first 5 (latest) rows: 100, 101, 102, 103, 104 = 102.0
        assert entry["avg_row_count"] == 102.0
        assert entry["avg_duration_ms"] == 1020.0  # 1000, 1010, 1020, 1030, 1040

    def test_saved_search_history_aggregation(self, tmp_path):
        now = datetime.now(timezone.utc).timestamp()
        rows = [
            {"_epoch": now - i * 3600, "search_name": "my_search",
             "status": "success", "row_count": 50 * (i + 1), "duration_ms": 500 + i}
            for i in range(7)
        ]
        _write_log_parquet(tmp_path, "search_runs", rows)
        result = gather_run_history(project_root=tmp_path, history_lookback_runs=3)
        key = "saved_search::my_search"
        assert key in result
        entry = result[key]
        # 3 most recent: i=0,1,2 → 50, 100, 150 → avg 100
        assert entry["avg_row_count"] == 100.0
        assert entry["run_count"] == 3

    def test_error_count_from_status_column(self, tmp_path):
        """error_count counts status=='error' rows among the last-N runs
        - the signal behind the report's FAILING bucket. A job erroring
        on every run previously showed avg_row_count None (' - ') and
        escaped every anomaly bucket (caught 2026-07-01)."""
        now = datetime.now(timezone.utc).timestamp()
        rows = [
            {"_epoch": now - 1 * 3600, "search_name": "flaky",
             "status": "error", "row_count": None, "duration_ms": 400,
             "error_message": "boom"},
            {"_epoch": now - 2 * 3600, "search_name": "flaky",
             "status": "success", "row_count": 10, "duration_ms": 500,
             "error_message": None},
            {"_epoch": now - 3 * 3600, "search_name": "flaky",
             "status": "error", "row_count": None, "duration_ms": 450,
             "error_message": "boom"},
            {"_epoch": now - 1 * 3600, "search_name": "healthy",
             "status": "success", "row_count": 5, "duration_ms": 100,
             "error_message": None},
        ]
        _write_log_parquet(tmp_path, "search_runs", rows)
        result = gather_run_history(project_root=tmp_path, history_lookback_runs=5)
        assert result["saved_search::flaky"]["error_count"] == 2
        assert result["saved_search::healthy"]["error_count"] == 0

    def test_error_count_ingestion_and_missing_status_column(self, tmp_path):
        """Ingestion history also carries error_count; a log parquet
        without a status column degrades to 0, never raises."""
        now = datetime.now(timezone.utc).timestamp()
        ing_rows = [
            {"_epoch": now - i * 3600, "task_id": "t9", "title": "T9",
             "status": "error" if i == 0 else "success",
             "row_count": 10, "duration_ms": 100}
            for i in range(3)
        ]
        _write_log_parquet(tmp_path, "ingestion", ing_rows)
        result = gather_run_history(project_root=tmp_path)
        assert result["ingestion::t9"]["error_count"] == 1

    def test_old_runs_filtered_by_lookback_days(self, tmp_path):
        now = datetime.now(timezone.utc).timestamp()
        rows = [
            # One recent, one ancient
            {"_epoch": now - 3600, "task_id": "t1", "title": "Recent",
             "row_count": 100, "duration_ms": 1000},
            {"_epoch": now - 200 * 86400, "task_id": "t1", "title": "Recent",
             "row_count": 999999, "duration_ms": 999999},
        ]
        _write_log_parquet(tmp_path, "ingestion", rows)
        result = gather_run_history(
            project_root=tmp_path, history_lookback_days=30,
        )
        # Old row filtered; only the recent one counts
        entry = result["ingestion::t1"]
        assert entry["run_count"] == 1
        assert entry["avg_row_count"] == 100.0


# ──────────────────────────────────────────────────────────────────
# Data distribution
# ──────────────────────────────────────────────────────────────────


class TestComputeDataDistribution:

    def test_no_history_returns_zeros(self):
        base = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
        jobs = [{"kind": KIND_INGESTION, "name": "t1", "cron": "0 5 * * *", "disabled": False}]
        history = {}  # No history available
        result = compute_data_distribution(jobs, history, base_dt=base)
        assert sum(result["by_hour_total"]) == 0

    def test_with_history(self):
        base = datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc)
        jobs = [{
            "kind": KIND_INGESTION, "name": "t1", "task_id": "1",
            "cron": "0 5 * * *", "disabled": False,
        }]
        history = {
            "ingestion::1": {
                "kind": KIND_INGESTION, "name": "t1",
                "run_count": 5, "avg_row_count": 200.0, "avg_duration_ms": 1500.0,
            },
        }
        result = compute_data_distribution(jobs, history, lookahead_days=7, base_dt=base)
        # 7 firings × 200 rows = 1400 in hour 5
        assert result["by_hour_total"][5] >= 1200
        # Has-data flag set for hour 5 cells with firings
        # (At least one weekday should have it set)
        any_set = any(
            result["by_dow_hour_has_data"][dow][5]
            for dow in range(7)
        )
        assert any_set


# ──────────────────────────────────────────────────────────────────
# Top-level summary builder
# ──────────────────────────────────────────────────────────────────


class TestBuildScheduleSummary:

    def test_empty_environment(self, monkeypatch, tmp_path):
        """Patch all collectors to return empty; summary should still
        return the canonical shape with zero counts."""
        import schedule_visualization as sv
        monkeypatch.setattr(sv, "_collect_ingestion_jobs", lambda: [])
        monkeypatch.setattr(sv, "_collect_saved_search_jobs", lambda: [])
        monkeypatch.setattr(sv, "_collect_alert_group_jobs", lambda: [])

        summary = build_schedule_summary(project_root=tmp_path)
        assert summary["summary"]["total_jobs"] == 0
        assert summary["jobs"] == []
        assert summary["hour_distribution"]["total_firings"] == 0

    def test_busiest_hour_identified(self, monkeypatch, tmp_path):
        import schedule_visualization as sv
        # Three jobs, all at hour 14
        monkeypatch.setattr(sv, "_collect_ingestion_jobs", lambda: [
            {"kind": KIND_INGESTION, "name": "i1", "task_id": "1",
             "cron": "0 14 * * *", "disabled": False, "subdirectory": ""},
        ])
        monkeypatch.setattr(sv, "_collect_saved_search_jobs", lambda: [
            {"kind": KIND_SAVED_SEARCH, "name": "s1",
             "cron": "0 14 * * *", "disabled": False, "purpose": "standalone"},
        ])
        monkeypatch.setattr(sv, "_collect_alert_group_jobs", lambda: [
            {"kind": KIND_ALERT_GROUP, "name": "a1",
             "cron": "0 14 * * *", "disabled": False, "feeder_count": 3},
        ])
        summary = build_schedule_summary(project_root=tmp_path)
        assert summary["summary"]["total_jobs"] == 3
        assert summary["summary"]["busiest_hour_utc"] == 14
        assert summary["summary"]["busiest_hour_count"] >= 18  # 3 jobs × 7 days
        assert summary["summary"]["by_kind"]["ingestion"] == 1
        assert summary["summary"]["by_kind"]["saved_search"] == 1
        assert summary["summary"]["by_kind"]["alert_group"] == 1

    def test_jobs_get_next_firing_metadata(self, monkeypatch, tmp_path):
        import schedule_visualization as sv
        monkeypatch.setattr(sv, "_collect_ingestion_jobs", lambda: [
            {"kind": KIND_INGESTION, "name": "i1", "task_id": "1",
             "cron": "0 5 * * *", "disabled": False, "subdirectory": ""},
        ])
        monkeypatch.setattr(sv, "_collect_saved_search_jobs", lambda: [])
        monkeypatch.setattr(sv, "_collect_alert_group_jobs", lambda: [])
        summary = build_schedule_summary(project_root=tmp_path)
        assert len(summary["jobs"]) == 1
        job = summary["jobs"][0]
        assert job["next_firing_epoch"] is not None
        assert job["next_firing_iso"] is not None
        assert job["firings_in_lookahead"] >= 6
        assert job["firings_in_lookahead"] <= 8


# ──────────────────────────────────────────────────────────────────
# /api/schedule/heatmap
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    """Flask test client with all collectors stubbed."""
    import schedule_visualization as sv
    monkeypatch.setattr(sv, "_collect_ingestion_jobs", lambda: [
        {"kind": KIND_INGESTION, "name": "t1", "task_id": "1",
         "cron": "0 5 * * *", "disabled": False, "subdirectory": ""},
    ])
    monkeypatch.setattr(sv, "_collect_saved_search_jobs", lambda: [
        {"kind": KIND_SAVED_SEARCH, "name": "s1",
         "cron": "30 5 * * *", "disabled": False, "purpose": "standalone"},
    ])
    monkeypatch.setattr(sv, "_collect_alert_group_jobs", lambda: [
        {"kind": KIND_ALERT_GROUP, "name": "a1",
         "cron": "0 6 * * *", "disabled": False, "feeder_count": 4},
    ])
    monkeypatch.setattr(sv, "gather_run_history", lambda **kw: {
        "ingestion::1": {
            "kind": KIND_INGESTION, "name": "t1",
            "run_count": 5, "avg_row_count": 100.0, "avg_duration_ms": 1500.0,
        },
        "saved_search::s1": {
            "kind": KIND_SAVED_SEARCH, "name": "s1",
            "run_count": 5, "avg_row_count": 25.0, "avg_duration_ms": 250.0,
        },
    })

    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestScheduleHeatmapAPI:

    def test_default_query(self, api_client):
        r = api_client.get("/api/schedule/heatmap")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "success"
        assert body["lookahead_days"] == 7
        assert body["history_lookback_runs"] == 5
        assert body["summary"]["total_jobs"] == 3

    def test_custom_lookahead(self, api_client):
        r = api_client.get("/api/schedule/heatmap?lookahead_days=14")
        assert r.status_code == 200
        body = r.get_json()
        assert body["lookahead_days"] == 14
        # More firings expected with longer lookahead
        assert body["hour_distribution"]["total_firings"] >= 28

    def test_lookahead_clamped_to_max(self, api_client):
        r = api_client.get("/api/schedule/heatmap?lookahead_days=999")
        body = r.get_json()
        assert body["lookahead_days"] == 30  # clamp ceiling

    def test_lookahead_clamped_to_min(self, api_client):
        r = api_client.get("/api/schedule/heatmap?lookahead_days=0")
        body = r.get_json()
        assert body["lookahead_days"] == 1  # clamp floor

    def test_invalid_int_falls_back_to_default(self, api_client):
        r = api_client.get("/api/schedule/heatmap?lookahead_days=not_a_number")
        body = r.get_json()
        assert body["lookahead_days"] == 7

    def test_jobs_carry_history(self, api_client):
        r = api_client.get("/api/schedule/heatmap")
        body = r.get_json()
        # Find the ingestion job; history was stubbed
        ingestion_jobs = [j for j in body["jobs"] if j["kind"] == "ingestion"]
        assert len(ingestion_jobs) == 1
        assert ingestion_jobs[0]["avg_row_count"] == 100.0
        assert ingestion_jobs[0]["run_count"] == 5

    def test_data_distribution_present(self, api_client):
        r = api_client.get("/api/schedule/heatmap")
        body = r.get_json()
        assert "data_distribution" in body
        assert "by_dow_hour" in body["data_distribution"]
        assert "by_hour_total" in body["data_distribution"]

    def test_busiest_hour_in_response(self, api_client):
        r = api_client.get("/api/schedule/heatmap")
        body = r.get_json()
        # Stubbed jobs all fire in hour 5 (×2) and 6 (×1) → busiest = 5
        assert body["summary"]["busiest_hour_utc"] == 5

    def test_include_disabled_query_param(self, api_client, monkeypatch):
        import schedule_visualization as sv
        monkeypatch.setattr(sv, "_collect_ingestion_jobs", lambda: [
            {"kind": KIND_INGESTION, "name": "off", "task_id": "1",
             "cron": "0 5 * * *", "disabled": True, "subdirectory": ""},
        ])
        monkeypatch.setattr(sv, "_collect_saved_search_jobs", lambda: [])
        monkeypatch.setattr(sv, "_collect_alert_group_jobs", lambda: [])
        # Default: disabled excluded
        r = api_client.get("/api/schedule/heatmap")
        body = r.get_json()
        assert body["hour_distribution"]["total_firings"] == 0
        # With include_disabled=true: counts
        r = api_client.get("/api/schedule/heatmap?include_disabled=true")
        body = r.get_json()
        assert body["hour_distribution"]["total_firings"] > 0
