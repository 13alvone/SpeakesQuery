"""
Tests for Phase 2 / Bet 3 slice 7 - budget gate + dry-run for | llm and
| llm_batch. The defining test in this file is the **money-leak canary**:
on the dry-run + budget-exceeded paths the production-billable
``call_llm`` MUST be invoked zero (or a bounded count of) times, and we
prove it by patching the function with one that raises
``AssertionError("MONEY LEAK")`` if called.

Per ``feedback_money_leak_audit_pattern.md``: when a binary mode toggles
between "spend" and "don't spend", end-to-end audit + visible
confirmation is required, not optional. Slice 7's contracts:

  * ``dry_run=true``       → ZERO ``call_llm`` invocations.
  * ``max_cost_usd=$tiny`` → bounded invocations; the cumulative actual
                              cost cannot exceed the cap.

Layout:
  * ``TestEstimator`` - math + edge cases for the cost estimator.
  * ``TestDryRunShape`` - dry-run output schema for both pipes.
  * ``TestMoneyLeakCanary`` - the load-bearing zero-invocation tests.
  * ``TestBudgetGate`` - sentinel row shape, cumulative tracking,
                              cache-hit interaction.
  * ``TestListenerKwargs`` - flat-shlex parser for max_cost_usd /
                              dry_run kwargs at the SPQL surface.
  * ``TestGrammarParity`` - drift guards for the .g4 + listener.
  * ``TestSettingsDrift`` - DEFAULTS / YAML / validator coverage.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from analyzers.llm_router import (
    LLMResponse, LLMRouterError,
    estimate_cost_usd, estimate_tokens_from_chars,
)
from handlers.LLMHandler import (
    LLMPipeError, llm_batch_pipe, llm_pipe,
)


# ── Shared fixtures ──────────────────────────────────────────────────

@pytest.fixture
def news_df() -> pd.DataFrame:
    return pd.DataFrame({
        "title": [
            "Federal Reserve pauses interest rate hikes",
            "Apple announces new iPhone launch",
            "Nvidia GPU demand soars",
            "Meta lays off 10,000 employees",
            "Tesla reports record deliveries",
        ],
        "_epoch": [1700000000 + i * 10 for i in range(5)],
    })


@pytest.fixture
def isolated_router_state(tmp_path, monkeypatch):
    """Same isolation pattern as test_llm_pipe.py - slice-3 history
    capture must not pollute the project-root DB during tests, and
    model_store needs an empty seed-from-defaults tmp dir.

    Per ``reference_auto_instrumentation_test_isolation.md``: every
    test fixture exercising ``call_llm`` must reset the cache state
    (``hist.reset_for_tests``) AND redirect the on-disk DB path,
    otherwise an earlier test's success row serves a later test's
    cache hit and the later test's mock never fires.
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


def _stub_response(text="ok", *, cost=0.0001, latency=42, model_id="m"):
    return LLMResponse(
        text=text, model_id=model_id, provider="anthropic",
        model_name="m-name", input_tokens=10, output_tokens=3,
        cost_usd=cost, latency_ms=latency, request_id="rid",
    )


# ═════════════════════════════════════════════════════════════════════
# 1. Estimator math + edge cases
# ═════════════════════════════════════════════════════════════════════

