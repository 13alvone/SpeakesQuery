#!/usr/bin/env python3
"""
Regression tests for the 2026-04-21 "Manual Run never completes" fix
batch. The user ran the default Daily Opportunity Brief alert group
and, though the UI helpfully said "Dispatching to Claude... 1-8 minutes",
nothing was ever sent. Five independent bugs were layered:

1. **Claude 120s timeout hit on every attempt** with retries just burning
   8 minutes against the same wall. Fix: raise default to 600s AND
   remove ``APITimeoutError`` from the retryable class.

2. **Dispatcher swallowed real query errors** and logged a misleading
   ``No cached result found for search "X"``. Fix: add
   ``process_query_with_diagnostics`` that propagates the reason;
   dispatcher logs it verbatim.

3. **SPQL ``where`` / ``table`` / ``sort`` crashed on empty DataFrames**
   when an ingestion produced zero rows and the parquet landed with only
   ``_epoch`` as a column. Fix: short-circuit each handler on empty.

4. **Installed saved_searches drifted from the shipped templates**
   (``sort -amount_usd`` after ``| table`` dropped the column,
   ``is_edge_zone=true`` missing proper SPQL quoting, etc.) and
   ``install_default`` refused to overwrite them. Fix: ``overwrite=True``
   param, REST query arg, UI "Sync Template" button, and a
   ``template_drift`` flag exposed on Feeder Health.

5. **Kalshi script emitted zero-column parquet on empty results** -
   pandas can't infer columns from an empty list. Fix: explicit
   ``columns=EXPECTED_COLUMNS`` so even an empty ingestion day still
   carries the schema.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# =====================================================================
# Part 1: Claude timeout + no-retry-on-timeout
# =====================================================================

class TestClaudeTimeoutPolicy:

    def test_default_timeout_raised_to_600s(self):
        """The default ``claude_request_timeout_seconds`` must be high
        enough for a web_search-enabled analyst brief - 120s is not.
        This pins the default so a future compaction doesn't silently
        drop it back."""
        from global_settings import DEFAULTS
        assert DEFAULTS["claude_request_timeout_seconds"] >= 600, (
            "Default claude_request_timeout_seconds dropped below 600. "
            "web_search-enabled analyst briefs (Daily Opportunity Brief) "
            "legitimately take 2-5 minutes. Raising this is the correct "
            "fix - retries don't help when every attempt hits the same "
            "ceiling."
        )

    def test_timeout_ceiling_allows_1_hour(self):
        """The upper bound must allow an operator to configure a long
        timeout for especially heavy briefs (e.g. a 30-feeder AG)."""
        from global_settings import _INT_VALIDATORS
        lo, hi = _INT_VALIDATORS["claude_request_timeout_seconds"]
        assert hi >= 3600, (
            "claude_request_timeout_seconds ceiling dropped below 1h "
            "(3600s). Leave headroom for future heavier briefs - a "
            "30-feeder AG with 50+ web_search invocations can push "
            "15-30 minutes."
        )

    def test_api_timeout_error_not_retried(self):
        """``APITimeoutError`` must be classified non-retryable - any
        retry will hit the same wall and just burn budget."""
        from analyzers.claude_client import _is_retryable

        class _FakeTimeout(Exception):
            pass
        _FakeTimeout.__name__ = "APITimeoutError"
        assert _is_retryable(_FakeTimeout("wall")) is False, (
            "APITimeoutError is being retried again. The SDK raises it "
            "when the per-request ceiling expires; retrying just fires "
            "another attempt against the same timeout. The correct fix "
            "is to raise claude_request_timeout_seconds, not retry."
        )

    def test_connection_error_still_retried(self):
        """Connection errors are transient and should still retry."""
        from analyzers.claude_client import _is_retryable

        class _Conn(Exception):
            pass
        _Conn.__name__ = "APIConnectionError"
        assert _is_retryable(_Conn("down")) is True

    def test_rate_limit_still_retried(self):
        """429 / RateLimitError still retry - a short backoff lets the
        bucket replenish. Different problem from the timeout case."""
        from analyzers.claude_client import _is_retryable

        class _RL(Exception):
            pass
        _RL.__name__ = "RateLimitError"
        assert _is_retryable(_RL("slow down")) is True

    def test_timeout_error_message_points_at_setting(self):
        """When a call exhausts with APITimeoutError, the raised
        ClaudeCallError must name the knob the operator should raise.
        Self-documenting error per user's production-quality bar."""
        from analyzers.claude_client import call_messages_create, ClaudeCallError

        class _Timeout(Exception):
            pass
        _Timeout.__name__ = "APITimeoutError"

        class _FakeMessages:
            def create(self, **kwargs):
                raise _Timeout("Request timed out.")

        class _FakeClient:
            messages = _FakeMessages()

        with pytest.raises(ClaudeCallError) as exc_info:
            call_messages_create(
                source="unit_test",
                client_factory=lambda key: _FakeClient(),
                api_key_override="sk-test",
                model="claude-sonnet-4-6",
                max_tokens=16,
                messages=[{"role": "user", "content": "x"}],
            )
        msg = str(exc_info.value)
        assert "claude_request_timeout_seconds" in msg, (
            "Timeout error doesn't name the setting to raise. "
            f"Got: {msg}"
        )
        assert "Settings" in msg and "timeout" in msg.lower()


