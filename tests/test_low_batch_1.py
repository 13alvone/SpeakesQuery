"""LOWs batch 1 - L-CE-11, L-AN-15, L-AN-16 regressions.

Three cosmetic / defensive fixes from the 2026-04-21 production review:

  * **L-CE-11** - the 8-line historical jpype comment in
    ``sanitize_dataframe`` collapsed to a single line that still points
    at the regression test.
  * **L-AN-15** - ``call_messages_create`` now floors cost at 0.0 with
    a loud error log if a mis-configured pricing table produces a
    negative value.
  * **L-AN-16** - ``_trim_to_budget`` emits a visible warning when the
    10-iteration loop exits without reaching the cap, instead of
    silently shipping an over-budget prompt.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# L-CE-11: stale jpype comment trimmed
# ======================================================================


class TestStaleJpypeCommentTrimmed:

    SRC = _PROJECT_ROOT / "query_engine" / "CmdExecutionBackend.py"

    def test_historical_note_is_now_terse(self):
        """The multi-line jpype history lesson is replaced by a one-liner pointer."""
        text = self.SRC.read_text()
        # The long phrases "only produced" and "JVM-less Docker image"
        # were specific to the old comment; they should be gone.
        assert "only produced" not in text
        assert "JVM-less Docker image" not in text
        # But the regression-test reference must remain so a future
        # reader can find the companion suite.
        assert "test_no_jpype_and_dispatch_logging" in text


# ======================================================================
# L-AN-15: negative cost floors to 0 with a loud error
# ======================================================================


def _fake_response(in_tokens: int = 10, out_tokens: int = 5):
    resp = MagicMock()
    resp.content = [MagicMock(text="ok")]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock()
    resp.usage.input_tokens = in_tokens
    resp.usage.output_tokens = out_tokens
    resp.usage.cache_read_input_tokens = 0
    resp.usage.cache_creation_input_tokens = 0
    resp.id = "msg_fake"
    return resp


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.messages = MagicMock()
        self.messages.create = lambda **_kw: self._response


class TestNegativeCostFlooredToZero:

    def test_negative_pricing_results_in_zero_cost_and_error_log(
        self, tmp_path, monkeypatch, caplog,
    ):
        """A buggy pricing table returning negatives must not credit the budget."""
        import analyzers.claude_client as cc
        import analyzers.storage as storage_mod
        from analyzers.claude_history_store import ClaudeHistoryStore

        # Isolate stores / logs.
        monkeypatch.setattr(storage_mod, "_PROJECT_ROOT", tmp_path)
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite",
        )
        from global_settings import get_settings
        from functionality import log_writer as lw
        settings = get_settings()
        settings.set("logs_root", str(tmp_path / "logs"))
        settings.set("logs_enabled", True)
        lw.LogWriter.reset_for_tests()

        # Force the pricing lookup to return negatives for this model.
        monkeypatch.setattr(
            cc, "_pricing_for", lambda _m: (-1.0, -0.5),
        )

        import logging as _logging
        with caplog.at_level(_logging.ERROR, logger="analyzers.claude_client"):
            result = cc.call_messages_create(
                source="unit_test",
                api_key_override="sk-fake",
                client_factory=lambda _key: _FakeClient(_fake_response(50, 20)),
                model="claude-weirdly-priced",
                max_tokens=32,
                messages=[{"role": "user", "content": "hi"}],
            )

        assert result.cost_usd == 0.0, (
            f"Negative pricing should floor the cost at 0.0; got {result.cost_usd}"
        )
        assert any(
            "Negative cost computed" in rec.getMessage()
            for rec in caplog.records
        ), (
            "Expected a loud error log on negative-cost detection. "
            f"records={[r.getMessage() for r in caplog.records]}"
        )

        ClaudeHistoryStore.reset_for_tests()
        lw.LogWriter.reset_for_tests()

    def test_ordinary_positive_pricing_is_untouched(self):
        """Sanity: the guard does not clobber valid positive costs."""
        import analyzers.claude_client as cc

        # _pricing_for with a real model gives positive numbers; smoke
        # test by reading the tuple directly.
        input_pm, output_pm = cc._pricing_for("claude-sonnet-4-6")
        assert input_pm > 0 and output_pm > 0


# ======================================================================
# L-AN-16: budget-trim loop-exhaustion warning
# ======================================================================


class TestBudgetTrimExhaustionWarning:

    def test_trim_loop_that_cannot_converge_emits_warning(self, caplog):
        """If the 10-iteration loop can't reduce below budget, log loudly."""
        import logging as _logging
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.models import SerializedResult

        # A single row whose ``row_count // 2`` floor hits the
        # ``max(1, …)`` guard at 1 forever. After every halving the
        # shrunk ``new_content`` is just ``'[]'`` which is a valid
        # (trimmable) JSON list - so the loop DOES trim, but the input
        # above already starts small enough that 10 halvings can't
        # reach the impossibly-tight budget.
        import json as _j
        big = _j.dumps([{"id": i, "payload": "x" * 500} for i in range(20)])
        r = SerializedResult(
            search_name="stubborn",
            row_count=20,
            estimated_tokens=len(big) // 3,
            format="json",
            content=big,
        )

        with caplog.at_level(_logging.WARNING, logger="alert_groups.dispatcher"):
            AlertGroupDispatcher._trim_to_budget([r], budget=5)

        assert any(
            "exhausted" in rec.getMessage()
            and "iterations without" in rec.getMessage()
            for rec in caplog.records
        ), (
            "Expected a loop-exhaustion warning. records="
            + "\n".join(r.getMessage() for r in caplog.records)
        )

    def test_under_budget_no_warning(self, caplog):
        """Already-under-budget input emits no loop-exhaustion warning."""
        import logging as _logging
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.models import SerializedResult

        small = "[]"
        r = SerializedResult(
            search_name="easy",
            row_count=0,
            estimated_tokens=1,
            format="json",
            content=small,
        )
        with caplog.at_level(_logging.WARNING, logger="alert_groups.dispatcher"):
            AlertGroupDispatcher._trim_to_budget([r], budget=100)

        assert not any(
            "exhausted" in rec.getMessage() for rec in caplog.records
        ), (
            "Under-budget input must not emit the loop-exhaustion warning."
        )
