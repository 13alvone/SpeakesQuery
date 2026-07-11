"""
Options Edge Brief - Wave 2 integration tests
─────────────────────────────────────────────

Pins the deterministic mark-to-market layer:

  1. IMMUTABLE namespace plumbing - settings expose immutable_dir() +
     immutable_subdir(); cleanup skips it; bad-name validation rejects
     traversal / hidden / empty names.
  2. ag_picks_closures + ag_picks_review_observations schemas exist
     and are routed to IMMUTABLE/ via the LogWriter (not the logs tree).
  3. Schema additivity - none of the IMMUTABLE-bound schemas may
     remove a column once shipped (decade-horizon trading record).
  4. log_ag_pick_closure / log_ag_review_observation accept their
     documented kwargs and round-trip through emit().
  5. The dispatcher's review-observations parser correctly extracts a
     summary row + N observation rows from a Claude-shaped JSON OBJECT
     in the response tail.
  6. Account-size setting validation (rejects 0, negative, non-numeric)
     + dual hit-rate flag computation (a pick with floor > current
     account size is account_fit=False, etc.).
  7. The OEB performance review AG YAML loads + references the 3
     expected feeders + the prompt documents the JSON-tail object
     shape required by the parser.
  8. The legacy ag_picks migration is idempotent - running it twice
     with no source files is a no-op; running with sources moves
     them; running with both sides populated leaves originals in
     place and warns.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import textwrap
from pathlib import Path

import pandas as pd
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── 1. IMMUTABLE namespace plumbing ──────────────────────────────


def test_settings_exposes_immutable_dir_and_subdir():
    from global_settings import get_settings
    s = get_settings()
    base = s.immutable_dir()
    assert base.name == "IMMUTABLE"
    assert s.immutable_subdir("ag_picks") == base / "ag_picks"
    assert s.immutable_subdir("ag_picks_closures") == base / "ag_picks_closures"


@pytest.mark.parametrize("bad_name", ["", "../escape", "foo/bar", "a\\b", ".hidden"])
def test_immutable_subdir_rejects_traversal_and_empty(bad_name):
    from global_settings import get_settings
    s = get_settings()
    with pytest.raises(ValueError):
        s.immutable_subdir(bad_name)


def test_engine_skip_subdirs_includes_both_logs_and_immutable(tmp_path, monkeypatch):
    """The cleanup skip list must include BOTH 'logs' AND 'IMMUTABLE'
    when both are nested under indexes/. Without this, the main
    cleanup would garbage-collect the trading record. We simulate the
    default production layout (logs/ + IMMUTABLE/ both under indexes/)
    so the test is independent of any test-harness path overrides."""
    from scheduled_input_engine.engine import ScheduledInputEngine
    eng = ScheduledInputEngine.__new__(ScheduledInputEngine)
    eng._settings = None  # force fallback to the default-layout path
    indexes = tmp_path / "indexes"
    monkeypatch.setattr(eng, "_get_indexes_dir", lambda: indexes)
    monkeypatch.setattr(eng, "_get_logs_dir", lambda: indexes / "logs")
    monkeypatch.setattr(eng, "_get_immutable_dir", lambda: indexes / "IMMUTABLE")
    skip = eng._logs_relative_skip()
    assert "logs" in skip
    assert "IMMUTABLE" in skip


# ── 2. Schema presence + IMMUTABLE routing ───────────────────────


def test_ag_picks_closures_schema_present():
    from functionality.log_writer import SCHEMAS
    assert "ag_picks_closures" in SCHEMAS
    expected = {
        "_epoch", "event_timestamp", "alert_group", "idea_id",
        "instrument_type", "instrument_id", "outcome", "trigger_rule",
        "entry_price", "exit_price", "exit_epoch",
        "pnl_per_contract_usd", "pnl_pct_vs_max_loss",
        "days_held", "leg_prices_at_close_json", "closure_quality",
        "account_size_floor_usd", "fits_account_at_entry",
        "current_account_size_usd_at_close", "fits_account_at_close",
    }
    cols = set(SCHEMAS["ag_picks_closures"])
    missing = expected - cols
    assert not missing, f"ag_picks_closures missing: {missing}"


def test_ag_picks_review_observations_schema_present():
    from functionality.log_writer import SCHEMAS
    assert "ag_picks_review_observations" in SCHEMAS
    expected = {
        "_epoch", "event_timestamp", "alert_group", "run_request_id",
        "review_period_start", "review_period_end", "review_period_days",
        "n_picks_overall", "n_picks_account_fit",
        "hit_rate_overall", "hit_rate_account_fit",
        "best_signal_class", "worst_signal_class",
        "observation_text", "observation_evidence", "observation_actionable",
        "rule_tweak_recommendation_text", "rule_tweak_rationale",
        "rule_tweak_expected_impact", "row_kind",
    }
    cols = set(SCHEMAS["ag_picks_review_observations"])
    missing = expected - cols
    assert not missing, f"ag_picks_review_observations missing: {missing}"


def test_immutable_categories_match_intended_set():
    """If a future change adds a new IMMUTABLE-bound category, the test
    fails so the developer documents the addition. Caller must update
    BOTH this test AND the IMMUTABLE_CATEGORIES set in log_writer.py."""
    from functionality.log_writer import IMMUTABLE_CATEGORIES
    assert IMMUTABLE_CATEGORIES == frozenset({
        "ag_picks",
        "ag_picks_closures",
        "ag_picks_review_observations",
        # Phase 6 / Bet 5 slice 1 (2026-05-16): curator ↔ speaktube
        # contract endpoints. All three are explicitly forever-data -
        # the user's viewing telemetry, their written reflections, and
        # the historical record of what the curator suggested. See
        # docs/lang/21_curator_speaktube.md.
        "curator_telemetry",
        "curator_reflections",
        "curator_playlist",
        # Phase 6 / Bet 5 slice 3 (2026-05-16): topic-evolution snapshots -
        # one row per cluster per snapshot, ties the curator's "what is the
        # user into 6 months ago vs today?" timeline. Added to
        # IMMUTABLE_CATEGORIES in slice 3 but this enumeration drifted
        # until slice 4 (2026-05-17) caught it.
        "curator_topic_snapshots",
        # Phase 6 / Bet 5 slice 11 (2026-05-17 - speaktube req #10):
        # operator-supplied keyword preferences. Each POST to
        # /api/preferences/keywords writes one row. Forever-data so the
        # operator's "what was I curious about last spring?" trail is
        # recoverable; the "active pool" semantic ("expires after the
        # next composer fire") is functional-only.
        "curator_keyword_prefs",
    })


def test_writer_routes_immutable_categories_to_immutable_dir(monkeypatch, tmp_path):
    """Pin the wire-up: an IMMUTABLE category resolves to a writer
    rooted at immutable_dir(); a non-IMMUTABLE category resolves to a
    writer rooted at logs_dir()."""
    from functionality.log_writer import LogWriter

    LogWriter.reset_for_tests()
    writer = LogWriter()
    writer._logs_root = tmp_path / "logs"
    writer._immutable_root = tmp_path / "IMM"

    immutable = writer._writer_for("ag_picks")
    standard = writer._writer_for("system")
    assert immutable is not None and standard is not None
    assert immutable is not standard
    # The two writers should target different parquet roots
    immutable_root = (tmp_path / "IMM").resolve()
    logs_root = (tmp_path / "logs").resolve()
    assert (immutable_root.exists() and immutable_root.is_dir())
    assert (logs_root.exists() and logs_root.is_dir())
    LogWriter.reset_for_tests()


# ── 3. Schema additivity guards ──────────────────────────────────


# Snapshots of the columns that MUST exist forever - the decade-horizon
# trading record cannot lose a column without breaking historical
# SPQL queries. Tests fail loud if a future commit removes any column
# from these snapshots. Adding a NEW column is fine - just append it.
_PICKS_FROZEN_COLS = {
    "_epoch", "alert_group", "run_request_id", "rank_in_brief",
    "idea_id", "instrument_type", "instrument_id", "direction",
    "conviction_pct", "expected_return_pct", "position_size_tier",
    "entry_price", "suggested_buy_epoch", "suggested_sell_epoch",
    "hold_hours", "take_profit_price", "stop_loss_price",
    "exit_catalyst", "thesis", "source_signals",
    # Wave 1 of OEB
    "option_structure", "option_legs_json",
    "option_max_loss_usd", "option_max_profit_usd",
    "option_net_debit_credit", "option_dte_days",
    "option_difficulty_tier", "account_size_floor_usd",
}

_CLOSURES_FROZEN_COLS = {
    "_epoch", "alert_group", "idea_id", "instrument_type", "instrument_id",
    "outcome", "trigger_rule", "entry_price", "exit_price", "exit_epoch",
    "pnl_per_contract_usd", "pnl_pct_vs_max_loss", "days_held",
    "leg_prices_at_close_json", "closure_quality",
    "account_size_floor_usd", "fits_account_at_entry",
    "current_account_size_usd_at_close", "fits_account_at_close",
}

_REVIEW_FROZEN_COLS = {
    "_epoch", "alert_group", "run_request_id",
    "review_period_start", "review_period_end", "review_period_days",
    "n_picks_overall", "n_picks_account_fit",
    "hit_rate_overall", "hit_rate_account_fit",
    "best_signal_class", "worst_signal_class",
    "observation_text", "observation_evidence", "observation_actionable",
    "rule_tweak_recommendation_text", "rule_tweak_rationale",
    "rule_tweak_expected_impact", "row_kind",
    # Calibration verdict columns added 2026-05-06 (Bucket 1.5 follow-on).
    # Once shipped, they are part of the IMMUTABLE record and cannot be
    # removed - additive-only forever.
    "calibration_status", "calibration_n_closures",
}


@pytest.mark.parametrize("category,frozen", [
    ("ag_picks", _PICKS_FROZEN_COLS),
    ("ag_picks_closures", _CLOSURES_FROZEN_COLS),
    ("ag_picks_review_observations", _REVIEW_FROZEN_COLS),
])
def test_immutable_schema_is_additive_only(category, frozen):
    """Wave 2 of OEB: the trading record must compound for a decade.
    Removing a column from any IMMUTABLE-bound schema breaks every
    historical SPQL query that references it. Adding columns is fine -
    the log writer projects-with-NULL on missing columns. This test
    fails loud if a developer removes a column; it does NOT fail when
    new columns are added."""
    from functionality.log_writer import SCHEMAS
    cols = set(SCHEMAS[category])
    missing = frozen - cols
    assert not missing, (
        f"{category}: removed columns from the IMMUTABLE schema "
        f"snapshot - {missing}. The decade-horizon trading record "
        f"cannot lose columns. Add a NEW column instead, or update "
        f"this snapshot ONLY if you have an explicit data migration "
        f"plan for every existing parquet on every install."
    )


# ── 4. Helper round-trips ────────────────────────────────────────


def test_log_ag_pick_closure_round_trip(monkeypatch):
    from functionality import log_writer
    captured = []
    monkeypatch.setattr(log_writer, "emit", lambda c, r: captured.append((c, r)))
    log_writer.log_ag_pick_closure(
        alert_group="options_edge_brief",
        idea_id="option:nvda_2026-06-20_long_put:short",
        instrument_type="option",
        instrument_id="nvda_2026-06-20_long_put",
        outcome="won",
        trigger_rule="take_profit_hit",
        entry_price=2.10,
        exit_price=4.20,
        exit_epoch=1700000000,
        pnl_per_contract_usd=210.0,
        pnl_pct_vs_max_loss=100.0,
        days_held=2.5,
        leg_prices_at_close_json='[{"contract_symbol":"O:NVDA260620P00115000","mid":4.20}]',
        closure_quality="clean",
        account_size_floor_usd=10500.0,
        fits_account_at_entry=False,
        current_account_size_usd_at_close=1000.0,
        fits_account_at_close=False,
    )
    assert len(captured) == 1
    cat, row = captured[0]
    assert cat == "ag_picks_closures"
    assert row["outcome"] == "won"
    assert row["pnl_per_contract_usd"] == 210.0
    assert row["fits_account_at_entry"] is False
    assert row["account_size_floor_usd"] == 10500.0


def test_log_ag_review_observation_round_trip(monkeypatch):
    from functionality import log_writer
    captured = []
    monkeypatch.setattr(log_writer, "emit", lambda c, r: captured.append((c, r)))
    log_writer.log_ag_review_observation(
        alert_group="options_performance_review",
        run_request_id="req_abc",
        review_period_start="2026-04-19",
        review_period_end="2026-04-26",
        review_period_days=7,
        n_picks_overall=10,
        n_picks_account_fit=4,
        hit_rate_overall=0.62,
        hit_rate_account_fit=0.50,
        observation_text="High-IVR sell-premium picks outperform low-IVR by 3x",
        observation_evidence="High-IVR: 4/5 won; low-IVR: 1/3 won",
        observation_actionable=True,
        row_kind="observation",
    )
    assert len(captured) == 1
    cat, row = captured[0]
    assert cat == "ag_picks_review_observations"
    assert row["row_kind"] == "observation"
    assert row["observation_actionable"] is True
    assert row["hit_rate_overall"] == 0.62


# ── 5. Dispatcher review-observations parser ─────────────────────


_VALID_REVIEW_RESPONSE = textwrap.dedent("""
    ## Executive Summary
    Sample brief.

    --- END BRIEF ---

    ```json
    {
      "review_period_start": "2026-04-19",
      "review_period_end": "2026-04-26",
      "review_period_days": 7,
      "n_picks_overall": 8,
      "n_picks_account_fit": 3,
      "hit_rate_overall": 0.625,
      "hit_rate_account_fit": 0.667,
      "best_signal_class": "iv_rank_high",
      "worst_signal_class": "earnings_implied_move",
      "rule_tweak": {
        "recommendation": "Raise IVR floor from 70 to 80",
        "rationale": "IVR 70-80 hit rate 35%, IVR > 80 hit rate 65%",
        "expected_impact": "Fewer picks, higher hit rate"
      },
      "observations": [
        {
          "text": "Earnings IV-crush plays underperformed",
          "evidence": "1 win out of 4 closures",
          "actionable": true
        },
        {
          "text": "Term-structure backwardation correlates with wins",
          "evidence": "3 of 4 BACKWARDATION picks closed positive",
          "actionable": false
        }
      ]
    }
    ```