# =====================================================================
# Part 2: Dispatcher surfaces the real query error
# =====================================================================

class TestDispatcherErrorSurfacing:

    def test_process_query_with_diagnostics_success(self):
        """Round-trip: a query that returns rows returns (df, job_id, None)."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        df = pd.DataFrame({"x": [1], "_epoch": [0]})

        with patch(
            "query_engine.CmdExecutionBackend.execute_query", return_value=df,
        ):
            with patch(
                "query_engine.CmdExecutionBackend._job_store",
            ) as mock_js:
                mock_js.save_auto.return_value = "job-xyz"
                result_df, job_id, diag = process_query_with_diagnostics(
                    'index="indexes/x/*" | head 1'
                )
        assert result_df is not None
        assert job_id == "job-xyz"
        assert diag is None

    def test_process_query_with_diagnostics_empty_result(self):
        """A query that returns zero rows must flag it as ``empty:``
        rather than as an error."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        with patch(
            "query_engine.CmdExecutionBackend.execute_query",
            return_value=pd.DataFrame(),
        ):
            result_df, job_id, diag = process_query_with_diagnostics(
                'index="indexes/x/*"'
            )
        assert result_df is None
        assert job_id is None
        assert diag and diag.startswith("empty:"), (
            f"Expected 'empty:' diagnostic, got: {diag}"
        )

    def test_process_query_with_diagnostics_error_propagates(self):
        """When execute_query raises, the diagnostic carries the
        exception class + message for the operator."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics

        def _boom(*_a, **_kw):
            raise ValueError("divergence_pct not in columns")

        with patch(
            "query_engine.CmdExecutionBackend.execute_query", side_effect=_boom,
        ):
            result_df, job_id, diag = process_query_with_diagnostics(
                'index="x" | where divergence_pct > 1'
            )
        assert result_df is None
        assert diag == "ValueError: divergence_pct not in columns"

    def test_dispatcher_logs_actual_query_error_not_cache_miss(self, caplog):
        """The 2026-04-21 incident: when a feeder query errored, the
        dispatcher logged ``No cached result found for search "X"`` -
        completely hiding the real error. This test pins the new
        behaviour: the query error itself is logged with the feeder name."""
        import logging
        from alert_groups.dispatcher import AlertGroupDispatcher

        # Reset the dispatcher's class-level SavedSearchStore cache
        # (added 2026-04-21 for efficiency, but our patch below needs to
        # replace it to inject the fake store).
        AlertGroupDispatcher._reset_ss_store_cache()

        d = AlertGroupDispatcher()

        fake_store = MagicMock()
        fake_store.get_search.return_value = {
            "name": "bad_feeder",
            "query": 'index="x" | where divergence_pct > 1',
        }

        caplog.set_level(logging.INFO, logger="alert_groups.dispatcher")
        with patch(
            "saved_search_store.SavedSearchStore", return_value=fake_store,
        ):
            with patch(
                "query_engine.CmdExecutionBackend.process_query_with_diagnostics",
                return_value=(None, None,
                              "UndefinedVariableError: divergence_pct not defined"),
            ):
                result = d._execute_feeder_query_now(
                    "bad_feeder", group_name="ag_test",
                )

        assert result is None
        lines = [r.getMessage() for r in caplog.records]
        error_lines = [
            line for line in lines
            if "bad_feeder" in line and "UndefinedVariableError" in line
        ]
        assert error_lines, (
            "Dispatcher didn't log the actual query error with the "
            "feeder name. Lines captured: "
            + "\n".join(lines)
        )


# =====================================================================
# Part 3: Pipe handlers short-circuit on empty DataFrame
# =====================================================================

class TestEmptyDataFrameShortCircuit:

    def test_where_on_empty_df_returns_empty(self):
        """``where`` over an empty DataFrame (zero rows, arbitrary
        columns) must return empty without evaluating the query string
        - otherwise pandas raises UndefinedVariableError on unknown
        columns. Caught 2026-04-21 for dob_kalshi_poly_arb whose Parquet
        legitimately had zero rows today."""
        from handlers.SearchCmdHandler import SearchDirective

        handler = SearchDirective()
        empty = pd.DataFrame(columns=["_epoch"])  # no rows, no divergence_pct
        # Signature is ``run_search(search_tokens, df)``. This would
        # previously have raised "name 'divergence_pct' is not defined"
        # and been caught by the handler's broad except. Now
        # short-circuits cleanly.
        result = handler.run_search(
            ["divergence_pct", ">=", "5.0", "AND", "foo", ">", "0"],
            empty,
        )
        assert result.empty
        assert isinstance(result, pd.DataFrame)

    def test_table_on_empty_df_returns_schema(self):
        """``table A, B, C`` over an empty input must return an empty
        DataFrame with columns [A, B, C] - the downstream pipeline (sort,
        head, serializer) assumes those columns exist."""
        from handlers.GeneralHandler import GeneralHandler

        empty = pd.DataFrame(columns=["_epoch"])
        result = GeneralHandler.filter_df_columns(
            empty, ["ticker", "price", "volume"], mode="+",
        )
        assert list(result.columns) == ["ticker", "price", "volume"]
        assert len(result) == 0

    def test_sort_on_empty_df_returns_empty(self):
        """``sort`` over empty must not raise even when the sort column
        doesn't exist. A 0-row DataFrame is already sorted by definition."""
        from handlers.GeneralHandler import GeneralHandler
        empty = pd.DataFrame(columns=["_epoch"])
        result = GeneralHandler.sort_df_by_columns(
            empty, ["nonexistent_column"], is_ascending="-",
        )
        assert len(result) == 0


