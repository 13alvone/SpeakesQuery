"""
Regression test - pipe handlers must tolerate a ``None`` upstream state.

The canonical rule (see ``reference_empty_df_pipe_handler_contract.md``)
says every SPQL pipe handler treats an empty DataFrame as a valid state
and returns an empty well-shaped output. Caught 2026-04-23 via
``tools/diagnose_alert_group``: when the index subdirectory does not
exist yet (day-1 of a freshly-deployed feeder), the DuckDB loader
sometimes yields ``None`` instead of an empty DataFrame, and handlers
that only guarded against ``df.empty`` crashed.

These tests pin the None-tolerance contract so the handlers can't
regress and silently blow up day-1 dispatches.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


class TestEvalHandlerNoneTolerance:
    def test_run_eval_returns_empty_df_when_input_is_none(self):
        from handlers.EvalHandler import EvalHandler
        out = EvalHandler().run_eval(["eval", "now_epoch = now()"], None)
        assert isinstance(out, pd.DataFrame)
        assert out.empty

    def test_run_eval_preserves_target_column_on_empty_df(self):
        """An empty DF going through ``eval foo = ...`` should come out
        with ``foo`` in its column list so downstream ``where foo > 0``
        doesn't raise UndefinedVariableError."""
        from handlers.EvalHandler import EvalHandler
        df = pd.DataFrame()
        out = EvalHandler().run_eval(["eval", "ago_24h = now() - 86400"], df)
        assert "ago_24h" in out.columns
        assert len(out) == 0

    def test_run_eval_still_works_with_populated_df(self):
        """Non-empty path must be unaffected by the None-tolerance guard."""
        from handlers.EvalHandler import EvalHandler
        df = pd.DataFrame({"x": [1, 2, 3]})
        out = EvalHandler().run_eval(["eval", "y = x + 1"], df)
        assert list(out["y"]) == [2, 3, 4]


class TestSortHandlerNoneTolerance:
    def test_sort_df_by_columns_returns_empty_on_none(self):
        from handlers.GeneralHandler import GeneralHandler
        out = GeneralHandler.sort_df_by_columns(None, ["a"], is_ascending="-")
        assert isinstance(out, pd.DataFrame)
        assert out.empty

    def test_sort_df_by_columns_unchanged_on_empty(self):
        from handlers.GeneralHandler import GeneralHandler
        df = pd.DataFrame({"a": [], "b": []})
        out = GeneralHandler.sort_df_by_columns(df, ["a"], is_ascending="+")
        assert list(out.columns) == ["a", "b"]
        assert len(out) == 0

    def test_sort_df_by_columns_still_sorts_populated_df(self):
        from handlers.GeneralHandler import GeneralHandler
        df = pd.DataFrame({"a": [3, 1, 2]})
        out = GeneralHandler.sort_df_by_columns(df, ["a"], is_ascending="+")
        assert list(out["a"]) == [1, 2, 3]


class TestDay1DispatchScenario:
    """End-to-end check: a feeder whose index subdirectory does not
    exist yet must be queryable without crashing. The query just
    returns zero rows - that's the correct dispatch-friendly state."""

    def test_query_against_nonexistent_index_returns_empty(self, tmp_path, monkeypatch):
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics

        # Point the index root at an empty tmp dir so every feeder we
        # query is guaranteed to have zero parquet files - simulating
        # a freshly-deployed AG where ingestion has not yet fired.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "indexes").mkdir()

        query = (
            'index="indexes/this/does/not/exist/*.parquet"\n'
            '| sort -_epoch\n'
            '| dedup some_key\n'
            '| eval ago_24h = now() - 86400\n'
            '| where _epoch >= ago_24h\n'
            '| table some_key, ago_24h\n'
            '| head 10'
        )
        df, _job, diagnostic = process_query_with_diagnostics(query)
        # Expected: either (a) empty DataFrame or (b) df=None with an
        # ``empty:...`` diagnostic (the canonical "no rows" signal).
        # Both are acceptable - the crucial thing is no hard error
        # like ``AttributeError: 'NoneType' object has no attribute
        # 'columns'`` that would surface as ``diagnostic = 'error:...'``.
        hard_error = diagnostic and not (
            diagnostic == "" or diagnostic.startswith("empty")
        )
        assert not hard_error, (
            f"Day-1 scenario (no parquet yet) must not hard-error. "
            f"diagnostic={diagnostic!r}"
        )
        if df is not None:
            assert len(df) == 0, (
                f"Expected zero rows on missing index, got {len(df)}"
            )
