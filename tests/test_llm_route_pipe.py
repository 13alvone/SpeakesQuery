"""
Tests for handlers/LLMHandler.py llm_route_pipe + the | llm_route SPQL pipe.

Phase 4 / Bet 3 slice 1 - confidence-based 2-stage cost cascade.

Test layout:
  * TestConfidenceParsing - _parse_confidence helper (the trigger logic)
  * TestLlmRouteContract - required kwargs, validation
  * TestStage1Only - all rows pass confidence; stage 2 not called
  * TestEscalation - low-confidence / errored rows escalate; stage 2 wins
  * TestEmptyInput - well-shaped empty result
  * TestCustomEscalatePrompt - escalate_prompt overrides primary
  * TestDryRun - slice-7 contract: zero provider calls, worst-case estimate
  * TestBudgetGate - slice-7 contract: cap stops escalation, sentinel emitted
  * TestMoneyLeakCanary - patch call_llm with raise; dry_run + cap path zero invocations
  * TestGrammarParity - .g4 token + listener dispatch + grammar_vocab pickup
  * TestEndToEndExecution - process_query path, full SPQL execution
  * TestExcludedColumnsDriftGuard - slice-9 columns excluded from feed-back
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
    _parse_confidence,
    llm_route_pipe,
)


PROJECT_ROOT = Path(__file__).parent.parent


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


def _stub_response(text="0.9", *, cost=0.0001, latency=42, model_id="m"):
    return LLMResponse(
        text=text, model_id=model_id, provider="anthropic",
        model_name="m-name", input_tokens=10, output_tokens=3,
        cost_usd=cost, latency_ms=latency, request_id="rid",
    )


@pytest.fixture
def isolated_router_state(tmp_path, monkeypatch):
    """Same isolation pattern as test_llm_pipe.py - slice-3 history
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


# ═══════════════════════════════════════════════════════════════════
# 1. _parse_confidence - the trigger logic
# ═══════════════════════════════════════════════════════════════════

class TestConfidenceParsing:
    """The 3-strategy parser: whole-string float → JSON → first number."""

    def test_whole_string_float(self):
        assert _parse_confidence("0.85") == pytest.approx(0.85)
        assert _parse_confidence("  0.7\n") == pytest.approx(0.7)
        assert _parse_confidence("1") == 1.0
        assert _parse_confidence("0") == 0.0

    def test_negative_numbers_parse(self):
        # Negatives are valid floats (unusual for confidence but the
        # parser doesn't gate on sign - threshold check does).
        assert _parse_confidence("-0.5") == -0.5

    def test_json_with_confidence_key(self):
        assert _parse_confidence('{"confidence": 0.85}') == pytest.approx(0.85)
        assert _parse_confidence(
            '{"label": "urgent", "confidence": 0.9}'
        ) == pytest.approx(0.9)

    def test_json_without_confidence_key_falls_through(self):
        # No confidence key → falls through to regex strategy.
        # The JSON has no number outside the dict, so regex finds none → NaN.
        v = _parse_confidence('{"label": "urgent"}')
        assert np.isnan(v) or v == 0.0  # either accepted

    def test_first_number_in_text(self):
        assert _parse_confidence("I'm 85% confident") == pytest.approx(0.85)
        assert _parse_confidence(
            "Confidence: 0.7"
        ) == pytest.approx(0.7)
        assert _parse_confidence("score=42") == pytest.approx(42.0)

    def test_no_number_returns_nan(self):
        assert np.isnan(_parse_confidence("nope"))
        assert np.isnan(_parse_confidence(""))
        assert np.isnan(_parse_confidence("   "))
        assert np.isnan(_parse_confidence(None))

    def test_percentage_normalised_to_decimal(self):
        # "85%" → 0.85; the % suffix triggers the divide
        assert _parse_confidence("85%") == pytest.approx(0.85)


# ═══════════════════════════════════════════════════════════════════
# 2. Required-kwarg validation
# ═══════════════════════════════════════════════════════════════════

class TestLlmRouteContract:
    def test_missing_model_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="model"):
            llm_route_pipe(
                news_df, model="", prompt="x", escalate_to="big",
            )

    def test_missing_prompt_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="prompt"):
            llm_route_pipe(
                news_df, model="cheap", prompt="", escalate_to="big",
            )

    def test_missing_escalate_to_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="escalate_to"):
            llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="",
            )

    def test_non_numeric_threshold_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="threshold"):
            llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold="not-a-number",
            )

    def test_bool_threshold_rejected(self, news_df):
        # Bool is technically a subclass of int - we reject it
        # explicitly so True doesn't silently mean 1.0
        with pytest.raises(LLMPipeError):
            llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold=True,
            )


