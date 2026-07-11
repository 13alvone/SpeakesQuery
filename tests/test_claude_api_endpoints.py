"""
Tests for the Flask endpoints added in Wave C:

  * POST /api/analyzer/test - fires a minimal Claude call to verify credentials
  * GET  /api/claude-history - paginated list
  * GET  /api/claude-history/<id> - detail with decoded payloads
  * GET  /api/claude-history/stats - aggregate cost / tokens / call counts
  * POST /api/claude-history/vacuum - prune + VACUUM
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Set up a Flask test client pointed at a tmp Claude history DB."""
    from desktop_app.server import app
    from analyzers.claude_history_store import ClaudeHistoryStore

    hist = ClaudeHistoryStore(db_path=tmp_path / "hist.sqlite")
    ClaudeHistoryStore._instance = hist

    from global_settings import get_settings
    settings = get_settings()
    settings.set("logs_root", str(tmp_path / "logs"))
    settings.set("logs_enabled", True)
    from functionality import log_writer as lw
    lw.LogWriter.reset_for_tests()

    with app.test_client() as c:
        yield c, hist

    ClaudeHistoryStore.reset_for_tests()
    lw.LogWriter.reset_for_tests()


class TestAnalyzerTestEndpoint:
    def test_success(self, app_client):
        client, _hist = app_client
        with patch("analyzers.claude_client.test_connectivity") as mock:
            mock.return_value = {
                "ok": True, "request_id": "rid-1",
                "model": "claude-haiku-4-5-20251001",
                "latency_ms": 42, "input_tokens": 7, "output_tokens": 2,
                "cost_usd": 0.00008, "attempts": 1,
            }
            resp = client.post(
                "/api/analyzer/test",
                json={"value": "sk-ant-fake"},
            )
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["status"] == "success"
            assert data["ok"] is True
            assert data["latency_ms"] == 42
            assert mock.call_count == 1
            # Confirm candidate key was forwarded
            assert mock.call_args.kwargs.get("api_key") == "sk-ant-fake"

    def test_failure_returns_400_with_detail(self, app_client):
        client, _hist = app_client
        with patch("analyzers.claude_client.test_connectivity") as mock:
            mock.return_value = {
                "ok": False, "request_id": "rid-2",
                "error_class": "AuthenticationError",
                "error_message": "invalid x-api-key",
                "attempts": 1,
            }
            resp = client.post("/api/analyzer/test", json={})
            data = resp.get_json()
            assert resp.status_code == 400
            assert data["ok"] is False
            assert data["error_class"] == "AuthenticationError"


class TestClaudeHistoryEndpoints:
    def _seed(self, hist):
        rid1 = hist.record_call(
            source="alert_group", model="claude-sonnet-4-6", status="success",
            group_name="g1", input_tokens=100, output_tokens=50, cost_usd=0.001,
            request_body={"messages": [{"role": "user", "content": "x"}]},
            response_body={"content": [{"type": "text", "text": "OK"}]},
        )
        rid2 = hist.record_call(
            source="analyzer", model="claude-haiku-4-5-20251001", status="error",
            input_tokens=20, error_class="AuthenticationError",
            error_message="invalid key",
        )
        return rid1, rid2

    def test_list_returns_paginated_rows(self, app_client):
        client, hist = app_client
        self._seed(hist)
        resp = client.get("/api/claude-history?limit=10")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["status"] == "success"
        assert data["count"] == 2
        # Newest first
        assert data["rows"][0]["source"] == "analyzer"

    def test_list_source_filter(self, app_client):
        client, hist = app_client
        self._seed(hist)
        resp = client.get("/api/claude-history?source=alert_group")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["rows"][0]["source"] == "alert_group"

    def test_detail_returns_decoded_payloads(self, app_client):
        client, hist = app_client
        rid1, _ = self._seed(hist)
        resp = client.get(f"/api/claude-history/{rid1}")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["row"]["request_body"]["messages"][0]["content"] == "x"

    def test_detail_404(self, app_client):
        client, _ = app_client
        resp = client.get("/api/claude-history/nope")
        assert resp.status_code == 404

    def test_stats_aggregates(self, app_client):
        client, hist = app_client
        self._seed(hist)
        resp = client.get("/api/claude-history/stats")
        data = resp.get_json()
        assert data["stats"]["calls"] == 2
        assert data["stats"]["success_count"] == 1
        assert data["stats"]["error_count"] == 1
        assert data["stats"]["db_size_bytes"] > 0

    def test_vacuum_removes_and_compacts(self, app_client):
        client, hist = app_client
        import time
        rid_old = hist.record_call(source="analyzer", model="m", status="success")
        import sqlite3
        with sqlite3.connect(hist._db_path) as conn:
            conn.execute(
                "UPDATE claude_api_calls SET triggered_at_epoch = ? "
                "WHERE request_id = ?",
                (1000, rid_old),
            )
            conn.commit()

        cutoff = int(time.time()) - 60
        resp = client.post(
            "/api/claude-history/vacuum",
            json={"older_than_epoch": cutoff},
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["removed"] == 1
