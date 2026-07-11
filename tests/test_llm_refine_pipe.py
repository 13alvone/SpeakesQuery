"""
Tests for handlers/LLMHandler.py llm_refine_pipe + the | llm_refine SPQL pipe.

Phase 4 / Bet 3 slice 2 - drafter/critic refinement loop.

Test layout:
  * TestConvergenceCheck - _check_converged helper (case-insensitive substring)
  * TestLlmRefineContract - required kwargs + bad max_rounds
  * TestSingleRoundNoConvergence - max_rounds=1 → 1 drafter + 1 critic per row
  * TestMultiRoundFullCycles - max_rounds=3, no convergence signal → full 3 rounds
  * TestEarlyConvergence - convergence signal short-circuits the loop
  * TestErrorHandling - drafter fails / critic fails / both fail
  * TestEmptyInput - well-shaped empty result
  * TestCustomReviseTemplate - operator-supplied revise_prompt overrides default
  * TestDryRun - slice-7 contract: zero provider calls, worst-case estimate
  * TestBudgetGate - slice-7 contract: cap stops mid-loop, sentinel emitted
  * TestMoneyLeakCanary - patch call_llm with raise; dry_run + cap path zero invocations
  * TestGrammarParity - .g4 token + listener dispatch + grammar_vocab pickup
  * TestEndToEndExecution - process_query path, full SPQL execution
  * TestExcludedColumnsDriftGuard - slice-2 columns excluded from feed-back
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
    _check_converged,
    llm_refine_pipe,
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


def _stub_response(text="draft", *, cost=0.0001, latency=42, model_id="m"):
    return LLMResponse(
        text=text, model_id=model_id, provider="anthropic",
        model_name="m-name", input_tokens=10, output_tokens=3,
        cost_usd=cost, latency_ms=latency, request_id="rid",
    )


@pytest.fixture
def isolated_router_state(tmp_path, monkeypatch):
    """Same isolation pattern as test_llm_route_pipe.py."""
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
# 1. _check_converged helper
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceCheck:
    def test_substring_match_case_insensitive(self):
        assert _check_converged("Looks good. APPROVED.", "approved")
        assert _check_converged("APPROVED", "approved")
        assert _check_converged("approved", "APPROVED")
        assert _check_converged("APP-ROVED. great work.", "approved") is False

    def test_no_signal_never_converges(self):
        # No signal → never short-circuits regardless of critique content
        assert _check_converged("APPROVED", None) is False
        assert _check_converged("APPROVED", "") is False

    def test_empty_critique_never_converges(self):
        assert _check_converged("", "approved") is False
        assert _check_converged(None, "approved") is False


# ═══════════════════════════════════════════════════════════════════
# 2. Required-kwarg validation
# ═══════════════════════════════════════════════════════════════════

class TestLlmRefineContract:
    def _good(self, df):
        return dict(
            df=df,
            drafter_model="d", critic_model="c",
            drafter_prompt="draft", critic_prompt="critique",
        )

    def test_missing_drafter_model_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="drafter_model"):
            llm_refine_pipe(
                news_df, drafter_model="", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
            )

    def test_missing_critic_model_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="critic_model"):
            llm_refine_pipe(
                news_df, drafter_model="d", critic_model="",
                drafter_prompt="d", critic_prompt="c",
            )

    def test_missing_drafter_prompt_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="drafter_prompt"):
            llm_refine_pipe(
                news_df, drafter_model="d", critic_model="c",
                drafter_prompt="", critic_prompt="c",
            )

    def test_missing_critic_prompt_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="critic_prompt"):
            llm_refine_pipe(
                news_df, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="",
            )

    def test_max_rounds_below_1_rejected(self, news_df):
        with pytest.raises(LLMPipeError, match="max_rounds"):
            llm_refine_pipe(
                news_df, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
                max_rounds=0,
            )

    def test_bool_max_rounds_rejected(self, news_df):
        # Bool is technically a subclass of int - reject explicitly
        with pytest.raises(LLMPipeError):
            llm_refine_pipe(
                news_df, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
                max_rounds=True,
            )


# ═══════════════════════════════════════════════════════════════════
# 3. Single round (no revisions)
# ═══════════════════════════════════════════════════════════════════

class TestSingleRoundNoConvergence:
    def test_max_rounds_1_makes_1_drafter_1_critic_per_row(
        self, news_df, isolated_router_state,
    ):
        # 2 rows × (1 drafter + 1 critic) = 4 calls total
        responses = [
            _stub_response(text="draft-A", model_id="d"),
            _stub_response(text="critique-A: ok", model_id="c"),
            _stub_response(text="draft-B", model_id="d"),
            _stub_response(text="critique-B: ok", model_id="c"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_refine_pipe(
                news_df, drafter_model="d", critic_model="c",
                drafter_prompt="draft this", critic_prompt="critique this",
                max_rounds=1,
                use_cache=False,
            )
        assert mock_call.call_count == 4
        # Final output = draft from round 1
        assert list(out["_llm_output"]) == ["draft-A", "draft-B"]
        # Each row had 1 drafter round
        assert all(out["_llm_refine_rounds"] == 1)
        # No convergence (no signal supplied)
        assert all(out["_llm_refine_converged"] == False)
        # Audit columns parse as JSON arrays
        for d in out["_llm_refine_drafts"]:
            assert isinstance(json.loads(d), list)
        for c in out["_llm_refine_critiques"]:
            assert isinstance(json.loads(c), list)


# ═══════════════════════════════════════════════════════════════════
# 4. Multi-round full cycles (no convergence signal)
# ═══════════════════════════════════════════════════════════════════

class TestMultiRoundFullCycles:
    def test_max_rounds_3_runs_3_drafters_3_critics(
        self, isolated_router_state,
    ):
        # 1 row × max_rounds=3 → 3 drafters + 3 critics = 6 calls
        single_row = pd.DataFrame({
            "title": ["Federal Reserve pauses"], "_epoch": [1700000000],
        })
        responses = [
            _stub_response(text="draft-1", model_id="d"),
            _stub_response(text="critique-1: needs work", model_id="c"),
            _stub_response(text="draft-2", model_id="d"),
            _stub_response(text="critique-2: better", model_id="c"),
            _stub_response(text="draft-3", model_id="d"),
            _stub_response(text="critique-3: ok", model_id="c"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="draft", critic_prompt="critique",
                max_rounds=3,
                use_cache=False,
            )
        assert mock_call.call_count == 6
        # Final output = last draft
        assert out.iloc[0]["_llm_output"] == "draft-3"
        # 3 rounds completed
        assert out.iloc[0]["_llm_refine_rounds"] == 3
        # All 3 drafts in audit
        assert json.loads(out.iloc[0]["_llm_refine_drafts"]) == [
            "draft-1", "draft-2", "draft-3",
        ]
        # All 3 critiques in audit
        assert json.loads(out.iloc[0]["_llm_refine_critiques"]) == [
            "critique-1: needs work", "critique-2: better", "critique-3: ok",
        ]
        assert out.iloc[0]["_llm_refine_converged"] == False


# ═══════════════════════════════════════════════════════════════════
# 5. Early convergence
# ═══════════════════════════════════════════════════════════════════

class TestEarlyConvergence:
    def test_critic_signals_approved_short_circuits(
        self, isolated_router_state,
    ):
        # Round 1 critic says APPROVED → loop exits after round 1
        # max_rounds=3 but only 1 round actually runs
        single_row = pd.DataFrame({
            "title": ["Federal Reserve pauses"], "_epoch": [1700000000],
        })
        responses = [
            _stub_response(text="draft-1", model_id="d"),
            _stub_response(text="Looks great. APPROVED.", model_id="c"),
            # Should never be called
            _stub_response(text="should-not-fire", model_id="d"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="draft", critic_prompt="critique",
                max_rounds=3,
                converge_when_critic_says="APPROVED",
                use_cache=False,
            )
        # Only 2 calls total (not 6)
        assert mock_call.call_count == 2
        assert out.iloc[0]["_llm_output"] == "draft-1"
        assert out.iloc[0]["_llm_refine_rounds"] == 1
        assert out.iloc[0]["_llm_refine_converged"] == True

    def test_convergence_after_round_2(self, isolated_router_state):
        single_row = pd.DataFrame({
            "title": ["Test"], "_epoch": [0],
        })
        responses = [
            _stub_response(text="draft-1", model_id="d"),
            _stub_response(text="needs revision", model_id="c"),
            _stub_response(text="draft-2", model_id="d"),
            _stub_response(text="approved", model_id="c"),
            # Should never fire
            _stub_response(text="never-3", model_id="d"),
        ]
        with patch("analyzers.llm_router.call_llm", side_effect=responses) as mock_call:
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="draft", critic_prompt="critique",
                max_rounds=5,
                converge_when_critic_says="approved",
                use_cache=False,
            )
        # 4 calls (2 drafter + 2 critic), not 10
        assert mock_call.call_count == 4
        assert out.iloc[0]["_llm_refine_rounds"] == 2
        assert out.iloc[0]["_llm_refine_converged"] == True
        assert out.iloc[0]["_llm_output"] == "draft-2"


# ═══════════════════════════════════════════════════════════════════
# 6. Error handling
# ═══════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_drafter_round_1_failure_marks_row_error(
        self, isolated_router_state,
    ):
        single_row = pd.DataFrame({
            "title": ["X"], "_epoch": [0],
        })
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=LLMRouterError("transport down", error_class="NetErr"),
        ):
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="draft", critic_prompt="critique",
                max_rounds=3,
                use_cache=False,
            )
        assert out.iloc[0]["_llm_status"] == "error"
        assert "drafter_round_1_failed" in out.iloc[0]["_llm_error"]
        assert out.iloc[0]["_llm_refine_rounds"] == 0

    def test_critic_failure_keeps_draft_marks_error(
        self, isolated_router_state,
    ):
        # Drafter round 1 OK; critic round 1 fails → loop stops; draft kept
        single_row = pd.DataFrame({
            "title": ["X"], "_epoch": [0],
        })
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=[
                _stub_response(text="my-draft", model_id="d"),
                LLMRouterError("critic API rate", error_class="RateLimit"),
            ],
        ):
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="draft", critic_prompt="critique",
                max_rounds=3,
                use_cache=False,
            )
        # Status stays success (we have a usable draft)
        assert out.iloc[0]["_llm_status"] == "success"
        assert out.iloc[0]["_llm_output"] == "my-draft"
        # But error column flags the critic failure
        assert "critic_round_1_failed" in out.iloc[0]["_llm_error"]
        assert out.iloc[0]["_llm_refine_rounds"] == 1


# ═══════════════════════════════════════════════════════════════════
# 7. Empty input
# ═══════════════════════════════════════════════════════════════════

class TestEmptyInput:
    def test_empty_dataframe_returns_well_shaped(
        self, isolated_router_state,
    ):
        empty = pd.DataFrame({"title": pd.array([], dtype="object")})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_refine_pipe(
                empty, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
            )
        assert mock_call.call_count == 0
        for col in (
            "_llm_output", "_llm_status", "_llm_error",
            "_llm_refine_rounds", "_llm_refine_drafts",
            "_llm_refine_critiques", "_llm_refine_converged",
        ):
            assert col in out.columns
        assert len(out) == 0

    def test_none_input_returns_well_shaped(self, isolated_router_state):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_refine_pipe(
                None, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
            )
        assert mock_call.call_count == 0
        assert len(out) == 0


# ═══════════════════════════════════════════════════════════════════
# 8. Custom revise template
# ═══════════════════════════════════════════════════════════════════

class TestCustomReviseTemplate:
    def test_custom_revise_prompt_used_for_round_2_drafter(
        self, isolated_router_state,
    ):
        # Capture all drafter prompts; verify round 2 uses the custom template
        captured_prompts = []

        def _record(model_id, *, prompt, **kwargs):
            captured_prompts.append((model_id, prompt))
            if "draft" in (kwargs.get("source") or ""):
                return _stub_response(
                    text=f"draft-{len([p for p in captured_prompts if p[0] == 'd'])}",
                    model_id="d",
                )
            return _stub_response(text="needs work", model_id="c")

        with patch("analyzers.llm_router.call_llm", side_effect=_record):
            llm_refine_pipe(
                pd.DataFrame({"title": ["X"], "_epoch": [0]}),
                drafter_model="d", critic_model="c",
                drafter_prompt="initial-draft",
                critic_prompt="critique",
                revise_prompt="MY CUSTOM REVISE TEMPLATE: prev={prev_draft} crit={critique}",
                max_rounds=2,
                use_cache=False,
            )
        # Round 1 drafter uses initial-draft; round 2 drafter uses custom template
        drafter_prompts = [p for m, p in captured_prompts if m == "d"]
        assert len(drafter_prompts) == 2
        assert "initial-draft" in drafter_prompts[0]
        assert "MY CUSTOM REVISE TEMPLATE" in drafter_prompts[1]


# ═══════════════════════════════════════════════════════════════════
# 9. Dry-run (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_returns_single_row_preview(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_refine_pipe(
                news_df,
                drafter_model="ollama-llama3-1-8b",
                critic_model="claude-haiku-4-5-20251001",
                drafter_prompt="d", critic_prompt="c",
                max_rounds=3,
                dry_run=True,
            )
        assert mock_call.call_count == 0
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"
        assert out.iloc[0]["_dry_run"] == True
        assert out.iloc[0]["_row_count"] == 2

    def test_dry_run_arrow_in_model_label(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm"):
            out = llm_refine_pipe(
                news_df,
                drafter_model="ollama-llama3-1-8b",
                critic_model="claude-haiku-4-5-20251001",
                drafter_prompt="d", critic_prompt="c",
                dry_run=True,
            )
        assert " ⇄ " in out.iloc[0]["_llm_model"]


# ═══════════════════════════════════════════════════════════════════
# 10. Budget gate (slice-7 contract)
# ═══════════════════════════════════════════════════════════════════

class TestBudgetGate:
    def test_cap_stops_loop_mid_row(
        self, isolated_router_state, monkeypatch,
    ):
        # Each call estimated at $1; cap=$2.5 → first 2 calls fit ($2),
        # 3rd would push to $3 → cap triggers
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
            mock_call.return_value = _stub_response(text="draft", cost=1.0)
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
                max_rounds=3,
                use_cache=False, max_cost_usd=2.5,
            )
        # 2 calls fit (drafter + critic round 1 = $2), 3rd (drafter round 2)
        # estimated $1 → cumulative $3 > cap $2.5 → stop
        assert mock_call.call_count == 2
        assert any(out["_llm_status"] == "budget_exceeded")

    def test_budget_zero_means_uncapped(
        self, isolated_router_state,
    ):
        single_row = pd.DataFrame({"title": ["X"], "_epoch": [0]})
        with patch("analyzers.llm_router.call_llm") as mock_call:
            mock_call.return_value = _stub_response(text="draft", cost=0.0001)
            out = llm_refine_pipe(
                single_row, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
                max_rounds=3,
                max_cost_usd=0,  # uncapped
                use_cache=False,
            )
        assert mock_call.call_count == 6  # full 3 rounds
        assert not any(out["_llm_status"] == "budget_exceeded")


# ═══════════════════════════════════════════════════════════════════
# 11. Money-leak canary - THE LOAD-BEARING TEST
# ═══════════════════════════════════════════════════════════════════

class TestMoneyLeakCanary:
    """Patch call_llm with AssertionError("MONEY LEAK"); both dry_run
    paths and budget-cap-immediately paths must produce zero
    invocations. Same shape as test_llm_route_pipe.py /
    test_llm_pipe_slice7.py.
    """

    def test_dry_run_makes_zero_call_llm_invocations(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=AssertionError("MONEY LEAK: call_llm fired in dry_run"),
        ):
            out = llm_refine_pipe(
                news_df,
                drafter_model="ollama-llama3-1-8b",
                critic_model="claude-haiku-4-5-20251001",
                drafter_prompt="d", critic_prompt="c",
                max_rounds=3,
                dry_run=True,
            )
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "dry_run"

    def test_budget_cap_below_first_call_makes_zero_calls(
        self, news_df, isolated_router_state, monkeypatch,
    ):
        # Estimator says first call costs $10; cap is $1 → stop before any call_llm
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
            out = llm_refine_pipe(
                news_df, drafter_model="d", critic_model="c",
                drafter_prompt="d", critic_prompt="c",
                max_cost_usd=1.0, use_cache=False,
            )
        # call_llm never fired; result is just the sentinel
        assert len(out) == 1
        assert out.iloc[0]["_llm_status"] == "budget_exceeded"


# ═══════════════════════════════════════════════════════════════════
# 12. Grammar parity drift guard
# ═══════════════════════════════════════════════════════════════════

class TestGrammarParity:
    def test_g4_declares_llm_refine_token(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert re.search(r"LLM_REFINE\s*:\s*'llm_refine'", g4)
        assert re.search(r"DRAFTER_MODEL\s*:\s*'drafter_model'", g4)
        assert re.search(r"CRITIC_MODEL\s*:\s*'critic_model'", g4)
        assert re.search(r"DRAFTER_PROMPT\s*:\s*'drafter_prompt'", g4)
        assert re.search(r"CRITIC_PROMPT\s*:\s*'critic_prompt'", g4)
        assert re.search(r"REVISE_PROMPT\s*:\s*'revise_prompt'", g4)
        assert re.search(r"MAX_ROUNDS\s*:\s*'max_rounds'", g4)
        assert re.search(
            r"CONVERGE_WHEN_CRITIC_SAYS\s*:\s*'converge_when_critic_says'",
            g4,
        )

    def test_g4_has_llm_refine_grammar_rule(self):
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert "LLM_REFINE DRAFTER_MODEL EQUALS DOUBLE_QUOTED_STRING" in g4

    def test_listener_dispatches_llm_refine(self):
        listener = (PROJECT_ROOT / "lexers" / "speakesQueryListener.py").read_text()
        assert '"llm_refine": self._cmd_llm_refine' in listener
        assert "def _cmd_llm_refine" in listener

    def test_grammar_vocab_picks_up_llm_refine(self):
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm_refine" in names

    def test_handler_module_exports_llm_refine_pipe(self):
        from handlers import LLMHandler
        assert "llm_refine_pipe" in LLMHandler.__all__


# ═══════════════════════════════════════════════════════════════════
# 13. End-to-end SPQL execution
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndExecution:
    def test_llm_refine_query_parses_and_dispatches(
        self, isolated_router_state,
    ):
        from query_engine.CmdExecutionBackend import process_query
        responses = [
            _stub_response(text="draft", model_id="ollama-llama3-1-8b"),
            _stub_response(text="critique: APPROVED", model_id="claude-haiku-4-5-20251001"),
        ] * 5
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            df, _job = process_query(
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| head 1 '
                '| llm_refine drafter_model="ollama-llama3-1-8b" '
                'critic_model="claude-haiku-4-5-20251001" '
                'drafter_prompt="draft this" '
                'critic_prompt="critique this" '
                'max_rounds=2 '
                'converge_when_critic_says="APPROVED"'
            )
        assert df is not None
        assert "_llm_refine_rounds" in df.columns
        assert "_llm_refine_drafts" in df.columns
        assert "_llm_refine_critiques" in df.columns
        assert "_llm_refine_converged" in df.columns
        # Critic said APPROVED on round 1 → converged
        assert df.iloc[0]["_llm_refine_converged"] == True


# ═══════════════════════════════════════════════════════════════════
# 14. Excluded-columns drift guard
# ═══════════════════════════════════════════════════════════════════

class TestExcludedColumnsDriftGuard:
    def test_slice_2_columns_excluded_from_text_feed(self):
        from handlers.LLMHandler import _EXCLUDED_TEXT_COLUMNS
        for col in (
            "_llm_refine_rounds",
            "_llm_refine_drafts",
            "_llm_refine_critiques",
            "_llm_refine_converged",
        ):
            assert col in _EXCLUDED_TEXT_COLUMNS, (
                f"_EXCLUDED_TEXT_COLUMNS missing slice-2 column {col!r}. "
                "Without it, re-running | llm on llm_refine output feeds "
                "drafts/critiques back as input text - silent footgun."
            )
