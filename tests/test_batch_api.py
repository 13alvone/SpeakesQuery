#!/usr/bin/env python3
"""
Unit tests for the Claude batch API layer.

Covers:
  - Batch submission via ClaudeAnalyzer.analyze() when enable_batch=True
  - _call_batch_api parameter correctness
  - Fallback to synchronous path on batch submission failure
  - Batch poller: in_progress, succeeded, errored, expired, empty pending
  - 50% batch cost discount applied in _handle_batch_result
  - AnalyzerStorage batch request CRUD (get_pending_batch_ids,
    get_request, mark_batch_completed, create_batch_request)

All tests run without an API key or network access.
"""

import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from analyzers.models import AnalysisResult, AnalyzerConfig
from analyzers.claude_analyzer import ClaudeAnalyzer, _compute_cost_cents
from analyzers.storage import AnalyzerStorage


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def batch_config():
    """AnalyzerConfig with batch mode enabled."""
    return AnalyzerConfig(api_key="sk-test-batch", enable_batch=True)


@pytest.fixture
def sync_config():
    """AnalyzerConfig with batch mode disabled (default)."""
    return AnalyzerConfig(api_key="sk-test-sync")


@pytest.fixture
def sample_results():
    """Realistic sample result rows that pass all gates."""
    return [
        {
            "question": "Will Arsenal win the 2025-26 EPL?",
            "yes_price": 0.86,
            "volume_24h": 50000,
            "spike_multiple": 6.25,
            "liquidity": 407131,
        },
    ]


@pytest.fixture
def storage(tmp_path):
    """AnalyzerStorage backed by a temp directory."""
    db_path = str(tmp_path / "test_analyzer.sqlite")
    return AnalyzerStorage(db_path=db_path)