""").strip()


def test_dispatcher_parses_review_summary_plus_observations(monkeypatch):
    """End-to-end through the new parser: a Claude-shaped review
    response should produce 1 summary row + 2 observation rows."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    captured = []
    monkeypatch.setattr(
        "alert_groups.dispatcher.log_ag_review_observation",
        lambda **kw: captured.append(kw),
        raising=False,
    )
    # The dispatcher's _extract_and_log_review_observations imports
    # log_ag_review_observation from functionality.log_writer at call
    # time, so patch the source module too.
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: captured.append(kw),
    )

    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text=_VALID_REVIEW_RESPONSE,
        group_name="options_performance_review",
        run_request_id="req_review_test",
    )
    assert written == 3  # 1 summary + 2 observations
    summary = [r for r in captured if r.get("row_kind") == "summary"]
    observations = [r for r in captured if r.get("row_kind") == "observation"]
    assert len(summary) == 1
    assert len(observations) == 2
    assert summary[0]["hit_rate_overall"] == 0.625
    assert summary[0]["rule_tweak_recommendation_text"].startswith("Raise IVR")
    assert observations[0]["observation_actionable"] is True
    assert observations[1]["observation_actionable"] is False


def test_dispatcher_handles_review_with_no_observations(monkeypatch):
    """If Claude returns observations=[] (small dataset), the parser
    should still write the summary row and return 1."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    captured = []
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: captured.append(kw),
    )
    minimal_obj = {
        "review_period_start": "2026-04-19",
        "review_period_end": "2026-04-26",
        "review_period_days": 7,
        "n_picks_overall": 0,
        "n_picks_account_fit": 0,
        "hit_rate_overall": 0.0,
        "hit_rate_account_fit": 0.0,
        "best_signal_class": "",
        "worst_signal_class": "",
        "rule_tweak": {"recommendation": "", "rationale": "", "expected_impact": ""},
        "observations": [],
    }
    response = (
        "## Brief\n\n--- END BRIEF ---\n\n```json\n"
        + json.dumps(minimal_obj)
        + "\n```\n"
    )
    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text=response,
        group_name="options_performance_review",
        run_request_id="req_review_empty",
    )
    assert written == 1
    assert captured[0]["row_kind"] == "summary"


def test_dispatcher_review_parser_tolerates_missing_block(monkeypatch):
    """No fenced JSON block → return 0 (don't raise)."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: None,
    )
    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text="## Brief\n\nNo JSON tail.",
        group_name="options_performance_review",
        run_request_id="req_no_json",
    )
    assert written == 0