# ═══════════════════════════════════════════════════════════════════
# 3. Stage 1 only - all rows pass; no escalation
# ═══════════════════════════════════════════════════════════════════

class TestStage1Only:
    def test_high_confidence_skips_escalation(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="0.95", cost=0.0001)
            out = llm_route_pipe(
                news_df, model="ollama-llama3-1-8b",
                prompt="rate confidence 0-1",
                escalate_to="claude-haiku-4-5-20251001",
                confidence_threshold=0.5,
                use_cache=False,
            )
        # Every row called the cheap model exactly once; stage 2 zero times
        assert mock_call.call_count == 3, (
            f"Expected 3 calls (cheap-only); got {mock_call.call_count}"
        )
        assert all(out["_llm_route_escalated"] == False)
        assert all(out["_llm_route_confidence"] == 0.95)
        assert all(out["_llm_route_stage_1_output"] == "0.95")
        assert all(out["_llm_status"] == "success")

    def test_threshold_boundary_exactly_at_threshold_does_not_escalate(
        self, news_df, isolated_router_state,
    ):
        # confidence == threshold → does NOT escalate (< is strict)
        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="0.5", cost=0.0001)
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold=0.5, use_cache=False,
            )
        assert mock_call.call_count == 3
        assert all(out["_llm_route_escalated"] == False)


# ═══════════════════════════════════════════════════════════════════
# 4. Escalation - low-confidence rows escalate; stage 2 wins
# ═══════════════════════════════════════════════════════════════════