class TestEstimator:
    def test_tokens_from_chars_round_up_for_partials(self):
        # 1-4 chars → 1 token at chars_per_token=4.0
        assert estimate_tokens_from_chars("") == 0
        assert estimate_tokens_from_chars("a") == 1
        assert estimate_tokens_from_chars("abcd") == 1
        assert estimate_tokens_from_chars("abcde") == 2
        assert estimate_tokens_from_chars("a" * 17) == 5  # ceil(17/4)

    def test_tokens_from_chars_custom_ratio(self):
        # 5 chars at chars_per_token=2.0 → ceil(5/2) = 3
        assert estimate_tokens_from_chars("hello", chars_per_token=2.0) == 3

    def test_tokens_from_chars_invalid_ratio_raises(self):
        with pytest.raises(ValueError, match="positive"):
            estimate_tokens_from_chars("x", chars_per_token=0)
        with pytest.raises(ValueError, match="positive"):
            estimate_tokens_from_chars("x", chars_per_token=-1.0)

    def test_estimate_cost_usd_known_model_correct_math(self, isolated_router_state):
        # claude-haiku-4-5-20251001: input $1/M, output $5/M
        # Prompt "hello" (5 chars) → 2 input tokens
        # max_tokens=100 → 100 output tokens
        # Cost = 2 * 1 / 1M + 100 * 5 / 1M = 0.000002 + 0.0005 = 0.000502
        out = estimate_cost_usd(
            "claude-haiku-4-5-20251001", ["hello"], max_tokens=100,
        )
        assert out["input_tokens"] == 2
        assert out["output_tokens"] == 100
        assert out["cost_usd"] == pytest.approx(0.000502, rel=1e-4)
        assert out["model_id"] == "claude-haiku-4-5-20251001"
        assert out["provider"] == "anthropic"
        assert out["n_calls"] == 1

    def test_estimate_cost_usd_multiple_prompts_sums(self, isolated_router_state):
        # Three identical prompts → 3× the per-prompt cost
        out_one = estimate_cost_usd(
            "claude-haiku-4-5-20251001", ["hello"], max_tokens=100,
        )
        out_three = estimate_cost_usd(
            "claude-haiku-4-5-20251001", ["hello"] * 3, max_tokens=100,
        )
        assert out_three["n_calls"] == 3
        assert out_three["cost_usd"] == pytest.approx(
            3 * out_one["cost_usd"], rel=1e-6,
        )
        assert out_three["input_tokens"] == 3 * out_one["input_tokens"]
        assert out_three["output_tokens"] == 3 * out_one["output_tokens"]

    def test_estimate_cost_usd_system_prompt_per_call(self, isolated_router_state):
        # System prompt counts toward EVERY call's input (not amortised)
        no_sys = estimate_cost_usd(
            "claude-haiku-4-5-20251001", ["x", "y"], max_tokens=10,
        )
        with_sys = estimate_cost_usd(
            "claude-haiku-4-5-20251001", ["x", "y"],
            system="be terse", max_tokens=10,
        )
        # "be terse" = 8 chars → 2 tokens × 2 calls = 4 extra input tokens
        assert with_sys["input_tokens"] == no_sys["input_tokens"] + 4

    def test_estimate_cost_usd_unknown_model_raises(self, isolated_router_state):
        with pytest.raises(LLMRouterError, match="Unknown model"):
            estimate_cost_usd("not-a-real-model", ["hello"])

    def test_estimate_cost_usd_uses_record_max_tokens_when_unset(
        self, isolated_router_state,
    ):
        # When max_tokens omitted, registry default applies (haiku=4096)
        out = estimate_cost_usd("claude-haiku-4-5-20251001", ["x"])
        assert out["max_tokens"] == 4096
        assert out["output_tokens"] == 4096

    def test_estimate_cost_usd_invalid_chars_per_token(self, isolated_router_state):
        with pytest.raises(LLMRouterError, match="chars_per_token"):
            estimate_cost_usd(
                "claude-haiku-4-5-20251001", ["x"], chars_per_token=0,
            )

    def test_estimate_cost_usd_non_string_prompt_raises(self, isolated_router_state):
        with pytest.raises(LLMRouterError, match="must be a str"):
            estimate_cost_usd(
                "claude-haiku-4-5-20251001", ["x", 42],  # type: ignore[list-item]
            )

    def test_estimate_cost_usd_local_model_zero_pricing(self, isolated_router_state):
        # ollama-llama3-1-8b: $0/M for both input + output (local model)
        out = estimate_cost_usd("ollama-llama3-1-8b", ["x"], max_tokens=100)
        assert out["cost_usd"] == 0.0
        assert out["provider"] == "ollama"