def test_dispatcher_review_parser_tolerates_malformed_json(monkeypatch):
    """Malformed JSON → return 0 (don't raise)."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: None,
    )
    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text="--- END BRIEF ---\n\n```json\n{invalid: json}\n```",
        group_name="options_performance_review",
        run_request_id="req_bad_json",
    )
    assert written == 0


# ── 5b. Calibration persistence (Bucket 1.5, 2026-05-06) ─────────


def test_review_observations_schema_includes_calibration_columns():
    """The 2026-05-06 calibration-persistence work added two columns to
    the IMMUTABLE-bound ag_picks_review_observations schema:
    calibration_status (verdict) + calibration_n_closures (sample
    size). Both are additive - the existing schema is otherwise
    unchanged. Pin them so a future commit can't quietly drop them."""
    from functionality.log_writer import SCHEMAS
    cols = set(SCHEMAS["ag_picks_review_observations"])
    assert "calibration_status" in cols, (
        "calibration_status column missing - added 2026-05-06 to "
        "persist the OEB performance review's calibration verdict"
    )
    assert "calibration_n_closures" in cols, (
        "calibration_n_closures column missing - added 2026-05-06 "
        "to track the calibration sample size"
    )


def test_dispatcher_parses_calibration_when_present(monkeypatch):
    """When Claude emits calibration_status + calibration_n_closures in
    the JSON tail, the parser must extract them and pass to BOTH the
    summary row AND every observation row (so SPQL queries can filter
    by calibration verdict regardless of row_kind)."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    captured = []
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: captured.append(kw),
    )
    obj_with_cal = {
        "review_period_start": "2026-04-19",
        "review_period_end": "2026-04-26",
        "review_period_days": 7,
        "n_picks_overall": 25,
        "n_picks_account_fit": 18,
        "hit_rate_overall": 0.6,
        "hit_rate_account_fit": 0.72,
        "best_signal_class": "iv_rank_high",
        "worst_signal_class": "earnings_implied_move",
        "calibration_status": "well_calibrated",
        "calibration_n_closures": 25,
        "rule_tweak": {"recommendation": "", "rationale": "", "expected_impact": ""},
        "observations": [
            {"text": "obs A", "evidence": "ev A", "actionable": True},
            {"text": "obs B", "evidence": "ev B", "actionable": False},
        ],
    }
    response = (
        "## Brief\n\n--- END BRIEF ---\n\n```json\n"
        + json.dumps(obj_with_cal)
        + "\n```\n"
    )
    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text=response,
        group_name="options_performance_review",
        run_request_id="req_cal_present",
    )
    assert written == 3  # 1 summary + 2 observations
    for row in captured:
        assert row["calibration_status"] == "well_calibrated"
        assert row["calibration_n_closures"] == 25


def test_dispatcher_defaults_calibration_when_absent(monkeypatch):
    """Back-compat: review responses that pre-date the calibration
    prompt edit (or where the prompt skipped the section due to
    insufficient sample) won't carry calibration_status /
    calibration_n_closures keys. The parser must NOT raise - it must
    default to "" / 0 so old responses still persist cleanly."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    captured = []
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: captured.append(kw),
    )
    obj_without_cal = {
        "review_period_start": "2026-04-12",
        "review_period_end": "2026-04-19",
        "review_period_days": 7,
        "n_picks_overall": 5,
        "n_picks_account_fit": 4,
        "hit_rate_overall": 0.4,
        "hit_rate_account_fit": 0.5,
        "best_signal_class": "iv_rank_high",
        "worst_signal_class": "",
        # NB: no calibration_status, no calibration_n_closures keys.
        "rule_tweak": {"recommendation": "", "rationale": "", "expected_impact": ""},
        "observations": [],
    }
    response = (
        "## Brief\n\n--- END BRIEF ---\n\n```json\n"
        + json.dumps(obj_without_cal)
        + "\n```\n"
    )
    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text=response,
        group_name="options_performance_review",
        run_request_id="req_no_cal",
    )
    assert written == 1  # just the summary row (no observations)
    assert captured[0]["calibration_status"] == ""
    assert captured[0]["calibration_n_closures"] == 0


