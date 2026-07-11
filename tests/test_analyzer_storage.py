#!/usr/bin/env python3
"""
Unit tests for the AnalyzerStorage SQLite persistence layer.

All tests run without network access.  They validate:
  - Analysis result round-trip (store / retrieve / filter / order / limit)
  - Budget tracking (load / increment / accumulate / re-open / daily reset)
  - Batch request lifecycle (create / pending IDs / mark complete / lookup)
  - Concurrent budget writes from multiple threads

Each test class uses a fresh SQLite database via the ``tmp_path`` fixture.
"""

import os
import sys
import threading
from datetime import datetime

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from analyzers.storage import AnalyzerStorage
from analyzers.models import AnalysisResult


# =====================================================================
# Helpers
# =====================================================================

def _make_result(**overrides) -> AnalysisResult:
    """Return an AnalysisResult with sensible defaults, accepting overrides."""
    defaults = dict(
        status="analyzed",
        alert_priority="HIGH",
        summary="Volume spike detected.",
        model_used="claude-haiku-4-5-20251001",
        input_tokens=500,
        output_tokens=200,
        cost_cents=0.35,
        filter_passed=True,
        filter_answer="YES",
        skip_reason="",
        error_message="",
        batch_id="",
    )
    defaults.update(overrides)
    return AnalysisResult(**defaults)


# =====================================================================
# TestAnalyzerResultStore
# =====================================================================

class TestAnalyzerResultStore:
    """Store and retrieve analysis results."""

    @pytest.fixture(autouse=True)
    def setup_storage(self, tmp_path):
        self.db_path = str(tmp_path / "test.sqlite")
        self.storage = AnalyzerStorage(db_path=self.db_path)

    def test_store_and_retrieve_roundtrip(self):
        """A stored result should be retrievable with all key fields intact."""
        result = _make_result(summary="Arsenal spike detected.")
        self.storage.store_result("volume_spikes", "2026-04-07T12:00:00", result)

        rows = self.storage.get_results()
        assert len(rows) == 1
        row = rows[0]
        assert row["search_name"] == "volume_spikes"
        assert row["execution_time"] == "2026-04-07T12:00:00"
        assert row["status"] == "analyzed"
        assert row["alert_priority"] == "HIGH"
        assert row["summary"] == "Arsenal spike detected."
        assert row["model_used"] == "claude-haiku-4-5-20251001"
        assert row["input_tokens"] == 500
        assert row["output_tokens"] == 200
        assert abs(row["cost_cents"] - 0.35) < 0.001
        assert row["filter_passed"] == 1  # stored as integer

    def test_filter_by_search_name(self):
        """get_results(search_name=...) returns only matching rows."""
        self.storage.store_result("alpha", "t1", _make_result(summary="A"))
        self.storage.store_result("beta", "t2", _make_result(summary="B"))
        self.storage.store_result("alpha", "t3", _make_result(summary="C"))

        alpha_rows = self.storage.get_results(search_name="alpha")
        assert len(alpha_rows) == 2
        assert all(r["search_name"] == "alpha" for r in alpha_rows)

        beta_rows = self.storage.get_results(search_name="beta")
        assert len(beta_rows) == 1
        assert beta_rows[0]["summary"] == "B"

    def test_results_ordered_by_created_at_desc(self):
        """Most recent results should appear first."""
        for i in range(5):
            self.storage.store_result(
                "search", f"t{i}", _make_result(summary=f"Result {i}")
            )

        rows = self.storage.get_results()
        # created_at timestamps are sequential; last inserted should be first
        assert rows[0]["summary"] == "Result 4"
        assert rows[-1]["summary"] == "Result 0"

    def test_limit_parameter(self):
        """The limit parameter caps how many rows are returned."""
        for i in range(10):
            self.storage.store_result("s", f"t{i}", _make_result())

        rows = self.storage.get_results(limit=3)
        assert len(rows) == 3

    def test_store_result_never_raises(self):
        """store_result should silently handle bad data without raising."""
        # Pass an object missing expected attributes
        class FakeResult:
            pass

        fake = FakeResult()
        # Should not raise
        self.storage.store_result("bad", "t0", fake)

        # DB should still be functional afterward
        self.storage.store_result("good", "t1", _make_result())
        rows = self.storage.get_results()
        assert len(rows) == 1
        assert rows[0]["search_name"] == "good"