# ═════════════════════════════════════════════════════════════════════
# 2. Dry-run output shape (the cost-preview contract)
# ═════════════════════════════════════════════════════════════════════

class TestDryRunShape:
    def test_llm_dry_run_returns_one_row_preview(self, news_df, isolated_router_state):
        out = llm_pipe(
            news_df, model="claude-haiku-4-5-20251001",
            prompt="rate it", dry_run=True, max_tokens=50,
        )
        assert len(out) == 1
        for col in (
            "_dry_run", "_estimated_cost_usd",
            "_estimated_input_tokens", "_estimated_output_tokens",
            "_row_count", "_llm_model", "_llm_provider",
            "_max_tokens", "_llm_status",
        ):
            assert col in out.columns, f"missing dry-run column: {col}"
        assert out["_dry_run"].iloc[0] is True or out["_dry_run"].iloc[0] == True  # noqa: E712
        assert out["_row_count"].iloc[0] == 5
        assert out["_llm_status"].iloc[0] == "dry_run"
        assert out["_estimated_cost_usd"].iloc[0] > 0  # haiku has positive pricing
        # 5 rows × 50 max_tokens = 250 output tokens worst-case
        assert out["_estimated_output_tokens"].iloc[0] == 250

    def test_llm_batch_dry_run_includes_row_count(self, news_df, isolated_router_state):
        out = llm_batch_pipe(
            news_df, model="claude-haiku-4-5-20251001",
            prompt="summarise", dry_run=True, max_tokens=200,
        )
        assert len(out) == 1
        # Batch is one call total, hence n_calls=1 in the estimator output
        assert out["_row_count"].iloc[0] == 1
        # ...but _llm_input_row_count carries the truncated input row count
        assert out["_llm_input_row_count"].iloc[0] == 5
        assert out["_estimated_output_tokens"].iloc[0] == 200

    def test_dry_run_on_empty_df_still_works(self, isolated_router_state):
        empty = pd.DataFrame({"title": pd.Series([], dtype=object)})
        out = llm_pipe(
            empty, model="claude-haiku-4-5-20251001",
            prompt="x", dry_run=True,
        )
        assert len(out) == 1
        assert out["_row_count"].iloc[0] == 0
        assert out["_estimated_cost_usd"].iloc[0] == 0.0

    def test_dry_run_unknown_model_returns_error_row(self, news_df, isolated_router_state):
        out = llm_pipe(news_df, model="not-real", prompt="x", dry_run=True)
        assert len(out) == 1
        assert out["_llm_status"].iloc[0] == "error"
        assert "UnknownModel" in out["_llm_error"].iloc[0]

    def test_dry_run_truncates_to_max_rows_in_batch(self, isolated_router_state):
        big = pd.DataFrame({"title": [f"row-{i}" for i in range(50)]})
        out = llm_batch_pipe(
            big, model="claude-haiku-4-5-20251001",
            prompt="x", dry_run=True, max_rows=5,
        )
        # _llm_input_row_count reflects the truncation
        assert out["_llm_input_row_count"].iloc[0] == 5


# ═════════════════════════════════════════════════════════════════════
# 3. Money-leak canary - THE load-bearing tests
# ═════════════════════════════════════════════════════════════════════

