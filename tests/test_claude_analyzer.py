#!/usr/bin/env python3
"""
Unit tests for the Claude API analysis layer.

All tests run without an API key or network access.  They validate:
  - Gate logic ordering and skip reasons
  - Model routing based on spike_multiple
  - Token resolution (global tokens, column tokens, mv truncation)
  - Cost calculation
  - Budget tracking and daily reset
  - Response parsing (valid JSON, malformed JSON, missing keys)
  - Config validation in global_settings
  - AnalyzerPromptStore CRUD
  - AnalyzerPromptValidation

Integration tests that hit the real API are gated behind
RUN_INTEGRATION_TESTS=1 and are not part of this file.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd

from analyzers.models import (
    ActionableMarket,
    AnalysisResult,
    AnalyzerConfig,
    UsageStats,
)
from analyzers.claude_analyzer import (
    ClaudeAnalyzer,
    _compute_cost_cents,
    _truncate_multivalue,
    resolve_analyzer_prompt,
)
from validation.AnalyzerPromptValidation import AnalyzerPromptValidation


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def default_config():
    """AnalyzerConfig with defaults and a dummy API key."""
    return AnalyzerConfig(api_key="sk-test-key-not-real")


@pytest.fixture
def sample_results():
    """Realistic sample result rows."""
    return [
        {
            "question": "Will Arsenal win the 2025-26 EPL?",
            "yes_price": 0.86, "volume_24h": 50000,
            "spike_multiple": 6.25, "liquidity": 407131,
            "alert_level": "HIGH",
        },
        {
            "question": "Will Iran withdraw from NPT before 2027?",
            "yes_price": 0.255, "volume_24h": 5871,
            "spike_multiple": 8.99, "liquidity": 28154,
            "alert_level": "HIGH",
        },
        {
            "question": "Will Scottie Scheffler win the 2026 Masters?",
            "yes_price": 0.135, "volume_24h": 15000,
            "spike_multiple": 3.0, "liquidity": 379200,
            "alert_level": "MODERATE",
        },
    ]


@pytest.fixture
def sample_df(sample_results):
    return pd.DataFrame(sample_results)


@pytest.fixture
def sample_search_metadata():
    return {
        "name": "volume_spikes",
        "description": "Detects unusual volume spikes on Polymarket",
        "query": "search index=polymarket | where spike_multiple > 2",
        "cron_schedule": "0 */6 * * *",
        "lookback": "-1d",
        "trigger": "once",
        "email_address": "test@example.com",
        "created_at": "2026-04-01T00:00:00",
        "mv_truncate_limit": 3,
    }


# =====================================================================
# Gate Logic Tests
# =====================================================================

class TestGateLogic:
    """Gate checks run in order: api_key → empty → budget → liquidity."""

    def test_gate_no_api_key(self):
        config = AnalyzerConfig(api_key="")
        analyzer = ClaudeAnalyzer(config)
        result = analyzer.analyze("test", [{"foo": "bar"}])
        assert result.status == "skipped"
        assert result.skip_reason == "no_api_key"

    def test_gate_empty_results(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        result = analyzer.analyze("test", [])
        assert result.status == "skipped"
        assert result.skip_reason == "empty_results"

    def test_gate_empty_results_none(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        result = analyzer.analyze("test", None)
        assert result.status == "skipped"
        assert result.skip_reason == "empty_results"

    def test_gate_budget_exceeded(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        analyzer._usage.budget_remaining_cents = 0
        result = analyzer.analyze("test", [{"foo": "bar"}])
        assert result.status == "skipped"
        assert result.skip_reason == "budget_exceeded"

    def test_gate_below_min_liquidity(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        low_liq = [{"liquidity": 100}, {"liquidity": 200}]
        result = analyzer.analyze("test", low_liq)
        assert result.status == "skipped"
        assert result.skip_reason == "below_min_liquidity"

    def test_gate_passes_with_sufficient_liquidity(self, default_config, sample_results):
        """When liquidity is above threshold, gate passes (API call follows)."""
        analyzer = ClaudeAnalyzer(default_config)
        skip = analyzer._gate_check(sample_results)
        assert skip is None  # All gates passed

    def test_gate_no_liquidity_field_passes(self, default_config):
        """Rows without a liquidity field should not trigger the liquidity gate."""
        analyzer = ClaudeAnalyzer(default_config)
        rows = [{"question": "Test?", "spike_multiple": 5.0}]
        skip = analyzer._gate_check(rows)
        assert skip is None


# =====================================================================
# Model Routing Tests
# =====================================================================

class TestModelRouting:

    def test_routes_to_triage_below_threshold(self, default_config, sample_results):
        analyzer = ClaudeAnalyzer(default_config)
        # All spikes below 10.0
        low_spike = [{"spike_multiple": 5.0}, {"spike_multiple": 3.0}]
        assert analyzer._select_model(low_spike) == default_config.model_triage

    def test_routes_to_primary_above_threshold(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        high_spike = [{"spike_multiple": 15.0}]
        assert analyzer._select_model(high_spike) == default_config.model_primary

    def test_routes_to_primary_at_threshold(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        at_threshold = [{"spike_multiple": 10.0}]
        assert analyzer._select_model(at_threshold) == default_config.model_primary

    def test_routes_to_triage_no_spike_field(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        no_spike = [{"question": "Test?"}]
        assert analyzer._select_model(no_spike) == default_config.model_triage


# =====================================================================
# Token Resolution Tests
# =====================================================================

class TestTokenResolution:

    def test_global_tokens_resolved(self, sample_df, sample_search_metadata):
        prompt = "Search: $scheduled_search_name$ ran at $execution_time$ with $result_count$ rows."
        resolved = resolve_analyzer_prompt(
            prompt, sample_df, sample_search_metadata, "2026-04-07T12:00:00"
        )
        assert "volume_spikes" in resolved
        assert "2026-04-07T12:00:00" in resolved
        assert "3" in resolved  # 3 rows

    def test_column_tokens_resolved(self, sample_df, sample_search_metadata):
        prompt = "Alert for $question$."
        resolved = resolve_analyzer_prompt(
            prompt, sample_df, sample_search_metadata, "2026-04-07T12:00:00"
        )
        assert "Arsenal" in resolved
        assert "Iran" in resolved
        assert "Scheffler" in resolved

    def test_mv_truncation(self, sample_search_metadata):
        """Column with more distinct values than limit gets truncated."""
        data = {"item": [f"item_{i}" for i in range(20)]}
        df = pd.DataFrame(data)
        prompt = "Items: $item$"
        resolved = resolve_analyzer_prompt(
            prompt, df, sample_search_metadata, "2026-04-07T12:00:00",
            mv_truncate_limit=3,
        )
        assert "TRUNCATED" in resolved
        assert "17 TRUNCATED" in resolved  # 20 - 3 = 17

    def test_mv_no_truncation_below_limit(self, sample_search_metadata):
        data = {"item": ["a", "b"]}
        df = pd.DataFrame(data)
        prompt = "Items: $item$"
        resolved = resolve_analyzer_prompt(
            prompt, df, sample_search_metadata, "2026-04-07T12:00:00",
            mv_truncate_limit=5,
        )
        assert "TRUNCATED" not in resolved
        assert '"a"' in resolved
        assert '"b"' in resolved

    def test_unresolved_tokens_left_asis(self, sample_df, sample_search_metadata):
        prompt = "Unknown: $nonexistent_field$"
        resolved = resolve_analyzer_prompt(
            prompt, sample_df, sample_search_metadata, "2026-04-07T12:00:00"
        )
        assert "$nonexistent_field$" in resolved

    def test_global_tokens_override_column_names(self, sample_search_metadata):
        """If a column is named 'result_count', the global token wins."""
        data = {"result_count": [999]}
        df = pd.DataFrame(data)
        prompt = "$result_count$"
        resolved = resolve_analyzer_prompt(
            prompt, df, sample_search_metadata, "2026-04-07T12:00:00"
        )
        assert resolved == "1"  # global: len(df) = 1, not column value 999

    def test_column_names_token(self, sample_df, sample_search_metadata):
        prompt = "Columns: $column_names$"
        resolved = resolve_analyzer_prompt(
            prompt, sample_df, sample_search_metadata, "2026-04-07T12:00:00"
        )
        for col in sample_df.columns:
            assert col in resolved


# =====================================================================
# Truncate Multivalue Tests
# =====================================================================

class TestTruncateMultivalue:

    def test_empty(self):
        assert _truncate_multivalue([]) == ""

    def test_below_limit(self):
        result = _truncate_multivalue(["a", "b"], limit=5)
        assert result == '"a", "b"'

    def test_at_limit(self):
        result = _truncate_multivalue(["a", "b", "c"], limit=3)
        assert result == '"a", "b", "c"'
        assert "TRUNCATED" not in result

    def test_above_limit(self):
        result = _truncate_multivalue(["a", "b", "c", "d", "e", "f"], limit=3)
        assert '"a", "b", "c"' in result
        assert "3 TRUNCATED" in result

    def test_numeric_values(self):
        result = _truncate_multivalue([1, 2.5, 300], limit=5)
        assert '"1"' in result
        assert '"2.5"' in result


# =====================================================================
# Cost Calculation Tests
# =====================================================================

class TestCostCalculation:

    def test_haiku_cost(self):
        # 1000 input tokens, 500 output tokens on Haiku
        # input: (1000/1M) * $1.00 = $0.001 = 0.1 cents
        # output: (500/1M) * $5.00 = $0.0025 = 0.25 cents
        # total: $0.0035 = 0.35 cents
        cost = _compute_cost_cents("claude-haiku-4-5-20251001", 1000, 500)
        assert abs(cost - 0.35) < 0.01

    def test_sonnet_cost(self):
        # 1000 input, 500 output on Sonnet
        # input: (1000/1M) * $3.00 = $0.003 = 0.3 cents
        # output: (500/1M) * $15.00 = $0.0075 = 0.75 cents
        # total: $0.0105 = 1.05 cents
        cost = _compute_cost_cents("claude-sonnet-4-6", 1000, 500)
        assert abs(cost - 1.05) < 0.01

    def test_unknown_model_uses_haiku_rates(self):
        cost = _compute_cost_cents("unknown-model", 1000, 500)
        expected = _compute_cost_cents("claude-haiku-4-5-20251001", 1000, 500)
        assert cost == expected

    def test_zero_tokens(self):
        assert _compute_cost_cents("claude-sonnet-4-6", 0, 0) == 0.0


# =====================================================================
# Budget Tracking Tests
# =====================================================================

class TestBudgetTracking:

    def test_budget_decrements_after_usage(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        initial = analyzer._usage.budget_remaining_cents
        analyzer._record_usage("claude-haiku-4-5-20251001", 10000, 5000)
        assert analyzer._usage.budget_remaining_cents < initial
        assert analyzer._usage.total_calls == 1

    def test_daily_reset(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        analyzer._usage.budget_remaining_cents = 0
        analyzer._usage.total_cost_cents = 50.0
        analyzer._usage.last_reset_date = "2000-01-01"  # Force stale date
        analyzer._maybe_reset_daily_budget()
        assert analyzer._usage.budget_remaining_cents == float(default_config.daily_budget_cents)
        assert analyzer._usage.total_cost_cents == 0.0
        # M-AN-10 (2026-04-22): analyzer resets use UTC date.
        assert analyzer._usage.last_reset_date == datetime.now(timezone.utc).date().isoformat()

    def test_no_reset_same_day(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        analyzer._record_usage("claude-haiku-4-5-20251001", 10000, 5000)
        spent = analyzer._usage.total_cost_cents
        analyzer._maybe_reset_daily_budget()
        # Should NOT reset since it's the same day
        assert analyzer._usage.total_cost_cents == spent


# =====================================================================
# Response Parsing Tests
# =====================================================================

class TestResponseParsing:

    def _make_response(self, text, input_tokens=100, output_tokens=50):
        """Build a mock API response object."""
        content_block = MagicMock()
        content_block.text = text
        usage = MagicMock()
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        response = MagicMock()
        response.content = [content_block]
        response.usage = usage
        return response

    def test_valid_json(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        payload = json.dumps({
            "alert_priority": "HIGH",
            "summary": "Significant volume spike detected.",
            "actionable_markets": [
                {
                    "question": "Will Arsenal win?",
                    "position": "YES",
                    "confidence": 0.85,
                    "reasoning": "Strong momentum.",
                    "estimated_roi": 15.0,
                }
            ],
            "pattern_detected": "correlated spike",
            "cross_reference_needed": ["news feeds"],
        })
        response = self._make_response(payload)
        parsed = analyzer._parse_response(response)
        assert parsed["status"] == "analyzed"
        assert parsed["alert_priority"] == "HIGH"
        assert len(parsed["actionable_markets"]) == 1
        assert parsed["actionable_markets"][0].position == "YES"

    def test_json_in_code_block(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        payload = '```json\n' + json.dumps({
            "alert_priority": "LOW",
            "summary": "Nothing notable.",
            "actionable_markets": [],
        }) + '\n```'
        response = self._make_response(payload)
        parsed = analyzer._parse_response(response)
        assert parsed["status"] == "analyzed"
        assert parsed["alert_priority"] == "LOW"

    def test_malformed_json(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        response = self._make_response("This is not JSON at all.")
        parsed = analyzer._parse_response(response)
        assert parsed["status"] == "error"
        assert "Failed to parse" in parsed["error_message"]
        assert parsed["raw_response"] == "This is not JSON at all."

    def test_missing_required_keys(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        payload = json.dumps({"alert_priority": "HIGH"})  # missing summary, actionable_markets
        response = self._make_response(payload)
        parsed = analyzer._parse_response(response)
        assert parsed["status"] == "error"
        assert "Missing keys" in parsed["error_message"]

    def test_actionable_markets_capped_at_five(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        markets = [{"question": f"Q{i}", "position": "YES", "confidence": 0.5,
                     "reasoning": "r", "estimated_roi": 1.0} for i in range(10)]
        payload = json.dumps({
            "alert_priority": "HIGH",
            "summary": "Test.",
            "actionable_markets": markets,
        })
        response = self._make_response(payload)
        parsed = analyzer._parse_response(response)
        assert len(parsed["actionable_markets"]) == 5


# =====================================================================
# Validation Tests
# =====================================================================

class TestAnalyzerPromptValidation:

    def test_valid_name(self):
        assert AnalyzerPromptValidation.validate_name("my_prompt-1") == "my_prompt-1"

    def test_invalid_name_empty(self):
        with pytest.raises(ValueError):
            AnalyzerPromptValidation.validate_name("")

    def test_invalid_name_special_chars(self):
        with pytest.raises(ValueError):
            AnalyzerPromptValidation.validate_name("prompt@#!")

    def test_valid_prompt_text(self):
        assert AnalyzerPromptValidation.validate_prompt_text("Analyze $question$") == "Analyze $question$"

    def test_invalid_prompt_text_empty(self):
        with pytest.raises(ValueError):
            AnalyzerPromptValidation.validate_prompt_text("")

    def test_extract_tokens(self):
        tokens = AnalyzerPromptValidation.extract_tokens(
            "Alert for $question$ with spike $spike_multiple$ at $execution_time$."
        )
        assert tokens == {"question", "spike_multiple", "execution_time"}

    def test_validate_tokens_all_resolved(self):
        report = AnalyzerPromptValidation.validate_tokens_against_columns(
            "Search $scheduled_search_name$ found $question$",
            ["question", "volume"],
        )
        assert report["valid"] is True
        assert "scheduled_search_name" in report["global_tokens"]
        assert "question" in report["column_tokens"]
        assert report["unresolved"] == []

    def test_validate_tokens_with_unresolved(self):
        report = AnalyzerPromptValidation.validate_tokens_against_columns(
            "$scheduled_search_name$ $nonexistent$",
            ["question"],
        )
        assert report["valid"] is False
        assert "nonexistent" in report["unresolved"]


# =====================================================================
# AnalyzerPromptStore CRUD Tests
# =====================================================================

class TestAnalyzerPromptStore:

    @pytest.fixture(autouse=True)
    def setup_temp_store(self, tmp_path):
        """Create a store backed by a temp directory."""
        from analyzer_prompt_store import AnalyzerPromptStore

        self.store = AnalyzerPromptStore()
        self.store._dir = tmp_path / "analyzer_prompts"
        self.store._db = str(tmp_path / "last_chance.sqlite")
        self.store.initialize()

    def test_create_and_get(self):
        data = {"name": "test_prompt", "prompt_text": "Analyze $question$."}
        result = self.store.save_prompt(data)
        assert result["name"] == "test_prompt"
        assert result["prompt_text"] == "Analyze $question$."
        assert "created_at" in result

        fetched = self.store.get_prompt("test_prompt")
        assert fetched["name"] == "test_prompt"

    def test_list_prompts(self):
        self.store.save_prompt({"name": "alpha", "prompt_text": "A"})
        self.store.save_prompt({"name": "beta", "prompt_text": "B"})
        prompts = self.store.list_prompts()
        assert len(prompts) == 2
        assert prompts[0]["name"] == "alpha"

    def test_update_prompt(self):
        self.store.save_prompt({"name": "test", "prompt_text": "Original."})
        updated = self.store.update_prompt("test", {"prompt_text": "Updated."})
        assert updated["prompt_text"] == "Updated."

    def test_delete_prompt_soft(self):
        self.store.save_prompt({"name": "deleteme", "prompt_text": "Gone."})
        self.store.delete_prompt("deleteme")
        with pytest.raises(FileNotFoundError):
            self.store.get_prompt("deleteme")

    def test_duplicate_name_raises(self):
        self.store.save_prompt({"name": "unique", "prompt_text": "First."})
        with pytest.raises(FileExistsError):
            self.store.save_prompt({"name": "unique", "prompt_text": "Second."})

    def test_overwrite_preserves_created_at(self):
        original = self.store.save_prompt({"name": "overwrite_me", "prompt_text": "V1."})
        updated = self.store.save_prompt(
            {"name": "overwrite_me", "prompt_text": "V2."}, overwrite=True
        )
        assert updated["created_at"] == original["created_at"]
        assert updated["prompt_text"] == "V2."

    def test_get_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            self.store.get_prompt("does_not_exist")

    def test_get_yaml_raw(self):
        self.store.save_prompt({"name": "raw_test", "prompt_text": "Raw."})
        raw = self.store.get_prompt_yaml("raw_test")
        assert "Raw." in raw
        assert "name:" in raw


# =====================================================================
# Config Validation Tests
# =====================================================================

class TestConfigValidation:

    def test_defaults_registered(self):
        from global_settings import DEFAULTS
        assert "claude_analyzer_enabled" in DEFAULTS
        assert "claude_analyzer_daily_budget_cents" in DEFAULTS
        assert DEFAULTS["claude_analyzer_enabled"] is False

    def test_int_validators_registered(self):
        from global_settings import _INT_VALIDATORS
        assert "claude_analyzer_max_output_tokens" in _INT_VALIDATORS
        assert "claude_analyzer_daily_budget_cents" in _INT_VALIDATORS

    def test_validate_bool_setting(self):
        from global_settings import _validate_key, DEFAULTS
        assert _validate_key("claude_analyzer_enabled", True, DEFAULTS) is None
        assert _validate_key("claude_analyzer_enabled", "yes", DEFAULTS) is not None

    def test_validate_string_setting(self):
        from global_settings import _validate_key, DEFAULTS
        assert _validate_key("claude_analyzer_model_primary", "claude-sonnet-4-6", DEFAULTS) is None
        assert _validate_key("claude_analyzer_model_primary", 123, DEFAULTS) is not None

    def test_validate_boilerplate_prompt_setting(self):
        from global_settings import _validate_key, DEFAULTS
        assert _validate_key("claude_analyzer_boilerplate_prompt", "You are a financial analyst.", DEFAULTS) is None
        assert _validate_key("claude_analyzer_boilerplate_prompt", "", DEFAULTS) is None
        assert _validate_key("claude_analyzer_boilerplate_prompt", 123, DEFAULTS) is not None

    def test_api_key_env_removed(self):
        from global_settings import DEFAULTS
        assert "claude_analyzer_api_key_env" not in DEFAULTS
        assert "claude_analyzer_boilerplate_prompt" in DEFAULTS

    def test_validate_float_setting(self):
        from global_settings import _validate_key, DEFAULTS
        assert _validate_key("claude_analyzer_spike_threshold", 10.0, DEFAULTS) is None
        assert _validate_key("claude_analyzer_spike_threshold", -1.0, DEFAULTS) is not None

    def test_validate_int_bounds(self):
        from global_settings import _validate_key, DEFAULTS
        # max_output_tokens bounds: (128, 32768) - ceiling raised 2026-04-20 from
        # 4096 to 32768 for full analyst-brief output; see memory entry
        # `reference_claude_output_token_budget.md`.
        assert _validate_key("claude_analyzer_max_output_tokens", 1024, DEFAULTS) is None
        assert _validate_key("claude_analyzer_max_output_tokens", 16384, DEFAULTS) is None
        assert _validate_key("claude_analyzer_max_output_tokens", 50, DEFAULTS) is not None
        assert _validate_key("claude_analyzer_max_output_tokens", 50000, DEFAULTS) is not None


# =====================================================================
# Dataclass Tests
# =====================================================================

class TestDataclasses:

    def test_analysis_result_defaults(self):
        r = AnalysisResult()
        assert r.status == "skipped"
        assert r.alert_priority == "LOW"
        assert r.actionable_markets == []
        assert r.cost_cents == 0.0

    def test_analyzer_config_defaults(self):
        c = AnalyzerConfig()
        assert c.model_primary == "claude-sonnet-4-6"
        assert c.daily_budget_cents == 50
        assert c.mv_truncate_limit == 5

    def test_usage_stats_defaults(self):
        u = UsageStats()
        assert u.total_calls == 0
        assert u.total_cost_cents == 0.0

    def test_actionable_market_fields(self):
        m = ActionableMarket(
            question="Test?", position="YES", confidence=0.9,
            reasoning="Strong signal.", estimated_roi=25.0,
        )
        assert m.confidence == 0.9

    def test_analysis_result_filter_defaults(self):
        r = AnalysisResult()
        assert r.filter_passed is True
        assert r.filter_answer == ""

    def test_analysis_result_batch_fields(self):
        r = AnalysisResult(batch_id="batch_123", batch_custom_id="uuid-456")
        assert r.batch_id == "batch_123"
        assert r.batch_custom_id == "uuid-456"

    def test_analysis_result_batch_defaults(self):
        r = AnalysisResult()
        assert r.batch_id == ""
        assert r.batch_custom_id == ""


# =====================================================================
# Filter Gate Tests
# =====================================================================

class TestFilterGate:

    @pytest.fixture
    def analyzed_result(self):
        """A completed analysis result to filter against."""
        return AnalysisResult(
            status="analyzed",
            alert_priority="HIGH",
            summary="Significant volume spike detected on Arsenal market.",
            actionable_markets=[
                ActionableMarket(
                    question="Will Arsenal win?",
                    position="YES",
                    confidence=0.85,
                    reasoning="Strong momentum.",
                    estimated_roi=15.0,
                )
            ],
            pattern_detected="correlated spike",
        )

    def _make_filter_response(self, text, input_tokens=50, output_tokens=5):
        """Build a mock API response for a filter call."""
        content_block = MagicMock()
        content_block.text = text
        usage = MagicMock()
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        response = MagicMock()
        response.content = [content_block]
        response.usage = usage
        return response

    def test_filter_empty_question_passes(self, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        result = analyzer.evaluate_filter(analyzed_result, "")
        assert result.filter_passed is True

    def test_filter_skipped_analysis_passes(self, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        skipped = AnalysisResult(status="skipped", skip_reason="empty_results")
        result = analyzer.evaluate_filter(skipped, "Is this actionable?")
        assert result.filter_passed is True

    def test_filter_budget_exhausted_passes(self, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        analyzer._usage.budget_remaining_cents = 0
        result = analyzer.evaluate_filter(analyzed_result, "Is this actionable?")
        assert result.filter_passed is True
        assert result.filter_answer == "BUDGET_EXHAUSTED"

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_yes_passes(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_filter_response("YES")
        result = analyzer.evaluate_filter(analyzed_result, "Is this a genuine signal?")
        assert result.filter_passed is True
        assert result.filter_answer == "YES"

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_no_blocks(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_filter_response("NO")
        result = analyzer.evaluate_filter(analyzed_result, "Is this a genuine signal?")
        assert result.filter_passed is False
        assert result.filter_answer == "NO"

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_lowercase_yes(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_filter_response("yes")
        result = analyzer.evaluate_filter(analyzed_result, "Should we alert?")
        assert result.filter_passed is True
        assert result.filter_answer == "YES"

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_ambiguous_defaults_to_pass(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_filter_response("Maybe, I'm not sure")
        result = analyzer.evaluate_filter(analyzed_result, "Is this actionable?")
        assert result.filter_passed is True
        assert "AMBIGUOUS" in result.filter_answer

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_api_error_defaults_to_pass(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.side_effect = RuntimeError("Connection failed")
        result = analyzer.evaluate_filter(analyzed_result, "Is this actionable?")
        assert result.filter_passed is True
        assert "ERROR" in result.filter_answer

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_uses_triage_model(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_filter_response("YES")
        analyzer.evaluate_filter(analyzed_result, "Should we alert?")
        # Verify the triage (cheap) model was used
        call_args = mock_api.call_args
        assert call_args[0][0] == default_config.model_triage

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_filter_cost_added_to_analysis(self, mock_api, default_config, analyzed_result):
        analyzer = ClaudeAnalyzer(default_config)
        original_cost = analyzed_result.cost_cents
        mock_api.return_value = self._make_filter_response("YES", input_tokens=100, output_tokens=10)
        result = analyzer.evaluate_filter(analyzed_result, "Is this actionable?")
        assert result.cost_cents > original_cost
        assert result.input_tokens > 0


# =====================================================================
# Boilerplate Prompt Tests
# =====================================================================

class TestBoilerplatePrompt:

    def _make_response(self, text="{}"):
        content_block = MagicMock()
        content_block.text = text
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        response = MagicMock()
        response.content = [content_block]
        response.usage = usage
        return response

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_boilerplate_prepended_to_system_prompt(self, mock_api, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_response(json.dumps({
            "alert_priority": "LOW", "summary": "ok", "actionable_markets": [],
        }))
        analyzer.analyze(
            "test", [{"foo": "bar"}],
            system_prompt="Per-search prompt.",
            boilerplate_prompt="Global boilerplate.",
        )
        # Verify the system prompt passed to _call_api includes both
        call_args = mock_api.call_args
        system_sent = call_args[0][1]
        assert system_sent.startswith("Global boilerplate.")
        assert "Per-search prompt." in system_sent

    @patch.object(ClaudeAnalyzer, "_call_api")
    def test_empty_boilerplate_not_prepended(self, mock_api, default_config):
        analyzer = ClaudeAnalyzer(default_config)
        mock_api.return_value = self._make_response(json.dumps({
            "alert_priority": "LOW", "summary": "ok", "actionable_markets": [],
        }))
        analyzer.analyze(
            "test", [{"foo": "bar"}],
            system_prompt="Per-search prompt.",
            boilerplate_prompt="",
        )
        call_args = mock_api.call_args
        system_sent = call_args[0][1]
        assert system_sent == "Per-search prompt."


# =====================================================================
# JSON Serialization Tests
# =====================================================================

class TestJsonSerialization:

    def test_result_df_to_json_format(self):
        from analyzers.claude_analyzer import _result_df_to_json
        df = pd.DataFrame({"question": ["Test?"], "value": [42]})
        result = _result_df_to_json(df)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert parsed[0]["question"] == "Test?"
        assert parsed[0]["value"] == 42

    def test_result_df_to_json_multiple_rows(self):
        from analyzers.claude_analyzer import _result_df_to_json
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        result = _result_df_to_json(df)
        parsed = json.loads(result)
        assert len(parsed) == 3


# =====================================================================
# Static parse_response_text Tests
# =====================================================================

class TestParseResponseText:

    def test_valid_json(self):
        raw = json.dumps({
            "alert_priority": "HIGH",
            "summary": "Spike detected.",
            "actionable_markets": [],
        })
        parsed = ClaudeAnalyzer.parse_response_text(raw, 100, 50)
        assert parsed["status"] == "analyzed"
        assert parsed["alert_priority"] == "HIGH"
        assert parsed["input_tokens"] == 100

    def test_json_in_code_block(self):
        raw = "```json\n" + json.dumps({
            "alert_priority": "LOW",
            "summary": "Nothing.",
            "actionable_markets": [],
        }) + "\n```"
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "analyzed"

    def test_garbage_input(self):
        parsed = ClaudeAnalyzer.parse_response_text("not json at all")
        assert parsed["status"] == "error"
        assert "Failed to parse" in parsed["error_message"]

    def test_missing_keys(self):
        raw = json.dumps({"alert_priority": "HIGH"})
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "error"
        assert "Missing keys" in parsed["error_message"]

    def test_markets_capped_at_five(self):
        markets = [{"question": f"Q{i}", "position": "YES",
                     "confidence": 0.5, "reasoning": "r",
                     "estimated_roi": 1.0} for i in range(10)]
        raw = json.dumps({
            "alert_priority": "HIGH",
            "summary": "Test.",
            "actionable_markets": markets,
        })
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert len(parsed["actionable_markets"]) == 5

    # ------------------------------------------------------------------
    # B-AN-1 regression: non-dict JSON must not crash at set(data.keys())
    # ------------------------------------------------------------------

    def test_non_dict_json_list(self):
        """Top-level JSON array must return a structured error, not crash."""
        parsed = ClaudeAnalyzer.parse_response_text("[1, 2, 3]")
        assert parsed["status"] == "error"
        assert "not an object" in parsed["error_message"]
        assert "list" in parsed["error_message"]

    def test_non_dict_json_scalar(self):
        """Top-level JSON number must return a structured error."""
        parsed = ClaudeAnalyzer.parse_response_text("42")
        assert parsed["status"] == "error"
        assert "not an object" in parsed["error_message"]
        assert "int" in parsed["error_message"]

    def test_non_dict_json_string(self):
        """Top-level JSON string must return a structured error."""
        parsed = ClaudeAnalyzer.parse_response_text('"just a string"')
        assert parsed["status"] == "error"
        assert "not an object" in parsed["error_message"]

    def test_non_dict_json_null(self):
        """Top-level JSON null must return a structured error, not crash."""
        parsed = ClaudeAnalyzer.parse_response_text("null")
        assert parsed["status"] == "error"
        assert "not an object" in parsed["error_message"]
        assert "NoneType" in parsed["error_message"]

    def test_non_dict_json_in_code_block(self):
        """Fenced JSON array (same crash surface via the fallback path)."""
        parsed = ClaudeAnalyzer.parse_response_text("```json\n[1, 2, 3]\n```")
        assert parsed["status"] == "error"
        assert "not an object" in parsed["error_message"]

    # ------------------------------------------------------------------
    # H-AN-3 regression: fence-strip + prose-preamble fallbacks
    # ------------------------------------------------------------------

    def test_leading_whitespace_in_fence(self):
        """Fenced JSON with leading/trailing whitespace inside the fence must parse."""
        raw = (
            "```json\n"
            "   " + json.dumps({
                "alert_priority": "HIGH",
                "summary": "whitespace edge case",
                "actionable_markets": [],
            }) + "   \n"
            "```"
        )
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "analyzed", f"errors: {parsed}"
        assert parsed["alert_priority"] == "HIGH"
        assert parsed["summary"] == "whitespace edge case"

    def test_prose_before_unfenced_json(self):
        """Claude emits prose before the JSON object without a fence - brace-balanced fallback must catch it."""
        raw = (
            "Here is the analysis you requested:\n\n"
            + json.dumps({
                "alert_priority": "LOW",
                "summary": "nothing urgent",
                "actionable_markets": [],
            })
            + "\n\nThat's everything."
        )
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "analyzed", f"errors: {parsed}"
        assert parsed["alert_priority"] == "LOW"
        assert parsed["summary"] == "nothing urgent"

    def test_fenced_markdown_non_json_falls_through_cleanly(self):
        """Fenced content that isn't JSON and has no {...} elsewhere → generic parse error."""
        raw = "```\nsome text that isn't json at all\n```"
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "error"
        assert "Failed to parse" in parsed["error_message"]

    def test_json_with_nested_braces_in_string(self):
        """Brace-balanced extractor must respect string literals (braces inside strings)."""
        raw = (
            "prose preamble\n"
            + json.dumps({
                "alert_priority": "HIGH",
                "summary": "note the {braces} in this string",
                "actionable_markets": [],
            })
            + "\ntrailing prose"
        )
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "analyzed"
        assert "{braces}" in parsed["summary"]

    def test_escaped_quote_in_balanced_extract(self):
        """Brace-balanced extractor must respect backslash-escaped quotes inside strings."""
        raw = (
            'Here is the JSON:\n'
            '{"alert_priority": "LOW", '
            '"summary": "she said \\"ok\\" and left", '
            '"actionable_markets": []}\n'
            'end prose'
        )
        parsed = ClaudeAnalyzer.parse_response_text(raw)
        assert parsed["status"] == "analyzed"
        assert "she said" in parsed["summary"]


# =====================================================================
# Batch Poll Interval Config Tests
# =====================================================================

class TestBatchPollIntervalConfig:

    def test_batch_poll_interval_default(self):
        from global_settings import DEFAULTS
        assert "claude_analyzer_batch_poll_interval_minutes" in DEFAULTS
        assert DEFAULTS["claude_analyzer_batch_poll_interval_minutes"] == 5

    def test_batch_poll_interval_validators(self):
        from global_settings import _INT_VALIDATORS
        assert "claude_analyzer_batch_poll_interval_minutes" in _INT_VALIDATORS
        lo, hi = _INT_VALIDATORS["claude_analyzer_batch_poll_interval_minutes"]
        assert lo == 1
        assert hi == 60

    def test_batch_poll_interval_validation(self):
        from global_settings import _validate_key, DEFAULTS
        assert _validate_key("claude_analyzer_batch_poll_interval_minutes", 5, DEFAULTS) is None
        assert _validate_key("claude_analyzer_batch_poll_interval_minutes", 0, DEFAULTS) is not None
        assert _validate_key("claude_analyzer_batch_poll_interval_minutes", 61, DEFAULTS) is not None
