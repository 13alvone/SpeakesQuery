#!/usr/bin/env python3
"""
Graceful AG completion overhaul (2026-08-04). Four contracts under test,
all born from a production audit that found:

  * daily_opportunity_brief tripped its circuit breaker on 07-27 and then
    skipped (with a failure email) every single day - the breaker had no
    recovery path short of a manual API call.
  * The strict rolling-24h ``max_dispatches_per_day`` window measured
    against run COMPLETION timestamps dropped every other day's dispatch
    for three AGs (run finishes at 14:33, next day's 14:30 cron sees
    "success 23.95h ago" and is rejected).
  * One transient LLM failure (gateway 504, LAN blip) threw away the
    entire dispatch - feeders had already run; the data was simply lost.
  * options_edge_brief emailed Claude's raw output: internal analysis
    notes, tool narration, and the machine JSON tail, with no BLUF.

Contracts:

1. **Rate-limit grace window** - the 24h window shrinks by
   ``alert_group_daily_window_grace_minutes`` so cron jitter + run
   duration can't starve a daily AG.
2. **Half-open circuit breaker** - cooldown skips are CLEAN (status
   "skipped", no failure email); after the cooldown a probe dispatch
   runs; success closes the breaker; failure restarts the cooldown.
3. **Graduated local-LLM retry + salvage** - transient failures retry
   with backoff; config errors (HTTP 401) fail immediately; after final
   failure the built prompt is emailed so the day's data survives.
4. **Email digest** - ``email_digest_model_id`` distills the raw
   response into a BLUF-first body via the router; the raw response
   still feeds pick extraction and the .md attachment; digest failure
   falls back to the raw text minus the JSON tail. Canary: no digest
   model set → zero router invocations.
"""

import datetime as dt
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

from alert_groups.dispatcher import AlertGroupDispatcher
from analyzers.llm_router import LLMRouterError, LLMResponse

LOCAL_MODEL_ID = "llamacpp-qwen35-122b-a10b"

RAW_BRIEF = (
    "## INTERNAL ANALYSIS NOTES (silent workflow)\n"
    "Step 1: tallying rows...\n\n"
    "---\n\n"
    "## Options Edge Brief\n\n"
    "## Executive Summary\n"
    "Zero picks today because nothing cleared the bar.\n\n"
    "--- END BRIEF ---\n\n"
    "```json\n"
    "[]\n"
    "```"
)

DIGEST_BODY = (
    "# options_digest_test - Daily Report\n\n"
    "## BLUF\nZero picks today. Nothing cleared the max-loss bar.\n"
)


def _llm_response(text="local text", finish_reason="stop"):
    return LLMResponse(
        text=text,
        model_id=LOCAL_MODEL_ID,
        provider="lmstudio",
        model_name="Qwen3.5-122B-A10B",
        input_tokens=100,
        output_tokens=200,
        cost_usd=0.0,
        latency_ms=1000,
        request_id="rid-1",
        raw_response={"choices": [{"finish_reason": finish_reason}]},
    )


def _claude_result(text=RAW_BRIEF):
    from analyzers.claude_client import ClaudeCallResult
    response = SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        stop_reason="end_turn",
    )
    return ClaudeCallResult(
        response=response,
        request_id="rid-claude-1",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.01,
        latency_ms=2000,
        attempts=1,
    )


def _feeder_df():
    now = int(time.time())
    return pd.DataFrame({
        "ticker": ["XLV", "CRM"],
        "signal": ["ivr", "skew"],
        "_epoch": [now, now],
    })


def _group(name, **overrides):
    g = {
        "name": name,
        "description": "graceful-overhaul test group",
        "search_names": ["feeder_a"],
        "prompt_text": "Analyze the block below.",
        "schedule": "0 9 * * *",
        "max_rows": 50,
        "email_address": "ops@example.com",
        "disabled": False,
    }
    g.update(overrides)
    return g


