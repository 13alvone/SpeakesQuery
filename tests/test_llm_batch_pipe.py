"""
Tests for handlers/LLMHandler.py::llm_batch_pipe and the | llm_batch
SPQL pipe (slice 5).

Differs from | llm (slice 4) in that the WHOLE DataFrame is JSON-
serialised and sent as ONE prompt to the model. Output is a SINGLE-row
DataFrame containing the model's holistic response. The original rows
are gone (use `| append [llm_batch ...]` if you need both).

Covers:
  * Whole-DataFrame serialisation as JSON list-of-records
  * max_rows truncation (default 20; override per-call)
  * field= constraint (only that column in the JSON)
  * Empty input → single-row "skipped_empty" output
  * use_cache + system + max_tokens threaded through
  * Per-call error capture (router error → single-row error result)
  * _llm_input_row_count column tracks truncation honestly
  * Grammar-parity drift guards (LLM_BATCH + MAX_ROWS tokens, directive
    rule, listener dispatch, grammar_vocab exposure)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from analyzers.llm_router import LLMResponse, LLMRouterError
from handlers.LLMHandler import (
    LLMPipeError,
    build_batch_prompt,
    llm_batch_pipe,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def news_df() -> pd.DataFrame:
    return pd.DataFrame({
        "title": [f"headline {i}" for i in range(5)],
        "body": [f"body {i}" for i in range(5)],
        "_epoch": [1700000000 + i for i in range(5)],
    })


def _stub_response(text="batch reply", *, cost=0.0001, latency=42):
    return LLMResponse(
        text=text, model_id="m", provider="anthropic",
        model_name="m-name", input_tokens=200, output_tokens=50,
        cost_usd=cost, latency_ms=latency, request_id="rid-batch",
    )


@pytest.fixture
def isolated_router_state(tmp_path, monkeypatch):
    """Same isolation as test_llm_router.py + test_llm_pipe.py per
    reference_auto_instrumentation_test_isolation.md.
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
    model_store.get_store()
    llm_router._invalidate_api_key_cache()
    yield
    model_store.reset_for_tests()
    hist.reset_for_tests()
    llm_router._invalidate_api_key_cache()


# ── Output shape ─────────────────────────────────────────────────────

class TestOutputShape:
    def test_returns_single_row_dataframe(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(text="summary"),
        ):
            out = llm_batch_pipe(news_df, model="m", prompt="summarize")
        assert len(out) == 1
        assert out.iloc[0]["_llm_output"] == "summary"

    def test_all_expected_columns_present(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ):
            out = llm_batch_pipe(news_df, model="m", prompt="P")
        for col in (
            "_llm_output", "_llm_model", "_llm_provider",
            "_llm_cost_usd", "_llm_latency_ms",
            "_llm_status", "_llm_error", "_llm_input_row_count",
        ):
            assert col in out.columns

    def test_input_row_count_reflects_truncation(self, isolated_router_state):
        df = pd.DataFrame({"title": [f"x-{i}" for i in range(50)]})
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ):
            out = llm_batch_pipe(df, model="m", prompt="P", max_rows=10)
        # Truncated: only 10 rows fed to the model
        assert out.iloc[0]["_llm_input_row_count"] == 10


# ── JSON serialisation ──────────────────────────────────────────────

class TestSerialisation:
    def test_full_prompt_includes_data_block_with_json(self):
        df = pd.DataFrame({"title": ["A", "B"], "body": ["x", "y"]})
        full = build_batch_prompt("summarize", df, ["title", "body"])
        assert full.startswith("summarize\n\n<data>\n")
        assert full.endswith("</data>")
        # JSON list with 2 records
        match = re.search(r"<data>\n(.*)\n</data>", full, re.DOTALL)
        assert match is not None
        records = json.loads(match.group(1))
        assert len(records) == 2
        assert records[0] == {"title": "A", "body": "x"}
        assert records[1] == {"title": "B", "body": "y"}

    def test_none_cells_become_json_null(self):
        df = pd.DataFrame({"title": [None, "Z"], "body": ["x", None]})
        full = build_batch_prompt("P", df, ["title", "body"])
        match = re.search(r"<data>\n(.*)\n</data>", full, re.DOTALL)
        records = json.loads(match.group(1))
        assert records[0] == {"title": None, "body": "x"}
        assert records[1] == {"title": "Z", "body": None}

    def test_nan_cells_become_json_null(self):
        df = pd.DataFrame({"score": [1.5, float("nan")], "name": ["a", "b"]})
        full = build_batch_prompt("P", df, ["score", "name"])
        match = re.search(r"<data>\n(.*)\n</data>", full, re.DOTALL)
        records = json.loads(match.group(1))
        assert records[1]["score"] is None

    def test_field_kwarg_constrains_columns(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ) as mock_call:
            llm_batch_pipe(news_df, model="m", prompt="P", field="title")
            sent = mock_call.call_args.kwargs["prompt"]
            # JSON only has 'title', no 'body' or '_epoch'
            match = re.search(r"<data>\n(.*)\n</data>", sent, re.DOTALL)
            records = json.loads(match.group(1))
            for rec in records:
                assert set(rec.keys()) == {"title"}


# ── max_rows ─────────────────────────────────────────────────────────

