"""
Tests for analyzers/llm_history_store.py + the slice-3 router cache
integration in analyzers/llm_router.py.

Covers:

  * Schema initialised on first connect; idempotent re-init
  * record_call round-trips (gz payloads decode back to originals)
  * retain_payloads=False stores metadata, leaves payload columns NULL
  * compute_content_hash determinism + sensitivity (every input field
    affects the hash)
  * get_cached_response: hit, miss, TTL window, errors not cacheable
  * Multiple-success cache hit returns the MOST RECENT success
  * list_calls filtering (model_id, provider, status, since_epoch)
  * stats aggregates match underlying rows
  * delete_older_than + vacuum
  * Singleton: get_store() reuses, reset_for_tests clears
  * Router integration:
    - call_llm records to history on success
    - call_llm records on error then re-raises
    - call_llm uses cache when use_cache=True (default) + hash matches
    - call_llm bypasses cache when use_cache=False
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from analyzers.llm_history_store import (
    LLMHistoryStore,
    compute_content_hash,
    get_store,
    reset_for_tests,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def tmp_history(tmp_path: Path) -> LLMHistoryStore:
    """Fresh history store at a tmp DB path."""
    return LLMHistoryStore(db_path=tmp_path / "test_llm_history.sqlite")


@pytest.fixture
def isolated_singleton(tmp_path: Path):
    """Point the module-level singleton at a tmp DB path; reset after."""
    reset_for_tests()
    store = get_store(db_path=tmp_path / "singleton.sqlite")
    yield store
    reset_for_tests()


# ── Schema + record_call round-trip ──────────────────────────────────

class TestRecordCall:
    def test_round_trip_with_payloads(self, tmp_history):
        rid = tmp_history.record_call(
            request_id="req-1",
            content_hash="hash-abc",
            model_id="claude-haiku",
            provider="anthropic",
            model_name="claude-haiku-4-5",
            source="unit_test",
            status="success",
            prompt="hello",
            system="be concise",
            response_text="hi back",
            raw_response={"id": "msg_x", "stop_reason": "end_turn"},
            input_tokens=10,
            output_tokens=3,
            cost_usd=0.000125,
            latency_ms=42,
            max_tokens=4096,
        )
        assert rid == "req-1"
        row = tmp_history.get_call("req-1")
        assert row is not None
        assert row["model_id"] == "claude-haiku"
        assert row["provider"] == "anthropic"
        assert row["status"] == "success"
        assert row["prompt"] == "hello"
        assert row["system"] == "be concise"
        assert row["response_text"] == "hi back"
        assert row["raw_response"] == {"id": "msg_x", "stop_reason": "end_turn"}
        assert row["input_tokens"] == 10
        assert row["output_tokens"] == 3
        assert row["cost_usd"] == pytest.approx(0.000125)

    def test_retain_payloads_false_keeps_metadata(self, tmp_history):
        tmp_history.record_call(
            request_id="req-2",
            content_hash="h",
            model_id="m", provider="anthropic", model_name="n",
            source="t", status="success",
            prompt="P", system="S", response_text="R",
            raw_response={"k": "v"},
            input_tokens=1, output_tokens=1, cost_usd=0.0,
            latency_ms=1, max_tokens=100,
            retain_payloads=False,
        )
        row = tmp_history.get_call("req-2")
        # Payloads gone
        assert row["prompt"] is None
        assert row["system"] is None
        assert row["response_text"] is None
        assert row["raw_response"] is None
        # Metadata kept
        assert row["model_id"] == "m"
        assert row["status"] == "success"
        assert row["input_tokens"] == 1

    def test_record_error_status(self, tmp_history):
        tmp_history.record_call(
            request_id="req-err",
            content_hash="h",
            model_id="m", provider="ollama", model_name="n",
            source="t", status="error",
            prompt="boom", system=None,
            response_text=None, raw_response=None,
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=10, max_tokens=100,
            error_class="HTTP500", error_message="server error",
        )
        row = tmp_history.get_call("req-err")
        assert row["status"] == "error"
        assert row["error_class"] == "HTTP500"
        assert row["error_message"] == "server error"


# ── Content hash ─────────────────────────────────────────────────────

class TestContentHash:
    BASE = dict(
        model_id="claude-haiku", model_name="claude-haiku-4-5",
        provider="anthropic", prompt="hello world",
        system="be concise", max_tokens=100,
    )

    def test_determinism(self):
        h1 = compute_content_hash(**self.BASE)
        h2 = compute_content_hash(**self.BASE)
        assert h1 == h2

    @pytest.mark.parametrize("field,changed_value", [
        ("model_id", "different-model"),
        ("model_name", "claude-haiku-4-7"),  # registry edit case
        ("provider", "ollama"),
        ("prompt", "hello universe"),
        ("system", "be verbose"),
        ("max_tokens", 200),
    ])
    def test_sensitive_to_each_field(self, field, changed_value):
        original = compute_content_hash(**self.BASE)
        modified_inputs = {**self.BASE, field: changed_value}
        modified = compute_content_hash(**modified_inputs)
        assert original != modified, f"hash unchanged when {field} changed"

    def test_system_none_vs_empty_string_collide(self):
        # Documented quirk: ``None`` and ``""`` produce the same hash
        # because the joiner uses ``system or ""``. Treat empty system
        # as no-system so the cache shouldn't get TWO entries for what
        # the model sees as the same prompt.
        h_none = compute_content_hash(**{**self.BASE, "system": None})
        h_empty = compute_content_hash(**{**self.BASE, "system": ""})
        assert h_none == h_empty


# ── Cache lookup ─────────────────────────────────────────────────────

class TestCacheLookup:
    def _record_success(self, store, *, content_hash, request_id="req-x", text="ok"):
        store.record_call(
            request_id=request_id, content_hash=content_hash,
            model_id="m", provider="anthropic", model_name="n",
            source="t", status="success",
            prompt="P", system="S",
            response_text=text, raw_response={"text": text},
            input_tokens=10, output_tokens=3, cost_usd=0.0001,
            latency_ms=42, max_tokens=100,
        )

    def test_cache_hit(self, tmp_history):
        self._record_success(tmp_history, content_hash="h1", text="cached!")
        hit = tmp_history.get_cached_response("h1")
        assert hit is not None
        assert hit["response_text"] == "cached!"
        assert hit["status"] == "success"

    def test_cache_miss(self, tmp_history):
        # Empty DB → miss
        assert tmp_history.get_cached_response("nope") is None

    def test_error_rows_not_cacheable(self, tmp_history):
        tmp_history.record_call(
            request_id="req-err", content_hash="h-err",
            model_id="m", provider="anthropic", model_name="n",
            source="t", status="error",
            prompt="P", system=None, response_text=None,
            raw_response=None,
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=1, max_tokens=100,
            error_class="X",
        )
        assert tmp_history.get_cached_response("h-err") is None

    def test_cache_returns_most_recent_success(self, tmp_history):
        # Two successes with the same content_hash; cache returns the
        # newer one. We seed older row first via a small timestamp gap.
        self._record_success(tmp_history, content_hash="h", request_id="old", text="old")
        time.sleep(1.05)  # ensure triggered_at_epoch differs
        self._record_success(tmp_history, content_hash="h", request_id="new", text="new")
        hit = tmp_history.get_cached_response("h")
        assert hit["response_text"] == "new"

    def test_ttl_excludes_old(self, tmp_history):
        self._record_success(tmp_history, content_hash="h", text="ancient")
        # Sleep 2.05s instead of 1.05s - `int(time.time())` truncates so a
        # 1-second window is non-deterministically 1 OR 2 integer seconds
        # depending on where in the second the insert + query land. 2.05s
        # of elapsed wall-clock guarantees at least 2 integer ticks
        # crossed, which max_age=1 reliably excludes.
        time.sleep(2.05)
        # max_age=1 second → ancient row falls outside the window
        assert tmp_history.get_cached_response("h", max_age_seconds=1) is None
        # max_age=None or large → ancient row qualifies
        assert tmp_history.get_cached_response("h") is not None
        assert tmp_history.get_cached_response("h", max_age_seconds=3600) is not None


# ── List + stats + retention ─────────────────────────────────────────

class TestListAndStats:
    def _seed(self, store):
        for i in range(3):
            store.record_call(
                request_id=f"req-{i}", content_hash=f"h{i}",
                model_id="claude-haiku", provider="anthropic",
                model_name="claude-haiku-4-5",
                source="t", status="success",
                prompt=f"prompt-{i}", system=None,
                response_text=f"resp-{i}", raw_response=None,
                input_tokens=10, output_tokens=3, cost_usd=0.0001,
                latency_ms=42, max_tokens=100,
            )
        store.record_call(
            request_id="req-other", content_hash="h-o",
            model_id="lmstudio-remote", provider="lmstudio",
            model_name="local-model",
            source="t", status="success",
            prompt="hi", system=None,
            response_text="ok", raw_response=None,
            input_tokens=5, output_tokens=1, cost_usd=0.0,
            latency_ms=20, max_tokens=100,
        )

    def test_list_filter_by_provider(self, tmp_history):
        self._seed(tmp_history)
        anth = tmp_history.list_calls(provider="anthropic")
        assert len(anth) == 3
        lms = tmp_history.list_calls(provider="lmstudio")
        assert len(lms) == 1
        assert lms[0]["model_id"] == "lmstudio-remote"

    def test_list_filter_by_model_id(self, tmp_history):
        self._seed(tmp_history)
        rows = tmp_history.list_calls(model_id="claude-haiku")
        assert len(rows) == 3

    def test_stats_aggregates(self, tmp_history):
        self._seed(tmp_history)
        s = tmp_history.stats()
        assert s["total"] == 4
        assert s["successes"] == 4
        assert s["errors"] == 0
        # 3 anthropic × 0.0001 + 1 lmstudio × 0.0 = 0.0003
        assert s["total_cost_usd"] == pytest.approx(0.0003)

    def test_delete_older_than(self, tmp_history):
        self._seed(tmp_history)
        future = int(time.time()) + 9999
        deleted = tmp_history.delete_older_than(future)
        assert deleted == 4
        assert tmp_history.stats()["total"] == 0


# ── Singleton ────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_store_returns_same_instance(self, isolated_singleton):
        a = get_store()
        b = get_store()
        assert a is b is isolated_singleton

    def test_reset_clears(self, tmp_path):
        reset_for_tests()
        a = get_store(db_path=tmp_path / "a.sqlite")
        reset_for_tests()
        b = get_store(db_path=tmp_path / "b.sqlite")
        assert a is not b
        reset_for_tests()


# ── Router integration ──────────────────────────────────────────────

class TestRouterIntegration:
    """call_llm should record every dispatch (success or error) to
    history, AND short-circuit on cache hits when use_cache=True.
    """

    @pytest.fixture
    def isolated_router_state(self, tmp_path, monkeypatch):
        """Point both the model_store AND llm_history_store at tmp DBs
        so the test's calls don't pollute production state.
        """
        import model_store
        import analyzers.llm_history_store as hist
        from analyzers import llm_router
        model_store.reset_for_tests()
        hist.reset_for_tests()
        monkeypatch.setattr(model_store, "MODELS_DIR", tmp_path / "models")
        # The history store reads DEFAULT_DB_PATH on first get_store(),
        # so we patch it to a tmp path before the router calls it.
        monkeypatch.setattr(
            hist, "DEFAULT_DB_PATH", tmp_path / "history.sqlite",
        )
        store = model_store.get_store()
        llm_router._invalidate_api_key_cache()
        yield store
        model_store.reset_for_tests()
        hist.reset_for_tests()
        llm_router._invalidate_api_key_cache()

    def test_success_recorded_to_history(self, isolated_router_state):
        from analyzers.llm_router import call_llm
        from analyzers.llm_history_store import get_store as hist_store
        # Stub claude_client so we don't hit the real API
        result = MagicMock()
        result.response = MagicMock(content=[MagicMock(text="hi")])
        result.input_tokens = 5
        result.output_tokens = 2
        result.cost_usd = 0.000035
        result.latency_ms = 12
        result.request_id = "req-success"
        with patch(
            "analyzers.claude_client.call_messages_create", return_value=result,
        ):
            response = call_llm(
                "claude-haiku-4-5-20251001",
                prompt="ping", use_cache=False,
            )
        assert response.text == "hi"

        # History row exists
        rows = hist_store().list_calls(model_id="claude-haiku-4-5-20251001")
        assert len(rows) == 1
        assert rows[0]["status"] == "success"
        assert rows[0]["prompt"] == "ping"
        assert rows[0]["response_text"] == "hi"

    def test_error_recorded_then_reraised(self, isolated_router_state):
        from analyzers.llm_router import call_llm, LLMRouterError
        from analyzers.claude_client import ClaudeCallError
        from analyzers.llm_history_store import get_store as hist_store
        boom = ClaudeCallError(
            "rate limit", request_id="req-err",
            error_class="RateLimitError",
        )
        with patch(
            "analyzers.claude_client.call_messages_create", side_effect=boom,
        ):
            with pytest.raises(LLMRouterError):
                call_llm(
                    "claude-haiku-4-5-20251001",
                    prompt="explode", use_cache=False,
                )
        rows = hist_store().list_calls(status="error")
        assert len(rows) == 1
        assert rows[0]["error_class"] == "RateLimitError"
        assert rows[0]["prompt"] == "explode"

    def test_cache_hit_short_circuits_dispatch(self, isolated_router_state):
        from analyzers.llm_router import call_llm
        from analyzers.llm_history_store import (
            compute_content_hash, get_store as hist_store,
        )
        # Pre-seed a successful row matching the call we're about to make
        h = compute_content_hash(
            model_id="claude-haiku-4-5-20251001",
            model_name="claude-haiku-4-5-20251001",
            provider="anthropic",
            prompt="cached",
            system=None,
            max_tokens=4096,  # default for haiku per registry
        )
        hist_store().record_call(
            request_id="seeded", content_hash=h,
            model_id="claude-haiku-4-5-20251001", provider="anthropic",
            model_name="claude-haiku-4-5-20251001",
            source="seed", status="success",
            prompt="cached", system=None,
            response_text="cached response",
            raw_response={"foo": "bar"},
            input_tokens=10, output_tokens=4, cost_usd=0.0001,
            latency_ms=42, max_tokens=4096,
        )

        # Now call call_llm WITHOUT mocking the dispatch path. Cache hit
        # MUST short-circuit before any provider call. We patch the
        # claude_client to fail loudly if it gets called.
        with patch(
            "analyzers.claude_client.call_messages_create",
            side_effect=AssertionError(
                "cache hit MUST short-circuit before provider dispatch"
            ),
        ):
            response = call_llm(
                "claude-haiku-4-5-20251001",
                prompt="cached", use_cache=True,
            )
        # Cache hit → response is reconstructed from history
        assert response.text == "cached response"
        # cost_usd=0 + latency_ms=0 are the cache-hit signature
        assert response.cost_usd == 0.0
        assert response.latency_ms == 0

    def test_use_cache_false_bypasses_cache(self, isolated_router_state):
        from analyzers.llm_router import call_llm
        from analyzers.llm_history_store import (
            compute_content_hash, get_store as hist_store,
        )
        # Same setup as above but with use_cache=False
        h = compute_content_hash(
            model_id="claude-haiku-4-5-20251001",
            model_name="claude-haiku-4-5-20251001",
            provider="anthropic",
            prompt="cached", system=None, max_tokens=4096,
        )
        hist_store().record_call(
            request_id="seeded", content_hash=h,
            model_id="claude-haiku-4-5-20251001", provider="anthropic",
            model_name="claude-haiku-4-5-20251001",
            source="seed", status="success",
            prompt="cached", system=None,
            response_text="cached response", raw_response=None,
            input_tokens=10, output_tokens=4, cost_usd=0.0001,
            latency_ms=42, max_tokens=4096,
        )

        # Stub the provider; call should reach it because use_cache=False
        result = MagicMock()
        result.response = MagicMock(content=[MagicMock(text="fresh")])
        result.input_tokens = 5
        result.output_tokens = 2
        result.cost_usd = 0.000035
        result.latency_ms = 12
        result.request_id = "req-fresh"
        with patch(
            "analyzers.claude_client.call_messages_create", return_value=result,
        ) as mock_call:
            response = call_llm(
                "claude-haiku-4-5-20251001",
                prompt="cached", use_cache=False,
            )
            mock_call.assert_called_once()
        assert response.text == "fresh"