@pytest.fixture
def valid_response_json():
    """A valid Claude JSON response payload."""
    return json.dumps({
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


def _make_sync_response(text, input_tokens=100, output_tokens=50):
    """Build a mock synchronous API response object."""
    content_block = MagicMock()
    content_block.text = text
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    return response


# =====================================================================
# TestBatchSubmission
# =====================================================================

class TestBatchSubmission:
    """Tests for batch submission via ClaudeAnalyzer.analyze()."""

    @patch.object(ClaudeAnalyzer, "_call_batch_api", return_value="batch_abc123")
    def test_batch_mode_returns_pending(self, mock_batch, batch_config, sample_results):
        """When enable_batch=True, analyze() returns status='batch_pending'."""
        analyzer = ClaudeAnalyzer(batch_config)
        result = analyzer.analyze("test_search", sample_results)

        assert result.status == "batch_pending"
        assert result.batch_id == "batch_abc123"
        assert result.batch_custom_id != ""
        assert result.model_used != ""

    @patch.object(ClaudeAnalyzer, "_call_batch_api", return_value="batch_xyz789")
    def test_batch_api_called_with_correct_params(self, mock_batch, batch_config, sample_results):
        """Verify _call_batch_api receives model, system_prompt, user_content, custom_id."""
        analyzer = ClaudeAnalyzer(batch_config)
        analyzer.analyze(
            "test_search",
            sample_results,
            system_prompt="Test system prompt.",
        )

        mock_batch.assert_called_once()
        args = mock_batch.call_args[0]
        # args: (model, system_prompt, user_content, custom_id)
        model, system_prompt, user_content, custom_id = args

        assert model in (batch_config.model_triage, batch_config.model_primary)
        assert "Test system prompt." in system_prompt
        assert custom_id  # UUID string, non-empty
        # user_content should be JSON of the results
        parsed_content = json.loads(user_content)
        assert isinstance(parsed_content, list)

    @patch.object(ClaudeAnalyzer, "_call_batch_api", return_value="batch_999")
    def test_batch_custom_id_is_uuid(self, mock_batch, batch_config, sample_results):
        """The custom_id passed to _call_batch_api should be a valid UUID."""
        import uuid
        analyzer = ClaudeAnalyzer(batch_config)
        analyzer.analyze("test_search", sample_results)

        custom_id = mock_batch.call_args[0][3]
        # Should parse as a valid UUID
        parsed_uuid = uuid.UUID(custom_id)
        assert str(parsed_uuid) == custom_id

    def test_sync_mode_does_not_call_batch(self, sync_config, sample_results, valid_response_json):
        """When enable_batch=False, _call_batch_api is never invoked."""
        analyzer = ClaudeAnalyzer(sync_config)
        with patch.object(analyzer, "_call_batch_api") as mock_batch, \
             patch.object(analyzer, "_call_api") as mock_sync:
            mock_sync.return_value = _make_sync_response(valid_response_json)
            analyzer.analyze("test_search", sample_results)

            mock_batch.assert_not_called()
            mock_sync.assert_called_once()

    @patch("anthropic.Anthropic")
    def test_call_batch_api_creates_batch(self, mock_anthropic_cls, batch_config):
        """_call_batch_api calls client.messages.batches.create with correct structure."""
        mock_client = MagicMock()
        mock_batch_obj = MagicMock()
        mock_batch_obj.id = "batch_created_123"
        mock_client.messages.batches.create.return_value = mock_batch_obj
        mock_anthropic_cls.return_value = mock_client

        analyzer = ClaudeAnalyzer(batch_config)
        batch_id = analyzer._call_batch_api(
            model="claude-haiku-4-5-20251001",
            system_prompt="Test prompt",
            user_content='[{"foo": "bar"}]',
            custom_id="custom-uuid-123",
        )

        assert batch_id == "batch_created_123"
        create_call = mock_client.messages.batches.create.call_args
        requests = create_call[1]["requests"]
        assert len(requests) == 1
        assert requests[0]["custom_id"] == "custom-uuid-123"
        assert requests[0]["params"]["model"] == "claude-haiku-4-5-20251001"
        assert requests[0]["params"]["max_tokens"] == batch_config.max_output_tokens
        assert requests[0]["params"]["system"] == "Test prompt"
        assert requests[0]["params"]["messages"][0]["role"] == "user"


# =====================================================================
# TestBatchFallback
# =====================================================================

class TestBatchFallback:
    """When batch submission fails, analyzer falls back to synchronous path."""

    @patch.object(ClaudeAnalyzer, "_call_api")
    @patch.object(ClaudeAnalyzer, "_call_batch_api", side_effect=RuntimeError("Batch API down"))
    def test_fallback_to_sync_on_batch_failure(
        self, mock_batch, mock_sync, batch_config, sample_results, valid_response_json,
    ):
        """If _call_batch_api raises, analyze() falls back to _call_api."""
        mock_sync.return_value = _make_sync_response(valid_response_json)

        analyzer = ClaudeAnalyzer(batch_config)
        result = analyzer.analyze("test_search", sample_results)

        # Batch was attempted
        mock_batch.assert_called_once()
        # Sync fallback was used
        mock_sync.assert_called_once()
        # Result should be from the sync path (analyzed, not batch_pending)
        assert result.status == "analyzed"
        assert result.alert_priority == "HIGH"

    @patch.object(ClaudeAnalyzer, "_call_api")
    @patch.object(ClaudeAnalyzer, "_call_batch_api", side_effect=ImportError("No anthropic SDK"))
    def test_fallback_on_import_error(
        self, mock_batch, mock_sync, batch_config, sample_results, valid_response_json,
    ):
        """ImportError from _call_batch_api also triggers sync fallback."""
        mock_sync.return_value = _make_sync_response(valid_response_json)

        analyzer = ClaudeAnalyzer(batch_config)
        result = analyzer.analyze("test_search", sample_results)

        mock_batch.assert_called_once()
        mock_sync.assert_called_once()
        assert result.status == "analyzed"

    @patch.object(ClaudeAnalyzer, "_call_api")
    @patch.object(ClaudeAnalyzer, "_call_batch_api", side_effect=Exception("Generic failure"))
    def test_fallback_preserves_model_routing(
        self, mock_batch, mock_sync, batch_config, valid_response_json,
    ):
        """Fallback sync call should use the same model that was selected."""
        # High spike triggers primary model
        high_spike_results = [{"spike_multiple": 15.0, "liquidity": 500000}]
        mock_sync.return_value = _make_sync_response(valid_response_json)

        analyzer = ClaudeAnalyzer(batch_config)
        analyzer.analyze("test_search", high_spike_results)

        # The sync call should use the primary model
        sync_args = mock_sync.call_args[0]
        assert sync_args[0] == batch_config.model_primary


# =====================================================================
# TestBatchPoller
# =====================================================================

class TestBatchPoller:
    """Tests for analyzers.batch_poller functions."""

    @pytest.fixture
    def mock_anthropic(self):
        """Provide a mocked anthropic module and client."""
        mock_client = MagicMock()
        return mock_client

    def _make_succeeded_result(self, custom_id, raw_text, input_tokens=200, output_tokens=100):
        """Build a mock MessageBatchIndividualResponse with type='succeeded'."""
        result = MagicMock()
        result.custom_id = custom_id

        message = MagicMock()
        content_block = MagicMock()
        content_block.text = raw_text
        message.content = [content_block]
        message.usage.input_tokens = input_tokens
        message.usage.output_tokens = output_tokens

        result.result.type = "succeeded"
        result.result.message = message
        return result

    def _make_errored_result(self, custom_id, error_msg="Something went wrong"):
        """Build a mock result with type='errored'."""
        result = MagicMock()
        result.custom_id = custom_id
        result.result.type = "errored"
        result.result.error = error_msg
        return result

    def _make_expired_result(self, custom_id):
        """Build a mock result with type='expired'."""
        result = MagicMock()
        result.custom_id = custom_id
        result.result.type = "expired"
        return result

    def test_empty_pending_returns_zero(self, storage):
        """poll_pending_batches with no pending IDs returns 0."""
        from analyzers.batch_poller import poll_pending_batches

        # Storage has no pending batch IDs
        count = poll_pending_batches(storage)
        assert count == 0

    @patch("analyzers.batch_poller._get_api_key", return_value="sk-test")
    @patch("analyzers.batch_poller.anthropic", create=True)
    def test_in_progress_returns_zero(self, mock_sdk, mock_key, storage):
        """Batch still in_progress returns 0 processed."""
        from analyzers.batch_poller import _process_batch

        mock_client = MagicMock()
        mock_batch = MagicMock()
        mock_batch.processing_status = "in_progress"
        mock_client.messages.batches.retrieve.return_value = mock_batch

        processed = _process_batch(mock_client, "batch_123", storage)
        assert processed == 0

    @patch("analyzers.batch_poller._get_api_key", return_value="sk-test")
    def test_succeeded_result_stores_and_marks_completed(self, mock_key, storage, valid_response_json):
        """Batch ended with succeeded result: parses, stores, marks completed."""
        from analyzers.batch_poller import _process_batch

        custom_id = "custom-abc-123"
        batch_id = "batch_ended_ok"

        # Pre-populate a pending batch request in storage
        storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name="volume_spikes",
            model="claude-haiku-4-5-20251001",
            system_prompt="Test prompt",
            user_content="[]",
            search_metadata={},
            result_parquet_path="",
        )

        mock_client = MagicMock()
        mock_batch = MagicMock()
        mock_batch.processing_status = "ended"
        mock_client.messages.batches.retrieve.return_value = mock_batch

        succeeded = self._make_succeeded_result(custom_id, valid_response_json)
        mock_client.messages.batches.results.return_value = [succeeded]

        processed = _process_batch(mock_client, batch_id, storage)
        assert processed == 1

        # Verify the request was marked completed
        request = storage.get_request(custom_id)
        assert request["status"] == "succeeded"
        assert request["completed_at"] != ""
        assert request["result_json"] != ""

    @patch("analyzers.batch_poller._get_api_key", return_value="sk-test")
    def test_errored_result_marks_completed_with_error(self, mock_key, storage):
        """Batch ended with errored result: marks completed with error status."""
        from analyzers.batch_poller import _process_batch

        custom_id = "custom-err-456"
        batch_id = "batch_ended_err"

        storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name="errored_search",
            model="claude-haiku-4-5-20251001",
            system_prompt="Test",
            user_content="[]",
            search_metadata={},
            result_parquet_path="",
        )

        mock_client = MagicMock()
        mock_batch = MagicMock()
        mock_batch.processing_status = "ended"
        mock_client.messages.batches.retrieve.return_value = mock_batch

        errored = self._make_errored_result(custom_id, "Rate limit exceeded")
        mock_client.messages.batches.results.return_value = [errored]

        processed = _process_batch(mock_client, batch_id, storage)
        assert processed == 1

        request = storage.get_request(custom_id)
        assert request["status"] == "errored"
        assert "Rate limit exceeded" in request["result_json"]

    @patch("analyzers.batch_poller._get_api_key", return_value="sk-test")
    def test_expired_result_marks_completed_as_expired(self, mock_key, storage):
        """Batch ended with expired result: marks completed as expired."""
        from analyzers.batch_poller import _process_batch

        custom_id = "custom-exp-789"
        batch_id = "batch_ended_exp"

        storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name="expired_search",
            model="claude-haiku-4-5-20251001",
            system_prompt="Test",
            user_content="[]",
            search_metadata={},
            result_parquet_path="",
        )

        mock_client = MagicMock()
        mock_batch = MagicMock()
        mock_batch.processing_status = "ended"
        mock_client.messages.batches.retrieve.return_value = mock_batch

        expired = self._make_expired_result(custom_id)
        mock_client.messages.batches.results.return_value = [expired]

        processed = _process_batch(mock_client, batch_id, storage)
        assert processed == 1

        request = storage.get_request(custom_id)
        assert request["status"] == "expired"

    @patch("analyzers.batch_poller._get_api_key", return_value="sk-test")
    def test_unexpected_status_returns_zero(self, mock_key, storage):
        """Batch with unexpected processing_status returns 0."""
        from analyzers.batch_poller import _process_batch

        mock_client = MagicMock()
        mock_batch = MagicMock()
        mock_batch.processing_status = "canceling"
        mock_client.messages.batches.retrieve.return_value = mock_batch

        processed = _process_batch(mock_client, "batch_weird", storage)
        assert processed == 0

    @patch("analyzers.batch_poller._get_api_key", return_value="sk-test")
    @patch("anthropic.Anthropic")
    def test_poll_pending_batches_full_flow(self, mock_anthropic_cls, mock_key, storage, valid_response_json):
        """End-to-end: poll_pending_batches finds pending, processes ended batch."""
        from analyzers.batch_poller import poll_pending_batches

        custom_id = "custom-full-flow"
        batch_id = "batch_full"

        storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name="full_flow_search",
            model="claude-haiku-4-5-20251001",
            system_prompt="Test",
            user_content="[]",
            search_metadata={},
            result_parquet_path="",
        )

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        mock_batch = MagicMock()
        mock_batch.processing_status = "ended"
        mock_client.messages.batches.retrieve.return_value = mock_batch

        succeeded = self._make_succeeded_result(custom_id, valid_response_json)
        mock_client.messages.batches.results.return_value = [succeeded]

        count = poll_pending_batches(storage)
        assert count == 1


