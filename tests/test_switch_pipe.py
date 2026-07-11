"""
Tests for the | switch ... case SPQL pipe (slice 6).

| switch is the first SPQL pipe with multiple sub-pipelines per
directive - different from | join / | append / | multisearch which
all take exactly one subsearch. The case-keyed dispatch makes it
the natural pairing for | llm classifications:

  | llm prompt="classify as urgent|routine|drop"
  | switch _llm_output
     case "urgent" [ <heavy analysis subpipe> ]
     case "routine" [ <stats only subpipe> ]
     case "*" [ <noop / log only> ]

Covers:
  * Per-row routing by column value
  * `case "*"` catchall for unmatched values
  * Unmatched-no-catchall rows are silently dropped
  * Empty input produces empty output
  * Multiple cases concatenate with column union (NaN-fill)
  * Per-case output ordering preserved
  * Missing column raises
  * Grammar-parity drift guards
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest


# ── Helpers ──────────────────────────────────────────────────────────

def _run(query: str):
    from query_engine.CmdExecutionBackend import process_query
    df, _ = process_query(query)
    return df


# ── End-to-end via process_query ─────────────────────────────────────

class TestSwitchRouting:
    """The fixture parquet at indexes/default_test/output_parquets/test0.parquet
    has 5 rows with `level` values: DEBUG, ERROR, WARN, INFO, CRITICAL.
    Use it to exercise routing.
    """

    FIXTURE = "indexes/default_test/output_parquets/test0.parquet"

    def test_each_case_processes_only_matching_rows(self):
        # Two named cases + no catchall → only DEBUG + ERROR rows pass
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "DEBUG" [ table level message ] '
            'case "ERROR" [ table level message ]'
        )
        assert df is not None
        # Should have 2 rows (DEBUG + ERROR), 3 dropped (WARN, INFO, CRITICAL)
        assert len(df) == 2
        assert set(df["level"].tolist()) == {"DEBUG", "ERROR"}

    def test_catchall_picks_up_unmatched(self):
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "DEBUG" [ table level ] '
            'case "*" [ table level ]'
        )
        assert df is not None
        # All 5 rows pass - DEBUG via its case, others via "*"
        assert len(df) == 5

    def test_unmatched_no_catchall_dropped(self):
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "MYTHICAL" [ table level ]'
        )
        # No rows match the only case AND no catchall; result should
        # be empty (process_query collapses empty → None)
        assert df is None

    def test_column_union_nanfill_across_cases(self):
        # Different cases produce different schemas; concat fills NaN
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "DEBUG" [ table message ] '
            'case "ERROR" [ table errorCode ]'
        )
        assert df is not None
        # Both columns present; DEBUG row has NaN errorCode, ERROR row
        # has NaN message
        assert "message" in df.columns
        assert "errorCode" in df.columns

    def test_each_case_can_apply_different_transforms(self):
        # DEBUG case keeps `message`; CRITICAL case keeps `userRole` +
        # `errorCode`. Concat across cases preserves both column sets
        # with NaN-fill.
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "DEBUG" [ table level message ] '
            'case "CRITICAL" [ table level userRole errorCode ]'
        )
        assert df is not None
        assert len(df) == 2
        critical = df[df["level"] == "CRITICAL"]
        debug = df[df["level"] == "DEBUG"]
        assert len(critical) == 1
        assert len(debug) == 1
        # CRITICAL row got the columns from its own subpipe (userRole
        # populated, message NaN); DEBUG got the inverse.
        assert pd.notna(critical.iloc[0]["userRole"])
        assert pd.notna(debug.iloc[0]["message"])


class TestSwitchEdgeCases:
    FIXTURE = "indexes/default_test/output_parquets/test0.parquet"

    def test_missing_column_returns_none_via_process_query(self):
        # process_query swallows internal exceptions and returns None
        # (project convention from CmdExecutionBackend). The runtime
        # error is logged at ERROR for the operator to see in
        # `docker logs -f`.
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch nonexistent_col '
            'case "x" [ head 1 ]'
        )
        assert df is None

    def test_missing_column_raises_via_listener_direct(self):
        # When called directly (bypassing process_query's
        # error-swallowing wrapper), the listener raises a clear
        # RuntimeError with the offending column name.
        from lexers.speakesQueryListener import speakesQueryListener
        listener = speakesQueryListener("")
        listener.main_df = pd.DataFrame({"present_col": ["a", "b"]})
        with pytest.raises(RuntimeError, match="does not exist"):
            listener._cmd_switch(
                ["switch", "nonexistent_col"],
                'switch nonexistent_col case "x" [ head 1 ]',
            )

    def test_only_catchall_acts_as_passthrough(self):
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "*" [ head 100 ]'
        )
        assert df is not None
        # All 5 rows pass through the catchall
        assert len(df) == 5

    def test_case_subpipe_can_aggregate(self):
        # Subpipe applies stats - the case's output is the aggregate, not
        # the original rows. Concat across cases works on aggregated rows.
        df = _run(
            f'index="{self.FIXTURE}" '
            '| switch level '
            'case "*" [ stats count ]'
        )
        assert df is not None
        # stats count produces a single-row {count} aggregate
        assert len(df) == 1
        assert "count" in df.columns
        assert int(df.iloc[0]["count"]) == 5


# ── Composition with | llm (the headline use case) ─────────────────

class TestComposeWithLLM:
    """The headline use case: classify rows with | llm, route them
    with | switch. Mock the router so we don't touch a real provider.
    """

    @pytest.fixture
    def isolated_router_state(self, tmp_path, monkeypatch):
        import model_store
        import analyzers.llm_history_store as hist
        from analyzers import llm_router
        model_store.reset_for_tests()
        hist.reset_for_tests()
        monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
        monkeypatch.setattr(
            hist, "DEFAULT_DB_PATH", tmp_path / "llm_call_history.sqlite",
        )
        model_store.get_store()
        llm_router._invalidate_api_key_cache()
        yield
        model_store.reset_for_tests()
        hist.reset_for_tests()
        llm_router._invalidate_api_key_cache()

    def test_llm_classify_then_switch_routes(self, isolated_router_state):
        from unittest.mock import patch
        from analyzers.llm_router import LLMResponse

        # Mock | llm to return alternating "urgent" / "routine" labels
        # for the 5 fixture rows
        def stub_call(model_id, *, prompt, **kw):
            # Simple: alternate urgent / routine based on whether
            # the prompt contains "DEBUG"
            label = "urgent" if "DEBUG" in prompt or "ERROR" in prompt else "routine"
            return LLMResponse(
                text=label, model_id=model_id, provider="anthropic",
                model_name=model_id, input_tokens=10, output_tokens=2,
                cost_usd=0.0001, latency_ms=10, request_id="rid",
            )

        with patch("analyzers.llm_router.call_llm", side_effect=stub_call):
            q = (
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| llm model="claude-haiku-4-5-20251001" prompt="classify" '
                '| switch _llm_output '
                'case "urgent" [ table level _llm_output ] '
                'case "routine" [ table level _llm_output ]'
            )
            df = _run(q)
        assert df is not None
        # DEBUG + ERROR → urgent (2 rows); WARN + INFO + CRITICAL → routine (3 rows)
        urgent_count = (df["_llm_output"] == "urgent").sum()
        routine_count = (df["_llm_output"] == "routine").sum()
        assert urgent_count == 2
        assert routine_count == 3


# ── Grammar parity drift guards ─────────────────────────────────────

class TestGrammarParity:
    def test_grammar_declares_switch_token(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert re.search(r"\bSWITCH\s*:\s*'switch'", g4)
        # CASE token already existed (used by case() function); switch
        # reuses it for the directive.
        assert re.search(r"\bCASE\s*:\s*'case'", g4)

    def test_grammar_has_directive_rule(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert "SWITCH variableName (CASE DOUBLE_QUOTED_STRING subsearch)+" in g4

    def test_listener_dispatches_switch(self):
        from lexers.speakesQueryListener import speakesQueryListener
        listener = speakesQueryListener("")
        assert "switch" in listener._command_map

    def test_grammar_vocab_exposes_switch(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "switch" in names