def test_dispatcher_rejects_invalid_calibration_status(monkeypatch):
    """If Claude hallucinates a label outside the four valid values
    (well_calibrated / overconfident / underconfident /
    insufficient_data), the parser must coerce to "" rather than
    persist the bad label. Otherwise, future SPQL queries would have
    to handle an open vocabulary of made-up statuses, defeating the
    enum's purpose."""
    from alert_groups.dispatcher import AlertGroupDispatcher
    captured = []
    monkeypatch.setattr(
        "functionality.log_writer.log_ag_review_observation",
        lambda **kw: captured.append(kw),
    )
    obj_bad_cal = {
        "review_period_start": "2026-04-19",
        "review_period_end": "2026-04-26",
        "review_period_days": 7,
        "n_picks_overall": 12,
        "n_picks_account_fit": 8,
        "hit_rate_overall": 0.5,
        "hit_rate_account_fit": 0.5,
        "best_signal_class": "",
        "worst_signal_class": "",
        "calibration_status": "mostly_calibrated",  # NOT in the enum
        "calibration_n_closures": 12,
        "rule_tweak": {"recommendation": "", "rationale": "", "expected_impact": ""},
        "observations": [],
    }
    response = (
        "## Brief\n\n--- END BRIEF ---\n\n```json\n"
        + json.dumps(obj_bad_cal)
        + "\n```\n"
    )
    written = AlertGroupDispatcher._extract_and_log_review_observations(
        response_text=response,
        group_name="options_performance_review",
        run_request_id="req_bad_cal",
    )
    assert written == 1
    # Bad label coerces to ""; n_closures still trusted as int.
    assert captured[0]["calibration_status"] == ""
    assert captured[0]["calibration_n_closures"] == 12