class TestEscalation:
    def test_low_confidence_triggers_escalation(
        self, news_df, isolated_router_state,
    ):
        # Cheap returns 0.2 (below threshold 0.5) on every row → all escalate
        # Expensive returns "ESCALATED-OUTPUT"
        responses = [
            _stub_response(text="0.2", cost=0.0001, model_id="cheap"),  # row 0 stage 1
            _stub_response(text="0.2", cost=0.0001, model_id="cheap"),  # row 1 stage 1
            _stub_response(text="0.2", cost=0.0001, model_id="cheap"),  # row 2 stage 1
            _stub_response(text="ESCALATED-0", cost=0.001, model_id="big"),
            _stub_response(text="ESCALATED-1", cost=0.001, model_id="big"),
            _stub_response(text="ESCALATED-2", cost=0.001, model_id="big"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_route_pipe(
                news_df, model="ollama-llama3-1-8b",
                prompt="rate", escalate_to="claude-haiku-4-5-20251001",
                confidence_threshold=0.5,
                use_cache=False,
            )
        # All escalated
        assert all(out["_llm_route_escalated"] == True)
        # Stage 1 output preserved verbatim
        assert all(out["_llm_route_stage_1_output"] == "0.2")
        # Final output = stage 2 output
        assert list(out["_llm_output"]) == [
            "ESCALATED-0", "ESCALATED-1", "ESCALATED-2",
        ]
        # Final model = stage 2 model
        assert all(out["_llm_model"] == "big")

    def test_mixed_confidence_partial_escalation(
        self, news_df, isolated_router_state,
    ):
        # Stage 1: row 0 → 0.9 (passes), row 1 → 0.3 (escalates),
        # row 2 → 0.95 (passes)
        # Stage 2: only row 1 escalates → returns "BIG-1"
        responses = [
            _stub_response(text="0.9", model_id="cheap"),
            _stub_response(text="0.3", model_id="cheap"),
            _stub_response(text="0.95", model_id="cheap"),
            _stub_response(text="BIG-1", model_id="big"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold=0.5,
                use_cache=False,
            )
        assert list(out["_llm_route_escalated"]) == [False, True, False]
        assert list(out["_llm_output"]) == ["0.9", "BIG-1", "0.95"]
        assert list(out["_llm_model"]) == ["cheap", "big", "cheap"]
        # Stage 1 outputs preserved for ALL rows (audit trail)
        assert list(out["_llm_route_stage_1_output"]) == ["0.9", "0.3", "0.95"]

    def test_unparseable_output_triggers_escalation(
        self, news_df, isolated_router_state,
    ):
        # Stage 1 returns text without a number → confidence=NaN → escalates
        responses = [
            _stub_response(text="urgent", model_id="cheap"),
            _stub_response(text="urgent", model_id="cheap"),
            _stub_response(text="urgent", model_id="cheap"),
            _stub_response(text="big-0", model_id="big"),
            _stub_response(text="big-1", model_id="big"),
            _stub_response(text="big-2", model_id="big"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold=0.5,
                use_cache=False,
            )
        assert all(out["_llm_route_escalated"] == True)
        # Confidence is NaN for unparseable outputs
        assert all(out["_llm_route_confidence"].isna())

    def test_stage_1_error_triggers_escalation(
        self, news_df, isolated_router_state,
    ):
        # Stage 1 errors → row should escalate with the expensive model
        responses = [
            LLMRouterError("network down", error_class="NetworkError"),
            _stub_response(text="0.9", model_id="cheap"),
            _stub_response(text="0.9", model_id="cheap"),
            _stub_response(text="big-recovery", model_id="big"),  # stage 2 for row 0
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold=0.5,
                use_cache=False,
            )
        # Row 0 escalated due to stage-1 error; rows 1-2 did not escalate
        assert list(out["_llm_route_escalated"]) == [True, False, False]
        assert out.iloc[0]["_llm_output"] == "big-recovery"
        assert out.iloc[0]["_llm_status"] == "success"

    def test_both_stages_fail_keeps_error(
        self, news_df, isolated_router_state,
    ):
        # Stage 1 errors AND stage 2 errors → row marked error with
        # combined message
        df = news_df.iloc[:1].copy()
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                LLMRouterError("cheap fail", error_class="A"),
                LLMRouterError("big fail", error_class="B"),
            ],
        ):
            out = llm_route_pipe(
                df, model="cheap", prompt="x", escalate_to="big",
                use_cache=False,
            )
        assert out.iloc[0]["_llm_route_escalated"] == True
        assert "both_stages_failed" in out.iloc[0]["_llm_error"]


# ═══════════════════════════════════════════════════════════════════
# 5. Empty input
# ═══════════════════════════════════════════════════════════════════

class TestEmptyInput:
    def test_empty_dataframe_returns_well_shaped(
        self, isolated_router_state,
    ):
        empty = pd.DataFrame({"title": pd.array([], dtype="object")})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_route_pipe(
                empty, model="cheap", prompt="x", escalate_to="big",
            )
        assert mock_call.call_count == 0
        # All slice-9 columns present
        for col in (
            "_llm_output", "_llm_status", "_llm_error",
            "_llm_route_escalated", "_llm_route_stage_1_output",
            "_llm_route_confidence",
        ):
            assert col in out.columns
        assert len(out) == 0

    def test_none_input_returns_well_shaped(self, isolated_router_state):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_route_pipe(
                None, model="cheap", prompt="x", escalate_to="big",
            )
        assert mock_call.call_count == 0
        assert len(out) == 0


# ═══════════════════════════════════════════════════════════════════
# 6. Custom escalate_prompt
# ═══════════════════════════════════════════════════════════════════

class TestCustomEscalatePrompt:
    def test_escalate_prompt_overrides_primary_for_stage_2(
        self, news_df, isolated_router_state,
    ):
        # Stage 1 prompt: "rate confidence"; stage 2 prompt: "deep analysis"
        captured_prompts = []

        def _record(model_id, *, prompt, **kwargs):
            captured_prompts.append((model_id, prompt))
            if model_id == "big":
                return _stub_response(text="big-out", model_id="big")
            return _stub_response(text="0.1", model_id="cheap")

        with patch("analyzers.llm_router.call_llm", side_effect=_record):
            llm_route_pipe(
                news_df.iloc[:1].copy(),
                model="cheap", prompt="rate confidence",
                escalate_to="big", escalate_prompt="DEEP ANALYSIS PROMPT",
                confidence_threshold=0.5,
                use_cache=False,
            )
        # Two prompts captured: stage 1 with "rate confidence",
        # stage 2 with "DEEP ANALYSIS PROMPT"
        assert len(captured_prompts) == 2
        assert "rate confidence" in captured_prompts[0][1]
        assert captured_prompts[0][0] == "cheap"
        assert "DEEP ANALYSIS PROMPT" in captured_prompts[1][1]
        assert captured_prompts[1][0] == "big"

    def test_escalate_prompt_defaults_to_primary(
        self, news_df, isolated_router_state,
    ):
        captured_prompts = []

        def _record(model_id, *, prompt, **kwargs):
            captured_prompts.append(prompt)
            return _stub_response(
                text="0.1" if model_id == "cheap" else "big-out",
                model_id=model_id,
            )

        with patch("analyzers.llm_router.call_llm", side_effect=_record):
            llm_route_pipe(
                news_df.iloc[:1].copy(),
                model="cheap", prompt="SAME PROMPT",
                escalate_to="big",
                # NOTE: no escalate_prompt kwarg
                confidence_threshold=0.5,
                use_cache=False,
            )
        # Both stages got the same prompt
        assert len(captured_prompts) == 2
        assert "SAME PROMPT" in captured_prompts[0]
        assert "SAME PROMPT" in captured_prompts[1]


# ═══════════════════════════════════════════════════════════════════
# 7. Dry-run (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_returns_single_row_preview(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_route_pipe(
                news_df, model="ollama-llama3-1-8b",
                prompt="rate", escalate_to="claude-haiku-4-5-20251001",
                dry_run=True,
            )
        assert mock_call.call_count == 0
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"
        assert out.iloc[0]["_dry_run"] == True
        assert out.iloc[0]["_row_count"] == 3
        # Worst-case = cheap cost + escalate cost (every row escalates)
        assert out.iloc[0]["_estimated_cost_usd"] >= 0.0

    def test_dry_run_arrow_in_model_label(
        self, news_df, isolated_router_state,
    ):
        # Model label shows "cheap → escalate" so operators see both
        with patch("analyzers.llm_router.call_llm"):
            out = llm_route_pipe(
                news_df, model="ollama-llama3-1-8b",
                prompt="rate", escalate_to="claude-haiku-4-5-20251001",
                dry_run=True,
            )
        assert " → " in out.iloc[0]["_llm_model"]


# ═══════════════════════════════════════════════════════════════════
# 8. Budget gate (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestBudgetGate:
    def test_budget_cap_stops_stage_1_processing(
        self, news_df, isolated_router_state, monkeypatch,
    ):
        # Set up: stub estimate_cost_usd to claim each row costs $1
        # cap=$1.5 → first row passes ($1 cumulative), second row check
        # would push to $2 → exceeded → stop
        from analyzers import llm_router

        def _est(model, prompts, **kwargs):
            return {
                "model_id": model, "provider": "anthropic", "n_calls": len(prompts),
                "input_tokens": 10, "output_tokens": 3,
                "cost_usd": 1.0 * len(prompts), "max_tokens": 100,
            }
        monkeypatch.setattr(llm_router, "estimate_cost_usd", _est)

        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="0.9", cost=1.0)
            out = llm_route_pipe(
                news_df, model="m", prompt="x", escalate_to="big",
                use_cache=False, max_cost_usd=1.5,
            )
        # First row processed, second pre-call check failed → stopped
        # Result: 1 success row + 1 sentinel row
        assert mock_call.call_count == 1
        assert len(out) == 2
        assert out.iloc[-1]["_llm_status"] == "budget_exceeded"

    def test_budget_cap_stops_stage_2_escalation(
        self, news_df, isolated_router_state, monkeypatch,
    ):
        # Stage 1: cheap rows pass (low cost); stage 2 calls are
        # expensive enough to trip the cap.
        from analyzers import llm_router

        # Track estimate calls so we can return small costs for cheap, big for escalate
        def _est(model, prompts, **kwargs):
            cost_per = 0.01 if model == "cheap" else 5.0
            return {
                "model_id": model, "provider": "anthropic", "n_calls": len(prompts),
                "input_tokens": 10, "output_tokens": 3,
                "cost_usd": cost_per * len(prompts), "max_tokens": 100,
            }
        monkeypatch.setattr(llm_router, "estimate_cost_usd", _est)

        with patch("analyzers.llm_router.call_llm") as mock_call:
            # Stage 1 returns low confidence → all want to escalate
            mock_call.return_value = _stub_response(text="0.1", cost=0.01)
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                confidence_threshold=0.5,
                use_cache=False, max_cost_usd=2.0,  # Stage 1 = $0.03 ok; first stage-2 call est $5 → cap
            )
        # Stage 1: 3 cheap calls. Stage 2: zero (first one would exceed cap).
        assert mock_call.call_count == 3
        # Sentinel appended
        assert any(out["_llm_status"] == "budget_exceeded")

    def test_budget_zero_means_uncapped(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="0.9", cost=0.0001)
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                max_cost_usd=0,  # zero = uncapped per slice-7 convention
                use_cache=False,
            )
        # All 3 rows processed without sentinel
        assert mock_call.call_count == 3
        assert not any(out["_llm_status"] == "budget_exceeded")