# =====================================================================
# TestBatchCostDiscount
# =====================================================================

class TestBatchCostDiscount:
    """Verify batch results apply the 50% cost discount."""

    def test_batch_cost_is_half_of_sync(self, storage, valid_response_json):
        """_handle_batch_result applies 50% discount to computed cost."""
        from analyzers.batch_poller import _handle_batch_result

        custom_id = "custom-cost-test"
        batch_id = "batch_cost"

        storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name="cost_test",
            model="claude-haiku-4-5-20251001",
            system_prompt="Test",
            user_content="[]",
            search_metadata={},
            result_parquet_path="",
        )

        input_tokens = 1000
        output_tokens = 500

        # Compute what sync cost would be
        sync_cost = _compute_cost_cents("claude-haiku-4-5-20251001", input_tokens, output_tokens)
        expected_batch_cost = sync_cost * 0.5

        # Build a succeeded result
        result = MagicMock()
        result.custom_id = custom_id
        result.result.type = "succeeded"

        message = MagicMock()
        content_block = MagicMock()
        content_block.text = valid_response_json
        message.content = [content_block]
        message.usage.input_tokens = input_tokens
        message.usage.output_tokens = output_tokens
        result.result.message = message

        # Capture what gets stored by patching store_result
        stored_analyses = []
        original_store = storage.store_result

        def capture_store(search_name, execution_time, analysis):
            stored_analyses.append(analysis)
            return original_store(search_name, execution_time, analysis)

        storage.store_result = capture_store

        _handle_batch_result(result, storage)

        assert len(stored_analyses) == 1
        analysis = stored_analyses[0]
        assert abs(analysis.cost_cents - expected_batch_cost) < 0.001

    def test_batch_cost_discount_sonnet(self, storage, valid_response_json):
        """50% discount also applies to the more expensive Sonnet model."""
        from analyzers.batch_poller import _handle_batch_result

        custom_id = "custom-cost-sonnet"
        batch_id = "batch_cost_sonnet"

        storage.create_batch_request(
            custom_id=custom_id,
            batch_id=batch_id,
            search_name="cost_sonnet_test",
            model="claude-sonnet-4-6",
            system_prompt="Test",
            user_content="[]",
            search_metadata={},
            result_parquet_path="",
        )

        input_tokens = 2000
        output_tokens = 800

        sync_cost = _compute_cost_cents("claude-sonnet-4-6", input_tokens, output_tokens)
        expected_batch_cost = sync_cost * 0.5

        result = MagicMock()
        result.custom_id = custom_id
        result.result.type = "succeeded"

        message = MagicMock()
        content_block = MagicMock()
        content_block.text = valid_response_json
        message.content = [content_block]
        message.usage.input_tokens = input_tokens
        message.usage.output_tokens = output_tokens
        result.result.message = message

        stored_analyses = []
        original_store = storage.store_result

        def capture_store(search_name, execution_time, analysis):
            stored_analyses.append(analysis)
            return original_store(search_name, execution_time, analysis)

        storage.store_result = capture_store

        _handle_batch_result(result, storage)

        assert len(stored_analyses) == 1
        assert abs(stored_analyses[0].cost_cents - expected_batch_cost) < 0.001