def test_perf_review_prompt_documents_calibration_json_tail_keys():
    """The prompt's MANDATORY STRUCTURED TAIL spec must enumerate
    calibration_status + calibration_n_closures alongside the other
    JSON-tail keys, so Claude knows to emit them. Without this,
    Claude will skip the new fields and the persisted column stays
    empty forever."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    assert "calibration_status" in prompt
    assert "calibration_n_closures" in prompt
    # The four canonical labels must be enumerated for Claude.
    for label in ("well_calibrated", "overconfident", "underconfident", "insufficient_data"):
        assert label in prompt, (
            f"calibration label '{label}' missing from JSON tail spec"
        )


# ── 6. Account-size setting ──────────────────────────────────────


def test_current_account_size_default_is_1000():
    from global_settings import DEFAULTS
    assert DEFAULTS["current_account_size_usd"] == 1000.0


def test_account_size_validation_rejects_zero_and_negative():
    from global_settings import _validate_key, DEFAULTS
    err = _validate_key("current_account_size_usd", 0, dict(DEFAULTS))
    assert err is not None
    err = _validate_key("current_account_size_usd", -100, dict(DEFAULTS))
    assert err is not None
    err = _validate_key("current_account_size_usd", "not_a_number", dict(DEFAULTS))
    assert err is not None


def test_account_size_validation_accepts_positive_numbers():
    from global_settings import _validate_key, DEFAULTS
    assert _validate_key("current_account_size_usd", 1000.0, dict(DEFAULTS)) is None
    assert _validate_key("current_account_size_usd", 250000, dict(DEFAULTS)) is None
    assert _validate_key("current_account_size_usd", 1_000_000.0, dict(DEFAULTS)) is None


# ── 7. Performance review AG YAML structure ──────────────────────


def test_perf_review_yaml_loads_and_references_3_feeders():
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    assert yaml_path.exists()
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    assert data["name"] == "options_performance_review"
    # Migrated 2026-04-27 from "0 22 * * 0" UTC to America/New_York.
    # 2026-05-02 cron audit (e3c5514): switched DOW from numeric "0" to
    # named "sun" (numeric DOW is the APScheduler-vs-Linux 0=Mon-vs-0=Sun
    # silent-bug pattern; named days are unambiguous and survive any
    # future translator regression). Schedule moved to 18:30 ET so the
    # Sunday post-market review fires cleanly after the cash close.
    # See reference_apscheduler_dow_numbering_bug.md +
    # reference_market_aware_schedules_must_be_timezone_explicit.md.
    assert data["schedule"] == "30 18 * * sun"
    assert data["timezone"] == "America/New_York"
    assert data["max_dispatches_per_day"] == 1
    assert set(data["search_names"]) == {
        "oeb_perf_weekly", "oeb_perf_monthly", "oeb_perf_open_positions",
    }


def test_perf_review_prompt_documents_marker_examiner_separation():
    """Pin the prompt's principle: the review AG must NOT re-judge
    individual picks. This is the anti-bias guarantee that keeps the
    metric trustworthy for the user's go-live decision."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"].lower()
    assert "marker" in prompt or "examiner" in prompt
    assert "hindsight" in prompt
    assert "deterministic" in prompt or "fixed exit rules" in prompt


