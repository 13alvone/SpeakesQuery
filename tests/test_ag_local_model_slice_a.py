#!/usr/bin/env python3
"""
Slice A (2026-06-23): Alert groups can route their analysis call through
the provider-agnostic LLM router to a local/registry model (e.g.
``llamacpp-qwen35-122b-a10b`` on the LAN, $0/token) instead of the Claude
API. This is the reusable unlock under two new alert groups (AI paper
diffs + hot GitHub repos) that must run entirely on the LAN with zero
cloud dependency.

The contract under test:

1. **Field + validation** - an optional ``model_id`` field round-trips
   through ``AlertGroupStore``; ``validate_model_id`` accepts the
   empty/Claude default, rejects a known-unknown registry id, and
   tolerates a missing registry.
2. **Router helper** - ``_call_router_llm`` normalises an ``LLMResponse``
   into the same ``(call, response_text, response_meta)`` shape the Claude
   path produces, dispatches with cache OFF + ``source="alert_group"`` +
   NO web_search tool, and propagates router errors.
3. **Money-leak canary** - when ``model_id`` is set the dispatcher MUST
   NOT call the Claude API (zero invocations) and MUST call the router.
   The reverse canary proves the Claude path is unchanged when ``model_id``
   is absent.
4. **Empty-response guard** - a local model that returns empty text (the
   122B thinking-loop failure mode) fails the dispatch loud instead of
   emailing a blank brief.
5. **Budget short-circuit** - the per-AG dollar budget is a no-op for a
   $0 local model.
6. **Source drift guard** - the routing branch + helper stay wired.

Mirrors the money-leak audit pattern in
``tests/test_ag_disabled_money_leak_audit.py``: patch the billable client
to raise, run the path, assert zero invocations.
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from validation.AlertGroupValidation import AlertGroupValidation
from alert_group_store import AlertGroupStore
from alert_groups.dispatcher import AlertGroupDispatcher

# The shipped "big local" default. Used wherever we want a real registry id.
LOCAL_MODEL_ID = "llamacpp-qwen35-122b-a10b"


# ===========================================================================
# Factories
# ===========================================================================


def _make_llm_response(text="ELI5 summary of today's AI papers.",
                       finish_reason="stop"):
    from analyzers.llm_router import LLMResponse
    return LLMResponse(
        text=text,
        model_id=LOCAL_MODEL_ID,
        provider="lmstudio",
        model_name="Qwen3.5-122B-A10B",
        input_tokens=120,
        output_tokens=240,
        cost_usd=0.0,
        latency_ms=4200,
        request_id="rid-local-1",
        raw_response={"choices": [{"finish_reason": finish_reason}]},
    )


def _make_claude_result(text="Claude analyst brief."):
    from analyzers.claude_client import ClaudeCallResult
    response = SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        stop_reason="end_turn",
    )
    return ClaudeCallResult(
        response=response,
        request_id="rid-claude-1",
        model="claude-sonnet-4-6",
        input_tokens=120,
        output_tokens=240,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0123,
        latency_ms=3000,
        attempts=1,
    )


def _feeder_df():
    now = int(time.time())
    return pd.DataFrame({
        "title": ["Paper A: attention redux", "Paper B: tiny MoE"],
        "url": ["http://x/a", "http://x/b"],
        "_epoch": [now, now],
    })


def _local_ag(name="ai_paper_diffs_test"):
    return {
        "name": name,
        "description": "AI paper diffs via local model",
        "search_names": ["papers_feeder"],
        "prompt_text": "ELI5 what changed in these papers today.",
        "schedule": "0 9 * * *",
        "max_rows": 50,
        "email_address": "ops@example.com",
        "disabled": False,
        "model_id": LOCAL_MODEL_ID,
    }


def _email_patches():
    """Patch every email egress so no canary touches SMTP."""
    return (
        patch.object(AlertGroupDispatcher, "_send_html_email", MagicMock()),
        patch.object(AlertGroupDispatcher, "_send_plain_email", MagicMock()),
        patch.object(AlertGroupDispatcher, "_maybe_send_failure_email",
                     MagicMock()),
    )


# ===========================================================================
# 1 - Field validation
# ===========================================================================


class TestValidateModelId:

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_variants_return_empty(self, value):
        assert AlertGroupValidation.validate_model_id(value) == ""

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_model_id(123)

    def test_real_122b_model_id_accepted(self):
        """The shipped big-local default must be registered - this doubles
        as a check that ``default_models`` seeds it into the registry."""
        assert (
            AlertGroupValidation.validate_model_id(LOCAL_MODEL_ID)
            == LOCAL_MODEL_ID
        )

    def test_unknown_model_id_rejected_when_registry_present(self):
        fake_store = MagicMock()
        fake_store.get_model.return_value = None
        with patch("model_store.get_store", return_value=fake_store):
            with pytest.raises(ValueError):
                AlertGroupValidation.validate_model_id("totally-not-a-model")

    def test_registry_unavailable_accepts_shape(self):
        with patch("model_store.get_store",
                   side_effect=RuntimeError("no registry")):
            assert (
                AlertGroupValidation.validate_model_id("some-id") == "some-id"
            )


# ===========================================================================
# 2 - Router helper normalization
# ===========================================================================


class TestCallRouterLLM:

    def test_normalizes_success(self):
        d = AlertGroupDispatcher()
        with patch("analyzers.llm_router.call_llm",
                   return_value=_make_llm_response()):
            call, text, meta = d._call_router_llm(
                group_name="t", model_id=LOCAL_MODEL_ID,
                user_content="hello", max_tokens=500,
            )
        assert text == "ELI5 summary of today's AI papers."
        assert call.input_tokens == 120
        assert call.output_tokens == 240
        assert call.cost_usd == 0.0
        assert call.request_id == "rid-local-1"
        assert call.attempts == 1
        # email path reads call.response.stop_reason - must not crash
        assert call.response.stop_reason == "stop"
        assert meta["block_count"] == 1
        assert meta["stop_reason"] == "stop"

    def test_cache_off_source_tagged_no_tools(self):
        d = AlertGroupDispatcher()
        with patch("analyzers.llm_router.call_llm",
                   return_value=_make_llm_response()) as cl:
            d._call_router_llm(group_name="t", model_id=LOCAL_MODEL_ID,
                               user_content="hi", max_tokens=500)
        _args, kwargs = cl.call_args
        assert kwargs["use_cache"] is False
        assert kwargs["source"] == "alert_group"
        # A single-shot completion can't use Anthropic's server-side tool.
        assert "tools" not in kwargs

    def test_empty_text_yields_empty_meta(self):
        d = AlertGroupDispatcher()
        with patch("analyzers.llm_router.call_llm",
                   return_value=_make_llm_response(text="")):
            _call, text, meta = d._call_router_llm(
                group_name="t", model_id=LOCAL_MODEL_ID,
                user_content="hi", max_tokens=500)
        assert text == ""
        assert meta["block_count"] == 0
        assert meta["block_types"] == []

    def test_router_error_propagates(self):
        from analyzers.llm_router import LLMRouterError
        d = AlertGroupDispatcher()
        with patch("analyzers.llm_router.call_llm",
                   side_effect=LLMRouterError("boom", model_id=LOCAL_MODEL_ID)):
            with pytest.raises(LLMRouterError):
                d._call_router_llm(group_name="t", model_id=LOCAL_MODEL_ID,
                                   user_content="hi", max_tokens=500)


# ===========================================================================
# 3 - Money-leak canary (full run())
# ===========================================================================


class TestLocalModelMoneyLeakCanary:

    def test_model_id_set_never_calls_claude(self):
        claude_calls = {"n": 0}

        def _fail_loud(*_a, **_k):
            claude_calls["n"] += 1
            raise AssertionError(
                "MONEY LEAK: dispatcher called Claude for a local-model AG"
            )

        e_html, e_plain, e_fail = _email_patches()
        with patch("alert_groups.dispatcher.call_messages_create",
                   _fail_loud), \
             patch("analyzers.llm_router.call_llm",
                   return_value=_make_llm_response()) as cl, \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             e_html, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(_local_ag(), force=True)

        assert claude_calls["n"] == 0, (
            "MONEY LEAK: Claude was invoked for a local-model AG"
        )
        assert cl.call_count == 1, (
            "router was not called - the local branch never executed"
        )
        assert result.status == "success", (
            f"unexpected status: {result.status} / {result.error_message}"
        )
        assert result.cost_usd == 0.0

    def test_no_model_id_uses_claude_not_router(self):
        router_calls = {"n": 0}

        def _fail_loud(*_a, **_k):
            router_calls["n"] += 1
            raise AssertionError("router called for a Claude AG (model_id unset)")

        group = _local_ag(name="claude_path_test")
        group.pop("model_id")
        e_html, e_plain, e_fail = _email_patches()
        with patch("analyzers.llm_router.call_llm", _fail_loud), \
             patch("alert_groups.dispatcher.call_messages_create",
                   return_value=_make_claude_result()) as cc, \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             e_html, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(group, force=True)

        assert router_calls["n"] == 0
        assert cc.call_count == 1
        assert result.status == "success", (
            f"{result.status} / {result.error_message}"
        )

    def test_local_empty_response_fails_loud(self):
        """The 122B thinking-loop empty-content failure must error the
        dispatch, not email a blank brief."""
        e_html, e_plain, e_fail = _email_patches()
        with patch("alert_groups.dispatcher.call_messages_create",
                   MagicMock()), \
             patch("analyzers.llm_router.call_llm",
                   return_value=_make_llm_response(text="")), \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             patch.object(AlertGroupDispatcher, "_maybe_trip_circuit_breaker",
                          MagicMock()), \
             e_html, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(_local_ag(name="empty_resp_test"), force=True)
        assert result.status == "error"
        assert "no text" in (result.error_message or "").lower()


# ===========================================================================
# 4 - Budget short-circuit
# ===========================================================================


class TestBudgetShortCircuit:

    def test_local_model_bypasses_dollar_budget(self):
        # 1M tokens would massively exceed a $0.0001 cap on Claude pricing -
        # but a local model is genuinely $0, so the gate must return None.
        group = {"model_id": LOCAL_MODEL_ID, "max_cost_usd_per_run": 0.0001}
        assert (
            AlertGroupDispatcher._check_per_ag_budget(group, "g", 1_000_000)
            is None
        )

    def test_claude_model_still_gated(self):
        group = {"max_cost_usd_per_run": 0.0001}
        err = AlertGroupDispatcher._check_per_ag_budget(group, "g", 1_000_000)
        assert err is not None and "exceeds per-run cap" in err


# ===========================================================================
# 5 - Store round-trip
# ===========================================================================


@pytest.fixture
def isolated_store(tmp_path):
    empty_defaults = tmp_path / "_empty_default_alert_groups"
    empty_defaults.mkdir()
    store = AlertGroupStore()
    store._dir = tmp_path / "alert_groups"
    store._defaults_dir = empty_defaults
    store._db = str(tmp_path / "last_chance.sqlite")
    store._runs_db = str(tmp_path / "alert_group_runs.sqlite")
    store.initialize()
    return store


class TestModelIdPersistence:

    def test_model_id_round_trips_on_save(self, isolated_store):
        isolated_store.save_group(_local_ag(name="persist_test"))
        loaded = isolated_store.get_group("persist_test")
        assert loaded["model_id"] == LOCAL_MODEL_ID

    def test_absent_model_id_defaults_empty(self, isolated_store):
        group = _local_ag(name="no_model_test")
        group.pop("model_id")
        isolated_store.save_group(group)
        assert isolated_store.get_group("no_model_test")["model_id"] == ""

    def test_model_id_updatable(self, isolated_store):
        group = _local_ag(name="update_test")
        group.pop("model_id")
        isolated_store.save_group(group)
        isolated_store.update_group("update_test", {"model_id": LOCAL_MODEL_ID})
        assert (
            isolated_store.get_group("update_test")["model_id"]
            == LOCAL_MODEL_ID
        )


# ===========================================================================
# 6 - Source drift guard
# ===========================================================================


class TestSourceContracts:

    SRC = (PROJECT_ROOT / "alert_groups" / "dispatcher.py").read_text()

    def test_routing_branch_present(self):
        assert 'local_model_id = (group.get("model_id")' in self.SRC
        assert "_call_router_llm" in self.SRC

    def test_budget_short_circuit_present(self):
        assert 'if (group.get("model_id") or "").strip():' in self.SRC

    def test_no_web_search_on_local_branch(self):
        # The local branch must NOT inject the Anthropic web_search tool.
        assert "no web_search tool" in self.SRC
