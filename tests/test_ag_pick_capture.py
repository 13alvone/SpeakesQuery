#!/usr/bin/env python3
"""
Regression tests for the 2026-04-21 Daily Opportunity Brief pick-capture
feature.

What this pins:

1. **`ag_picks` schema is registered** in ``functionality/log_writer.py``
   with the exact columns the downstream reserved-picks feeder + future
   backtest script depend on.
2. **`log_ag_pick()` helper** accepts the expected kwargs and emits one
   row with all required fields.
3. **Dispatcher extraction**:
   - Valid fenced JSON block → one row per pick written.
   - Malformed JSON → warning log, zero rows written, dispatch continues.
   - Missing block (truncated brief) → warning log, zero rows, no crash.
   - ``idea_id`` lowercased as the "verify" step on top of trust.
   - Schema validation (missing required key, bad instrument_type, bad
     direction, bad epoch ordering) → pick skipped with warning.
4. **Reserved-picks feeder YAML** is well-formed + SPQL-valid shape.
5. **Daily Brief AG YAML** lists the reserved-picks feeder AND its
   prompt includes the mandatory JSON-block instructions.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# =====================================================================
# Part 1: Schema registration
# =====================================================================

class TestAgPicksSchema:

    def test_ag_picks_category_present(self):
        from functionality.log_writer import SCHEMAS, VALID_CATEGORIES
        assert "ag_picks" in SCHEMAS, (
            "ag_picks category missing from log_writer.SCHEMAS. "
            "Add it back or the reserved-picks feeder + backtest will "
            "have nowhere to write."
        )
        assert "ag_picks" in VALID_CATEGORIES

    def test_ag_picks_schema_has_all_required_columns(self):
        from functionality.log_writer import SCHEMAS
        cols = set(SCHEMAS["ag_picks"])
        required = {
            "_epoch", "event_timestamp", "alert_group", "run_request_id",
            "rank_in_brief", "idea_id", "instrument_type", "instrument_id",
            "direction", "conviction_pct", "expected_return_pct",
            "position_size_tier", "entry_price",
            "suggested_buy_epoch", "suggested_sell_epoch", "hold_hours",
            "take_profit_price", "stop_loss_price",
            "exit_catalyst", "thesis", "source_signals", "status",
        }
        missing = required - cols
        assert not missing, (
            f"ag_picks schema missing columns: {sorted(missing)}. "
            "Every column listed here is load-bearing for "
            "backtesting + dedup."
        )

    def test_log_ag_pick_helper_importable_with_expected_signature(self):
        """The helper exists and accepts every kwarg the dispatcher uses."""
        from functionality.log_writer import log_ag_pick
        import inspect
        sig = inspect.signature(log_ag_pick)
        required_params = {
            "alert_group", "run_request_id", "rank_in_brief", "idea_id",
            "instrument_type", "instrument_id", "direction",
            "conviction_pct", "expected_return_pct", "position_size_tier",
            "entry_price", "suggested_buy_epoch", "suggested_sell_epoch",
            "hold_hours",
        }
        missing = required_params - set(sig.parameters)
        assert not missing, (
            f"log_ag_pick is missing these kwargs: {sorted(missing)}"
        )


# =====================================================================
# Part 2: Dispatcher extraction + validation
# =====================================================================

class TestPickExtraction:

    @staticmethod
    def _sample_pick(**overrides):
        base = {
            "idea_id": "polymarket:will-trump-visit-china-2026:yes",
            "instrument_type": "polymarket",
            "instrument_id": "will-trump-visit-china-2026",
            "direction": "YES",
            "conviction_pct": 82,
            "expected_return_pct": 19.5,
            "position_size_tier": "MEDIUM",
            "entry_price": 0.81,
            "suggested_buy_epoch": 1777089600,
            "suggested_sell_epoch": 1777348800,
            "hold_hours": 72,
            "take_profit_price": 0.95,
            "stop_loss_price": 0.65,
            "exit_catalyst": "resolution at market close 2026-04-27",
            "thesis": "Strong convergent signals.",
            "source_signals": ["dob_poly_high_prob", "dob_reddit_buzz"],
        }
        base.update(overrides)
        return base

    def _response_with(self, picks_list):
        prose = (
            "## Executive Summary\nmarkets look good\n\n"
            "## TOP 5 OPPORTUNITIES\n### #1: stuff\n\n"
            "--- END BRIEF ---\n\n"
            "```json\n" + json.dumps(picks_list, indent=2) + "\n```\n"
        )
        return prose

    def test_valid_response_emits_rows(self):
        from alert_groups.dispatcher import AlertGroupDispatcher

        writes = []
        def _fake_emit(**kwargs):
            writes.append(kwargs)

        with patch(
            "functionality.log_writer.log_ag_pick", side_effect=_fake_emit,
        ):
            with patch(
                "alert_groups.dispatcher.log_ag_pick", side_effect=_fake_emit,
            ):
                n = AlertGroupDispatcher._extract_and_log_picks(
                    response_text=self._response_with([
                        self._sample_pick(),
                        self._sample_pick(
                            idea_id="equity:nvda:long",
                            instrument_type="equity",
                            instrument_id="nvda",
                            direction="LONG",
                        ),
                    ]),
                    group_name="daily_opportunity_brief",
                    run_request_id="rid-test",
                )
        assert n == 2
        assert len(writes) == 2
        w0 = writes[0]
        assert w0["alert_group"] == "daily_opportunity_brief"
        assert w0["run_request_id"] == "rid-test"
        assert w0["rank_in_brief"] == 1
        assert w0["idea_id"] == "polymarket:will-trump-visit-china-2026:yes"
        assert w0["instrument_type"] == "polymarket"
        assert w0["direction"] == "YES"
        assert w0["suggested_buy_epoch"] == 1777089600
        assert w0["suggested_sell_epoch"] == 1777348800
        assert w0["hold_hours"] == 72
        assert w0["take_profit_price"] == 0.95
        assert w0["stop_loss_price"] == 0.65
        # source_signals is normalised to semicolon-joined text
        assert "dob_poly_high_prob" in w0["source_signals"]
        assert "dob_reddit_buzz" in w0["source_signals"]

    def test_idea_id_is_lowercased(self):
        """Defence in depth on top of Claude's format compliance."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        writes = []
        def _fake_emit(**kwargs):
            writes.append(kwargs)

        pick = self._sample_pick(
            idea_id="Polymarket:Will-Trump-Visit-China-2026:YES",
            instrument_type="POLYMARKET",
            instrument_id="Will-Trump-Visit-China-2026",
        )

        with patch(
            "alert_groups.dispatcher.log_ag_pick", side_effect=_fake_emit,
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=self._response_with([pick]),
                group_name="daily_opportunity_brief",
                run_request_id="rid",
            )
        assert n == 1
        assert writes[0]["idea_id"] == "polymarket:will-trump-visit-china-2026:yes"
        assert writes[0]["instrument_type"] == "polymarket"

    def test_missing_json_block_returns_zero_no_crash(self, caplog):
        """A truncated brief or drift-to-free-text response must NOT
        crash the dispatch - just log a warning + return 0."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        import logging
        caplog.set_level(logging.WARNING, logger="alert_groups.dispatcher")

        with patch(
            "alert_groups.dispatcher.log_ag_pick",
            side_effect=AssertionError("should not be called"),
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text="## Summary\nno picks, no json block.",
                group_name="daily_opportunity_brief",
                run_request_id="rid-missing",
            )
        assert n == 0
        warns = " ".join(r.getMessage() for r in caplog.records)
        assert "no fenced JSON picks block" in warns.lower() \
            or "no fenced json picks block" in warns.lower()

    def test_malformed_json_block_returns_zero_no_crash(self, caplog):
        from alert_groups.dispatcher import AlertGroupDispatcher
        import logging
        caplog.set_level(logging.WARNING, logger="alert_groups.dispatcher")

        # Bracket-balanced but invalid JSON inside - regex matches the
        # ``[...]`` envelope, parser rejects the body.
        response = (
            "--- END BRIEF ---\n\n"
            "```json\n[{ bad json, unquoted keys, trailing comma, }]\n```"
        )
        with patch(
            "alert_groups.dispatcher.log_ag_pick",
            side_effect=AssertionError("should not be called"),
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=response,
                group_name="daily_opportunity_brief",
                run_request_id="rid-bad",
            )
        assert n == 0
        warns = " ".join(r.getMessage() for r in caplog.records)
        assert "failed to parse" in warns.lower()

    def test_non_list_json_returns_zero(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        response = (
            "--- END BRIEF ---\n\n"
            "```json\n{\"not\": \"a list\"}\n```"
        )
        with patch(
            "alert_groups.dispatcher.log_ag_pick",
            side_effect=AssertionError("should not be called"),
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=response,
                group_name="daily_opportunity_brief",
                run_request_id="rid",
            )
        assert n == 0

    @pytest.mark.parametrize("broken_field, broken_value, reason", [
        ("idea_id", "has spaces in it", "bad format"),
        ("instrument_type", "stocks",     "unknown instrument_type"),
        ("direction", "HOLD",             "unknown direction"),
        ("position_size_tier", "TINY",    "unknown position_size_tier"),
        ("conviction_pct", 150,           "out of [0,100]"),
        ("suggested_buy_epoch", 0,        "non-positive epoch"),
    ])
    def test_bad_field_drops_pick_but_keeps_others(
        self, broken_field, broken_value, reason,
    ):
        from alert_groups.dispatcher import AlertGroupDispatcher

        writes = []

        def _fake_emit(**kwargs):
            writes.append(kwargs)

        good = self._sample_pick()
        bad = self._sample_pick(**{broken_field: broken_value})
        # Give bad a distinct idea_id so dedup-by-rank is clean
        if broken_field != "idea_id":
            bad = self._sample_pick(
                idea_id="equity:bad:long",
                **{broken_field: broken_value},
            )

        with patch(
            "alert_groups.dispatcher.log_ag_pick", side_effect=_fake_emit,
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=self._response_with([bad, good]),
                group_name="daily_opportunity_brief",
                run_request_id=f"rid-{broken_field}",
            )
        assert n == 1, (
            f"Expected exactly the good pick to be written ({reason}); "
            f"got n={n} writes={[w['idea_id'] for w in writes]}"
        )
        # The one write should be the good one (rank 2).
        assert writes[0]["rank_in_brief"] == 2

    def test_sell_epoch_before_buy_epoch_skipped(self):
        from alert_groups.dispatcher import AlertGroupDispatcher

        writes = []
        bad = self._sample_pick(
            suggested_buy_epoch=1777200000,
            suggested_sell_epoch=1777100000,
        )
        with patch(
            "alert_groups.dispatcher.log_ag_pick",
            side_effect=lambda **kw: writes.append(kw),
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=self._response_with([bad]),
                group_name="daily_opportunity_brief",
                run_request_id="rid",
            )
        assert n == 0
        assert writes == []

    def test_hold_hours_computed_when_claude_omits(self):
        """If Claude forgets ``hold_hours``, compute from the epochs."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        pick = self._sample_pick(
            suggested_buy_epoch=1000,
            suggested_sell_epoch=1000 + 72 * 3600,
        )
        pick.pop("hold_hours")  # Claude omitted

        writes = []
        with patch(
            "alert_groups.dispatcher.log_ag_pick",
            side_effect=lambda **kw: writes.append(kw),
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=self._response_with([pick]),
                group_name="daily_opportunity_brief",
                run_request_id="rid",
            )
        assert n == 1
        assert writes[0]["hold_hours"] == 72

    def test_optional_price_thresholds_accept_null(self):
        from alert_groups.dispatcher import AlertGroupDispatcher

        pick = self._sample_pick(
            take_profit_price=None,
            stop_loss_price=None,
        )

        writes = []
        with patch(
            "alert_groups.dispatcher.log_ag_pick",
            side_effect=lambda **kw: writes.append(kw),
        ):
            n = AlertGroupDispatcher._extract_and_log_picks(
                response_text=self._response_with([pick]),
                group_name="daily_opportunity_brief",
                run_request_id="rid",
            )
        assert n == 1
        assert writes[0]["take_profit_price"] is None
        assert writes[0]["stop_loss_price"] is None


