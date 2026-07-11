#!/usr/bin/env python3
"""
Edge-case tests for the analyzer pipeline.

Covers:
  - Missing / empty API key handling (vault + analyzer)
  - Corrupted SQLite database resilience
  - Concurrent budget updates (thread safety)
  - Budget boundary conditions (exact exhaustion)
  - Empty batch polling
  - Static response text parsing (valid, code-block, garbage, missing keys)
"""

import json
import os
import sys
import threading
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from analyzers.claude_analyzer import ClaudeAnalyzer
from analyzers.models import AnalysisResult, AnalyzerConfig
from analyzers.storage import AnalyzerStorage


# =====================================================================
# 1. Vault / Missing API Key
# =====================================================================

class TestVaultMissingApiKey:
    """ClaudeAnalyzer and batch_poller behaviour when no API key is available."""

    def test_analyze_returns_skipped_no_api_key(self):
        config = AnalyzerConfig(api_key="")
        analyzer = ClaudeAnalyzer(config)
        result = analyzer.analyze("edge_test", [{"foo": "bar"}])
        assert result.status == "skipped"
        assert result.skip_reason == "no_api_key"

    def test_batch_poller_get_api_key_returns_empty_on_vault_failure(self):
        """When the credential vault import raises, _get_api_key returns ''."""
        with patch.dict("sys.modules", {"global_settings": None}):
            from analyzers.batch_poller import _get_api_key
            assert _get_api_key() == ""

    def test_batch_poller_get_api_key_returns_empty_on_import_error(self):
        """When CredentialVault itself raises, _get_api_key returns ''."""
        mock_gs = MagicMock()
        mock_gs.get_settings.return_value = {}
        mock_creds = MagicMock()
        mock_creds.CredentialVault.side_effect = RuntimeError("vault locked")
        with patch.dict(
            "sys.modules",
            {
                "global_settings": mock_gs,
                "scheduled_input_engine": MagicMock(),
                "scheduled_input_engine.credentials": mock_creds,
            },
        ):
            from importlib import reload
            import analyzers.batch_poller as bp_mod
            reload(bp_mod)
            assert bp_mod._get_api_key() == ""


# =====================================================================
# 2. Corrupted Database
# =====================================================================

class TestCorruptedDb:
    """Storage operations against a non-SQLite file should not raise."""

    @pytest.fixture(autouse=True)
    def setup_bad_db(self, tmp_path):
        bad_file = tmp_path / "corrupt.db"
        bad_file.write_text("not a database")
        self.storage = AnalyzerStorage(db_path=str(bad_file))

    def test_store_result_does_not_raise(self):
        analysis = AnalysisResult(status="analyzed", summary="test")
        # Should silently log, never raise
        self.storage.store_result("search1", "2026-04-07T00:00:00", analysis)

    def test_load_daily_budget_returns_zeroed(self):
        result = self.storage.load_daily_budget("2026-04-07")
        assert result["total_input_tokens"] == 0
        assert result["total_output_tokens"] == 0
        assert result["total_cost_cents"] == 0.0
        assert result["total_calls"] == 0

    def test_get_pending_batch_ids_returns_empty(self):
        assert self.storage.get_pending_batch_ids() == []

    def test_record_usage_does_not_raise(self):
        self.storage.record_usage("2026-04-07", 500, 200, 0.5)


# =====================================================================
# 3. Concurrent Budget Updates
# =====================================================================

class TestConcurrentBudgetUpdates:
    """Thread-safe budget recording via AnalyzerStorage."""

    def test_ten_concurrent_writes(self, tmp_path):
        db_path = str(tmp_path / "budget_concurrent.db")
        storage = AnalyzerStorage(db_path=db_path)
        today = date.today().isoformat()

        errors = []

        def _record():
            try:
                storage.record_usage(today, 100, 50, 0.5)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_record) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"

        budget = storage.load_daily_budget(today)
        assert budget["total_input_tokens"] == 1000
        assert budget["total_output_tokens"] == 500
        assert abs(budget["total_cost_cents"] - 5.0) < 1e-6
        assert budget["total_calls"] == 10


