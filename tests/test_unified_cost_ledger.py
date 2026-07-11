"""H-AN-6 / X-2 regression: single daily-budget ledger for every Claude caller.

Before 2026-04-22 the analyzer's budget gate only saw calls routed through
``ClaudeAnalyzer._record_usage``. Alert-group dispatcher calls went through
``claude_client.call_messages_create`` directly and bypassed the per-day
counter, so runaway AG schedules could exhaust the daily budget without
tripping the gate.

After the fix every successful ``call_messages_create`` increments the
``analyzer_budget`` SQLite table via ``_record_daily_budget_usd``. The
``ClaudeAnalyzer`` budget gate re-reads the table on each call so
cross-caller spend is visible.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _fake_response(in_tokens: int = 100, out_tokens: int = 50):
    """Minimal duck-typed anthropic response."""
    resp = MagicMock()
    resp.content = [MagicMock(text="ok")]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock()
    resp.usage.input_tokens = in_tokens
    resp.usage.output_tokens = out_tokens
    resp.usage.cache_read_input_tokens = 0
    resp.usage.cache_creation_input_tokens = 0
    resp.id = "msg_fake_123"
    return resp


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.messages = MagicMock()
        self.messages.create = lambda **_kw: self._response


@pytest.fixture
def tmp_ledger_db(tmp_path, monkeypatch):
    """Point AnalyzerStorage + ClaudeHistoryStore at tmp paths for isolation."""
    # Swap the default analyzer DB location.
    db_path = tmp_path / "analyzer_results.sqlite"
    import analyzers.storage as storage_mod
    monkeypatch.setattr(storage_mod, "_PROJECT_ROOT", tmp_path)

    # Also redirect the Claude history store (written by _record_attempt).
    from analyzers.claude_history_store import ClaudeHistoryStore
    ClaudeHistoryStore._instance = ClaudeHistoryStore(
        db_path=tmp_path / "hist.sqlite",
    )

    # The log writer is initialised by claude_client - redirect too.
    from global_settings import get_settings
    from functionality import log_writer as lw
    settings = get_settings()
    orig_root = settings.get("logs_root")
    orig_enabled = settings.get("logs_enabled")
    settings.set("logs_root", str(tmp_path / "logs"))
    settings.set("logs_enabled", True)
    lw.LogWriter.reset_for_tests()

    yield db_path

    try:
        settings.set("logs_root", orig_root)
        settings.set("logs_enabled", orig_enabled)
    except Exception:
        pass
    lw.LogWriter.reset_for_tests()
    ClaudeHistoryStore.reset_for_tests()


class TestClaudeClientWritesLedger:

    def test_successful_call_increments_analyzer_budget(self, tmp_ledger_db):
        """A single call_messages_create success must land in analyzer_budget."""
        import analyzers.claude_client as cc

        result = cc.call_messages_create(
            source="unit_test",
            api_key_override="sk-fake",
            client_factory=lambda _key: _FakeClient(_fake_response(120, 30)),
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result.input_tokens == 120 and result.output_tokens == 30

        # Read back directly from storage.
        from analyzers.storage import AnalyzerStorage
        storage = AnalyzerStorage()
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        row = storage.load_daily_budget(today)
        assert row["total_calls"] == 1
        assert row["total_input_tokens"] == 120
        assert row["total_output_tokens"] == 30
        assert row["total_cost_cents"] > 0, (
            f"Non-zero cost should be recorded; got {row['total_cost_cents']}"
        )

    def test_two_calls_sum_in_ledger(self, tmp_ledger_db):
        """Two successive calls from different sources must both land in the counter."""
        import analyzers.claude_client as cc

        # Call #1 (scheduled-search path)
        cc.call_messages_create(
            source="analyzer",
            api_key_override="sk-fake",
            client_factory=lambda _k: _FakeClient(_fake_response(100, 20)),
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "a"}],
        )
        # Call #2 (AG dispatcher path - same cost model, different source tag)
        cc.call_messages_create(
            source="alert_group",
            group_name="ag_test",
            api_key_override="sk-fake",
            client_factory=lambda _k: _FakeClient(_fake_response(200, 60)),
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "b"}],
        )

        from analyzers.storage import AnalyzerStorage
        storage = AnalyzerStorage()
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        row = storage.load_daily_budget(today)
        assert row["total_calls"] == 2
        assert row["total_input_tokens"] == 300  # 100 + 200
        assert row["total_output_tokens"] == 80   # 20 + 60

    def test_ledger_write_failure_does_not_break_call(self, tmp_ledger_db, monkeypatch):
        """If the ledger write raises, the Claude call still succeeds."""
        import analyzers.claude_client as cc
        import analyzers.storage as storage_mod

        # Force AnalyzerStorage construction to raise.
        original_init = storage_mod.AnalyzerStorage.__init__

        def boom(self, *args, **kwargs):
            raise RuntimeError("simulated ledger unavailability")

        monkeypatch.setattr(storage_mod.AnalyzerStorage, "__init__", boom)

        # Call should still return a valid result.
        result = cc.call_messages_create(
            source="resilience_test",
            api_key_override="sk-fake",
            client_factory=lambda _k: _FakeClient(_fake_response(10, 5)),
            model="claude-sonnet-4-6",
            max_tokens=16,
            messages=[{"role": "user", "content": "ok"}],
        )
        assert result.input_tokens == 10
        assert result.output_tokens == 5

        # Restore for subsequent tests.
        monkeypatch.setattr(storage_mod.AnalyzerStorage, "__init__", original_init)


class TestClaudeAnalyzerReadsSharedLedger:
    """ClaudeAnalyzer's budget gate must observe AG-dispatcher writes."""

    def test_ag_path_writes_visible_to_analyzer(self, tmp_ledger_db):
        """A claude_client call (simulating AG path) must decrement what ClaudeAnalyzer sees."""
        import analyzers.claude_client as cc
        from analyzers.claude_analyzer import ClaudeAnalyzer
        from analyzers.models import AnalyzerConfig
        from analyzers.storage import AnalyzerStorage

        # Make a single AG-path call that costs >0 cents.
        cc.call_messages_create(
            source="alert_group",
            group_name="ag_x",
            api_key_override="sk-fake",
            client_factory=lambda _k: _FakeClient(_fake_response(500, 100)),
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "b"}],
        )

        # Now spin up a fresh ClaudeAnalyzer - its budget gate should
        # reflect the AG call's cost via storage, not an empty in-memory
        # counter.
        storage = AnalyzerStorage()
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        persisted_cost = storage.load_daily_budget(today)["total_cost_cents"]
        assert persisted_cost > 0, (
            "AG-path call did not reach the ledger; check _record_daily_budget_usd wiring."
        )

        config = AnalyzerConfig(daily_budget_cents=100)
        analyzer = ClaudeAnalyzer(config=config, storage=storage)
        stats = analyzer.get_usage_stats()
        assert abs(stats.total_cost_cents - persisted_cost) < 1e-6, (
            f"ClaudeAnalyzer.get_usage_stats should match ledger; "
            f"got {stats.total_cost_cents}, ledger={persisted_cost}"
        )
        assert stats.budget_remaining_cents == (100.0 - persisted_cost)

    def test_tiny_budget_gate_blocks_after_ag_call(self, tmp_ledger_db):
        """A 0.01-cent budget must trip the gate after any AG call lands in the ledger."""
        import analyzers.claude_client as cc
        from analyzers.claude_analyzer import ClaudeAnalyzer
        from analyzers.models import AnalyzerConfig
        from analyzers.storage import AnalyzerStorage

        # AG-path call that certainly exceeds 0.01 cents.
        cc.call_messages_create(
            source="alert_group",
            group_name="ag_gate",
            api_key_override="sk-fake",
            client_factory=lambda _k: _FakeClient(_fake_response(1000, 200)),
            model="claude-sonnet-4-6",
            max_tokens=64,
            messages=[{"role": "user", "content": "b"}],
        )

        # Now the analyzer's gate, with a 0.01-cent budget, must block.
        # Supply a fake api_key so the gate reaches the budget branch
        # (Gate 1 of _gate_check short-circuits on missing API key).
        storage = AnalyzerStorage()
        config = AnalyzerConfig(daily_budget_cents=0, api_key="sk-fake-for-gate")
        analyzer = ClaudeAnalyzer(config=config, storage=storage)
        reason = analyzer._gate_check([{"market_id": "x", "liquidity": 100_000}])
        assert reason == "budget_exceeded", (
            f"Expected budget_exceeded; got {reason!r}. "
            f"Stats: {analyzer.get_usage_stats()}"
        )

    def test_analyzer_does_not_double_write_ledger(self, tmp_ledger_db):
        """ClaudeAnalyzer's _record_usage must NOT write to storage (claude_client owns it)."""
        from analyzers.claude_analyzer import ClaudeAnalyzer
        from analyzers.models import AnalyzerConfig
        from analyzers.storage import AnalyzerStorage

        storage = AnalyzerStorage()
        config = AnalyzerConfig(daily_budget_cents=100)
        analyzer = ClaudeAnalyzer(config=config, storage=storage)

        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        before = storage.load_daily_budget(today)["total_calls"]

        # Invoke _record_usage directly - simulates the in-memory telemetry
        # update that used to also write to storage.
        analyzer._record_usage(
            model="claude-sonnet-4-6",
            input_tokens=50,
            output_tokens=10,
        )

        after = storage.load_daily_budget(today)["total_calls"]
        assert after == before, (
            f"_record_usage must no longer write to analyzer_budget "
            f"(claude_client now owns it); before={before}, after={after}"
        )
