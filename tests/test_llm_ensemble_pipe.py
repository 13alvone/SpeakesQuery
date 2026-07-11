"""
Tests for handlers/LLMHandler.py llm_ensemble_pipe + the | llm_ensemble SPQL pipe.

Phase 4 / Bet 3 slice 3 - multi-model voting.

Test layout:
  * TestAggregatorMajority - case-insensitive plurality, agreement metric
  * TestAggregatorAverage - numeric mean via _parse_confidence reuse
  * TestAggregatorUnanimous - all-or-nothing
  * TestLlmEnsembleContract - required kwargs + bad aggregator + min_agreement validation
  * TestPerRowDispatch - N models called per row in order
  * TestMinAgreementThreshold - flips status to no_consensus
  * TestPartialModelFailure - some models error, rest aggregate
  * TestEmptyInput - well-shaped empty result
  * TestDryRun - slice-7 contract: zero provider calls, worst-case estimate
  * TestBudgetGate - slice-7 contract: cap stops mid-row, sentinel emitted
  * TestMoneyLeakCanary - patch call_llm with raise; dry_run + cap-zero paths zero invocations
  * TestPendingStatusDriftGuard - cap-on-row-0-model-0 produces sentinel only (per pending-status pattern)
  * TestGrammarParity - .g4 token + listener dispatch + grammar_vocab pickup
  * TestEndToEndExecution - process_query path, full SPQL execution
  * TestExcludedColumnsDriftGuard - slice-3 columns excluded from feed-back
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
    _aggregate_average,
    _aggregate_majority,
    _aggregate_unanimous,
    llm_ensemble_pipe,
)


PROJECT_ROOT = Path(__file__).parent.parent


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def news_df() -> pd.DataFrame:
    return pd.DataFrame({
        "title": [
            "Federal Reserve pauses interest rate hikes",
            "Apple announces new iPhone launch",
        ],
        "_epoch": [1700000000, 1700000010],
    })


def _stub_response(text="urgent", *, cost=0.0001, latency=42, model_id="m"):
    return LLMResponse(
        text=text, model_id=model_id, provider="anthropic",
        model_name="m-name", input_tokens=10, output_tokens=3,
        cost_usd=cost, latency_ms=latency, request_id="rid",
    )


@pytest.fixture
def isolated_router_state(tmp_path, monkeypatch):
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


# ═══════════════════════════════════════════════════════════════════
# 1. Aggregator unit tests
# ═══════════════════════════════════════════════════════════════════

class TestAggregatorMajority:
    def test_clear_winner(self):
        winner, agree, status = _aggregate_majority(
            ["urgent", "urgent", "skip"]
        )
        assert winner == "urgent"
        assert agree == pytest.approx(2 / 3)
        assert status == "success"

    def test_case_insensitive_voting(self):
        winner, agree, _ = _aggregate_majority(
            ["URGENT", "Urgent", "skip"]
        )
        # Case folded for voting; winner is the original-cased first match
        assert winner.lower() == "urgent"
        assert agree == pytest.approx(2 / 3)

    def test_unanimous_inputs(self):
        winner, agree, status = _aggregate_majority(
            ["yes", "yes", "yes"]
        )
        assert winner == "yes"
        assert agree == 1.0
        assert status == "success"

    def test_empty_outputs_no_consensus(self):
        winner, agree, status = _aggregate_majority(["", "", ""])
        assert winner == ""
        assert agree == 0.0
        assert status == "no_consensus"

    def test_some_empty_outputs_excluded_from_vote(self):
        winner, agree, status = _aggregate_majority(["urgent", "", "skip"])
        # Only 2 valid; tie at 1 each. Counter ties → first inserted wins.
        assert status == "success"
        assert agree == 0.5


class TestAggregatorAverage:
    def test_clear_average(self):
        winner, agree, status = _aggregate_average(["0.8", "0.6", "0.7"])
        assert float(winner) == pytest.approx(0.7)
        assert agree == 1.0
        assert status == "success"

    def test_unparseable_excluded(self):
        winner, agree, status = _aggregate_average(["0.8", "not-a-number", "0.6"])
        assert float(winner) == pytest.approx(0.7)
        assert agree == pytest.approx(2 / 3)
        assert status == "success"

    def test_all_unparseable_no_consensus(self):
        winner, agree, status = _aggregate_average(["foo", "bar", "baz"])
        assert winner == ""
        assert agree == 0.0
        assert status == "no_consensus"

    def test_json_confidence_keys(self):
        winner, agree, status = _aggregate_average([
            '{"confidence": 0.9}',
            '{"confidence": 0.7}',
        ])
        assert float(winner) == pytest.approx(0.8)
        assert agree == 1.0


class TestAggregatorUnanimous:
    def test_all_match_success(self):
        winner, agree, status = _aggregate_unanimous(
            ["yes", "yes", "YES"]
        )
        assert winner == "yes"
        assert agree == 1.0
        assert status == "success"

    def test_one_dissent_fails(self):
        winner, agree, status = _aggregate_unanimous(
            ["yes", "yes", "no"]
        )
        assert winner == ""
        assert agree == 0.0
        assert status == "no_consensus"

    def test_empty_output_breaks_unanimity(self):
        winner, agree, status = _aggregate_unanimous(
            ["yes", "", "yes"]
        )
        assert status == "no_consensus"


# ═══════════════════════════════════════════════════════════════════
# 2. Required-kwarg validation
# ═══════════════════════════════════════════════════════════════════

class TestLlmEnsembleContract:
    def test_too_few_models_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="≥ 2"):
            llm_ensemble_pipe(
                news_df, models=["m1"], prompt="x",
            )

    def test_non_list_models_rejected(self, news_df):
        with pytest.raises(LLMPipeError):
            llm_ensemble_pipe(
                news_df, models="m1,m2", prompt="x",
            )

    def test_empty_model_entry_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="non-empty"):
            llm_ensemble_pipe(
                news_df, models=["m1", ""], prompt="x",
            )

    def test_missing_prompt_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="prompt"):
            llm_ensemble_pipe(
                news_df, models=["m1", "m2"], prompt="",
            )

    def test_invalid_aggregator_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="aggregator"):
            llm_ensemble_pipe(
                news_df, models=["m1", "m2"], prompt="x",
                aggregator="weighted_average",
            )

    def test_min_agreement_out_of_range_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="\\[0, 1\\]"):
            llm_ensemble_pipe(
                news_df, models=["m1", "m2"], prompt="x",
                min_agreement=1.5,
            )

    def test_bool_min_agreement_rejected(self, news_df):
        with pytest.raises(LLMPipeError):
            llm_ensemble_pipe(
                news_df, models=["m1", "m2"], prompt="x",
                min_agreement=True,
            )


# ═══════════════════════════════════════════════════════════════════
# 3. Per-row dispatch - N models called in order per row
# ═══════════════════════════════════════════════════════════════════

class TestPerRowDispatch:
    def test_three_models_three_calls_per_row(
        self, news_df, isolated_router_state,
    ):
        # 2 rows × 3 models = 6 calls
        responses = [
            _stub_response(text="urgent", model_id="m1"),
            _stub_response(text="urgent", model_id="m2"),
            _stub_response(text="urgent", model_id="m3"),
            _stub_response(text="skip", model_id="m1"),
            _stub_response(text="skip", model_id="m2"),
            _stub_response(text="skip", model_id="m3"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_ensemble_pipe(
                news_df, models=["m1", "m2", "m3"],
                prompt="classify",
                aggregator="majority",
                use_cache=False,
            )
        assert mock_call.call_count == 6
        # All rows had unanimous votes
        assert list(out["_llm_output"]) == ["urgent", "skip"]
        assert all(out["_llm_status"] == "success")
        assert all(out["_llm_ensemble_agreement"] == 1.0)
        # Audit columns parse as JSON arrays
        for o in out["_llm_ensemble_outputs"]:
            assert len(json.loads(o)) == 3
        for m in out["_llm_ensemble_models"]:
            assert json.loads(m) == ["m1", "m2", "m3"]
        # Cost is cumulative across models for each row
        for c in out["_llm_cost_usd"]:
            assert c == pytest.approx(3 * 0.0001)

    def test_aggregator_label_persisted_per_row(
        self, news_df, isolated_router_state,
    ):
        responses = [
            _stub_response(text="urgent") for _ in range(6)
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_ensemble_pipe(
                news_df, models=["m1", "m2", "m3"],
                prompt="x", aggregator="unanimous",
                use_cache=False,
            )
        assert all(out["_llm_ensemble_aggregator"] == "unanimous")


# ═══════════════════════════════════════════════════════════════════
# 4. min_agreement threshold
# ═══════════════════════════════════════════════════════════════════

class TestMinAgreementThreshold:
    def test_low_agreement_flips_to_no_consensus(
        self, isolated_router_state,
    ):
        # 1 row × 3 models, all different outputs → agreement = 1/3
        # min_agreement = 0.5 → status = no_consensus
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="A"),
                _stub_response(text="B"),
                _stub_response(text="C"),
            ],
        ):
            out = llm_ensemble_pipe(
                single_row, models=["m1", "m2", "m3"],
                prompt="x", aggregator="majority",
                min_agreement=0.5,
                use_cache=False,
            )
        assert out.iloc[0]["_llm_status"] == "no_consensus"
        # Agreement metric still surfaced for audit
        assert out.iloc[0]["_llm_ensemble_agreement"] == pytest.approx(1 / 3)

    def test_high_agreement_passes_threshold(
        self, isolated_router_state,
    ):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="urgent"),
                _stub_response(text="urgent"),
                _stub_response(text="skip"),
            ],
        ):
            out = llm_ensemble_pipe(
                single_row, models=["m1", "m2", "m3"],
                prompt="x", aggregator="majority",
                min_agreement=0.5,
                use_cache=False,
            )
        assert out.iloc[0]["_llm_status"] == "success"
        assert out.iloc[0]["_llm_ensemble_agreement"] == pytest.approx(2 / 3)


# ═══════════════════════════════════════════════════════════════════
# 5. Partial model failures
# ═══════════════════════════════════════════════════════════════════

class TestPartialModelFailure:
    def test_one_model_errors_others_aggregate(
        self, isolated_router_state,
    ):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="urgent", model_id="m1"),
                LLMRouterError("transport down", error_class="NetErr"),
                _stub_response(text="urgent", model_id="m3"),
            ],
        ):
            out = llm_ensemble_pipe(
                single_row, models=["m1", "m2", "m3"],
                prompt="x", aggregator="majority",
                use_cache=False,
            )
        # 2 of 3 succeeded → urgent wins. Agreement based on non-empty outputs.
        assert out.iloc[0]["_llm_output"] == "urgent"
        # Audit array still has 3 entries (1 empty for the errored model)
        outputs_audit = json.loads(out.iloc[0]["_llm_ensemble_outputs"])
        assert outputs_audit == ["urgent", "", "urgent"]
        # Error column captures the failed model
        assert "m2" in out.iloc[0]["_llm_error"]
        assert "NetErr" in out.iloc[0]["_llm_error"]

    def test_all_models_error_marks_no_consensus(
        self, isolated_router_state,
    ):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                LLMRouterError("e1", error_class="A"),
                LLMRouterError("e2", error_class="B"),
            ],
        ):
            out = llm_ensemble_pipe(
                single_row, models=["m1", "m2"],
                prompt="x", aggregator="majority",
                use_cache=False,
            )
        assert out.iloc[0]["_llm_status"] == "no_consensus"
        assert out.iloc[0]["_llm_output"] == ""


# ═══════════════════════════════════════════════════════════════════
# 6. Empty input
# ═══════════════════════════════════════════════════════════════════

class TestEmptyInput:
    def test_empty_dataframe_returns_well_shaped(
        self, isolated_router_state,
    ):
        empty = pd.DataFrame({"title": pd.array([], dtype="object")})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_ensemble_pipe(
                empty, models=["m1", "m2"], prompt="x",
            )
        assert mock_call.call_count == 0
        for col in (
            "_llm_output", "_llm_status",
            "_llm_ensemble_models", "_llm_ensemble_outputs",
            "_llm_ensemble_agreement", "_llm_ensemble_aggregator",
        ):
            assert col in out.columns
        assert len(out) == 0


# ═══════════════════════════════════════════════════════════════════
# 7. Dry-run (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_returns_single_row_preview(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_ensemble_pipe(
                news_df,
                models=["ollama-llama3-1-8b", "claude-haiku-4-5-20251001"],
                prompt="classify",
                dry_run=True,
            )
        assert mock_call.call_count == 0
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"
        assert out.iloc[0]["_dry_run"] == True
        assert out.iloc[0]["_row_count"] == 2

    def test_dry_run_label_concatenates_models(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm"):
            out = llm_ensemble_pipe(
                news_df,
                models=["ollama-llama3-1-8b", "claude-haiku-4-5-20251001"],
                prompt="classify",
                dry_run=True,
            )
        assert "+" in out.iloc[0]["_llm_model"]


# ═══════════════════════════════════════════════════════════════════
# 8. Budget gate (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestBudgetGate:
    def test_cap_stops_mid_row(
        self, isolated_router_state, monkeypatch,
    ):
        # Each call estimated $1; cap=$2.5 → first 2 calls fit ($2),
        # 3rd call estimate would push to $3 → stop
        from analyzers import llm_router

        def _est(model, prompts, **kwargs):
            return {
                "model_id": model, "provider": "anthropic", "n_calls": len(prompts),
                "input_tokens": 10, "output_tokens": 3,
                "cost_usd": 1.0 * len(prompts), "max_tokens": 100,
            }
        monkeypatch.setattr(llm_router, "estimate_cost_usd", _est)

        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="urgent", cost=1.0)
            out = llm_ensemble_pipe(
                single_row, models=["m1", "m2", "m3"],
                prompt="x", aggregator="majority",
                use_cache=False, max_cost_usd=2.5,
            )
        # 2 calls fit, 3rd estimate-check fails
        assert mock_call.call_count == 2
        # Result has 1 partial row (2 of 3 models succeeded) + sentinel
        assert any(out["_llm_status"] == "budget_exceeded")
        # Partial-result row is present too with the partial aggregation
        non_sentinel = out[out["_llm_status"] != "budget_exceeded"]
        assert len(non_sentinel) == 1
        outputs_audit = json.loads(non_sentinel.iloc[0]["_llm_ensemble_outputs"])
        assert outputs_audit == ["urgent", "urgent"]


# ═══════════════════════════════════════════════════════════════════
# 9. Money-leak canary - THE LOAD-BEARING TEST
# ═══════════════════════════════════════════════════════════════════

class TestMoneyLeakCanary:
    def test_dry_run_makes_zero_call_llm_invocations(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=AssertionError("MONEY LEAK: call_llm fired in dry_run"),
        ):
            out = llm_ensemble_pipe(
                news_df,
                models=["ollama-llama3-1-8b", "claude-haiku-4-5-20251001"],
                prompt="x",
                dry_run=True,
            )
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"

    def test_budget_cap_below_first_call_makes_zero_calls(
        self, news_df, isolated_router_state, monkeypatch,
    ):
        from analyzers import llm_router
        monkeypatch.setattr(
            llm_router, "estimate_cost_usd",
            lambda model, prompts, **kw: {
                "model_id": model, "provider": "x", "n_calls": len(prompts),
                "input_tokens": 1, "output_tokens": 1,
                "cost_usd": 10.0, "max_tokens": 100,
            },
        )
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=AssertionError("MONEY LEAK: cap-below-first-call leaked"),
        ):
            out = llm_ensemble_pipe(
                news_df, models=["m1", "m2"],
                prompt="x",
                max_cost_usd=1.0, use_cache=False,
            )
        # Result is exactly the sentinel - no partial rows persisted.
        # Per the pending-status pattern: if no model call lands for
        # row 0, we don't persist row 0 with bogus state.
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "budget_exceeded"


# ═══════════════════════════════════════════════════════════════════
# 10. Pending-status drift guard - pattern from
# reference_pending_status_for_iterative_pipes.md
# ═══════════════════════════════════════════════════════════════════

class TestPendingStatusDriftGuard:
    """The 2026-05-09 slice-2 bug: when budget cap fires before any
    model call lands for row 0, the row was being persisted with
    bogus _llm_output="" and status="success". Same fix applied in
    slice-3 ensemble: only persist rows that got at least one model
    call attempt.
    """

    def test_cap_before_any_model_call_returns_only_sentinel(
        self, isolated_router_state, monkeypatch,
    ):
        from analyzers import llm_router
        monkeypatch.setattr(
            llm_router, "estimate_cost_usd",
            lambda model, prompts, **kw: {
                "model_id": model, "provider": "x", "n_calls": len(prompts),
                "input_tokens": 1, "output_tokens": 1,
                "cost_usd": 999.0, "max_tokens": 100,
            },
        )
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_ensemble_pipe(
                pd.DataFrame({"title": ["X"], "_epoch": [0]}),
                models=["m1", "m2"], prompt="x",
                max_cost_usd=1.0, use_cache=False,
            )
        assert mock_call.call_count == 0
        # Output is EXACTLY the sentinel - no partial bogus row
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "budget_exceeded"


# ═══════════════════════════════════════════════════════════════════
# 11. Grammar parity drift guard
# ═══════════════════════════════════════════════════════════════════

class TestGrammarParity:
    def test_g4_declares_llm_ensemble_token(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert re.search(r"LLM_ENSEMBLE\s*:\s*'llm_ensemble'", g4)
        assert re.search(r"MODELS\s*:\s*'models'", g4)
        assert re.search(r"AGGREGATOR\s*:\s*'aggregator'", g4)
        assert re.search(r"MIN_AGREEMENT\s*:\s*'min_agreement'", g4)

    def test_models_token_before_model_token_in_g4(self):
        # ANTLR longest-match precedence: `models="..."` would lex as
        # MODEL "s=..." if MODELS came after MODEL. The token order
        # MUST have MODELS first.
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        models_pos = g4.find("MODELS                  : 'models'")
        model_pos = g4.find("MODEL                   : 'model'")
        assert models_pos != -1 and model_pos != -1
        assert models_pos < model_pos, (
            "MODELS token MUST come before MODEL in the lexer rules - "
            "ANTLR longest-match precedence depends on declaration order."
        )

    def test_g4_has_llm_ensemble_grammar_rule(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert "LLM_ENSEMBLE MODELS EQUALS DOUBLE_QUOTED_STRING" in g4

    def test_listener_dispatches_llm_ensemble(self):
        listener = (
            PROJECT_ROOT / "lexers" / "speakesQueryListener.py"
        ).read_text()
        assert '"llm_ensemble": self._cmd_llm_ensemble' in listener
        assert "def _cmd_llm_ensemble" in listener

    def test_grammar_vocab_picks_up_llm_ensemble(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm_ensemble" in names

    def test_handler_module_exports_llm_ensemble_pipe(self):
        from handlers import LLMHandler
        assert "llm_ensemble_pipe" in LLMHandler.__all__


# ═══════════════════════════════════════════════════════════════════
# 12. End-to-end SPQL execution
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndExecution:
    def test_llm_ensemble_query_parses_and_dispatches(
        self, isolated_router_state,
    ):
        from query_engine.CmdExecutionBackend import process_query
        responses = [
            _stub_response(text="urgent", model_id="ollama-llama3-1-8b"),
            _stub_response(text="urgent", model_id="claude-haiku-4-5-20251001"),
        ] * 5
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            df, _job = process_query(
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| head 1 '
                '| llm_ensemble models="ollama-llama3-1-8b,claude-haiku-4-5-20251001" '
                'prompt="classify" '
                'aggregator="majority"'
            )
        assert df is not None
        assert "_llm_ensemble_models" in df.columns
        assert "_llm_ensemble_outputs" in df.columns
        assert "_llm_ensemble_agreement" in df.columns
        assert "_llm_ensemble_aggregator" in df.columns
        assert df.iloc[0]["_llm_ensemble_aggregator"] == "majority"


# ═══════════════════════════════════════════════════════════════════
# 13. Excluded-columns drift guard
# ═══════════════════════════════════════════════════════════════════

class TestExcludedColumnsDriftGuard:
    def test_slice_3_columns_excluded_from_text_feed(self):
        from handlers.LLMHandler import _EXCLUDED_TEXT_COLUMNS
        for col in (
            "_llm_ensemble_models",
            "_llm_ensemble_outputs",
            "_llm_ensemble_agreement",
            "_llm_ensemble_aggregator",
        ):
            assert col in _EXCLUDED_TEXT_COLUMNS, (
                f"_EXCLUDED_TEXT_COLUMNS missing slice-3 column {col!r}. "
                "Without it, re-running | llm on llm_ensemble output feeds "
                "per-model outputs JSON back as input text - silent footgun."
            )
