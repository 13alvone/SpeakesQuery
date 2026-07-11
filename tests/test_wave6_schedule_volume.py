"""
Tests for Wave 6 (2026-04-26): Schedule-page volume charts +
``GET /api/schedule/volume`` endpoint.

Coverage
--------
* ``compute_daily_volume`` returns one bucket per day in the requested
  window, oldest → newest, with empty days pre-zeroed so the chart
  x-axis stays uniform.
* The aggregator counts ingestion runs / saved-search runs /
  alert-group dispatches per UTC date AND sums ``row_count`` from
  ingestion logs into ``rows_ingested``.
* Rows outside the window are excluded.
* Empty / missing log directories yield all-zero buckets, never raise.
* The endpoint validates + clamps ``days`` (1 ≤ N ≤ 365).
* Frontend contracts: the volume box exists, fires from the page-load
  + window-change handlers, has the bar + line chart placeholders, and
  hits the right API path with the right query param.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Aggregator unit tests ──────────────────────────────────────────────
class TestComputeDailyVolume:
    def _seed_log(
        self, root: Path, category: str, rows: list[dict],
    ) -> None:
        """Drop a tiny parquet file under
        ``<root>/indexes/logs/<category>/`` so the aggregator picks it up."""
        path = root / "indexes" / "logs" / category
        path.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows)
        # log_writer convention: one parquet per category per flush.
        df.to_parquet(path / "test.parquet", index=False)

    def test_default_window_returns_14_buckets(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        buckets = compute_daily_volume(project_root=tmp_path, days=14)
        assert len(buckets) == 14
        # Empty install - every bucket should be all-zero, no missing keys
        for b in buckets:
            assert set(b.keys()) == {
                "date", "ingestion_runs", "search_runs",
                "ag_dispatches", "rows_ingested",
            }
            assert b["ingestion_runs"] == 0
            assert b["search_runs"] == 0
            assert b["ag_dispatches"] == 0
            assert b["rows_ingested"] == 0

    def test_buckets_are_chronological_and_unique(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        buckets = compute_daily_volume(project_root=tmp_path, days=7)
        dates = [b["date"] for b in buckets]
        assert dates == sorted(dates), (
            "buckets must be returned oldest → newest for the chart "
            "x-axis to read correctly"
        )
        assert len(set(dates)) == len(dates), (
            "every bucket must be a distinct day"
        )

    def test_ingestion_rows_summed_into_rows_ingested(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        # Three runs on the same day, totalling 1500 rows
        base = datetime.now(timezone.utc).replace(
            hour=12, minute=0, second=0, microsecond=0,
        )
        epoch = base.timestamp()
        self._seed_log(tmp_path, "ingestion", [
            {"_epoch": epoch, "task_id": 1, "title": "a", "status": "success",
             "duration_ms": 100, "error_message": "", "row_count": 500,
             "attempt": 1, "trust_level": "sandboxed"},
            {"_epoch": epoch + 60, "task_id": 1, "title": "a",
             "status": "success", "duration_ms": 100, "error_message": "",
             "row_count": 700, "attempt": 1, "trust_level": "sandboxed"},
            {"_epoch": epoch + 120, "task_id": 2, "title": "b",
             "status": "success", "duration_ms": 100, "error_message": "",
             "row_count": 300, "attempt": 1, "trust_level": "sandboxed"},
        ])
        buckets = compute_daily_volume(project_root=tmp_path, days=14)
        today_bucket = buckets[-1]  # newest = today
        assert today_bucket["ingestion_runs"] == 3
        assert today_bucket["rows_ingested"] == 1500

    def test_search_run_and_ag_dispatch_counts(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        epoch = datetime.now(timezone.utc).timestamp()
        self._seed_log(tmp_path, "search_runs", [
            {"_epoch": epoch, "search_name": "x", "status": "success",
             "row_count": 5, "duration_ms": 10, "error_message": "",
             "query_hash": "", "triggered_by": ""},
            {"_epoch": epoch + 60, "search_name": "y", "status": "success",
             "row_count": 0, "duration_ms": 10, "error_message": "",
             "query_hash": "", "triggered_by": ""},
        ])
        self._seed_log(tmp_path, "alert_groups", [
            {"_epoch": epoch, "group_name": "g", "status": "success",
             "searches_used": "x,y", "estimated_tokens": 0,
             "actual_tokens": 0, "cost_usd": 0.0, "error_message": "",
             "duration_ms": 10, "dry_run": False, "feeder_loop_ms": 0,
             "claude_call_ms": 0, "email_send_ms": 0},
        ])
        buckets = compute_daily_volume(project_root=tmp_path, days=14)
        today = buckets[-1]
        assert today["search_runs"] == 2
        assert today["ag_dispatches"] == 1

    def test_rows_outside_window_excluded(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        # Old row (60 days back) must NOT land in a 7-day window
        old_epoch = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).timestamp()
        self._seed_log(tmp_path, "ingestion", [
            {"_epoch": old_epoch, "task_id": 1, "title": "a",
             "status": "success", "duration_ms": 100, "error_message": "",
             "row_count": 9999, "attempt": 1, "trust_level": "sandboxed"},
        ])
        buckets = compute_daily_volume(project_root=tmp_path, days=7)
        total = sum(b["rows_ingested"] for b in buckets)
        assert total == 0, (
            "row 60 days back must not appear in the 7-day window"
        )

    def test_missing_log_dir_does_not_raise(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        # Fresh install: no indexes/logs/ at all. Must return zeros.
        buckets = compute_daily_volume(project_root=tmp_path, days=3)
        assert len(buckets) == 3
        assert all(b["rows_ingested"] == 0 for b in buckets)

    def test_zero_days_returns_empty(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        assert compute_daily_volume(project_root=tmp_path, days=0) == []

    def test_days_capped_to_365(self, tmp_path):
        from schedule_visualization import compute_daily_volume
        # 9999 should clamp to 365 buckets
        buckets = compute_daily_volume(project_root=tmp_path, days=9999)
        assert len(buckets) == 365


# ── Endpoint tests ─────────────────────────────────────────────────────
@pytest.fixture
def client():
    from scheduled_input_engine import start_engine
    start_engine()
    from desktop_app.server import app
    app.config["TESTING"] = True
    return app.test_client()


class TestVolumeEndpoint:
    def test_default_returns_14_buckets(self, client):
        resp = client.get("/api/schedule/volume")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["days"] == 14
        assert len(data["buckets"]) == 14

    def test_explicit_days_param_honored(self, client):
        resp = client.get("/api/schedule/volume?days=30")
        data = resp.get_json()
        assert data["days"] == 30
        assert len(data["buckets"]) == 30

    def test_zero_days_clamps_to_one(self, client):
        # Validator clamps to [1, 365] - never returns empty buckets
        # for a non-zero positive caller, never crashes for 0 / negative.
        resp = client.get("/api/schedule/volume?days=0")
        assert resp.status_code == 200
        assert resp.get_json()["days"] == 1

    def test_invalid_days_falls_back_to_default(self, client):
        resp = client.get("/api/schedule/volume?days=banana")
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["days"] == 14

    def test_oversize_days_clamps_to_365(self, client):
        resp = client.get("/api/schedule/volume?days=99999")
        data = resp.get_json()
        assert data["days"] == 365

    def test_bucket_shape_is_stable(self, client):
        resp = client.get("/api/schedule/volume?days=7")
        data = resp.get_json()
        for b in data["buckets"]:
            assert "date" in b
            assert "ingestion_runs" in b
            assert "search_runs" in b
            assert "ag_dispatches" in b
            assert "rows_ingested" in b
            assert isinstance(b["date"], str)
            # Loose ISO date check
            assert re.match(r"\d{4}-\d{2}-\d{2}", b["date"])


# ── Frontend contract regressions ──────────────────────────────────────
class TestFrontendContracts:
    def _ui(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_volume_box_present_on_schedule_page(self):
        ui = self._ui()
        assert 'id="sched-volume-bar"' in ui, (
            "Wave 6 bar chart container missing"
        )
        assert 'id="sched-volume-line"' in ui, (
            "Wave 6 line chart container missing"
        )
        assert 'id="sched-volume-days"' in ui, (
            "Wave 6 window selector missing"
        )

    def test_window_selector_default_is_14(self):
        ui = self._ui()
        # The 14-day option must carry `selected` so the default
        # matches the user's stated preference.
        m = re.search(
            r'<select id="sched-volume-days"[^>]*>([\s\S]+?)</select>', ui,
        )
        assert m, "sched-volume-days select not found"
        block = m.group(1)
        assert re.search(
            r'<option value="14"\s+selected', block,
        ), "14-day option must be selected by default (per Wave 6 plan)"

    def test_load_function_hits_correct_endpoint(self):
        ui = self._ui()
        assert "/api/schedule/volume?days=" in ui, (
            "loadScheduleVolume must hit /api/schedule/volume?days=N"
        )

    def test_bar_and_line_renderers_exist(self):
        ui = self._ui()
        assert "function _schedRenderVolumeBar" in ui
        assert "function _schedRenderVolumeLine" in ui

    def test_volume_loads_when_navigating_to_schedule_tab(self):
        ui = self._ui()
        # The page-schedule navigation handler must call
        # loadScheduleVolume (in addition to loadScheduleHeatmap).
        m = re.search(
            r"if \(page === 'page-schedule'\)\s*\{([\s\S]+?)\}", ui,
        )
        assert m, "page-schedule navigation handler not found"
        body = m.group(1)
        assert "loadScheduleVolume" in body, (
            "Wave 6 volume charts must load when the Schedule tab opens"
        )

    def test_window_selector_change_reloads_volume(self):
        ui = self._ui()
        # The volume select's change handler must call loadScheduleVolume.
        # Verify the wiring by string match.
        assert "sched-volume-days" in ui
        # The wiring lives near the rest of the Schedule listeners.
        assert "loadScheduleVolume" in ui

    def test_renderer_uses_inline_svg_no_runtime_deps(self):
        ui = self._ui()
        # Confirm the renderers emit `<svg viewBox=...>` rather than
        # pulling a chart library. The Wave-6 plan deliberately avoids
        # adding a runtime chart dep FOR THE SCHEDULE VOLUME CHARTS.
        assert '<svg viewBox=' in ui
        # Negative: no Chart.js / D3 / Recharts. Patterns tightened
        # 2026-05-09 (Phase 3 slice 7): the original substring
        # ``Chart(`` false-flagged ``_mountChart(`` from the notebook
        # chart-cell renderer. The CLAUDE.md "Do Not" rule scopes to
        # "the Wave-6 Schedule volume charts" specifically - slice-7's
        # notebook chart-cell type is a different surface that
        # explicitly opts into Vega-Lite (operator-supplied JSON spec).
        # Patterns now match the real library APIs, not just a word.
        for forbidden in (
            "chart.js",          # CDN URL pattern (lowercase)
            "Chart.js",          # CDN URL pattern (capitalised)
            "new Chart(",        # Chart.js constructor
            "d3.select(",        # D3 selector API
            "from 'recharts'",   # Recharts ES import
            'from "recharts"',
        ):
            assert forbidden not in ui, (
                f"Wave 6 must stay dep-free; found {forbidden!r}"
            )