# =====================================================================
# TestBudgetPersistence
# =====================================================================

class TestBudgetPersistence:
    """Daily budget tracking via load_daily_budget / record_usage."""

    @pytest.fixture(autouse=True)
    def setup_storage(self, tmp_path):
        self.db_path = str(tmp_path / "test.sqlite")
        self.storage = AnalyzerStorage(db_path=self.db_path)

    def test_load_budget_new_date_returns_zeroed(self):
        """A date with no recorded usage returns a zeroed budget dict."""
        budget = self.storage.load_daily_budget("2026-04-07")
        assert budget["date"] == "2026-04-07"
        assert budget["total_input_tokens"] == 0
        assert budget["total_output_tokens"] == 0
        assert budget["total_calls"] == 0
        assert budget["total_cost_cents"] == 0.0

    def test_record_usage_increments(self):
        """A single record_usage call creates the budget row with correct values."""
        self.storage.record_usage("2026-04-07", 1000, 500, 0.35)
        budget = self.storage.load_daily_budget("2026-04-07")
        assert budget["total_input_tokens"] == 1000
        assert budget["total_output_tokens"] == 500
        assert budget["total_calls"] == 1
        assert abs(budget["total_cost_cents"] - 0.35) < 0.001

    def test_multiple_record_usage_accumulates(self):
        """Multiple record_usage calls for the same date accumulate."""
        self.storage.record_usage("2026-04-07", 1000, 500, 0.35)
        self.storage.record_usage("2026-04-07", 2000, 1000, 1.05)
        self.storage.record_usage("2026-04-07", 500, 100, 0.10)

        budget = self.storage.load_daily_budget("2026-04-07")
        assert budget["total_input_tokens"] == 3500
        assert budget["total_output_tokens"] == 1600
        assert budget["total_calls"] == 3
        assert abs(budget["total_cost_cents"] - 1.50) < 0.001

    def test_budget_survives_new_instance(self, tmp_path):
        """Budget data persists when a new AnalyzerStorage opens the same DB."""
        db_path = str(tmp_path / "persist.sqlite")
        storage1 = AnalyzerStorage(db_path=db_path)
        storage1.record_usage("2026-04-07", 1000, 500, 0.35)

        # Open a fresh instance against the same file
        storage2 = AnalyzerStorage(db_path=db_path)
        budget = storage2.load_daily_budget("2026-04-07")
        assert budget["total_input_tokens"] == 1000
        assert budget["total_calls"] == 1

    def test_daily_reset_separate_dates(self):
        """Different date_str values create independent budget entries."""
        self.storage.record_usage("2026-04-07", 1000, 500, 0.35)
        self.storage.record_usage("2026-04-08", 2000, 1000, 1.05)

        day1 = self.storage.load_daily_budget("2026-04-07")
        day2 = self.storage.load_daily_budget("2026-04-08")

        assert day1["total_input_tokens"] == 1000
        assert day1["total_calls"] == 1
        assert day2["total_input_tokens"] == 2000
        assert day2["total_calls"] == 1


# =====================================================================
# TestBatchRequestStore
# =====================================================================