# ═══════════════════════════════════════════════════════════════════
# 9. Money-leak canary - THE LOAD-BEARING TEST
# ═══════════════════════════════════════════════════════════════════

class TestMoneyLeakCanary:
    """Patch call_llm with AssertionError("MONEY LEAK"); both dry_run
    paths and the budget-cap-immediately paths must produce zero
    invocations. Same pattern as test_llm_pipe_slice7.py.
    """

    def test_dry_run_makes_zero_call_llm_invocations(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=AssertionError("MONEY LEAK: call_llm fired in dry_run"),
        ):
            out = llm_route_pipe(
                news_df, model="ollama-llama3-1-8b",
                prompt="rate", escalate_to="claude-haiku-4-5-20251001",
                dry_run=True,
            )
        # Test passes by NOT raising - call_llm was never called
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"

    def test_budget_cap_below_first_call_estimate_makes_zero_calls(
        self, news_df, isolated_router_state, monkeypatch,
    ):
        # estimate says first call alone would cost $10; cap is $1
        # → stops BEFORE the first call_llm invocation
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
            out = llm_route_pipe(
                news_df, model="cheap", prompt="x", escalate_to="big",
                max_cost_usd=1.0, use_cache=False,
            )
        # call_llm never invoked - output is just the sentinel
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "budget_exceeded"


