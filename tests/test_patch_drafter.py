"""
Tests for Phase 4 / Bet 4 slice 8a - failed-feeder patch drafter.

Slice 8a ships:
  * ``analyzers/patch_drafter.py`` - Claude-driven unified-diff
    suggester. Honors slice-7 budget-gate contract (max_cost_usd +
    dry_run + money-leak canary).
  * 4 new global settings keys (enabled, model, max_cost_usd,
    timeout_seconds) with validators.
  * ``patch_suggestions`` log category + ``log_patch_suggestion``
    helper.
  * Engine wire-in (``_run_task`` failure paths fire-and-forget
    dispatch via daemon thread, deduplicated by error_hash).

This file pins:
  * Drafter happy path returns a populated PatchDraftResult.
  * Dry run + budget cap + missing-key paths all bypass the
    Anthropic call (money-leak canary).
  * Error-hash dedup helper is stable.
  * Settings drift (DEFAULTS dict, YAML defaults, validators).
  * Log schema drift.
  * Engine integration (failure path triggers drafter when enabled,
    skips when disabled).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


PROJECT_ROOT = Path(__file__).parent.parent


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_claude_response():
    """An anthropic-shape Messages response object with one text block."""
    block = MagicMock()
    block.text = (
        "```diff\n"
        "--- a/script.py\n"
        "+++ b/script.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+import time\n"
        " import pandas as pd\n"
        " import requests\n"
        "```\n\n"
        "The script imports pandas and requests but uses `time.sleep` "
        "without importing time, causing the NameError. Adding the "
        "import resolves the failure."
    )
    response = MagicMock()
    response.content = [block]
    return response


@pytest.fixture
def fake_call_result(fake_claude_response):
    """A ClaudeCallResult-shape object for a successful call."""
    from analyzers.claude_client import ClaudeCallResult
    return ClaudeCallResult(
        response=fake_claude_response,
        request_id="rid-123",
        model="claude-haiku-4-5-20251001",
        input_tokens=100,
        output_tokens=80,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0008,
        latency_ms=1234,
        attempts=1,
    )


# ═══════════════════════════════════════════════════════════════════
# 1. Pure helpers - error_hash + estimate
# ═══════════════════════════════════════════════════════════════════

class TestComputeErrorHash:
    def test_same_message_produces_same_hash(self):
        from analyzers.patch_drafter import compute_error_hash
        a = compute_error_hash("NameError: name 'time' is not defined")
        b = compute_error_hash("NameError: name 'time' is not defined")
        assert a == b

    def test_different_message_produces_different_hash(self):
        from analyzers.patch_drafter import compute_error_hash
        a = compute_error_hash("NameError: name 'time' is not defined")
        b = compute_error_hash("ValueError: invalid literal for int()")
        assert a != b

    def test_hash_is_stable_truncated_hex(self):
        from analyzers.patch_drafter import compute_error_hash
        h = compute_error_hash("anything")
        assert len(h) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", h), (
            "Hash must be 16 lowercase hex chars (sha256 truncated)"
        )

    def test_empty_message_hashes_safely(self):
        from analyzers.patch_drafter import compute_error_hash
        h1 = compute_error_hash("")
        h2 = compute_error_hash(None)  # type: ignore[arg-type]
        assert len(h1) == 16
        assert len(h2) == 16


class TestEstimateCost:
    def test_estimate_returns_positive_for_real_input(self):
        from analyzers.patch_drafter import estimate_patch_cost_usd
        out = estimate_patch_cost_usd(
            script_source="x = 1\n" * 100,
            error_message="NameError: x not defined",
            title="my_script",
        )
        assert out["cost_usd"] > 0.0
        assert out["input_tokens"] > 0
        assert out["output_tokens"] > 0
        assert out["model"]

    def test_estimate_is_conservative_overestimate(self):
        # The estimator should always assume worst-case max_output_tokens
        # output. This is the slice-7 budget-gate contract:
        # conservative-by-design.
        from analyzers.patch_drafter import (
            estimate_patch_cost_usd,
            _DEFAULT_MAX_OUTPUT_TOKENS,
        )
        out = estimate_patch_cost_usd(
            script_source="x", error_message="boom",
        )
        assert out["output_tokens"] == _DEFAULT_MAX_OUTPUT_TOKENS

    def test_estimate_rejects_unknown_model_gracefully(self):
        from analyzers.patch_drafter import estimate_patch_cost_usd
        out = estimate_patch_cost_usd(
            script_source="x", error_message="boom",
            model="some-imaginary-future-model",
        )
        # Falls back to overestimate pricing - must still return a
        # positive cost so the budget gate works.
        assert out["cost_usd"] > 0.0


# ═══════════════════════════════════════════════════════════════════
# 2. Money-leak canary - slice-7 contract for billable surfaces
# ═══════════════════════════════════════════════════════════════════

class TestMoneyLeakCanary:
    """Slice-7 contract: every billable surface MUST honour ``dry_run``
    and ``max_cost_usd`` such that no Anthropic call fires when the
    operator opts out / caps out. Patch drafter has the same shape as
    ``| llm`` pipes - same canary contract.

    We patch ``analyzers.patch_drafter`` to fail loud if
    ``call_messages_create`` is invoked. Then exercise the bypass
    paths. If any path silently calls Claude, the canary fires.
    """

    def test_dry_run_makes_zero_call_messages_create_invocations(self):
        from analyzers import patch_drafter
        from analyzers.patch_drafter import draft_patch_for_failed_task
        with patch.object(
            patch_drafter,
            "_pricing",
            return_value=(3.0, 15.0),  # ensure estimate works
        ):
            # Patch the SDK call site to raise if called.
            with patch(
                "analyzers.claude_client.call_messages_create",
                side_effect=AssertionError("MONEY LEAK"),
            ):
                result = draft_patch_for_failed_task(
                    script_source="x = 1",
                    error_message="boom",
                    dry_run=True,
                )
        assert result.status == "dry_run"
        assert result.cost_usd > 0.0  # estimate populated
        assert result.patch == ""

    def test_budget_cap_skips_zero_invocations(self):
        from analyzers.patch_drafter import draft_patch_for_failed_task
        # Build a script source long enough that even Haiku pricing
        # produces an estimate above $0.0001. Then cap at $0.0001 →
        # skipped_budget.
        with patch(
            "analyzers.claude_client.call_messages_create",
            side_effect=AssertionError("MONEY LEAK"),
        ):
            result = draft_patch_for_failed_task(
                script_source="x = 1\n" * 10000,
                error_message="boom",
                max_cost_usd=0.0001,
            )
        assert result.status == "skipped_budget"
        assert result.cost_usd == 0.0
        assert result.error_class == "BudgetCapExceeded"

    def test_zero_max_cost_usd_treated_as_uncapped(self):
        # 0.0 = uncapped - calls should NOT skip with budget cap.
        # We patch call_messages_create to raise; if the budget gate
        # erroneously skipped at 0.0, no call would fire, no exception.
        # Instead we expect the call to happen (and fail with our
        # AssertionError), confirming the budget gate didn't short-
        # circuit at 0.0.
        from analyzers.patch_drafter import draft_patch_for_failed_task
        with patch(
            "analyzers.claude_client.call_messages_create",
            side_effect=AssertionError("call attempted"),
        ):
            result = draft_patch_for_failed_task(
                script_source="x", error_message="boom",
                max_cost_usd=0.0,
            )
        # The AssertionError gets caught by the broad except in the
        # drafter and turned into status='error' with AssertionError class
        assert result.status == "error"
        assert "AssertionError" in result.error_class

    def test_missing_api_key_returns_skipped_no_key(self):
        from analyzers.patch_drafter import draft_patch_for_failed_task
        from analyzers.claude_client import ClaudeCallError
        with patch(
            "analyzers.claude_client.call_messages_create",
            side_effect=ClaudeCallError(
                "No Claude API key configured.",
                request_id="rid-x",
                error_class="MissingCredential",
            ),
        ):
            result = draft_patch_for_failed_task(
                script_source="x", error_message="boom",
            )
        assert result.status == "skipped_no_key"
        assert result.error_class == "MissingCredential"


# ═══════════════════════════════════════════════════════════════════
# 3. Happy path - drafter returns a populated result
# ═══════════════════════════════════════════════════════════════════

class TestHappyPath:
    def test_returns_patch_and_explanation_from_response(
        self, fake_call_result,
    ):
        from analyzers.patch_drafter import draft_patch_for_failed_task
        with patch(
            "analyzers.claude_client.call_messages_create",
            return_value=fake_call_result,
        ):
            result = draft_patch_for_failed_task(
                script_source="x = 1",
                error_message="NameError: name 'time' is not defined",
                script_title="my_script",
                max_cost_usd=10.0,  # generous to avoid budget gate
            )
        assert result.status == "success"
        assert "diff --git" in result.patch or "--- a/" in result.patch
        assert "import" in result.patch
        assert "NameError" in result.explanation or "import" in result.explanation
        assert result.cost_usd == 0.0008
        assert result.latency_ms == 1234
        assert result.request_id == "rid-123"

    def test_call_messages_create_invoked_with_correct_kwargs(
        self, fake_call_result,
    ):
        from analyzers.patch_drafter import draft_patch_for_failed_task
        with patch(
            "analyzers.claude_client.call_messages_create",
            return_value=fake_call_result,
        ) as mock_call:
            draft_patch_for_failed_task(
                script_source="x = 1",
                error_message="boom",
                script_title="t",
                max_cost_usd=10.0,
            )
        assert mock_call.called
        kwargs = mock_call.call_args.kwargs
        assert kwargs["source"] == "patch_drafter"
        assert "model" in kwargs
        assert "messages" in kwargs
        assert "system" in kwargs
        assert kwargs["max_tokens"] > 0
        # The user message must contain the script + error
        msg_content = kwargs["messages"][0]["content"]
        assert "x = 1" in msg_content
        assert "boom" in msg_content

    def test_no_confident_fix_response_yields_empty_patch(self):
        from analyzers.patch_drafter import draft_patch_for_failed_task
        from analyzers.claude_client import ClaudeCallResult
        block = MagicMock()
        block.text = (
            "NO_CONFIDENT_FIX The error suggests an upstream API "
            "outage that the script can't work around."
        )
        response = MagicMock()
        response.content = [block]
        result_obj = ClaudeCallResult(
            response=response, request_id="rid", model="m",
            input_tokens=10, output_tokens=20,
            cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.0001, latency_ms=10, attempts=1,
        )
        with patch(
            "analyzers.claude_client.call_messages_create",
            return_value=result_obj,
        ):
            out = draft_patch_for_failed_task(
                script_source="x", error_message="API 503",
                max_cost_usd=10.0,
            )
        assert out.status == "success"
        assert out.patch == ""  # no fenced block → empty patch
        assert "NO_CONFIDENT_FIX" in out.explanation


# ═══════════════════════════════════════════════════════════════════
# 4. Settings drift guards
# ═══════════════════════════════════════════════════════════════════

class TestSettingsDrift:
    """Per ``reference_setting_drift_five_layers.md``: a new setting
    lives in DEFAULTS dict, YAML mirror, validator branch, plus the
    SPA `<input>` + JS map. The DEFAULTS↔YAML drift is covered by the
    generic ``TestDefaultsYamlInSync``; the validator branch needs an
    explicit per-key assertion. SPA wiring lands in slice 8b (or
    operator edits the YAML directly for slice 8a)."""

    @pytest.mark.parametrize("key,expected_default", [
        ("patch_drafter_enabled", False),
        ("patch_drafter_model", "claude-haiku-4-5-20251001"),
        ("patch_drafter_max_cost_usd", 0.10),
        ("patch_drafter_timeout_seconds", 60),
    ])
    def test_default_present_in_DEFAULTS(self, key, expected_default):
        from global_settings import DEFAULTS
        assert key in DEFAULTS, f"Setting {key!r} missing from DEFAULTS"
        assert DEFAULTS[key] == expected_default, (
            f"Default for {key!r}: expected {expected_default!r}, "
            f"got {DEFAULTS[key]!r}"
        )

    @pytest.mark.parametrize("key", [
        "patch_drafter_enabled",
        "patch_drafter_model",
        "patch_drafter_max_cost_usd",
        "patch_drafter_timeout_seconds",
    ])
    def test_default_mirrored_in_yaml(self, key):
        text = (PROJECT_ROOT / "global_settings.defaults.yaml").read_text()
        assert (key + ":") in text, (
            f"Setting {key!r} missing from global_settings.defaults.yaml"
        )

    def test_enabled_validator_rejects_non_bool(self):
        from global_settings import _validate_key
        assert _validate_key("patch_drafter_enabled", "true", {}) is not None
        assert _validate_key("patch_drafter_enabled", 1, {}) is not None
        assert _validate_key("patch_drafter_enabled", True, {}) is None
        assert _validate_key("patch_drafter_enabled", False, {}) is None

    def test_model_validator_rejects_empty_string(self):
        from global_settings import _validate_key
        assert _validate_key("patch_drafter_model", "", {}) is not None
        assert _validate_key("patch_drafter_model", " ", {}) is not None
        assert _validate_key("patch_drafter_model", 123, {}) is not None
        assert _validate_key("patch_drafter_model", "claude-haiku", {}) is None

    def test_max_cost_usd_validator_enforces_range(self):
        from global_settings import _validate_key
        assert _validate_key("patch_drafter_max_cost_usd", -1, {}) is not None
        assert _validate_key("patch_drafter_max_cost_usd", 1001, {}) is not None
        assert _validate_key("patch_drafter_max_cost_usd", True, {}) is not None
        assert _validate_key("patch_drafter_max_cost_usd", 0.0, {}) is None
        assert _validate_key("patch_drafter_max_cost_usd", 0.1, {}) is None
        assert _validate_key("patch_drafter_max_cost_usd", 1000.0, {}) is None

    def test_timeout_validator_enforces_range(self):
        from global_settings import _validate_key
        assert _validate_key("patch_drafter_timeout_seconds", 0, {}) is not None
        assert _validate_key("patch_drafter_timeout_seconds", 4, {}) is not None
        assert _validate_key("patch_drafter_timeout_seconds", 700, {}) is not None
        assert _validate_key("patch_drafter_timeout_seconds", True, {}) is not None
        assert _validate_key("patch_drafter_timeout_seconds", 5, {}) is None
        assert _validate_key("patch_drafter_timeout_seconds", 60, {}) is None
        assert _validate_key("patch_drafter_timeout_seconds", 600, {}) is None


# ═══════════════════════════════════════════════════════════════════
# 5. Log schema drift
# ═══════════════════════════════════════════════════════════════════

class TestLogSchemaDrift:
    def test_patch_suggestions_category_in_SCHEMAS(self):
        from functionality.log_writer import SCHEMAS
        assert "patch_suggestions" in SCHEMAS

    def test_patch_suggestions_columns_present(self):
        from functionality.log_writer import SCHEMAS
        cols = SCHEMAS["patch_suggestions"]
        for required in (
            "_epoch", "task_id", "title", "error_hash", "status",
            "model", "cost_usd", "latency_ms", "patch", "explanation",
            "request_id", "error_message",
            "input_tokens", "output_tokens",
            "drafter_error_class", "drafter_error_message",
        ):
            assert required in cols, (
                f"patch_suggestions schema missing required column "
                f"{required!r}. Schema is ADDITIVE-ONLY going forward; "
                "do NOT remove existing columns."
            )

    def test_log_helper_emits_with_correct_category(self):
        from functionality import log_writer
        captured = {}

        def fake_emit(category, row):
            captured["category"] = category
            captured["row"] = row

        with patch.object(log_writer, "emit", side_effect=fake_emit):
            log_writer.log_patch_suggestion(
                task_id=42, title="t", error_hash="abc",
                status="success", model="claude-haiku",
                cost_usd=0.001, latency_ms=100,
                patch="diff", explanation="why",
                request_id="rid", error_message="boom",
                input_tokens=10, output_tokens=20,
            )
        assert captured["category"] == "patch_suggestions"
        assert captured["row"]["task_id"] == "42"
        assert captured["row"]["status"] == "success"
        assert captured["row"]["patch"] == "diff"


# ═══════════════════════════════════════════════════════════════════
# 6. Engine integration - failure path triggers drafter when enabled
# ═══════════════════════════════════════════════════════════════════

class TestEngineWiring:
    """The engine's ``_maybe_dispatch_patch_drafter`` is the single
    entry from the failure path. Pin its behaviour:
      - Disabled by default → no call
      - Enabled + new error_hash → daemon thread spawned
      - Enabled + repeated error_hash → deduped (no second call)
      - Errors in dispatch don't bubble back
    """

    def _make_engine(self):
        # Build a bare engine instance without invoking __init__'s full
        # setup. We only need _setting and the dispatch helper for these
        # tests.
        from scheduled_input_engine.engine import ScheduledInputEngine

        eng = ScheduledInputEngine.__new__(ScheduledInputEngine)
        eng._settings = {}
        # Reset class-level dedup cache between tests so previous test
        # state doesn't pollute.
        ScheduledInputEngine._patch_drafter_dedup.clear()
        return eng

    def test_disabled_means_no_thread_spawned(self):
        eng = self._make_engine()
        eng._settings = {"patch_drafter_enabled": False}
        with patch("threading.Thread") as mock_thread:
            eng._maybe_dispatch_patch_drafter(
                {"id": 1, "code": "x"}, "title", "boom",
            )
        assert not mock_thread.called

    def test_enabled_spawns_daemon_thread(self):
        eng = self._make_engine()
        eng._settings = {"patch_drafter_enabled": True}
        with patch("scheduled_input_engine.engine.threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            eng._maybe_dispatch_patch_drafter(
                {"id": 1, "code": "x"}, "title", "boom",
            )
        assert mock_thread.called, (
            "Enabled drafter should spawn a daemon thread"
        )
        # Verify daemon=True kwarg
        kwargs = mock_thread.call_args.kwargs
        assert kwargs.get("daemon") is True
        assert mock_instance.start.called

    def test_dedup_skips_repeat_error_hash(self):
        eng = self._make_engine()
        eng._settings = {"patch_drafter_enabled": True}
        with patch("scheduled_input_engine.engine.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            # First failure → dispatch
            eng._maybe_dispatch_patch_drafter(
                {"id": 7, "code": "x"}, "title", "the same error",
            )
            # Second failure with IDENTICAL message → dedup, no thread
            eng._maybe_dispatch_patch_drafter(
                {"id": 7, "code": "x"}, "title", "the same error",
            )
        assert mock_thread.call_count == 1, (
            "Dedup should suppress the second dispatch for the same "
            "error_hash"
        )

    def test_dedup_does_not_skip_different_error_hash(self):
        eng = self._make_engine()
        eng._settings = {"patch_drafter_enabled": True}
        with patch("scheduled_input_engine.engine.threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            eng._maybe_dispatch_patch_drafter(
                {"id": 9, "code": "x"}, "title", "first error",
            )
            eng._maybe_dispatch_patch_drafter(
                {"id": 9, "code": "x"}, "title", "different error",
            )
        assert mock_thread.call_count == 2

    def test_dispatch_errors_never_bubble(self):
        # If anything inside the dispatch raises, the engine path
        # MUST continue. An ingestion failure must not be made worse
        # by a buggy drafter wiring.
        eng = self._make_engine()
        eng._settings = {"patch_drafter_enabled": True}
        with patch(
            "scheduled_input_engine.engine.threading.Thread",
            side_effect=RuntimeError("simulated thread spawn failure"),
        ):
            # Should NOT raise
            eng._maybe_dispatch_patch_drafter(
                {"id": 1, "code": "x"}, "title", "boom",
            )


# ═══════════════════════════════════════════════════════════════════
# 7. Patch / explanation splitter
# ═══════════════════════════════════════════════════════════════════

class TestPatchSplitter:
    def test_extracts_diff_fence(self):
        from analyzers.patch_drafter import _split_diff_and_explanation
        text = (
            "Here's a fix:\n"
            "```diff\n"
            "--- a/x\n"
            "+++ b/x\n"
            "+import time\n"
            "```\n"
            "The script needs the time import."
        )
        patch_str, expl = _split_diff_and_explanation(text)
        assert "+import time" in patch_str
        assert "needs the time import" in expl

    def test_no_fence_yields_empty_patch(self):
        from analyzers.patch_drafter import _split_diff_and_explanation
        text = "NO_CONFIDENT_FIX External outage; cannot suggest fix."
        patch_str, expl = _split_diff_and_explanation(text)
        assert patch_str == ""
        assert "NO_CONFIDENT_FIX" in expl

    def test_empty_input_yields_two_empty_strings(self):
        from analyzers.patch_drafter import _split_diff_and_explanation
        assert _split_diff_and_explanation("") == ("", "")


# ═══════════════════════════════════════════════════════════════════
# 8. Module surface
# ═══════════════════════════════════════════════════════════════════

class TestModuleSurface:
    def test_exports(self):
        import analyzers.patch_drafter as pd
        for name in (
            "PatchDraftResult",
            "compute_error_hash",
            "draft_patch_for_failed_task",
            "estimate_patch_cost_usd",
            "estimate_tokens_from_chars",
        ):
            assert hasattr(pd, name), (
                f"analyzers.patch_drafter missing public symbol {name!r}"
            )
            assert name in pd.__all__, (
                f"analyzers.patch_drafter.__all__ missing {name!r}"
            )