# =====================================================================
# TestBatchStorage
# =====================================================================

class TestBatchStorage:
    """Tests for AnalyzerStorage batch request methods."""

    def test_create_and_get_request(self, storage):
        """create_batch_request then get_request returns the record."""
        storage.create_batch_request(
            custom_id="cid-001",
            batch_id="bid-001",
            search_name="test_search",
            model="claude-haiku-4-5-20251001",
            system_prompt="Analyze this.",
            user_content='[{"x":1}]',
            search_metadata={"name": "test"},
            result_parquet_path="/tmp/test.parquet",
            filter_enabled=True,
            filter_question="Is this real?",
        )

        req = storage.get_request("cid-001")
        assert req is not None
        assert req["custom_id"] == "cid-001"
        assert req["batch_id"] == "bid-001"
        assert req["search_name"] == "test_search"
        assert req["model"] == "claude-haiku-4-5-20251001"
        assert req["status"] == "submitted"
        assert req["filter_enabled"] == 1
        assert req["filter_question"] == "Is this real?"

    def test_get_pending_batch_ids(self, storage):
        """get_pending_batch_ids returns distinct batch IDs with status='submitted'."""
        storage.create_batch_request(
            custom_id="cid-a", batch_id="bid-A",
            search_name="s1", model="m", system_prompt="p",
            user_content="c", search_metadata={}, result_parquet_path="",
        )
        storage.create_batch_request(
            custom_id="cid-b", batch_id="bid-A",
            search_name="s2", model="m", system_prompt="p",
            user_content="c", search_metadata={}, result_parquet_path="",
        )
        storage.create_batch_request(
            custom_id="cid-c", batch_id="bid-B",
            search_name="s3", model="m", system_prompt="p",
            user_content="c", search_metadata={}, result_parquet_path="",
        )

        pending = storage.get_pending_batch_ids()
        assert set(pending) == {"bid-A", "bid-B"}

    def test_get_pending_excludes_completed(self, storage):
        """Completed requests should not appear in get_pending_batch_ids."""
        storage.create_batch_request(
            custom_id="cid-done", batch_id="bid-done",
            search_name="s", model="m", system_prompt="p",
            user_content="c", search_metadata={}, result_parquet_path="",
        )
        storage.mark_batch_completed("cid-done", "succeeded", '{"ok": true}')

        pending = storage.get_pending_batch_ids()
        assert "bid-done" not in pending

    def test_mark_batch_completed_updates_fields(self, storage):
        """mark_batch_completed sets status, completed_at, result_json."""
        storage.create_batch_request(
            custom_id="cid-mark", batch_id="bid-mark",
            search_name="s", model="m", system_prompt="p",
            user_content="c", search_metadata={}, result_parquet_path="",
        )

        storage.mark_batch_completed("cid-mark", "errored", "Some error detail")

        req = storage.get_request("cid-mark")
        assert req["status"] == "errored"
        assert req["completed_at"] != ""
        assert req["result_json"] == "Some error detail"

    def test_get_request_nonexistent_returns_none(self, storage):
        """get_request for an unknown custom_id returns None."""
        assert storage.get_request("does-not-exist") is None

    def test_get_pending_empty_db(self, storage):
        """get_pending_batch_ids on empty DB returns empty list."""
        assert storage.get_pending_batch_ids() == []