# =====================================================================
# Part 4: install_default(..., overwrite=True) + template_drift
# =====================================================================

class TestInstallDefaultOverwrite:

    def _store(self, tmp_path: Path):
        from saved_search_store import SavedSearchStore
        store = SavedSearchStore()
        store._dir = tmp_path / "saved"
        store._defaults_dir = tmp_path / "defaults"
        store._db = str(tmp_path / "lc.sqlite")
        store._dir.mkdir(parents=True, exist_ok=True)
        store._defaults_dir.mkdir(parents=True, exist_ok=True)
        store.initialize()
        return store

    def test_install_default_still_refuses_overwrite_by_default(
        self, tmp_path,
    ):
        """Default behaviour preserved: a bare install_default() call
        protects user edits."""
        store = self._store(tmp_path)
        (store._defaults_dir / "foo.yaml").write_text(
            'name: foo\nquery: "NEW"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        )
        (store._dir / "foo.yaml").write_text(
            'name: foo\nquery: "OLD_USER_EDIT"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        )
        with pytest.raises(FileExistsError):
            store.install_default("foo")
        # Ensure user edit survived
        assert "OLD_USER_EDIT" in (store._dir / "foo.yaml").read_text()

    def test_install_default_overwrite_true_replaces_installed(
        self, tmp_path,
    ):
        """overwrite=True force-replaces the stale YAML. Pins the
        2026-04-21 Sync Template behaviour."""
        store = self._store(tmp_path)
        (store._defaults_dir / "foo.yaml").write_text(
            'name: foo\nquery: "NEW"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        )
        (store._dir / "foo.yaml").write_text(
            'name: foo\nquery: "OLD_BROKEN"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        )
        result = store.install_default("foo", overwrite=True)
        assert "NEW" in (store._dir / "foo.yaml").read_text()
        assert "NEW" in result.get("query", "")

    def test_template_drift_detects_differing_query(self, tmp_path):
        """``template_drift`` returns the diff when queries differ."""
        store = self._store(tmp_path)
        (store._defaults_dir / "foo.yaml").write_text(
            'name: foo\nquery: "CORRECT"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        )
        (store._dir / "foo.yaml").write_text(
            'name: foo\nquery: "BROKEN"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        )
        drift = store.template_drift("foo")
        assert drift is not None
        assert drift["installed_query"] == "BROKEN"
        assert drift["template_query"] == "CORRECT"

    def test_template_drift_returns_none_when_matching(self, tmp_path):
        store = self._store(tmp_path)
        text = 'name: foo\nquery: "X"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: ""\n'
        (store._defaults_dir / "foo.yaml").write_text(text)
        (store._dir / "foo.yaml").write_text(text)
        assert store.template_drift("foo") is None

    def test_template_drift_returns_none_when_only_non_query_differs(
        self, tmp_path,
    ):
        """A drift in cosmetic metadata (description, email_body) is not
        worth nagging about - we only flag ``query`` drift."""
        store = self._store(tmp_path)
        (store._defaults_dir / "foo.yaml").write_text(
            'name: foo\nquery: "X"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: "a@b"\n'
        )
        (store._dir / "foo.yaml").write_text(
            'name: foo\nquery: "X"\ncron_schedule: ""\nlookback: ""\ntrigger: once\nemail_address: "c@d"\n'
        )
        assert store.template_drift("foo") is None


class TestFeederStatusTemplateDrift:

    def test_resolve_feeder_reports_drift(self):
        """When a drift checker returns non-None for this feeder, the
        resolver sets ``fs.template_drift=True`` AND appends an
        actionable warning to the message so the UI can show the
        Sync Template nudge."""
        from alert_groups.feeder_status import resolve_feeder

        def loader(name):
            return {
                "name": name,
                "query": 'index="indexes/foo/*.parquet" | head 1',
            }

        def drift_checker(_name):
            return {"installed_query": "OLD", "template_query": "NEW"}

        with tempfile.TemporaryDirectory() as td:
            fs = resolve_feeder(
                "feeder_x",
                saved_search_loader=loader,
                library_scripts=[],
                scheduled_tasks=[],
                credentials_lister=lambda _t: [],
                indexes_root=td,
                template_drift_checker=drift_checker,
            )
        assert fs.template_drift is True
        assert "Sync Template" in fs.message

    def test_resolve_feeder_drift_absent_when_checker_returns_none(self):
        from alert_groups.feeder_status import resolve_feeder
        def loader(name):
            return {
                "name": name,
                "query": 'index="indexes/foo/*.parquet" | head 1',
            }
        with tempfile.TemporaryDirectory() as td:
            fs = resolve_feeder(
                "feeder_x",
                saved_search_loader=loader,
                library_scripts=[],
                scheduled_tasks=[],
                credentials_lister=lambda _t: [],
                indexes_root=td,
                template_drift_checker=lambda _n: None,
            )
        assert fs.template_drift is False
        assert "Sync Template" not in fs.message


# =====================================================================
# Part 5: Script schema preservation on empty emit
# =====================================================================

class TestScriptSchemaPreservation:

    @staticmethod
    def _fake_get(url, *_args, **_kwargs):
        """Stub requests.get that returns empty for both Kalshi and
        Polymarket endpoints. Real HTTP is blocked via unittest.mock."""
        m = MagicMock()
        m.raise_for_status = lambda: None
        if "polymarket" in url:
            m.json = MagicMock(return_value=[])
        else:
            m.json = MagicMock(return_value={"markets": [], "cursor": ""})
        return m

    def _run_script(self, script_name: str) -> pd.DataFrame:
        """Compile and run a script with requests.get mocked out."""
        import json
        import time as _real_time

        path = (
            Path(PROJECT_ROOT) / "script_library" / "scripts"
            / f"{script_name}.json"
        )
        with open(path) as f:
            spec = json.load(f)

        captured: dict = {}

        def generate_results(df):
            captured["df"] = df

        # Patch requests.get AND time.sleep at module level so the
        # compiled script's local ``import requests`` still picks up
        # the patched version.
        with patch("requests.get", side_effect=self._fake_get):
            with patch.object(_real_time, "sleep", return_value=None):
                ns: dict = {"GENERATE_RESULTS": generate_results}
                exec(compile(spec["code"], path.name, "exec"), ns)
        return captured.get("df")

    def test_kalshi_pro_emits_expected_columns_on_empty(self):
        """Smoke-compile the script and run it with empty API responses
        to confirm the emitted DataFrame has the full expected schema
        even when zero arb opportunities are found.

        The key regression: ``divergence_pct`` and ``match_confidence``
        must be in the schema even on an empty emit so downstream SPQL
        ``| where divergence_pct >= 5.0`` doesn't crash with
        ``name 'divergence_pct' is not defined``."""
        df = self._run_script("kalshi_polymarket_arbitrage_pro")
        assert df is not None, "script did not call GENERATE_RESULTS"
        assert len(df) == 0, f"Expected empty DF, got {len(df)} rows"
        required = {
            "kalshi_ticker", "divergence_pct", "opportunity_strength",
            "match_confidence", "match_tier", "_epoch",
        }
        missing = required - set(df.columns)
        assert not missing, (
            f"Kalshi pro empty-emit is missing columns {sorted(missing)}. "
            "Pandas can't infer schema from an empty list - the script "
            "MUST pass columns=EXPECTED_COLUMNS to DataFrame(...)."
        )

    def test_base_kalshi_emits_expected_columns_on_empty(self):
        """Same guarantee for the sandboxed variant."""
        df = self._run_script("kalshi_polymarket_arbitrage")
        assert df is not None
        assert len(df) == 0
        required = {
            "kalshi_ticker", "divergence_pct", "opportunity_strength",
            "_epoch",
        }
        missing = required - set(df.columns)
        assert not missing, (
            f"Base kalshi empty-emit is missing columns {sorted(missing)}."
        )