# ═══════════════════════════════════════════════════════════════════
# 10. Grammar parity drift guard
# ═══════════════════════════════════════════════════════════════════

class TestGrammarParity:
    def test_g4_declares_llm_route_token(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert re.search(r"LLM_ROUTE\s*:\s*'llm_route'", g4)
        assert re.search(r"ESCALATE_TO\s*:\s*'escalate_to'", g4)
        assert re.search(r"ESCALATE_PROMPT\s*:\s*'escalate_prompt'", g4)
        assert re.search(
            r"CONFIDENCE_THRESHOLD\s*:\s*'confidence_threshold'", g4,
        )

    def test_g4_has_llm_route_grammar_rule(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        # The pipe rule should appear with LLM_ROUTE + required kwargs
        assert "LLM_ROUTE MODEL EQUALS DOUBLE_QUOTED_STRING" in g4
        assert "ESCALATE_TO EQUALS DOUBLE_QUOTED_STRING" in g4

    def test_listener_dispatches_llm_route(self):
        listener = (PROJECT_ROOT / "lexers" / "speakesQueryListener.py").read_text()
        assert '"llm_route": self._cmd_llm_route' in listener
        assert "def _cmd_llm_route" in listener

    def test_grammar_vocab_picks_up_llm_route(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab()
        # commands list contains dicts of {kind, name}; pick the names
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm_route" in names

    def test_handler_module_exports_llm_route_pipe(self):
        # __all__ in handlers/LLMHandler.py should include the new pipe
        from handlers import LLMHandler
        assert "llm_route_pipe" in LLMHandler.__all__


# ═══════════════════════════════════════════════════════════════════
# 11. End-to-end: SPQL parse → execute path
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndExecution:
    def test_llm_route_query_parses_and_dispatches(
        self, isolated_router_state, monkeypatch, tmp_path,
    ):
        """Full SPQL execution: parse `| llm_route ...` → listener
        dispatches → handler runs → result returned."""
        # Use the shipped test parquet
        from query_engine.CmdExecutionBackend import process_query

        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(
                text="0.9", cost=0.0001, model_id="ollama-llama3-1-8b",
            )
            df, _job = process_query(
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| head 2 '
                '| llm_route model="ollama-llama3-1-8b" '
                'prompt="rate confidence" '
                'escalate_to="claude-haiku-4-5-20251001" '
                'confidence_threshold=0.5'
            )
        assert df is not None
        assert "_llm_route_escalated" in df.columns
        assert "_llm_route_confidence" in df.columns
        # All rows passed (0.9 > 0.5) - no escalation
        assert all(df["_llm_route_escalated"] == False)


# ═══════════════════════════════════════════════════════════════════
# 12. Excluded-columns drift guard
# ═══════════════════════════════════════════════════════════════════

class TestExcludedColumnsDriftGuard:
    """The slice-9 columns must be in _EXCLUDED_TEXT_COLUMNS so a
    re-run of any | llm-shaped pipe on the prior pipe's output
    doesn't feed cascade metadata back as input. Pin this - adding
    a new _llm_route_* column without updating the exclude set is
    a footgun.
    """

    def test_slice_9_columns_excluded_from_text_feed(self):
        from handlers.LLMHandler import _EXCLUDED_TEXT_COLUMNS
        for col in (
            "_llm_route_escalated",
            "_llm_route_stage_1_output",
            "_llm_route_confidence",
        ):
            assert col in _EXCLUDED_TEXT_COLUMNS, (
                f"_EXCLUDED_TEXT_COLUMNS missing slice-9 column {col!r}. "
                "Without it, re-running | llm on llm_route output feeds "
                "cascade metadata back as input text - silent footgun."
            )