class TestMaxRows:
    def test_default_max_rows_is_20(self, isolated_router_state):
        df = pd.DataFrame({"title": [f"x-{i}" for i in range(100)]})
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ) as mock_call:
            out = llm_batch_pipe(df, model="m", prompt="P")
            sent = mock_call.call_args.kwargs["prompt"]
            match = re.search(r"<data>\n(.*)\n</data>", sent, re.DOTALL)
            records = json.loads(match.group(1))
            assert len(records) == 20
        assert out.iloc[0]["_llm_input_row_count"] == 20

    def test_max_rows_override(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ) as mock_call:
            llm_batch_pipe(news_df, model="m", prompt="P", max_rows=2)
            sent = mock_call.call_args.kwargs["prompt"]
            match = re.search(r"<data>\n(.*)\n</data>", sent, re.DOTALL)
            records = json.loads(match.group(1))
            assert len(records) == 2

    def test_invalid_max_rows_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="positive int"):
            llm_batch_pipe(news_df, model="m", prompt="P", max_rows=0)
        with pytest.raises(LLMPipeError, match="positive int"):
            llm_batch_pipe(news_df, model="m", prompt="P", max_rows=-1)


# ── Edge cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_input_returns_skipped_empty(self, isolated_router_state):
        df = pd.DataFrame({"title": pd.Series([], dtype=object)})
        out = llm_batch_pipe(df, model="m", prompt="P")
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "skipped_empty"
        assert out.iloc[0]["_llm_output"] == ""
        assert out.iloc[0]["_llm_input_row_count"] == 0

    def test_missing_model_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="model"):
            llm_batch_pipe(news_df, model="", prompt="P")

    def test_missing_prompt_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="prompt"):
            llm_batch_pipe(news_df, model="m", prompt="")

    def test_missing_field_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="does not exist"):
            llm_batch_pipe(news_df, model="m", prompt="P", field="bogus")

    def test_no_text_columns_raises(self, isolated_router_state):
        df = pd.DataFrame({"_epoch": [1, 2]})
        with pytest.raises(LLMPipeError, match="No text columns"):
            llm_batch_pipe(df, model="m", prompt="P")


# ── Kwargs threading ────────────────────────────────────────────────

class TestKwargsThreading:
    def test_system_threaded_through(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ) as mock_call:
            llm_batch_pipe(news_df, model="m", prompt="P", system="be brief")
            assert mock_call.call_args.kwargs["system"] == "be brief"

    def test_use_cache_threaded_through(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ) as mock_call:
            llm_batch_pipe(news_df, model="m", prompt="P", use_cache=False)
            assert mock_call.call_args.kwargs["use_cache"] is False

    def test_max_tokens_threaded_through(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(),
        ) as mock_call:
            llm_batch_pipe(news_df, model="m", prompt="P", max_tokens=512)
            assert mock_call.call_args.kwargs["max_tokens"] == 512


# ── Error capture ───────────────────────────────────────────────────

class TestErrorCapture:
    def test_router_error_returns_single_row_error(
        self, news_df, isolated_router_state,
    ):
        boom = LLMRouterError(
            "rate limited", model_id="m", provider="anthropic",
            error_class="RateLimitError", request_id="rid-err",
        )
        with patch(
            "analyzers.llm_router.call_llm", side_effect=boom,
        ):
            out = llm_batch_pipe(news_df, model="m", prompt="P")
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "error"
        assert "RateLimitError" in out.iloc[0]["_llm_error"]
        assert out.iloc[0]["_llm_cost_usd"] == 0.0
        # input_row_count still reflects truncated input even on error
        assert out.iloc[0]["_llm_input_row_count"] == 5


# ── Cache hit signature ─────────────────────────────────────────────

class TestCacheHitSignature:
    def test_cache_hit_reports_zero_cost(self, news_df, isolated_router_state):
        cached = _stub_response(text="cached", cost=0.0, latency=0)
        with patch(
            "analyzers.llm_router.call_llm", return_value=cached,
        ):
            out = llm_batch_pipe(news_df, model="m", prompt="P")
        assert out.iloc[0]["_llm_cost_usd"] == 0.0
        assert out.iloc[0]["_llm_latency_ms"] == 0
        assert out.iloc[0]["_llm_status"] == "success"


# ── End-to-end through process_query ────────────────────────────────

class TestEndToEnd:
    def test_pipe_dispatches_through_router(self, isolated_router_state):
        from query_engine.CmdExecutionBackend import process_query
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(text="overall summary"),
        ):
            q = (
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| llm_batch model="claude-haiku-4-5-20251001" '
                'prompt="summarize" max_rows=10'
            )
            df, _ = process_query(q)
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["_llm_output"] == "overall summary"
        assert df.iloc[0]["_llm_status"] == "success"


# ── Grammar parity drift guards ─────────────────────────────────────

class TestGrammarParity:
    def test_grammar_declares_llm_batch_tokens(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert re.search(r"\bLLM_BATCH\s*:\s*'llm_batch'", g4)
        assert re.search(r"\bMAX_ROWS\s*:\s*'max_rows'", g4)

    def test_grammar_has_directive_rule(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert "LLM_BATCH MODEL EQUALS DOUBLE_QUOTED_STRING" in g4
        assert "MAX_ROWS EQUALS NUMBER" in g4

    def test_listener_dispatches_llm_batch(self):
        from lexers.speakesQueryListener import speakesQueryListener
        listener = speakesQueryListener("")
        assert "llm_batch" in listener._command_map

    def test_grammar_vocab_exposes_llm_batch(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm_batch" in names