def test_perf_review_prompt_documents_dual_hit_rate():
    """The prompt must instruct Claude to compute BOTH
    hit_rate_overall AND hit_rate_account_fit - the user's stated
    go-live gate depends on the latter."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    assert "hit_rate_overall" in prompt
    assert "hit_rate_account_fit" in prompt


def test_perf_review_prompt_documents_json_object_tail():
    """The prompt must tell Claude to emit an OBJECT (not array) JSON
    tail, with the exact keys the parser expects."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    for required in (
        "review_period_start", "review_period_end", "review_period_days",
        "n_picks_overall", "n_picks_account_fit",
        "hit_rate_overall", "hit_rate_account_fit",
        "rule_tweak", "observations",
    ):
        assert required in prompt


def test_perf_review_prompt_marks_account_fit_as_headline():
    """The 2026-05-06 attribution-prep edits elevate hit_rate_account_fit
    to the headline metric. The user's go-live decision is gated on the
    metric that filters to picks within their $1000 account size - not
    the overall figure that includes picks they couldn't actually take.
    Pin the elevation so a future edit can't quietly demote it."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    # The phrase "HEADLINE" (uppercase) anchors the elevation; "go-live"
    # connects it to the user's stated gating decision.
    assert "HEADLINE" in prompt, (
        "prompt must explicitly mark hit_rate_account_fit as the HEADLINE "
        "metric - see 2026-05-06 attribution-prep edits"
    )
    assert "go-live" in prompt
    # The Executive Summary description must say "lead with hit_rate_account_fit".
    assert "lead with" in prompt.lower() and "hit_rate_account_fit" in prompt


def test_perf_review_prompt_enumerates_canonical_signal_classes():
    """The 2026-05-06 attribution-prep edits enumerate the six canonical
    OEB signal-class labels so Claude uses consistent buckets across
    weeks. Without this enumeration, "iv_rank_high" one week and
    "high_iv_rank" the next break trend analysis on the persisted
    best_signal_class / worst_signal_class columns."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    canonical = (
        "iv_rank_high",
        "iv_rank_low",
        "term_backwardation",
        "skew_extreme",
        "earnings_implied_move",
        "unusual_flow",
    )
    for label in canonical:
        assert label in prompt, (
            f"canonical signal class '{label}' missing from prompt - "
            f"the 2026-05-06 attribution-prep edits enumerate all six"
        )