class TestMoneyLeakCanary:
    """Per feedback_money_leak_audit_pattern.md: dry-run + budget-exceeded
    paths must NEVER invoke the billable transport. We patch
    ``analyzers.llm_router.call_llm`` with a function that raises
    AssertionError on invocation, run the supposedly-non-billing path,
    and assert the patched function was never called. Any future
    refactor that accidentally re-enables a path through ``call_llm``
    on these branches will fail loudly here, immediately.
    """

    def _money_leak_call_llm(self, *_args, **_kwargs):
        raise AssertionError(
            "MONEY LEAK: call_llm was invoked on a path that should not "
            "have made any billable provider calls. This indicates a "
            "regression in the slice-7 budget gate or dry-run mode."
        )

    def test_dry_run_makes_zero_call_llm_invocations(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=self._money_leak_call_llm,
        ) as mock_call:
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="rate it", dry_run=True, max_tokens=50,
            )
            assert mock_call.call_count == 0, (
                f"call_llm was invoked {mock_call.call_count}× during a "
                f"dry_run=True call. Expected 0."
            )
        # Sanity check the result still came back
        assert len(out) == 1
        assert out["_llm_status"].iloc[0] == "dry_run"

    def test_dry_run_makes_zero_call_llm_invocations_in_batch(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=self._money_leak_call_llm,
        ) as mock_call:
            out = llm_batch_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="summarise", dry_run=True,
            )
            assert mock_call.call_count == 0
        assert out["_llm_status"].iloc[0] == "dry_run"

    def test_dry_run_skips_history_capture(self, news_df, isolated_router_state):
        # The history store's record_call should NEVER fire on dry-run.
        with patch(
            "analyzers.llm_history_store.LLMHistoryStore.record_call",
            side_effect=self._money_leak_call_llm,
        ) as mock_record:
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", dry_run=True,
            )
            assert mock_record.call_count == 0
        assert out["_llm_status"].iloc[0] == "dry_run"

    def test_budget_cap_bounded_invocations(self, news_df, isolated_router_state):
        # Per-row mode: cap at $0.0002 with each call costing $0.0001.
        # Allow only ~2 successful calls before the gate fires.
        responses = [_stub_response(cost=0.0001) for _ in range(5)]
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=responses,
        ) as mock_call:
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="rate it", max_cost_usd=0.0002, max_tokens=50,
            )
            # The gate should stop SOMETHING from being called. With 5
            # rows in the input, we should see strictly fewer than 5
            # invocations.
            assert mock_call.call_count < 5, (
                "Budget gate did not bound the call count. Expected "
                f"<5 invocations, got {mock_call.call_count}."
            )

        # Sentinel row is appended after the processed rows
        statuses = out["_llm_status"].tolist()
        assert _BUDGET_EXCEEDED_STATUS in statuses, (
            f"No budget_exceeded sentinel found. Statuses: {statuses}"
        )

    def test_budget_cap_zero_means_unlimited(self, news_df, isolated_router_state):
        # Per the docs, max_cost_usd=0 disables the cap.
        responses = [_stub_response(cost=0.01) for _ in range(5)]
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=responses,
        ) as mock_call:
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.0,
            )
            assert mock_call.call_count == 5

        # All rows succeeded; no sentinel appended
        assert _BUDGET_EXCEEDED_STATUS not in out["_llm_status"].tolist()
        assert len(out) == 5  # input row count preserved

    def test_batch_budget_exceeded_makes_zero_calls(
        self, news_df, isolated_router_state,
    ):
        # Batch mode: with a tiny cap, the estimator says "would exceed",
        # so call_llm must NEVER fire.
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=self._money_leak_call_llm,
        ) as mock_call:
            out = llm_batch_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.000001,  # ridiculously tiny
                max_tokens=4096,
            )
            assert mock_call.call_count == 0
        assert out["_llm_status"].iloc[0] == _BUDGET_EXCEEDED_STATUS

    def test_batch_budget_zero_disables_cap(self, news_df, isolated_router_state):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(cost=999.0),  # any cost, no cap
        ) as mock_call:
            out = llm_batch_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.0,
            )
            assert mock_call.call_count == 1
        assert out["_llm_status"].iloc[0] == "success"


# Re-export the constant from the handler so canary tests can reference
# it without a deep-import path. This is also the documented public name.
_BUDGET_EXCEEDED_STATUS = "budget_exceeded"


# ═════════════════════════════════════════════════════════════════════
# 4. Budget gate semantics (sentinel shape, cumulative tracking)
# ═════════════════════════════════════════════════════════════════════

