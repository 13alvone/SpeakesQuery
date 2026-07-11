"""
QueryEngine scheduled-run logging - empty vs error distinction.

Pre-2026-07-01, ``execute_query`` used the legacy ``process_query``
which collapses "query produced zero rows" and "query failed" into
``(None, None)``. Every legitimately-quiet day was then logged to
``search_runs`` as ``status="error", error_message="process_query
returned None"`` - so the schedule report showed four feeders with
all-null row counts (" - ") and the operator could not tell which (if
any) were actually broken.

These tests pin the fix: ``execute_query`` uses
``process_query_with_diagnostics`` and logs

  - ``status="empty", row_count=0`` when the diagnostic starts with
    ``empty:`` (a quiet day - NOT an error), and
  - ``status="error"`` with the REAL diagnostic message otherwise.

Patch-target note (see ``reference_money_leak_canary_patch_target``):
``QueryEngine`` binds ``process_query_with_diagnostics`` at module
import, so tests must patch ``query_engine.QueryEngine.<name>`` - not
the source module.
"""

import asyncio

import pytest


@pytest.fixture()
def run_log_capture(monkeypatch):
    """Patch the search_runs emitter and the query backend binding."""
    from query_engine import QueryEngine

    calls = []

    def fake_emit(search_name, status, *, row_count=None,
                  duration_ms=None, error_message=None):
        calls.append({
            "search_name": search_name,
            "status": status,
            "row_count": row_count,
            "duration_ms": duration_ms,
            "error_message": error_message,
        })

    monkeypatch.setattr(QueryEngine, "_emit_search_run_log", fake_emit)
    return calls


def _run(coro):
    return asyncio.run(coro)


class TestEmptyVsErrorLogging:

    def test_empty_diagnostic_logs_empty_not_error(
        self, monkeypatch, run_log_capture,
    ):
        from query_engine import QueryEngine

        monkeypatch.setattr(
            QueryEngine, "process_query_with_diagnostics",
            lambda q: (None, None, "empty: query produced zero rows"),
        )
        _run(QueryEngine.execute_query(1, 'index="x" | head 1', "quiet_feeder"))

        assert len(run_log_capture) == 1
        entry = run_log_capture[0]
        assert entry["status"] == "empty", (
            "A zero-row result is a valid quiet day - it must be logged "
            f"status='empty', got {entry['status']!r}"
        )
        assert entry["row_count"] == 0
        assert entry["error_message"] is None

    def test_error_diagnostic_logs_error_with_reason(
        self, monkeypatch, run_log_capture,
    ):
        from query_engine import QueryEngine

        monkeypatch.setattr(
            QueryEngine, "process_query_with_diagnostics",
            lambda q: (None, None, "KeyError: 'severity_rank'"),
        )
        _run(QueryEngine.execute_query(2, 'index="x" | head 1', "broken_feeder"))

        assert len(run_log_capture) == 1
        entry = run_log_capture[0]
        assert entry["status"] == "error"
        assert "severity_rank" in (entry["error_message"] or ""), (
            "The real diagnostic must land in error_message - a generic "
            "'process_query returned None' hides the actual failure"
        )

    def test_none_result_without_diagnostic_still_logs_error(
        self, monkeypatch, run_log_capture,
    ):
        from query_engine import QueryEngine

        monkeypatch.setattr(
            QueryEngine, "process_query_with_diagnostics",
            lambda q: (None, None, None),
        )
        _run(QueryEngine.execute_query(3, 'index="x" | head 1', "odd_feeder"))

        assert len(run_log_capture) == 1
        entry = run_log_capture[0]
        assert entry["status"] == "error"
        assert entry["error_message"] == "process_query returned None"

    def test_engine_module_does_not_use_legacy_process_query(self):
        """Drift guard: the scheduler must call the diagnostics variant.
        The legacy ``process_query`` swallows the failure reason - the
        CLAUDE.md convention reserves it for callers that don't log."""
        import inspect
        from query_engine import QueryEngine

        src = inspect.getsource(QueryEngine.execute_query)
        assert "process_query_with_diagnostics(" in src
        assert "process_query(" not in src.replace(
            "process_query_with_diagnostics(", ""
        ), "execute_query must not fall back to legacy process_query"