# =====================================================================
# 4. Budget Boundary
# =====================================================================

class TestBudgetBoundary:
    """Exact budget exhaustion triggers skip on subsequent analyze()."""

    def test_exact_budget_exhaustion(self, tmp_path):
        db_path = str(tmp_path / "budget_boundary.db")
        storage = AnalyzerStorage(db_path=db_path)
        # M-AN-10 (2026-04-22): the analyzer internally uses
        # ``datetime.now(timezone.utc).date()`` as its "today" bucket -
        # ``date.today()`` here would diverge from that in any non-UTC
        # timezone when the test straddles UTC midnight (caught
        # 2026-04-23 17:57 PDT → 00:57 UTC = next day). Use the same
        # UTC-aware date so record + lookup agree.
        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).date().isoformat()

        # Record exactly 1 cent of usage
        storage.record_usage(today, 100, 50, 1.0)

        # Create analyzer with daily_budget_cents=1 backed by same storage
        config = AnalyzerConfig(api_key="sk-test-key", daily_budget_cents=1)
        analyzer = ClaudeAnalyzer(config, storage=storage)

        # Budget should be fully consumed
        stats = analyzer.get_usage_stats()
        assert abs(stats.budget_remaining_cents) < 1e-6

        # Next analyze() should be skipped due to budget
        result = analyzer.analyze("boundary_test", [{"question": "test?"}])
        assert result.status == "skipped"
        assert result.skip_reason == "budget_exceeded"


# =====================================================================
# 5. Empty Batch Poll
# =====================================================================

class TestEmptyBatchPoll:
    """poll_pending_batches with no pending work returns 0 cleanly."""

    def test_no_pending_batches(self, tmp_path):
        db_path = str(tmp_path / "empty_batch.db")
        storage = AnalyzerStorage(db_path=db_path)

        from analyzers.batch_poller import poll_pending_batches
        result = poll_pending_batches(storage=storage)
        assert result == 0


# =====================================================================
# 6. Static Response Text Parsing
# =====================================================================

class TestParseResponseTextStatic:
    """ClaudeAnalyzer.parse_response_text (static) edge cases."""

    def test_valid_json(self):
        payload = json.dumps({
            "alert_priority": "HIGH",
            "summary": "Spike detected.",
            "actionable_markets": [
                {
                    "question": "Will X happen?",
                    "position": "YES",
                    "confidence": 0.9,
                    "reasoning": "Strong signal.",
                    "estimated_roi": 20.0,
                }
            ],
        })
        parsed = ClaudeAnalyzer.parse_response_text(payload, 100, 50)
        assert parsed["status"] == "analyzed"
        assert parsed["alert_priority"] == "HIGH"
        assert len(parsed["actionable_markets"]) == 1

    def test_json_in_code_block(self):
        inner = json.dumps({
            "alert_priority": "LOW",
            "summary": "Nothing notable.",
            "actionable_markets": [],
        })
        payload = f"```json\n{inner}\n```"
        parsed = ClaudeAnalyzer.parse_response_text(payload)
        assert parsed["status"] == "analyzed"
        assert parsed["alert_priority"] == "LOW"

    def test_garbage_text(self):
        parsed = ClaudeAnalyzer.parse_response_text("This is total garbage!@#$%")
        assert parsed["status"] == "error"
        assert "Failed to parse" in parsed["error_message"]

    def test_missing_required_keys(self):
        payload = json.dumps({"alert_priority": "HIGH"})
        parsed = ClaudeAnalyzer.parse_response_text(payload)
        assert parsed["status"] == "error"
        assert "Missing keys" in parsed["error_message"]

    def test_empty_string(self):
        parsed = ClaudeAnalyzer.parse_response_text("")
        assert parsed["status"] == "error"

    def test_valid_json_preserves_token_counts(self):
        payload = json.dumps({
            "alert_priority": "LOW",
            "summary": "ok",
            "actionable_markets": [],
        })
        parsed = ClaudeAnalyzer.parse_response_text(payload, 999, 111)
        assert parsed["input_tokens"] == 999
        assert parsed["output_tokens"] == 111