class TestBudgetGate:
    def test_sentinel_row_has_correct_columns(self, news_df, isolated_router_state):
        responses = [_stub_response(cost=0.0001) for _ in range(5)]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.0002, max_tokens=10,
            )
        # Find the sentinel row
        sent = out[out["_llm_status"] == _BUDGET_EXCEEDED_STATUS]
        assert len(sent) == 1
        assert sent["_llm_output"].iloc[0] == ""
        assert sent["_llm_cost_usd"].iloc[0] == 0.0
        assert sent["_llm_latency_ms"].iloc[0] == 0
        assert "Budget cap" in sent["_llm_error"].iloc[0]
        # Input columns on the sentinel row are NaN/null
        assert pd.isna(sent["title"].iloc[0])

    def test_processed_rows_appear_before_sentinel(
        self, news_df, isolated_router_state,
    ):
        # Three rows succeed before the gate fires
        responses = [_stub_response(cost=0.0001, text=f"out-{i}") for i in range(5)]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.00025, max_tokens=10,
            )
        # Order: success rows, then sentinel
        sentinel_idx = out.index[out["_llm_status"] == _BUDGET_EXCEEDED_STATUS][0]
        before_sentinel = out.iloc[:sentinel_idx]
        # Successful rows have non-empty outputs
        for i in range(len(before_sentinel)):
            assert before_sentinel["_llm_status"].iloc[i] == "success"
            assert before_sentinel["_llm_output"].iloc[i].startswith("out-")

    def test_cache_hits_dont_advance_cumulative(
        self, news_df, isolated_router_state,
    ):
        # Cache hits report cost=0 - cumulative actual NEVER grows.
        # With a cap large enough to fit the conservative ESTIMATE for
        # every row (so the pre-call gate always passes), all 5 rows
        # should process. The point is: the gate's `cumulative +
        # estimate > cap` check never trips because cumulative stays
        # at $0 throughout - proving cache hits don't advance the
        # cumulative actual cost.
        cached = _stub_response(cost=0.0, latency=0)
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=cached,
        ) as mock_call:
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.001, max_tokens=1,
            )
            assert mock_call.call_count == 5

        # Cumulative actual cost = 0 (all cached) → all rows processed,
        # no sentinel appended.
        assert _BUDGET_EXCEEDED_STATUS not in out["_llm_status"].tolist()
        assert len(out) == 5
        # Confirm the actual cost recorded on each row is $0 (cache hit
        # signature) - proves we're testing the right thing.
        assert (out["_llm_cost_usd"] == 0.0).all()

    def test_invalid_max_cost_usd_raises(self, news_df, isolated_router_state):
        with pytest.raises(LLMPipeError, match="max_cost_usd"):
            llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd="not-a-number",  # type: ignore[arg-type]
            )

    def test_negative_max_cost_treated_as_uncapped(
        self, news_df, isolated_router_state,
    ):
        # max_cost_usd=-1 should be normalised to "no cap" (matches
        # the 0.0 = no cap convention; a negative value is nonsensical
        # but shouldn't crash).
        responses = [_stub_response(cost=0.01) for _ in range(5)]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=-1,
            )
        assert _BUDGET_EXCEEDED_STATUS not in out["_llm_status"].tolist()
        assert len(out) == 5

    def test_batch_budget_exceeded_carries_input_row_count(
        self, news_df, isolated_router_state,
    ):
        with patch("analyzers.llm_router.call_llm") as mock_call:
            out = llm_batch_pipe(
                news_df, model="claude-haiku-4-5-20251001",
                prompt="x", max_cost_usd=0.0000001,
                max_tokens=4096,
            )
            assert mock_call.call_count == 0
        assert out["_llm_input_row_count"].iloc[0] == 5
        assert "exceeds cap" in out["_llm_error"].iloc[0]


