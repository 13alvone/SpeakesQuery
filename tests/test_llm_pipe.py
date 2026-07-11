"""
Tests for handlers/LLMHandler.py and the | llm SPQL pipe (slice 4).

Two layers:
  * Unit tests against `llm_pipe` directly (mock the router transport
    so we don't hit a real provider).
  * End-to-end through process_query - exercises the grammar +
    listener dispatch + handler stack.

Plus a grammar-parity drift guard: LLM token in .g4, listener
command_map dispatches it, grammar_vocab exposes "llm" as a command.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from analyzers.llm_router import LLMResponse, LLMRouterError
from handlers.LLMHandler import (
    LLMPipeError,
    build_full_prompt,
    llm_pipe,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def news_df() -> pd.DataFrame:
    return pd.DataFrame({
        "title": [
            "Federal Reserve pauses interest rate hikes",
            "Apple announces new iPhone launch",
            "Nvidia GPU demand soars",
        ],
        "_epoch": [1700000000, 1700000010, 1700000020],
    })


def _stub_response(text="ok", *, cost=0.0001, latency=42, model_id="m"):
    return LLMResponse(
        text=text, model_id=model_id, provider="anthropic",
        model_name="m-name", input_tokens=10, output_tokens=3,
        cost_usd=cost, latency_ms=latency, request_id="rid",
    )


@pytest.fixture
def isolated_router_state(tmp_path, monkeypatch):
    """Same isolation as test_llm_router.py per
    reference_auto_instrumentation_test_isolation.md - slice-3 history
    capture must not pollute the project-root DB during tests.
    """
    import model_store
    import analyzers.llm_history_store as hist
    from analyzers import llm_router
    model_store.reset_for_tests()
    hist.reset_for_tests()
    monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(
        hist, "DEFAULT_DB_PATH", tmp_path / "llm_call_history.sqlite",
    )
    model_store.get_store()  # seeds defaults
    llm_router._invalidate_api_key_cache()
    yield
    model_store.reset_for_tests()
    hist.reset_for_tests()
    llm_router._invalidate_api_key_cache()


# ── Per-row dispatch ─────────────────────────────────────────────────

class TestLLMPipeDispatch:
    def test_basic_invocation_adds_columns(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[_stub_response(text=f"reply-{i}") for i in range(3)],
        ) as mock_call:
            out = llm_pipe(news_df, model="m", prompt="rate it")
            assert mock_call.call_count == 3

        for col in ("_llm_output", "_llm_model", "_llm_provider",
                    "_llm_cost_usd", "_llm_latency_ms",
                    "_llm_status", "_llm_error"):
            assert col in out.columns
        assert out["_llm_output"].tolist() == ["reply-0", "reply-1", "reply-2"]
        assert (out["_llm_status"] == "success").all()

    def test_field_kwarg_constrains_input(self, news_df, isolated_router_state):
        # Verify only the 'title' column lands in the per-row prompt
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[_stub_response() for _ in range(3)],
        ) as mock_call:
            llm_pipe(news_df, model="m", prompt="P", field="title")
            sent = mock_call.call_args_list[0].kwargs["prompt"]
            assert "title:" in sent
            # _epoch shouldn't appear because field was specified
            assert "_epoch" not in sent

    def test_field_does_not_exist_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="does not exist"):
            llm_pipe(news_df, model="m", prompt="P", field="nope")

    def test_no_text_columns_raises(self, isolated_router_state):
        df = pd.DataFrame({"_epoch": [1, 2, 3]})  # numeric only
        with pytest.raises(LLMPipeError, match="No text columns"):
            llm_pipe(df, model="m", prompt="P")

    def test_missing_model_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="model"):
            llm_pipe(news_df, model="", prompt="P")

    def test_missing_prompt_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="prompt"):
            llm_pipe(news_df, model="m", prompt="")

    def test_empty_input_returns_well_shaped_df(self, isolated_router_state):
        out = llm_pipe(pd.DataFrame({"title": pd.Series([], dtype=object)}),
                       model="m", prompt="P")
        assert len(out) == 0
        assert "_llm_output" in out.columns
        assert "_llm_status" in out.columns

    def test_system_prompt_threaded_through(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[_stub_response() for _ in range(3)],
        ) as mock_call:
            llm_pipe(news_df, model="m", prompt="P", system="be terse")
            assert mock_call.call_args_list[0].kwargs["system"] == "be terse"

    def test_use_cache_passed_through(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[_stub_response() for _ in range(3)],
        ) as mock_call:
            llm_pipe(news_df, model="m", prompt="P", use_cache=False)
            assert mock_call.call_args_list[0].kwargs["use_cache"] is False

    def test_max_tokens_passed_through(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[_stub_response() for _ in range(3)],
        ) as mock_call:
            llm_pipe(news_df, model="m", prompt="P", max_tokens=128)
            assert mock_call.call_args_list[0].kwargs["max_tokens"] == 128


# ── Per-row error capture ───────────────────────────────────────────

class TestErrorCapture:
    def test_one_row_fails_others_succeed(self, news_df, isolated_router_state):
        # First and third row succeed, middle row raises
        ok = _stub_response(text="ok")
        err = LLMRouterError(
            "transient HTTP 500", model_id="m", provider="anthropic",
            error_class="HTTP500", request_id="rid-err",
        )
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[ok, err, ok],
        ):
            out = llm_pipe(news_df, model="m", prompt="P")

        assert out["_llm_status"].tolist() == ["success", "error", "success"]
        assert out["_llm_output"].tolist() == ["ok", "", "ok"]
        # Error row carries the error class + message
        assert "HTTP500" in out["_llm_error"].iloc[1]
        # Cost on errored rows is zero
        assert out["_llm_cost_usd"].iloc[1] == 0.0


# ── Boundary-tag formatting ─────────────────────────────────────────

class TestBoundaryTagFormat:
    def test_data_block_wraps_row_content(self):
        row = pd.Series({"title": "Hello", "body": "World"})
        full = build_full_prompt("rate it", row, ["title", "body"])
        assert full.startswith("rate it\n\n<data>\n")
        assert full.endswith("</data>")
        assert "title: Hello" in full
        assert "body: World" in full

    def test_none_cells_become_empty(self):
        row = pd.Series({"title": None, "body": "World"})
        full = build_full_prompt("P", row, ["title", "body"])
        assert "title: \n" in full
        assert "body: World" in full

    def test_nan_cells_become_empty(self):
        row = pd.Series({"score": float("nan"), "body": "x"})
        full = build_full_prompt("P", row, ["score", "body"])
        assert "score: \n" in full


# ── Cost aggregation ────────────────────────────────────────────────

class TestCostAggregation:
    def test_cache_hits_have_zero_cost(self, news_df, isolated_router_state):
        # Slice-3 cache hit signature: cost_usd=0, latency_ms=0
        cached = _stub_response(text="cached", cost=0.0, latency=0)
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[cached, cached, cached],
        ):
            out = llm_pipe(news_df, model="m", prompt="P")
        assert (out["_llm_cost_usd"] == 0.0).all()
        assert (out["_llm_latency_ms"] == 0).all()

    def test_total_cost_via_stats_pipe(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(cost=0.0001),
                _stub_response(cost=0.0002),
                _stub_response(cost=0.0001),
            ],
        ):
            out = llm_pipe(news_df, model="m", prompt="P")
        assert out["_llm_cost_usd"].sum() == pytest.approx(0.0004)


# ── End-to-end through process_query ────────────────────────────────

class TestEndToEnd:
    def test_pipe_dispatches_through_router(self, isolated_router_state):
        # Use the test fixture parquet from tier1 SPQL tests
        from query_engine.CmdExecutionBackend import process_query
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[_stub_response(text=f"out-{i}") for i in range(5)],
        ):
            q = (
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| llm model="claude-haiku-4-5-20251001" prompt="rate it"'
            )
            df, _ = process_query(q)
        assert df is not None
        assert "_llm_output" in df.columns
        assert (df["_llm_status"] == "success").all()


# ── Grammar / dispatch parity drift guards ─────────────────────────

class TestGrammarParity:
    def test_grammar_declares_llm_tokens(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        for tok in (
            r"\bLLM\s*:\s*'llm'",
            r"\bMODEL\s*:\s*'model'",
            r"\bPROMPT\s*:\s*'prompt'",
            r"\bSYSTEM\s*:\s*'system'",
            r"\bUSE_CACHE\s*:\s*'use_cache'",
            r"\bMAX_TOKENS\s*:\s*'max_tokens'",
        ):
            assert re.search(tok, g4), f"missing token: {tok}"

    def test_grammar_has_directive_rule(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert "LLM MODEL EQUALS DOUBLE_QUOTED_STRING PROMPT EQUALS" in g4

    def test_listener_dispatches_llm(self):
        from lexers.speakesQueryListener import speakesQueryListener
        listener = speakesQueryListener("")
        assert "llm" in listener._command_map

    def test_grammar_vocab_exposes_llm(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm" in names