class TestBatchRequestStore:
    """Batch request lifecycle: create, query, complete."""

    @pytest.fixture(autouse=True)
    def setup_storage(self, tmp_path):
        self.db_path = str(tmp_path / "test.sqlite")
        self.storage = AnalyzerStorage(db_path=self.db_path)

    def _create_request(self, custom_id="req-001", batch_id="batch-abc",
                        search_name="volume_spikes", **kwargs):
        defaults = dict(
            model="claude-haiku-4-5-20251001",
            system_prompt="You are a financial analyst.",
            user_content='[{"question": "Test?"}]',
            search_metadata={"name": search_name},
            result_parquet_path="/tmp/results.parquet",
            filter_enabled=False,
            filter_question="",
        )
        defaults.update(kwargs)
        self.storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name=search_name,
            **defaults,
        )

    def test_create_and_retrieve(self):
        """A created batch request can be fetched by custom_id."""
        self._create_request(custom_id="req-001", batch_id="batch-abc")

        req = self.storage.get_request("req-001")
        assert req is not None
        assert req["custom_id"] == "req-001"
        assert req["batch_id"] == "batch-abc"
        assert req["search_name"] == "volume_spikes"
        assert req["status"] == "submitted"
        assert req["model"] == "claude-haiku-4-5-20251001"
        assert req["created_at"] != ""

    def test_get_pending_batch_ids_only_submitted(self):
        """get_pending_batch_ids returns only batch IDs with status='submitted'."""
        self._create_request(custom_id="req-001", batch_id="batch-1")
        self._create_request(custom_id="req-002", batch_id="batch-2")
        self._create_request(custom_id="req-003", batch_id="batch-1")

        # Mark one batch as completed
        self.storage.mark_batch_completed("req-001", "completed", '{"ok": true}')
        self.storage.mark_batch_completed("req-003", "completed", '{"ok": true}')

        pending = self.storage.get_pending_batch_ids()
        assert "batch-2" in pending
        assert "batch-1" not in pending  # all requests in batch-1 are completed

    def test_mark_batch_completed_updates_status(self):
        """mark_batch_completed sets status, completed_at, and result_json."""
        self._create_request(custom_id="req-001")
        self.storage.mark_batch_completed("req-001", "completed", '{"summary": "done"}')

        req = self.storage.get_request("req-001")
        assert req["status"] == "completed"
        assert req["completed_at"] != ""
        assert req["result_json"] == '{"summary": "done"}'

    def test_get_requests_for_batch(self):
        """get_requests_for_batch returns all requests sharing a batch_id."""
        self._create_request(custom_id="req-001", batch_id="batch-abc")
        self._create_request(custom_id="req-002", batch_id="batch-abc")
        self._create_request(custom_id="req-003", batch_id="batch-xyz")

        batch_abc = self.storage.get_requests_for_batch("batch-abc")
        assert len(batch_abc) == 2
        ids = {r["custom_id"] for r in batch_abc}
        assert ids == {"req-001", "req-002"}

        batch_xyz = self.storage.get_requests_for_batch("batch-xyz")
        assert len(batch_xyz) == 1

    def test_get_request_nonexistent_returns_none(self):
        """get_request for a missing custom_id returns None."""
        assert self.storage.get_request("does-not-exist") is None


# =====================================================================
# TestConcurrentBudget
# =====================================================================

class TestConcurrentBudget:
    """Thread-safety of record_usage under concurrent writes."""

    def test_concurrent_budget_accumulation(self, tmp_path):
        """10 threads each recording usage -- totals must equal the sum of all."""
        db_path = str(tmp_path / "concurrent.sqlite")
        storage = AnalyzerStorage(db_path=db_path)

        num_threads = 10
        input_per_call = 100
        output_per_call = 50
        cost_per_call = 0.10
        date_str = "2026-04-07"

        errors = []

        def worker():
            try:
                storage.record_usage(date_str, input_per_call, output_per_call, cost_per_call)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Threads raised errors: {errors}"

        budget = storage.load_daily_budget(date_str)
        assert budget["total_input_tokens"] == input_per_call * num_threads
        assert budget["total_output_tokens"] == output_per_call * num_threads
        assert budget["total_calls"] == num_threads
        assert abs(budget["total_cost_cents"] - cost_per_call * num_threads) < 0.001