def _email_patches():
    return (
        patch.object(AlertGroupDispatcher, "_send_html_email", MagicMock()),
        patch.object(AlertGroupDispatcher, "_send_plain_email", MagicMock()),
        patch.object(AlertGroupDispatcher, "_maybe_send_failure_email",
                     MagicMock()),
    )


# ===========================================================================
# 1 - Rate-limit grace window
# ===========================================================================


class TestRateLimitCalendarDay:
    """max_dispatches_per_day counts successes per CALENDAR DAY in the
    AG's timezone (2026-08-04) - not a rolling 24h window. The rolling
    window (measured against completion-stamped rows) dropped every
    other daily dispatch, and a late manual recovery run slid the clock
    so the next scheduled day skipped too."""

    def _runs_store(self, runs):
        store = MagicMock()
        store.list_runs.return_value = runs
        return store

    def _ts_utc(self, when: dt.datetime) -> str:
        return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def test_yesterdays_run_never_blocks_today(self):
        """Both production bugs at once: a success 23.95h ago (yesterday,
        completion-stamped) and a late recovery run yesterday evening
        must not block today's dispatch."""
        now = dt.datetime.now(dt.timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_late = today_start - dt.timedelta(hours=2)
        store = self._runs_store([
            {"status": "success", "triggered_at": self._ts_utc(yesterday_late)},
        ])
        with patch("alert_group_store.AlertGroupStore", return_value=store):
            err = AlertGroupDispatcher._check_rate_limit(
                {"max_dispatches_per_day": 1, "timezone": "UTC"},
                "calday_test",
            )
        assert err is None

    def test_same_day_second_dispatch_blocked(self):
        now = dt.datetime.now(dt.timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # A success earlier today (halfway between midnight and now).
        earlier_today = today_start + (now - today_start) / 2
        store = self._runs_store([
            {"status": "success", "triggered_at": self._ts_utc(earlier_today)},
        ])
        with patch("alert_group_store.AlertGroupStore", return_value=store):
            err = AlertGroupDispatcher._check_rate_limit(
                {"max_dispatches_per_day": 1, "timezone": "UTC"},
                "calday_test",
            )
        assert err is not None
        assert "max_dispatches_per_day" in err
        assert "calendar day" in err

    def test_day_boundary_uses_ag_timezone(self):
        """A run late yesterday in New York local time can still be
        'today' in UTC - the AG's timezone decides the boundary."""
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        now_ny = dt.datetime.now(ny)
        # Skip the edge where 'yesterday 23:00 NY' is within the last
        # hour (i.e. shortly after NY midnight the case is degenerate).
        if now_ny.hour == 0:
            pytest.skip("degenerate within the first NY hour of the day")
        yesterday_ny_late = (
            now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
            - dt.timedelta(hours=1)
        )
        store = self._runs_store([
            {"status": "success",
             "triggered_at": self._ts_utc(yesterday_ny_late)},
        ])
        with patch("alert_group_store.AlertGroupStore", return_value=store):
            err = AlertGroupDispatcher._check_rate_limit(
                {"max_dispatches_per_day": 1,
                 "timezone": "America/New_York"},
                "calday_tz_test",
            )
        assert err is None

    def test_bad_timezone_falls_back_to_utc(self):
        store = self._runs_store([])
        with patch("alert_group_store.AlertGroupStore", return_value=store):
            err = AlertGroupDispatcher._check_rate_limit(
                {"max_dispatches_per_day": 1, "timezone": "Not/AZone"},
                "calday_test",
            )
        assert err is None

    def test_min_interval_check_unchanged(self):
        t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        store = self._runs_store([
            {"status": "success",
             "triggered_at": t.strftime("%Y-%m-%d %H:%M:%S")},
        ])
        with patch("alert_group_store.AlertGroupStore", return_value=store):
            err = AlertGroupDispatcher._check_rate_limit(
                {"min_interval_between_runs_hours": 4}, "calday_test",
            )
        assert err is not None
        assert "min_interval" in err


# ===========================================================================
# 2 - Half-open circuit breaker
# ===========================================================================


class TestCircuitBreakerHalfOpen:

    def _iso(self, hours_ago: float) -> str:
        return (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=hours_ago)
        ).isoformat()

    def test_missing_timestamp_probes_immediately(self):
        """Legacy trips (pre-2026-08-04 YAMLs have no tripped_at) must
        self-heal on their next scheduled fire."""
        ok, msg = AlertGroupDispatcher._circuit_breaker_probe_state(
            {"circuit_breaker_tripped": True},
        )
        assert ok is True
        assert msg == ""

    def test_recent_trip_cools_down(self):
        ok, msg = AlertGroupDispatcher._circuit_breaker_probe_state(
            {"circuit_breaker_tripped": True,
             "circuit_breaker_tripped_at": self._iso(1.0)},
        )
        assert ok is False
        assert "cooling down" in msg
        assert "reset-circuit-breaker" in msg

    def test_elapsed_cooldown_probes(self):
        ok, _ = AlertGroupDispatcher._circuit_breaker_probe_state(
            {"circuit_breaker_tripped": True,
             "circuit_breaker_tripped_at": self._iso(21.0)},
        )
        assert ok is True

    def test_unparseable_timestamp_probes(self):
        ok, _ = AlertGroupDispatcher._circuit_breaker_probe_state(
            {"circuit_breaker_tripped": True,
             "circuit_breaker_tripped_at": "not-a-date"},
        )
        assert ok is True

    def test_cooldown_skip_is_clean_no_failure_email(self):
        """During cooldown the run must be status='skipped' (NOT 'error')
        and must NOT fire the daily failure email - the pre-fix behaviour
        emailed a failure notice every single day."""
        g = _group(
            "cb_cooldown_test",
            circuit_breaker_tripped=True,
            circuit_breaker_tripped_at=self._iso(1.0),
        )
        e_html, e_plain, e_fail = _email_patches()
        with e_html, e_plain, e_fail as fail_mock:
            d = AlertGroupDispatcher()
            result = d.run(g)
        assert result.status == "skipped"
        assert "cooling down" in (result.error_message or "")
        assert fail_mock.call_count == 0

    def test_half_open_probe_dispatches_and_success_closes_breaker(self):
        g = _group(
            "cb_probe_test",
            circuit_breaker_tripped=True,
            circuit_breaker_tripped_at=self._iso(25.0),
            model_id=LOCAL_MODEL_ID,
        )
        store = MagicMock()
        store.get_group.return_value = {"circuit_breaker_tripped": True}
        e_html, e_plain, e_fail = _email_patches()
        with patch("analyzers.llm_router.call_llm",
                   return_value=_llm_response()), \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             patch.object(AlertGroupDispatcher,
                          "_touch_circuit_breaker_timestamp") as touch, \
             patch("alert_group_store.AlertGroupStore", return_value=store), \
             e_html, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(g)
        assert result.status == "success"
        # Probe start refreshed the trip timestamp...
        touch.assert_called_once_with("cb_probe_test")
        # ...and the success exit closed the breaker.
        closed = [
            c for c in store.update_group.call_args_list
            if c.args[1].get("circuit_breaker_tripped") is False
        ]
        assert closed, "success did not clear circuit_breaker_tripped"

    def test_trip_records_timestamp(self):
        store = MagicMock()
        with patch("alert_group_store.AlertGroupStore", return_value=store), \
             patch.object(AlertGroupDispatcher, "_consecutive_error_count",
                          return_value=99):
            AlertGroupDispatcher._maybe_trip_circuit_breaker("cb_ts_test")
        args = store.update_group.call_args.args
        assert args[0] == "cb_ts_test"
        assert args[1]["circuit_breaker_tripped"] is True
        # ISO timestamp parses and is tz-aware.
        parsed = dt.datetime.fromisoformat(
            args[1]["circuit_breaker_tripped_at"],
        )
        assert parsed.tzinfo is not None


# ===========================================================================
# 3 - Graduated local-LLM retry + salvage email
# ===========================================================================


class TestTransientClassification:

    @pytest.mark.parametrize("error_class,expected", [
        ("HTTP504", True),
        ("HTTP502", True),
        ("HTTP503", True),
        ("HTTP429", True),
        ("HTTP500", True),
        ("HTTP401", False),
        ("HTTP400", False),
        ("HTTP404", False),
        ("ReadTimeout", True),
        ("ConnectTimeout", True),
        ("ConnectionError", True),
        ("MissingCredential", False),
        ("MissingEndpoint", False),
    ])
    def test_classification(self, error_class, expected):
        exc = LLMRouterError("boom", error_class=error_class)
        assert AlertGroupDispatcher._is_transient_llm_error(exc) is expected


class TestGraduatedRetry:

    def test_transient_failures_retry_then_succeed(self):
        d = AlertGroupDispatcher()
        calls = {"n": 0}

        def _flaky(**_k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise LLMRouterError("gateway", error_class="HTTP504")
            return (SimpleNamespace(attempts=1), "final text",
                    {"stop_reason": "stop"})

        with patch.object(d, "_call_router_llm", side_effect=_flaky), \
             patch("alert_groups.dispatcher.time.sleep") as sleeper:
            call, text, _meta = d._call_router_llm_with_retry(
                group_name="retry_test", model_id=LOCAL_MODEL_ID,
                user_content="hi", max_tokens=100,
            )
        assert text == "final text"
        assert calls["n"] == 3
        assert call.attempts == 3
        # Graduated backoff: 30s then 90s (defaults, tripling).
        delays = [c.args[0] for c in sleeper.call_args_list]
        assert delays == [30, 90]

    def test_config_error_fails_immediately(self):
        d = AlertGroupDispatcher()
        calls = {"n": 0}

        def _auth_fail(**_k):
            calls["n"] += 1
            raise LLMRouterError("bad token", error_class="HTTP401")

        with patch.object(d, "_call_router_llm", side_effect=_auth_fail), \
             patch("alert_groups.dispatcher.time.sleep") as sleeper:
            with pytest.raises(LLMRouterError):
                d._call_router_llm_with_retry(
                    group_name="retry_test", model_id=LOCAL_MODEL_ID,
                    user_content="hi", max_tokens=100,
                )
        assert calls["n"] == 1
        assert sleeper.call_count == 0

    def test_empty_text_retries_and_final_empty_returns(self):
        """The 122B trace-starvation failure mode returns empty content;
        each retry is $0 so we retry, and the final empty result flows to
        the shared empty-text guard for diagnostics."""
        d = AlertGroupDispatcher()
        calls = {"n": 0}

        def _empty(**_k):
            calls["n"] += 1
            return (SimpleNamespace(attempts=1), "",
                    {"stop_reason": "length", "block_count": 0,
                     "block_types": []})

        with patch.object(d, "_call_router_llm", side_effect=_empty), \
             patch("alert_groups.dispatcher.time.sleep"):
            _call, text, _meta = d._call_router_llm_with_retry(
                group_name="retry_test", model_id=LOCAL_MODEL_ID,
                user_content="hi", max_tokens=100,
            )
        assert calls["n"] == 3
        assert text == ""


class TestSalvagePromptEmail:

    def test_final_llm_failure_sends_salvage_email(self):
        g = _group("salvage_test", model_id=LOCAL_MODEL_ID)
        e_html, e_plain, e_fail = _email_patches()
        with patch("analyzers.llm_router.call_llm",
                   side_effect=LLMRouterError("bad token",
                                              error_class="HTTP401")), \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             e_html as html_mock, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(g, force=True)
        assert result.status == "error"
        salvage_calls = [
            c for c in html_mock.call_args_list
            if "SALVAGE" in c.kwargs.get("subject", "")
        ]
        assert len(salvage_calls) == 1
        meta = salvage_calls[0].kwargs["meta"]
        assert meta["salvage"] is True
        assert meta["cost_usd"] == 0.0
        # The built prompt (with the feeder data) is the body.
        assert "feeder_a" in salvage_calls[0].kwargs["plain_body"]

    def test_salvage_disabled_by_setting(self):
        g = _group("salvage_off_test", model_id=LOCAL_MODEL_ID)
        real_get = AlertGroupDispatcher._get_setting

        def _get(key, default):
            if key == "alert_group_llm_failure_prompt_fallback":
                return False
            return real_get(key, default)

        e_html, e_plain, e_fail = _email_patches()
        with patch("analyzers.llm_router.call_llm",
                   side_effect=LLMRouterError("bad token",
                                              error_class="HTTP401")), \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             patch.object(AlertGroupDispatcher, "_get_setting",
                          side_effect=_get), \
             e_html as html_mock, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(g, force=True)
        assert result.status == "error"
        assert not [
            c for c in html_mock.call_args_list
            if "SALVAGE" in c.kwargs.get("subject", "")
        ]


# ===========================================================================
# 4 - Email digest (BLUF-first body via local model)
# ===========================================================================


class TestEmailDigest:

    def _run_claude_ag(self, group, router_mock):
        e_html, e_plain, e_fail = _email_patches()
        with patch("alert_groups.dispatcher.call_messages_create",
                   return_value=_claude_result()), \
             patch("analyzers.llm_router.call_llm", router_mock), \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now",
                          return_value=_feeder_df()), \
             patch.object(AlertGroupDispatcher, "_extract_and_log_picks",
                          return_value=2), \
             e_html as html_mock, e_plain, e_fail:
            d = AlertGroupDispatcher()
            result = d.run(group, force=True)
        return result, html_mock

    def test_digest_body_used_raw_attached(self):
        g = _group("options_digest_test",
                   email_digest_model_id=LOCAL_MODEL_ID)
        router = MagicMock(return_value=_llm_response(text=DIGEST_BODY))
        result, html_mock = self._run_claude_ag(g, router)
        assert result.status == "success"
        assert router.call_count == 1
        kwargs = html_mock.call_args.kwargs
        # Inline body is the digest (builder strips outer whitespace);
        # the raw brief rides in the attachment.
        assert kwargs["plain_body"] == DIGEST_BODY.strip()
        assert kwargs["attachment_text"] == RAW_BRIEF
        assert kwargs["meta"]["digest"] is True
        assert kwargs["meta"]["digest_model"] == LOCAL_MODEL_ID
        # Subject-level BLUF: pick count from the journaler.
        assert "2 picks" in kwargs["subject"]

    def test_digest_failure_falls_back_to_raw_minus_json_tail(self):
        g = _group("options_digest_test",
                   email_digest_model_id=LOCAL_MODEL_ID)
        router = MagicMock(
            side_effect=LLMRouterError("down", error_class="HTTP400"),
        )
        result, html_mock = self._run_claude_ag(g, router)
        assert result.status == "success"
        body = html_mock.call_args.kwargs["plain_body"]
        assert "Executive Summary" in body
        assert "```json" not in body
        # Raw (with tail) still attached.
        assert html_mock.call_args.kwargs["attachment_text"] == RAW_BRIEF

    def test_no_digest_model_means_zero_router_calls(self):
        """Canary: an AG without email_digest_model_id must never touch
        the LLM router on the Claude path."""
        g = _group("options_no_digest_test")
        router = MagicMock(
            side_effect=AssertionError("ROUTER LEAK: digest ran unasked"),
        )
        result, html_mock = self._run_claude_ag(g, router)
        assert result.status == "success"
        assert router.call_count == 0
        assert html_mock.call_args.kwargs["plain_body"] == RAW_BRIEF


class TestStripJsonTail:

    def test_strips_trailing_fence(self):
        out = AlertGroupDispatcher._strip_json_tail(RAW_BRIEF)
        assert "```json" not in out
        assert out.rstrip().endswith("--- END BRIEF ---")

    def test_no_fence_passthrough(self):
        assert AlertGroupDispatcher._strip_json_tail("plain") == "plain"

    def test_mid_text_fence_untouched(self):
        text = "before\n```json\n{}\n```\nafter prose"
        assert AlertGroupDispatcher._strip_json_tail(text) == text


# ===========================================================================
# 5 - Store contract drift guard
# ===========================================================================


class TestOpus5Upgrade:
    """2026-08-04: options_edge_brief moved to Opus 5 (1.67x Sonnet 4.6
    pricing, within the user's 3x ceiling). Pins the cost-log pricing
    entries and the model-aware web_search tool version."""

    def test_opus5_pricing_registered(self):
        from analyzers.claude_client import _pricing_for
        assert _pricing_for("claude-opus-5") == (5.00, 25.00)
        # The stale Opus 4.1-era $15/$75 entry for 4.7 was corrected.
        assert _pricing_for("claude-opus-4-7") == (5.00, 25.00)
        assert _pricing_for("claude-sonnet-4-6") == (3.00, 15.00)

    @pytest.mark.parametrize("model,expected_type", [
        ("claude-opus-5", "web_search_20260209"),
        ("claude-sonnet-4-6", "web_search_20260209"),
        ("claude-opus-4-8", "web_search_20260209"),
        ("claude-haiku-4-5-20251001", "web_search_20250305"),
        ("claude-sonnet-4-5", "web_search_20250305"),
    ])
    def test_web_search_tool_version(self, model, expected_type):
        tool = AlertGroupDispatcher._web_search_tool_for(model)
        assert tool == {"type": expected_type, "name": "web_search"}

    def test_oeb_yaml_pins_opus5_with_headroom(self):
        import yaml as _yaml
        for path in ("alert_groups/options_edge_brief.yaml",
                     "default_alert_groups/options_edge_brief.yaml"):
            ag = _yaml.safe_load(open(PROJECT_ROOT / path))
            assert ag["claude_analyzer_model_primary"] == "claude-opus-5"
            # Opus 5 thinks by default; thinking counts against
            # max_tokens, so the cap needs headroom above the brief size.
            assert int(ag["max_output_tokens"]) >= 24576


class TestPerfOpenPositionsFeeder:
    """2026-08-04: the feeder errored every run with "DataFrame does not
    contain ['_epoch']" - `| table` (without _epoch) preceded
    `| sort -_epoch`. Pin the corrected pipe order in both copies."""

    @pytest.mark.parametrize("path", [
        "saved_searches/oeb_perf_open_positions.yaml",
        "default_saved_searches/oeb_perf_open_positions.yaml",
    ])
    def test_sort_precedes_table(self, path):
        import yaml as _yaml
        d = _yaml.safe_load(open(PROJECT_ROOT / path))
        pipes = [p.strip() for p in " ".join(d["query"].split()).split("|")]
        table_idx = next(i for i, p in enumerate(pipes)
                         if p.startswith("table "))
        sort_idx = next(i for i, p in enumerate(pipes)
                        if p.startswith("sort -_epoch"))
        assert sort_idx < table_idx, (
            "sort -_epoch must run before table drops the _epoch column"
        )


class TestStoreContract:

    def test_new_fields_are_updatable(self):
        import inspect
        from alert_group_store import AlertGroupStore
        src = inspect.getsource(AlertGroupStore.update_group)
        assert '"circuit_breaker_tripped_at"' in src
        assert '"email_digest_model_id"' in src

    def test_validation_accepts_empty_digest_model(self):
        from validation.AlertGroupValidation import AlertGroupValidation
        assert AlertGroupValidation.validate_model_id("") == ""