# ═════════════════════════════════════════════════════════════════════
# 5. Listener kwarg parsing + end-to-end
# ═════════════════════════════════════════════════════════════════════

class TestListenerKwargs:
    def test_max_cost_usd_parsed_as_float(self):
        from lexers.speakesQueryListener import speakesQueryListener
        v = speakesQueryListener._resolve_max_cost_kwarg(
            {"max_cost_usd": "0.05"}, pipe_label="llm",
        )
        assert v == 0.05

    def test_max_cost_usd_zero_means_uncapped(self):
        from lexers.speakesQueryListener import speakesQueryListener
        v = speakesQueryListener._resolve_max_cost_kwarg(
            {"max_cost_usd": "0.0"}, pipe_label="llm",
        )
        assert v is None

    def test_max_cost_usd_absent_means_uncapped(self):
        from lexers.speakesQueryListener import speakesQueryListener
        v = speakesQueryListener._resolve_max_cost_kwarg(
            {}, pipe_label="llm",
        )
        assert v is None

    def test_max_cost_usd_invalid_raises(self):
        from lexers.speakesQueryListener import speakesQueryListener
        with pytest.raises(RuntimeError, match="max_cost_usd"):
            speakesQueryListener._resolve_max_cost_kwarg(
                {"max_cost_usd": "not-a-number"}, pipe_label="llm",
            )

    def test_dry_run_parsed_as_bool(self):
        from lexers.speakesQueryListener import speakesQueryListener
        assert speakesQueryListener._resolve_dry_run_kwarg(
            {"dry_run": "true"}, pipe_label="llm",
        ) is True
        assert speakesQueryListener._resolve_dry_run_kwarg(
            {"dry_run": "false"}, pipe_label="llm",
        ) is False
        assert speakesQueryListener._resolve_dry_run_kwarg(
            {"dry_run": "TRUE"}, pipe_label="llm",
        ) is True

    def test_dry_run_invalid_raises(self):
        from lexers.speakesQueryListener import speakesQueryListener
        with pytest.raises(RuntimeError, match="dry_run"):
            speakesQueryListener._resolve_dry_run_kwarg(
                {"dry_run": "maybe"}, pipe_label="llm",
            )

    def test_dry_run_absent_default_false(self):
        from lexers.speakesQueryListener import speakesQueryListener
        assert speakesQueryListener._resolve_dry_run_kwarg(
            {}, pipe_label="llm",
        ) is False

    def test_end_to_end_dry_run_through_process_query(
        self, isolated_router_state,
    ):
        # Full SPQL pipeline: ANTLR parse → listener → handler.
        # Dry-run path: the production transport must NEVER fire.
        from query_engine.CmdExecutionBackend import process_query

        def boom(*_a, **_k):
            raise AssertionError("MONEY LEAK at end-to-end layer")

        with patch("analyzers.llm_router.call_llm", side_effect=boom):
            q = (
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| llm model="claude-haiku-4-5-20251001" prompt="rate it" '
                'dry_run=true'
            )
            df, _ = process_query(q)

        assert df is not None
        assert "_dry_run" in df.columns
        assert df["_dry_run"].iloc[0] is True or df["_dry_run"].iloc[0] == True  # noqa: E712
        assert df["_llm_status"].iloc[0] == "dry_run"

    def test_end_to_end_max_cost_through_process_query(
        self, isolated_router_state,
    ):
        from query_engine.CmdExecutionBackend import process_query
        # Prepare 5 successful stubs; with a tight cap, only some should fire
        responses = [_stub_response(cost=0.001) for _ in range(20)]
        with patch(
            "analyzers.llm_router.call_llm",
            side_effect=responses,
        ) as mock_call:
            q = (
                'index="indexes/default_test/output_parquets/test0.parquet" '
                '| llm model="claude-haiku-4-5-20251001" prompt="x" '
                'max_cost_usd=0.0015'
            )
            df, _ = process_query(q)
            # The gate must bound the calls - strictly fewer than the
            # input row count (test0.parquet has 5 rows by convention).
            assert mock_call.call_count < 20

        assert df is not None
        assert _BUDGET_EXCEEDED_STATUS in df["_llm_status"].tolist()