# =====================================================================
# Part 3: Reserved-picks feeder YAML well-formed
# =====================================================================

class TestReservedPicksFeeder:

    def test_feeder_yaml_exists(self):
        p = Path(PROJECT_ROOT) / "default_saved_searches" \
            / "dob_reserved_picks.yaml"
        assert p.exists(), (
            "default_saved_searches/dob_reserved_picks.yaml "
            "missing - the Daily Brief AG references it and a fresh "
            "install's _seed_defaults() relies on this file being present."
        )

    def test_feeder_query_has_required_elements(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_saved_searches" \
            / "dob_reserved_picks.yaml"
        spec = yaml.safe_load(p.read_text())
        assert spec["name"] == "dob_reserved_picks"
        q = spec["query"]
        # Wave 2 of OEB (2026-04-27) moved the pick journal from
        # indexes/logs/ag_picks/ to indexes/IMMUTABLE/ag_picks/.
        assert 'index="indexes/IMMUTABLE/ag_picks/' in q
        assert 'alert_group="daily_opportunity_brief"' in q
        assert "86400" in q, "Must filter to last 24h"
        assert "| head" in q
        assert "idea_id" in q, "Must expose idea_id for dedup"

    def test_feeder_does_not_send_email(self):
        """It's a data feeder only, never delivers."""
        import yaml
        p = Path(PROJECT_ROOT) / "default_saved_searches" \
            / "dob_reserved_picks.yaml"
        spec = yaml.safe_load(p.read_text())
        assert spec.get("send_email", "no").lower() in ("no", "false")


# =====================================================================
# Part 4: Daily Brief AG wiring + prompt contract
# =====================================================================

class TestDailyBriefWiring:

    def test_reserved_picks_in_ag_search_names(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_alert_groups" / "daily_opportunity_brief.yaml"
        spec = yaml.safe_load(p.read_text())
        names = spec.get("search_names", [])
        assert "dob_reserved_picks" in names, (
            "dob_reserved_picks must be in the AG's "
            "search_names so Claude sees yesterday's picks. Found: "
            f"{names}"
        )

    def test_prompt_mentions_reserved_ideas_rule(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_alert_groups" / "daily_opportunity_brief.yaml"
        spec = yaml.safe_load(p.read_text())
        prompt = spec["prompt_text"]
        lower = prompt.lower()
        assert "reserved" in lower
        assert "dob_reserved_picks" in prompt

    def test_prompt_pins_mandatory_json_tail(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_alert_groups" / "daily_opportunity_brief.yaml"
        spec = yaml.safe_load(p.read_text())
        prompt = spec["prompt_text"]
        assert "MANDATORY STRUCTURED TAIL" in prompt, (
            "Prompt must explicitly instruct Claude to emit the JSON "
            "tail - capture pipeline needs it."
        )
        assert "```json" in prompt, (
            "Prompt must include a ```json example block so Claude "
            "sees the exact format."
        )
        # Every required pick key must be named in the prompt
        for key in (
            "idea_id", "instrument_type", "instrument_id", "direction",
            "conviction_pct", "expected_return_pct", "position_size_tier",
            "entry_price", "suggested_buy_epoch", "suggested_sell_epoch",
            "hold_hours", "take_profit_price", "stop_loss_price",
            "exit_catalyst", "thesis", "source_signals",
        ):
            assert key in prompt, (
                f"Prompt doesn't mention '{key}' - Claude will omit it "
                "and dispatcher validation will reject the pick."
            )

    def test_end_brief_sentinel_is_prose_marker_not_document_end(self):
        """We want ``--- END BRIEF ---`` AFTER the prose but BEFORE the
        JSON tail. The prompt should make clear that the JSON follows."""
        import yaml
        p = Path(PROJECT_ROOT) / "default_alert_groups" / "daily_opportunity_brief.yaml"
        spec = yaml.safe_load(p.read_text())
        prompt = spec["prompt_text"]
        assert "--- END BRIEF ---" in prompt
        # The discipline that the JSON tail follows the sentinel must be
        # stated explicitly. The new prompt (2026-04-23) uses "After
        # `--- END BRIEF ---`, emit a fenced ```json code block..." - any
        # phrasing that pairs the sentinel with the follow-on JSON tail
        # satisfies the contract. Two accepted shapes:
        #   * "end the prose section with <sentinel>" (old phrasing)
        #   * "after <sentinel>, emit ... json ..." (new phrasing)
        old_phrasing = re.search(
            r"end\s+the\s+prose\s+section\s+with",
            prompt, flags=re.IGNORECASE,
        )
        new_phrasing = re.search(
            r"after\s+`?---\s*END\s+BRIEF\s*---`?[\s,]+.*?json",
            prompt, flags=re.IGNORECASE | re.DOTALL,
        )
        assert old_phrasing or new_phrasing, (
            "Prompt must tell Claude that END BRIEF is the prose end and "
            "the JSON tail follows immediately after. Neither the old "
            "phrasing ('end the prose section with') nor the new phrasing "
            "('after --- END BRIEF ---, emit ... json') was found."
        )
