"""
Tests for handlers/LLMHandler.py llm_until_pipe + the | llm_until SPQL pipe.

Phase 4 / Bet 3 slice 4 - convergence loop with hard ceiling.

Test layout:
  * TestOutputsMatch - _outputs_match helper (case-insensitive stripped equality)
  * TestLlmUntilContract - required kwargs + bad max_iterations + bad threshold
  * TestMaxIterationsCap - runs full max_iterations when no triggers set
  * TestConvergenceContains - substring trigger
  * TestConvergenceUnchanged - output-stability trigger
  * TestConvergenceLowConfidence - confidence-threshold trigger
  * TestConvergenceReason - convergence_reason label correctness
  * TestErrorHandling - call failure mid-loop
  * TestEmptyInput - well-shaped empty result
  * TestCustomIterateTemplate - operator-supplied iterate_prompt overrides default
  * TestDryRun - slice-7 contract: zero provider calls, worst-case estimate
  * TestBudgetGate - slice-7 contract: cap stops mid-loop, sentinel emitted
  * TestMoneyLeakCanary - patch call_llm with raise; dry_run + cap-zero zero invocations
  * TestPendingStatusDriftGuard - cap-on-row-0-iter-0 produces sentinel only
  * TestGrammarParity - .g4 token + listener dispatch + grammar_vocab pickup
  * TestEndToEndExecution - process_query path, full SPQL execution
  * TestExcludedColumnsDriftGuard - slice-4 columns excluded from feed-back
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from analyzers.llm_router import LLMResponse, LLMRouterError
from handlers.LLMHandler import (
    LLMPipeError,
    _outputs_match,
    llm_until_pipe,
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


def _stub_response(text="iter-1", *, cost=0.0001, latency=42, model_id="m"):
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
# 1. _outputs_match helper
# ═══════════════════════════════════════════════════════════════════

class TestOutputsMatch:
    def test_exact_match(self):
        assert _outputs_match("done", "done")

    def test_case_insensitive(self):
        assert _outputs_match("DONE", "done")
        assert _outputs_match("Done", "doNe")

    def test_whitespace_stripped(self):
        assert _outputs_match("  done\n", "done")
        assert _outputs_match("done", "  DONE  ")

    def test_different_strings(self):
        assert not _outputs_match("done", "doing")
        assert not _outputs_match("yes", "no")

    def test_empty_handling(self):
        assert _outputs_match("", "")
        assert _outputs_match(None, "")
        assert _outputs_match(None, None)
        assert not _outputs_match("done", "")


# ═══════════════════════════════════════════════════════════════════
# 2. Required-kwarg validation
# ═══════════════════════════════════════════════════════════════════

class TestLlmUntilContract:
    def test_missing_model_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="model"):
            llm_until_pipe(
                news_df, model="", prompt="x", max_iterations=3,
            )

    def test_missing_prompt_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="prompt"):
            llm_until_pipe(
                news_df, model="m", prompt="", max_iterations=3,
            )

    def test_max_iterations_zero_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="max_iterations"):
            llm_until_pipe(
                news_df, model="m", prompt="x", max_iterations=0,
            )

    def test_bool_max_iterations_rejected(self, news_df):
        with pytest.raises(LLMPipeError):
            llm_until_pipe(
                news_df, model="m", prompt="x", max_iterations=True,
            )

    def test_bool_below_confidence_rejected(self, news_df):
        with pytest.raises(LLMPipeError):
            llm_until_pipe(
                news_df, model="m", prompt="x", max_iterations=3,
                converge_when_below_confidence=True,
            )


# ═══════════════════════════════════════════════════════════════════
# 3. Max iterations cap (no triggers set)
# ═══════════════════════════════════════════════════════════════════

class TestMaxIterationsCap:
    def test_runs_full_max_iterations_with_no_triggers(
        self, isolated_router_state,
    ):
        # 1 row × max_iterations=3 → 3 calls
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        responses = [
            _stub_response(text="iter-1"),
            _stub_response(text="iter-2"),
            _stub_response(text="iter-3"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=3,
                use_cache=False,
            )
        assert mock_call.call_count == 3
        assert out.iloc[0]["_llm_output"] == "iter-3"
        assert out.iloc[0]["_llm_until_iterations"] == 3
        assert out.iloc[0]["_llm_until_converged"] == False
        assert out.iloc[0]["_llm_until_convergence_reason"] == "max_iterations"
        # Audit array contains all iterations
        assert json.loads(out.iloc[0]["_llm_until_outputs"]) == [
            "iter-1", "iter-2", "iter-3",
        ]


# ═══════════════════════════════════════════════════════════════════
# 4. Convergence: substring contains
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceContains:
    def test_first_round_match_short_circuits(self, isolated_router_state):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        responses = [
            _stub_response(text="Result: DONE"),
            # Should never fire
            _stub_response(text="iter-2"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=5,
                converge_when_output_contains="DONE",
                use_cache=False,
            )
        assert mock_call.call_count == 1
        assert out.iloc[0]["_llm_until_iterations"] == 1
        assert out.iloc[0]["_llm_until_converged"] == True
        assert out.iloc[0]["_llm_until_convergence_reason"] == "contains"

    def test_substring_case_insensitive(self, isolated_router_state):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(text="all done here"),
        ) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=5,
                converge_when_output_contains="DONE",
                use_cache=False,
            )
        assert mock_call.call_count == 1
        assert out.iloc[0]["_llm_until_converged"] == True


# ═══════════════════════════════════════════════════════════════════
# 5. Convergence: unchanged output
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceUnchanged:
    def test_stable_output_after_round_2(self, isolated_router_state):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        # Round 1 → "v1", round 2 → "v1" again → unchanged → stop
        responses = [
            _stub_response(text="v1"),
            _stub_response(text="V1"),  # case-insensitive match
            _stub_response(text="v3"),  # should never fire
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=5,
                converge_when_output_unchanged=True,
                use_cache=False,
            )
        assert mock_call.call_count == 2
        assert out.iloc[0]["_llm_until_iterations"] == 2
        assert out.iloc[0]["_llm_until_converged"] == True
        assert out.iloc[0]["_llm_until_convergence_reason"] == "unchanged"

    def test_round_1_alone_does_not_trigger_unchanged(
        self, isolated_router_state,
    ):
        # First iteration has no prior to compare - unchanged trigger
        # only fires from round 2 onward
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="v1"),
                _stub_response(text="v2"),  # different → keep going
                _stub_response(text="v2"),  # same as prev → stop
            ],
        ) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=5,
                converge_when_output_unchanged=True,
                use_cache=False,
            )
        assert mock_call.call_count == 3
        assert out.iloc[0]["_llm_until_iterations"] == 3
        assert out.iloc[0]["_llm_until_converged"] == True


# ═══════════════════════════════════════════════════════════════════
# 6. Convergence: low confidence
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceLowConfidence:
    def test_confidence_below_threshold_stops(self, isolated_router_state):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        # Outputs parse as numbers; loop stops when one is < threshold
        responses = [
            _stub_response(text="0.9"),
            _stub_response(text="0.5"),
            _stub_response(text="0.05"),  # below 0.1 → stop
            # Should never fire
            _stub_response(text="should-not-fire"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=5,
                converge_when_below_confidence=0.1,
                use_cache=False,
            )
        assert mock_call.call_count == 3
        assert out.iloc[0]["_llm_until_iterations"] == 3
        assert out.iloc[0]["_llm_until_converged"] == True
        assert out.iloc[0]["_llm_until_convergence_reason"] == "low_confidence"

    def test_unparseable_output_does_not_trigger_low_confidence(
        self, isolated_router_state,
    ):
        # When NaN (unparseable), this trigger does NOT fire - caller
        # using this trigger wants stable numerics. Loop runs to max.
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="garbage"),
                _stub_response(text="more garbage"),
                _stub_response(text="still no number"),
            ],
        ) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=3,
                converge_when_below_confidence=0.5,
                use_cache=False,
            )
        assert mock_call.call_count == 3
        assert out.iloc[0]["_llm_until_converged"] == False
        assert out.iloc[0]["_llm_until_convergence_reason"] == "max_iterations"


# ═══════════════════════════════════════════════════════════════════
# 7. Convergence reason precedence
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceReason:
    def test_contains_wins_when_both_contains_and_unchanged_could_fire(
        self, isolated_router_state,
    ):
        # Round 2 output is "DONE done" - both contains AND unchanged
        # would match. The contains check runs FIRST, so it wins.
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        responses = [
            _stub_response(text="DONE done"),
            # Doesn't matter - first round triggers contains
            _stub_response(text="DONE done"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_until_pipe(
                single_row, model="m", prompt="x",
                max_iterations=5,
                converge_when_output_contains="DONE",
                converge_when_output_unchanged=True,
                use_cache=False,
            )
        assert mock_call.call_count == 1
        assert out.iloc[0]["_llm_until_convergence_reason"] == "contains"


# ═══════════════════════════════════════════════════════════════════
# 8. Error handling
# ═══════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_first_call_failure_marks_row_error(self, isolated_router_state):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=LLMRouterError("transport down", error_class="NetErr"),
        ):
            out = llm_until_pipe(
                single_row, model="m", prompt="x", max_iterations=3,
                use_cache=False,
            )
        assert out.iloc[0]["_llm_status"] == "error"
        assert "iter_1_failed" in out.iloc[0]["_llm_error"]
        assert out.iloc[0]["_llm_until_iterations"] == 0

    def test_mid_loop_failure_keeps_partial_progress(
        self, isolated_router_state,
    ):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="v1"),
                _stub_response(text="v2"),
                LLMRouterError("api rate limit", error_class="RateLimit"),
            ],
        ):
            out = llm_until_pipe(
                single_row, model="m", prompt="x", max_iterations=5,
                use_cache=False,
            )
        # 2 iterations completed; last one errored mid-loop
        assert out.iloc[0]["_llm_until_iterations"] == 2
        assert out.iloc[0]["_llm_output"] == "v2"
        assert out.iloc[0]["_llm_status"] == "error"
        assert "iter_3_failed" in out.iloc[0]["_llm_error"]


# ═══════════════════════════════════════════════════════════════════
# 9. Empty input
# ═══════════════════════════════════════════════════════════════════

class TestEmptyInput:
    def test_empty_dataframe_returns_well_shaped(
        self, isolated_router_state,
    ):
        empty = pd.DataFrame({"title": pd.array([], dtype="object")})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_until_pipe(
                empty, model="m", prompt="x", max_iterations=3,
            )
        assert mock_call.call_count == 0
        for col in (
            "_llm_output", "_llm_status",
            "_llm_until_iterations", "_llm_until_outputs",
            "_llm_until_converged", "_llm_until_convergence_reason",
        ):
            assert col in out.columns
        assert len(out) == 0


# ═══════════════════════════════════════════════════════════════════
# 10. Custom iterate template
# ═══════════════════════════════════════════════════════════════════

class TestCustomIterateTemplate:
    def test_custom_template_used_for_round_2(self, isolated_router_state):
        captured_prompts = []

        def _record(model_id, *, prompt, **kwargs):
            captured_prompts.append(prompt)
            n = len(captured_prompts)
            return _stub_response(text=f"iter-{n}", model_id=model_id)

        with patch("analyzers.llm_router.call_llm", side_effect=_record):
            llm_until_pipe(
                pd.DataFrame({"title": ["X"], "_epoch": [0]}),
                model="m", prompt="initial-instruction",
                max_iterations=2,
                iterate_prompt="MY-CUSTOM-TEMPLATE: prev={prev_output}",
                use_cache=False,
            )
        assert len(captured_prompts) == 2
        assert "initial-instruction" in captured_prompts[0]
        assert "MY-CUSTOM-TEMPLATE" in captured_prompts[1]
        assert "prev=iter-1" in captured_prompts[1]


# ═══════════════════════════════════════════════════════════════════
# 11. Dry-run (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_returns_single_row_preview(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_until_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_iterations=3,
                dry_run=True,
            )
        assert mock_call.call_count == 0
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"
        assert out.iloc[0]["_dry_run"] == True
        assert out.iloc[0]["_row_count"] == 2


# ═══════════════════════════════════════════════════════════════════
# 12. Budget gate (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestBudgetGate:
    def test_cap_stops_loop_mid_iteration(
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
            mock_call.return_value = _stub_response(text="ok", cost=1.0)
            out = llm_until_pipe(
                single_row, model="m", prompt="x", max_iterations=5,
                use_cache=False, max_cost_usd=2.5,
            )
        # 2 calls fit, 3rd estimate-check fails
        assert mock_call.call_count == 2
        # 1 partial-result row + 1 sentinel
        assert any(out["_llm_status"] == "budget_exceeded")
        non_sentinel = out[out["_llm_status"] != "budget_exceeded"]
        assert len(non_sentinel) == 1
        assert non_sentinel.iloc[0]["_llm_until_iterations"] == 2

    def test_budget_zero_means_uncapped(self, isolated_router_state):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="ok", cost=0.0001)
            out = llm_until_pipe(
                single_row, model="m", prompt="x", max_iterations=3,
                max_cost_usd=0,  # uncapped
                use_cache=False,
            )
        assert mock_call.call_count == 3
        assert not any(out["_llm_status"] == "budget_exceeded")


# ═══════════════════════════════════════════════════════════════════
# 13. Money-leak canary - THE LOAD-BEARING TEST
# ═══════════════════════════════════════════════════════════════════

class TestMoneyLeakCanary:
    def test_dry_run_makes_zero_call_llm_invocations(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=AssertionError("MONEY LEAK: call_llm fired in dry_run"),
        ):
            out = llm_until_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_iterations=3,
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
            out = llm_until_pipe(
                news_df, model="m", prompt="x", max_iterations=3,
                max_cost_usd=1.0, use_cache=False,
            )
        # Result is exactly the sentinel (per pending-status pattern)
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "budget_exceeded"


# ═══════════════════════════════════════════════════════════════════
# 14. Pending-status drift guard
# ═══════════════════════════════════════════════════════════════════

class TestPendingStatusDriftGuard:
    """Same load-bearing pattern as slice-2 + slice-3: when the budget
    cap fires before any call lands for row 0, the result must be
    EXACTLY the sentinel - no partial bogus row.
    """

    def test_cap_before_any_call_returns_only_sentinel(
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
            out = llm_until_pipe(
                pd.DataFrame({"title": ["X"], "_epoch": [0]}),
                model="m", prompt="x", max_iterations=3,
                max_cost_usd=1.0, use_cache=False,
            )
        assert mock_call.call_count == 0
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "budget_exceeded"


# ═══════════════════════════════════════════════════════════════════
# 15. Grammar parity drift guard
# ═══════════════════════════════════════════════════════════════════

class TestGrammarParity:
    def test_g4_declares_llm_until_token(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert re.search(r"LLM_UNTIL\s*:\s*'llm_until'", g4)
        assert re.search(r"ITERATE_PROMPT\s*:\s*'iterate_prompt'", g4)
        assert re.search(r"MAX_ITERATIONS\s*:\s*'max_iterations'", g4)
        assert re.search(
            r"CONVERGE_WHEN_OUTPUT_CONTAINS\s*:\s*'converge_when_output_contains'",
            g4,
        )
        assert re.search(
            r"CONVERGE_WHEN_OUTPUT_UNCHANGED\s*:\s*'converge_when_output_unchanged'",
            g4,
        )
        assert re.search(
            r"CONVERGE_WHEN_BELOW_CONFIDENCE\s*:\s*'converge_when_below_confidence'",
            g4,
        )

    def test_g4_has_llm_until_grammar_rule(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert "LLM_UNTIL MODEL EQUALS DOUBLE_QUOTED_STRING" in g4
        assert "MAX_ITERATIONS EQUALS NUMBER" in g4

    def test_listener_dispatches_llm_until(self):
        listener = (
            PROJECT_ROOT / "lexers" / "speakesQueryListener.py"
        ).read_text()
        assert '"llm_until": self._cmd_llm_until' in listener
        assert "def _cmd_llm_until" in listener

    def test_grammar_vocab_picks_up_llm_until(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm_until" in names

    def test_handler_module_exports_llm_until_pipe(self):
        from handlers import LLMHandler
        assert "llm_until_pipe" in LLMHandler.__all__


# ═══════════════════════════════════════════════════════════════════
# 16. End-to-end SPQL execution
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndExecution:
    def test_llm_until_query_parses_and_dispatches(
        self, isolated_router_state,
    ):
        from query_engine.CmdExecutionBackend import process_query
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(text="DONE"),
        ):
            df, _job = process_query(
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| head 1 '
                '| llm_until model="claude-haiku-4-5-20251001" '
                'prompt="iterate" '
                'max_iterations=3 '
                'converge_when_output_contains="DONE"'
            )
        assert df is not None
        assert "_llm_until_iterations" in df.columns
        assert "_llm_until_outputs" in df.columns
        assert "_llm_until_converged" in df.columns
        assert "_llm_until_convergence_reason" in df.columns
        # First call returns DONE → converged after 1 iteration
        assert df.iloc[0]["_llm_until_converged"] == True
        assert df.iloc[0]["_llm_until_convergence_reason"] == "contains"


# ═══════════════════════════════════════════════════════════════════
# 17. Excluded-columns drift guard
# ═══════════════════════════════════════════════════════════════════

class TestExcludedColumnsDriftGuard:
    def test_slice_4_columns_excluded_from_text_feed(self):
        from handlers.LLMHandler import _EXCLUDED_TEXT_COLUMNS
        for col in (
            "_llm_until_iterations",
            "_llm_until_outputs",
            "_llm_until_converged",
            "_llm_until_convergence_reason",
        ):
            assert col in _EXCLUDED_TEXT_COLUMNS, (
                f"_EXCLUDED_TEXT_COLUMNS missing slice-4 column {col!r}. "
                "Without it, re-running | llm on llm_until output feeds "
                "iteration metadata back as input - silent footgun."
            )