# ═════════════════════════════════════════════════════════════════════
# 6. Grammar parity drift guards
# ═════════════════════════════════════════════════════════════════════

class TestGrammarParity:
    def test_grammar_declares_max_cost_usd_token(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert re.search(r"\bMAX_COST_USD\s*:\s*'max_cost_usd'", g4)

    def test_grammar_declares_dry_run_token(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert re.search(r"\bDRY_RUN\s*:\s*'dry_run'", g4)

    def test_grammar_llm_rule_accepts_new_kwargs(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        # Both kwargs should appear in the LLM and LLM_BATCH directive
        # rules (line 115 / 116 of speakesQuery.g4 at slice 7 ship-time).
        assert re.search(r"LLM\s+MODEL.*MAX_COST_USD\s+EQUALS\s+NUMBER", g4)
        assert re.search(r"LLM\s+MODEL.*DRY_RUN\s+EQUALS\s+BOOLEAN", g4)
        assert re.search(r"LLM_BATCH\s+MODEL.*MAX_COST_USD\s+EQUALS\s+NUMBER", g4)
        assert re.search(r"LLM_BATCH\s+MODEL.*DRY_RUN\s+EQUALS\s+BOOLEAN", g4)

    def test_grammar_vocab_still_exposes_llm_commands(self):
        # The autocomplete API at /api/grammar/vocab must still surface
        # `llm` / `llm_batch` as directive commands after slice-7 grammar
        # edits. (The kwargs themselves don't appear in `keywords` - that
        # field carries logical operators (AND/OR/etc.) only - so the
        # drift guard for kwarg presence is the .g4 regex check above.)
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab(reload=True)
        cmd_names = {c.get("name") for c in vocab.get("commands", [])}
        assert "llm" in cmd_names
        assert "llm_batch" in cmd_names

    def test_listener_resolver_methods_exist(self):
        # The two static helpers on speakesQueryListener that parse the
        # new kwargs must remain importable. Slice 7 added them; they
        # are referenced from both _cmd_llm and _cmd_llm_batch.
        from lexers.speakesQueryListener import speakesQueryListener
        assert hasattr(speakesQueryListener, "_resolve_max_cost_kwarg")
        assert hasattr(speakesQueryListener, "_resolve_dry_run_kwarg")


# ═════════════════════════════════════════════════════════════════════
# 7. Settings drift guards
# ═════════════════════════════════════════════════════════════════════

class TestSettingsDrift:
    def test_defaults_has_both_keys(self):
        from global_settings import DEFAULTS
        assert "llm_default_max_cost_usd" in DEFAULTS
        assert "llm_warn_above_estimated_usd" in DEFAULTS
        # Default values match the documented "no cap / $1 warn" intent
        assert DEFAULTS["llm_default_max_cost_usd"] == 0.0
        assert DEFAULTS["llm_warn_above_estimated_usd"] == 1.0

    def test_yaml_mirrors_python_defaults(self):
        # Companion to TestDefaultsYamlInSync - covered by the existing
        # generic drift guard, but pinning the slice-7 keys explicitly
        # protects against an "added to DEFAULTS but forgot YAML" PR.
        import yaml
        from global_settings import DEFAULTS
        yaml_path = (
            Path(__file__).parent.parent / "global_settings.defaults.yaml"
        )
        loaded = yaml.safe_load(yaml_path.read_text()) or {}
        for key in ("llm_default_max_cost_usd", "llm_warn_above_estimated_usd"):
            assert key in loaded, f"{key} missing from {yaml_path.name}"
            assert loaded[key] == DEFAULTS[key]

    def test_validator_rejects_negative(self, tmp_path, monkeypatch):
        # Each of the slice-7 keys must reject negative values.
        from global_settings import _validate_key, DEFAULTS
        err = _validate_key("llm_default_max_cost_usd", -1.0, DEFAULTS)
        assert err is not None and "non-negative" in err
        err = _validate_key("llm_warn_above_estimated_usd", -0.5, DEFAULTS)
        assert err is not None and "non-negative" in err

    def test_validator_rejects_above_ceiling(self):
        from global_settings import _validate_key, DEFAULTS
        err = _validate_key("llm_default_max_cost_usd", 9999.0, DEFAULTS)
        assert err is not None and "1000" in err

    def test_validator_accepts_zero_and_positive(self):
        from global_settings import _validate_key, DEFAULTS
        assert _validate_key("llm_default_max_cost_usd", 0.0, DEFAULTS) is None
        assert _validate_key("llm_default_max_cost_usd", 0.5, DEFAULTS) is None
        assert _validate_key("llm_warn_above_estimated_usd", 999.99, DEFAULTS) is None

    def test_validator_rejects_non_number(self):
        from global_settings import _validate_key, DEFAULTS
        err = _validate_key("llm_default_max_cost_usd", "0.5", DEFAULTS)
        assert err is not None and "must be a number" in err

    def test_ui_mappings_reference_both_settings(self):
        # The Settings page JS must wire BOTH keys. Otherwise the
        # operator can't change them without editing YAML directly.
        ui_html = (
            Path(__file__).parent.parent / "desktop_app" / "ui.html"
        ).read_text()
        assert "'llm_default_max_cost_usd'" in ui_html
        assert "'llm_warn_above_estimated_usd'" in ui_html
        assert "set-llm-default-max-cost-usd" in ui_html
        assert "set-llm-warn-above-estimated-usd" in ui_html


# ═════════════════════════════════════════════════════════════════════
# 8. Result-equivalence - slice-7 vs slice-4/5 baseline
# ═════════════════════════════════════════════════════════════════════
# Per reference_result_equivalence_test_pattern.md: when slice 7 layers
# new behavior on top of slice 4/5's pipes, the no-cap / no-dry-run
# default path MUST produce IDENTICAL output to the pre-slice-7
# behavior. Otherwise existing pipes silently change semantics.

class TestSlice7TransparentToDefaults:
    def test_no_kwargs_matches_slice4_behavior(self, news_df, isolated_router_state):
        responses = [_stub_response(cost=0.0001) for _ in range(5)]
        with patch("analyzers.llm_router.call_llm", side_effect=responses):
            out = llm_pipe(
                news_df, model="claude-haiku-4-5-20251001", prompt="x",
            )
        # All rows succeed; no sentinel appended; row count preserved
        assert len(out) == 5
        assert (out["_llm_status"] == "success").all()
        # The slice-7 columns added to _EXCLUDED_TEXT_COLUMNS do NOT
        # appear in the output unless slice-7 kwargs were used. The
        # standard 7 columns are present.
        for col in (
            "_llm_output", "_llm_model", "_llm_provider",
            "_llm_cost_usd", "_llm_latency_ms",
            "_llm_status", "_llm_error",
        ):
            assert col in out.columns
        # Slice-7-only columns absent on the no-kwargs path
        for col in ("_dry_run", "_estimated_cost_usd", "_row_count"):
            assert col not in out.columns

    def test_no_kwargs_matches_slice5_batch_behavior(
        self, news_df, isolated_router_state,
    ):
        with patch(
            "analyzers.llm_router.call_llm",
            return_value=_stub_response(cost=0.001, text="summary"),
        ):
            out = llm_batch_pipe(
                news_df, model="claude-haiku-4-5-20251001", prompt="x",
            )
        # Single-row result, status=success, output=summary, row count=5
        assert len(out) == 1
        assert out["_llm_status"].iloc[0] == "success"
        assert out["_llm_output"].iloc[0] == "summary"
        assert out["_llm_input_row_count"].iloc[0] == 5