def test_perf_review_prompt_documents_calibration_check():
    """The 2026-05-06 attribution-prep edits add a calibration check
    that buckets closures by the original pick's conviction_pct and
    asks whether high-conviction picks actually win at higher rates.
    Without calibration, conviction_pct is decorative - the user can't
    tell whether the analyst's confidence predicts outcomes."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    # Section header + workflow step
    assert "Calibration Check" in prompt
    assert "conviction_pct" in prompt
    # Bucket boundaries (anchor: 75-79 lower bound, 95-100 upper bound)
    assert "75-79" in prompt
    assert "95-100" in prompt
    # Verdict vocabulary - these labels are checked verbatim by the
    # markdown renderer's expectations and downstream summarisation.
    for verdict in ("well-calibrated", "overconfident", "underconfident"):
        assert verdict in prompt
    # Sample-size guard: don't emit a verdict on < 10 closures.
    assert "10 closures" in prompt or "< 10" in prompt


def test_perf_review_default_and_live_prompts_match():
    """The prompt_text in default_alert_groups/ and alert_groups/ MUST
    stay byte-identical. Per the 'local YAML vs live deployment drift
    footgun' memory: prompt edits made to one path that don't propagate
    to the other silently regress whichever side gets re-seeded next.

    Schedule, timezone, max_rows, etc. may legitimately diverge (live
    customisation), but the prompt is part of the AG's contract - both
    files must carry the SAME instruction surface to Claude."""
    default_path = PROJECT_ROOT / "default_alert_groups" / "options_performance_review.yaml"
    live_path = PROJECT_ROOT / "alert_groups" / "options_performance_review.yaml"
    if not default_path.exists() or not live_path.exists():
        pytest.skip("default or live OEB review YAML missing")
    with open(default_path) as f:
        default_prompt = yaml.safe_load(f)["prompt_text"]
    with open(live_path) as f:
        live_prompt = yaml.safe_load(f)["prompt_text"]
    assert default_prompt == live_prompt, (
        "default_alert_groups/options_performance_review.yaml and "
        "alert_groups/options_performance_review.yaml have divergent "
        "prompt_text. Mirror the prompt edit to BOTH trees - see "
        "reference_local_yaml_vs_live_drift_footgun.md."
    )


# ── 8. Legacy ag_picks migration ─────────────────────────────────


def test_migration_idempotent_when_source_empty(tmp_path, monkeypatch):
    """Running the migration with no files at the source path is a
    no-op (returns 0). Running again is also a no-op."""
    from scheduled_input_engine.engine import ScheduledInputEngine
    eng = ScheduledInputEngine.__new__(ScheduledInputEngine)
    monkeypatch.setattr(eng, "_get_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(eng, "_get_immutable_dir", lambda: tmp_path / "IMM")
    # Source doesn't exist
    assert eng._migrate_ag_picks_to_immutable() == 0
    # Source exists but empty
    (tmp_path / "logs" / "ag_picks").mkdir(parents=True)
    assert eng._migrate_ag_picks_to_immutable() == 0
    assert eng._migrate_ag_picks_to_immutable() == 0  # idempotent


def test_migration_moves_parquet_files_when_present(tmp_path, monkeypatch):
    from scheduled_input_engine.engine import ScheduledInputEngine
    eng = ScheduledInputEngine.__new__(ScheduledInputEngine)
    monkeypatch.setattr(eng, "_get_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(eng, "_get_immutable_dir", lambda: tmp_path / "IMM")
    src = tmp_path / "logs" / "ag_picks"
    src.mkdir(parents=True)
    # Seed three parquets at the source
    for i in range(3):
        f = src / f"file_{i}.parquet"
        f.write_bytes(b"PAR1...")
    moved = eng._migrate_ag_picks_to_immutable()
    assert moved == 3
    dst = tmp_path / "IMM" / "ag_picks"
    assert dst.exists()
    assert sorted(p.name for p in dst.iterdir()) == [
        "file_0.parquet", "file_1.parquet", "file_2.parquet",
    ]
    # Source dir should now be empty
    assert list(src.iterdir()) == []


def test_migration_skips_when_destination_already_has_same_filename(tmp_path, monkeypatch):
    from scheduled_input_engine.engine import ScheduledInputEngine
    eng = ScheduledInputEngine.__new__(ScheduledInputEngine)
    monkeypatch.setattr(eng, "_get_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(eng, "_get_immutable_dir", lambda: tmp_path / "IMM")
    src = tmp_path / "logs" / "ag_picks"
    dst = tmp_path / "IMM" / "ag_picks"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    # Seed a file at BOTH paths with the same name
    src_file = src / "collision.parquet"
    dst_file = dst / "collision.parquet"
    src_file.write_bytes(b"SOURCE")
    dst_file.write_bytes(b"DEST")
    moved = eng._migrate_ag_picks_to_immutable()
    assert moved == 0  # nothing moved, conflict
    # Both files should still exist with original contents
    assert src_file.read_bytes() == b"SOURCE"
    assert dst_file.read_bytes() == b"DEST"


# ── 9. Existing references updated to new path ───────────────────


@pytest.mark.parametrize("yaml_name", [
    "dob_reserved_picks", "spbeb_reserved_picks", "phpb_reserved_picks",
    "rcpb_reserved_picks", "cdsb_reserved_picks", "fxrb_reserved_picks",
    "egib_reserved_picks", "gmrb_reserved_picks", "pppb_reserved_picks",
    "sfcb_reserved_picks", "cpb_reserved_picks",
])
def test_legacy_reserved_picks_yamls_use_immutable_path(yaml_name):
    """Wave 2 migration check: every existing *_reserved_picks YAML in
    default_saved_searches/ must reference indexes/IMMUTABLE/ag_picks/
    (not the legacy logs/ path). Drift here breaks the dedup feeders
    after the migration runs."""
    yaml_path = PROJECT_ROOT / "default_saved_searches" / f"{yaml_name}.yaml"
    if not yaml_path.exists():
        pytest.skip(f"{yaml_name} not present")
    text = yaml_path.read_text()
    assert "indexes/logs/ag_picks" not in text, (
        f"{yaml_name} still references the legacy path. The migration "
        f"on engine startup will move the parquets, but this saved "
        f"search will read an empty directory until updated."
    )
    assert "indexes/IMMUTABLE/ag_picks" in text


@pytest.mark.parametrize("yaml_name", [
    "civilization_pulse_brief", "crypto_deep_signals_brief",
    "energy_grid_intelligence_brief", "fx_rate_brief",
    "global_macro_risk_brief", "politics_policy_prediction_brief",
    "public_health_pharma_brief", "religion_cultural_prediction_brief",
    "science_forecasting_brief", "sports_betting_edge_brief",
    "daily_opportunity_brief",
])
def test_existing_alert_groups_no_legacy_path_in_prompt(yaml_name):
    """Sanity: no AG prompt should hard-reference the legacy path."""
    yaml_path = PROJECT_ROOT / "alert_groups" / f"{yaml_name}.yaml"
    if not yaml_path.exists():
        pytest.skip(f"{yaml_name} not present")
    text = yaml_path.read_text()
    assert "indexes/logs/ag_picks" not in text
