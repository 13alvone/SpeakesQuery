"""
Tests for analyzers/llm_router.py - Phase 2 / Bet 3 slice 2 + 2.5.

Covers the implemented transports + the gemini stub + the dispatch
shell:

  * Unknown model_id → LLMRouterError(UnknownModel)
  * Empty / non-string prompt → LLMRouterError(InvalidPrompt)
  * Anthropic path delegates to claude_client.call_messages_create and
    adapts the ClaudeCallResult into an LLMResponse
  * LM Studio uses `_call_chat_completions` - verified by asserting
    the URL is built from the record's endpoint and the Authorization
    header carries the vault key when configured
  * LM Studio works with NO API key (Authorization header absent)
  * Ollama uses /api/chat with prompt_eval_count / eval_count token
    accounting
  * Gemini stub raises ProviderNotImplemented
  * Cost computed from registry pricing (cloud non-zero, local zero)
  * Per-record max_output_tokens / default_timeout_seconds defaults
    are picked up when caller omits them
  * HTTP errors / network errors / non-JSON responses surface as
    LLMRouterError with the right error_class

Tests use mocks - they don't hit a real API. The router's surface area
is small enough that mocking ``requests.post`` + ``call_messages_create``
covers the dispatch logic completely.

Slice 2.5 (2026-05-08): OpenAI removed from the supported provider
set per user direction. The ``_call_chat_completions`` transport stays
(LM Studio uses it; future self-hosted backends like vLLM, llama.cpp
server can use it too) but no provider entry points at OpenAI's cloud.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from analyzers import llm_router
from analyzers.llm_router import (
    LLMResponse,
    LLMRouterError,
    call_llm,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Point the model_store + llm_history_store at tmp paths + seed
    the default models so call_llm can resolve them. Slice 3 added
    automatic history capture inside call_llm - tests that mock
    transports MUST isolate the history store too, otherwise:
      (a) successful calls from one test pollute the cache and earn
          a cache HIT in a later test (bypassing the dispatch the
          later test is trying to verify);
      (b) the production llm_call_history.sqlite at project root gets
          spuriously written during the test run.
    Resets all three singletons on teardown.
    """
    import model_store
    import analyzers.llm_history_store as hist
    model_store.reset_for_tests()
    hist.reset_for_tests()
    monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(
        hist, "DEFAULT_DB_PATH", tmp_path / "llm_call_history.sqlite",
    )
    store = model_store.get_store()
    llm_router._invalidate_api_key_cache()
    yield store
    model_store.reset_for_tests()
    hist.reset_for_tests()
    llm_router._invalidate_api_key_cache()


def _stub_anthropic_response(text: str = "anthropic reply"):
    """Build a minimal stand-in for ClaudeCallResult."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    result = MagicMock()
    result.response = msg
    result.input_tokens = 10
    result.output_tokens = 5
    result.cost_usd = 0.000125
    result.latency_ms = 42
    result.request_id = "req-anthropic-test"
    return result


def _chat_completions_payload(
    *, text: str = "ok", prompt_tokens: int = 12, completion_tokens: int = 3,
) -> dict:
    """Stand-in payload in the Chat Completions response shape (the
    de-facto self-hosted-LLM JSON wire format used by LM Studio, vLLM,
    llama.cpp server, etc.).
    """
    return {
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _ollama_chat_payload(
    *, text: str = "local reply",
    prompt_eval_count: int = 8, eval_count: int = 2,
) -> dict:
    return {
        "model": "llama3.1:8b",
        "message": {"role": "assistant", "content": text},
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
        "done": True,
    }


# ── Dispatch shell ───────────────────────────────────────────────────

class TestDispatchShell:
    def test_unknown_model_raises(self, isolated_registry):
        with pytest.raises(LLMRouterError) as exc_info:
            call_llm("does-not-exist", prompt="hi")
        assert exc_info.value.error_class == "UnknownModel"
        assert exc_info.value.model_id == "does-not-exist"

    def test_empty_prompt_raises(self, isolated_registry):
        with pytest.raises(LLMRouterError) as exc_info:
            call_llm("claude-sonnet-4-6", prompt="")
        assert exc_info.value.error_class == "InvalidPrompt"

    def test_non_string_prompt_raises(self, isolated_registry):
        with pytest.raises(LLMRouterError):
            call_llm("claude-sonnet-4-6", prompt=None)  # type: ignore[arg-type]


# ── Anthropic path ───────────────────────────────────────────────────

class TestAnthropicTransport:
    def test_routes_through_claude_client(self, isolated_registry):
        with patch(
            "analyzers.claude_client.call_messages_create",
            return_value=_stub_anthropic_response(text="hello world"),
        ) as mock_call:
            response = call_llm(
                "claude-sonnet-4-6",
                prompt="rate this 1-10",
                system="You are concise.",
            )
            mock_call.assert_called_once()
            kwargs = mock_call.call_args.kwargs
            assert kwargs["model"] == "claude-sonnet-4-6"
            # System prompt threaded through
            assert kwargs["system"] == "You are concise."
            # User prompt as the only message
            assert kwargs["messages"] == [
                {"role": "user", "content": "rate this 1-10"}
            ]

        assert isinstance(response, LLMResponse)
        assert response.text == "hello world"
        assert response.provider == "anthropic"
        assert response.model_id == "claude-sonnet-4-6"
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.cost_usd == pytest.approx(0.000125)
        assert response.latency_ms == 42

    def test_per_record_max_tokens_used_when_caller_omits(self, isolated_registry):
        # claude-sonnet-4-6's record has max_output_tokens=8192
        with patch(
            "analyzers.claude_client.call_messages_create",
            return_value=_stub_anthropic_response(),
        ) as mock_call:
            call_llm("claude-sonnet-4-6", prompt="x")
            assert mock_call.call_args.kwargs["max_tokens"] == 8192

    def test_caller_max_tokens_overrides_record(self, isolated_registry):
        with patch(
            "analyzers.claude_client.call_messages_create",
            return_value=_stub_anthropic_response(),
        ) as mock_call:
            call_llm("claude-sonnet-4-6", prompt="x", max_tokens=128)
            assert mock_call.call_args.kwargs["max_tokens"] == 128

    def test_claude_call_error_re_raised_as_router_error(self, isolated_registry):
        from analyzers.claude_client import ClaudeCallError
        boom = ClaudeCallError(
            "rate limited", request_id="req-x",
            error_class="RateLimitError",
        )
        with patch(
            "analyzers.claude_client.call_messages_create", side_effect=boom,
        ):
            with pytest.raises(LLMRouterError) as exc_info:
                call_llm("claude-sonnet-4-6", prompt="x")
            assert exc_info.value.provider == "anthropic"
            assert exc_info.value.error_class == "RateLimitError"


# ── Chat Completions transport (LM Studio) ─────────────────────────

class TestChatCompletionsTransport:
    """LM Studio is the only currently-supported caller of the
    ``_call_chat_completions`` transport. Slice 2.5 removed OpenAI.
    Future similar self-hosted backends (vLLM, llama.cpp server) would
    add their own provider and route through the same transport.
    """

    def test_lmstudio_posts_to_record_endpoint(self, isolated_registry):
        # The seeded lmstudio-remote has endpoint http://localhost:1234/v1
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_chat_completions_payload()),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ) as mock_post:
            call_llm("lmstudio-remote", prompt="test")
            args, kwargs = mock_post.call_args
            assert args[0] == "http://localhost:1234/v1/chat/completions"
            assert kwargs["json"]["model"] == "local-model"
            assert kwargs["json"]["messages"] == [
                {"role": "user", "content": "test"}
            ]
            # No Authorization header without LMSTUDIO_API_KEY
            assert "Authorization" not in (kwargs["headers"] or {})

    def test_lmstudio_forwards_record_sampling_into_payload(self, isolated_registry):
        # A per-record sampling block must reach the Chat Completions
        # payload verbatim. This is the 2026-06-07 fix that stops the
        # Qwen3.5-122B <think> trace from looping past max_tokens and
        # returning empty content - presence_penalty is load-bearing.
        isolated_registry.save_model({
            "id": "big-local", "provider": "lmstudio",
            "model_name": "Qwen3.5-122B-A10B",
            "endpoint": "http://llama-host:8085/v1",
            "sampling": {"presence_penalty": 1.5, "temperature": 1.0,
                         "top_p": 0.95, "top_k": 20, "min_p": 0},
        })
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_chat_completions_payload()),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ) as mock_post:
            call_llm("big-local", prompt="label these")
            sent = mock_post.call_args.kwargs["json"]
            assert sent["presence_penalty"] == 1.5
            assert sent["temperature"] == 1.0
            assert sent["top_p"] == 0.95
            assert sent["top_k"] == 20
            assert sent["min_p"] == 0
            # Base fields still intact.
            assert sent["model"] == "Qwen3.5-122B-A10B"
            assert sent["max_tokens"] >= 4096

    def test_lmstudio_no_sampling_keeps_payload_minimal(self, isolated_registry):
        # The seeded lmstudio-remote has no sampling block → payload must
        # stay exactly {model, messages, max_tokens} (server defaults
        # apply, unchanged from before the field existed).
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_chat_completions_payload()),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ) as mock_post:
            call_llm("lmstudio-remote", prompt="test")
            sent = mock_post.call_args.kwargs["json"]
            assert set(sent.keys()) == {"model", "messages", "max_tokens"}

    def test_lmstudio_uses_api_key_when_vault_has_one(self, isolated_registry):
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_chat_completions_payload()),
        )
        with patch.object(
            llm_router, "_get_provider_api_key",
            return_value="lms-secret-token",
        ), patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ) as mock_post:
            call_llm("lmstudio-remote", prompt="test")
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer lms-secret-token"

    def test_response_normalised_to_uniform_shape(self, isolated_registry):
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_chat_completions_payload(
                text="answer", prompt_tokens=42, completion_tokens=17,
            )),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ):
            response = call_llm("lmstudio-remote", prompt="x")
        # Token-name remap: prompt_tokens → input_tokens
        assert response.input_tokens == 42
        assert response.output_tokens == 17
        assert response.text == "answer"
        # Cost = 0 for LM Studio (free local)
        assert response.cost_usd == 0.0

    def test_http_error_surfaces_as_router_error(self, isolated_registry):
        mock_resp = MagicMock(status_code=500, text="internal error")
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ):
            with pytest.raises(LLMRouterError) as exc_info:
                call_llm("lmstudio-remote", prompt="x")
            assert exc_info.value.error_class == "HTTP500"

    def test_network_error_surfaces_as_router_error(self, isolated_registry):
        with patch.object(
            llm_router.requests, "post",
            side_effect=requests.ConnectionError("conn refused"),
        ):
            with pytest.raises(LLMRouterError) as exc_info:
                call_llm("lmstudio-remote", prompt="x")
            assert exc_info.value.error_class == "ConnectionError"

    def test_non_json_response_surfaces_as_router_error(self, isolated_registry):
        mock_resp = MagicMock(
            status_code=200,
            text="not json",
            json=MagicMock(side_effect=ValueError("bad json")),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ):
            with pytest.raises(LLMRouterError) as exc_info:
                call_llm("lmstudio-remote", prompt="x")
            assert exc_info.value.error_class == "DecodeError"


# ── Ollama transport ─────────────────────────────────────────────────

class TestOllamaTransport:
    def test_posts_to_api_chat_endpoint(self, isolated_registry):
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_ollama_chat_payload()),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ) as mock_post:
            call_llm("ollama-llama3-1-8b", prompt="hey")
            args, kwargs = mock_post.call_args
            # Endpoint http://localhost:11434 + /api/chat
            assert args[0] == "http://localhost:11434/api/chat"
            assert kwargs["json"]["stream"] is False
            assert kwargs["json"]["model"] == "llama3.1:8b"

    def test_token_accounting_uses_ollama_field_names(self, isolated_registry):
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_ollama_chat_payload(
                prompt_eval_count=99, eval_count=33,
            )),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ):
            response = call_llm("ollama-llama3-1-8b", prompt="x")
        # Token-name remap: prompt_eval_count → input_tokens
        assert response.input_tokens == 99
        assert response.output_tokens == 33
        assert response.cost_usd == 0.0  # local, free

    def test_no_authorization_header_sent(self, isolated_registry):
        # Ollama doesn't auth. Even if a key were in the vault, the
        # router shouldn't send Authorization for Ollama.
        mock_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value=_ollama_chat_payload()),
        )
        with patch.object(
            llm_router.requests, "post", return_value=mock_resp,
        ) as mock_post:
            call_llm("ollama-llama3-1-8b", prompt="x")
        # The Ollama transport doesn't set headers explicitly; default
        # requests.post call should not pass Authorization.
        kwargs = mock_post.call_args.kwargs
        if "headers" in kwargs and kwargs["headers"]:
            assert "Authorization" not in kwargs["headers"]


# ── Gemini stub ──────────────────────────────────────────────────────

class TestGeminiStub:
    def test_gemini_raises_not_implemented(self, isolated_registry):
        # Build a custom gemini record (no default shipped)
        isolated_registry.save_model({
            "id": "gemini-test",
            "provider": "gemini",
            "model_name": "gemini-1.5-pro",
        })
        with pytest.raises(LLMRouterError) as exc_info:
            call_llm("gemini-test", prompt="x")
        assert exc_info.value.error_class == "ProviderNotImplemented"
        assert exc_info.value.provider == "gemini"


# ── Cost computation ─────────────────────────────────────────────────

class TestCostComputation:
    def test_compute_cost_cloud(self):
        # Sonnet pricing: $3/Mtok in, $15/Mtok out
        record = {
            "cost_per_input_million_usd": 3.0,
            "cost_per_output_million_usd": 15.0,
        }
        # 1000 input tokens × $3/Mtok = $0.003
        # 500 output tokens × $15/Mtok = $0.0075
        # Total: $0.0105
        assert llm_router._compute_cost(record, 1000, 500) == pytest.approx(0.0105)

    def test_compute_cost_local_is_zero(self):
        record = {
            "cost_per_input_million_usd": 0.0,
            "cost_per_output_million_usd": 0.0,
        }
        assert llm_router._compute_cost(record, 1_000_000, 1_000_000) == 0.0

    def test_compute_cost_floors_at_zero_on_negative_pricing(self):
        # Defensive: a misconfigured registry could land negative pricing.
        # Cost must NEVER be negative (would silently CREDIT a budget).
        record = {
            "cost_per_input_million_usd": -3.0,
            "cost_per_output_million_usd": 15.0,
            "id": "bad-pricing",
        }
        assert llm_router._compute_cost(record, 1000, 0) == 0.0


# ── API key lookup ──────────────────────────────────────────────────

class TestApiKeyCache:
    def test_empty_key_name_returns_empty(self):
        assert llm_router._get_provider_api_key("") == ""

    def test_cache_invalidate_clears_all(self):
        llm_router._api_key_cache["FOO"] = ("v1", 999999.0)
        llm_router._invalidate_api_key_cache()
        assert "FOO" not in llm_router._api_key_cache

    def test_cache_invalidate_clears_one(self):
        import time as _t
        now = _t.monotonic()
        llm_router._api_key_cache["FOO"] = ("vF", now)
        llm_router._api_key_cache["BAR"] = ("vB", now)
        llm_router._invalidate_api_key_cache("FOO")
        assert "FOO" not in llm_router._api_key_cache
        assert "BAR" in llm_router._api_key_cache
        llm_router._invalidate_api_key_cache()
