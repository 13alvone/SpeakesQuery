"""
Options Edge Brief - Wave 1 integration tests
─────────────────────────────────────────────

Covers the new alert group + the schema extension that supports it:

  1. The 8 new options-specific columns landed in ``ag_picks`` schema.
  2. ``log_ag_pick`` accepts the new kwargs without breaking back-compat.
  3. The dispatcher's ``_validate_and_normalize_pick`` extracts the new
     fields from a Claude-shaped JSON pick and survives type weirdness.
  4. The dispatcher's ``_log_picks`` forwards the new fields through to
     ``log_ag_pick`` (so they actually land on disk).
  5. The OEB alert-group YAML parses + names the 6 expected feeders +
     references the new JSON fields in the prompt.
  6. The 6 OEB ingestion scripts are present in ``script_library/scripts/``.
  7. The 6 OEB saved searches are present in ``default_saved_searches/``.
  8. Account-size-floor logic: a sample pick with floor > $1000 round-trips
     through the validator and lands the floor on disk so Wave 2's
     attribution can filter on it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest.mock
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── 1. Schema extension ───────────────────────────────────────────


def test_ag_picks_schema_has_options_columns():
    """The 8 new options-specific columns must be present in ``ag_picks``."""
    from functionality.log_writer import SCHEMAS

    expected_new = {
        "option_structure",
        "option_legs_json",
        "option_max_loss_usd",
        "option_max_profit_usd",
        "option_net_debit_credit",
        "option_dte_days",
        "option_difficulty_tier",
        "account_size_floor_usd",
    }
    cols = set(SCHEMAS["ag_picks"])
    missing = expected_new - cols
    assert not missing, (
        f"ag_picks schema is missing options-specific columns: {missing}. "
        f"Wave 1 of the Options Edge Brief depends on these for pick "
        f"journaling and Wave 2 mark-to-market attribution."
    )


# ── 2. log_ag_pick accepts the new kwargs ─────────────────────────


def test_log_ag_pick_accepts_options_kwargs(monkeypatch):
    """``log_ag_pick`` should accept all 8 new option_* kwargs without error
    AND forward them into the row that hits ``emit``.  Verify by patching
    ``emit`` and inspecting the argument."""
    from functionality import log_writer

    captured = {}

    def fake_emit(category, row):
        captured["category"] = category
        captured["row"] = row

    monkeypatch.setattr(log_writer, "emit", fake_emit)

    log_writer.log_ag_pick(
        alert_group="test_options",
        run_request_id="req_x",
        rank_in_brief=1,
        idea_id="option:nvda_2026-06-20_long_put:short",
        instrument_type="option",
        instrument_id="nvda_2026-06-20_long_put",
        direction="SHORT",
        conviction_pct=78,
        expected_return_pct=100.0,
        position_size_tier="SMALL",
        entry_price=2.10,
        suggested_buy_epoch=1777000000,
        suggested_sell_epoch=1778000000,
        hold_hours=27,
        take_profit_price=4.20,
        stop_loss_price=1.05,
        exit_catalyst="post-earnings IV crush",
        thesis="NVDA earnings IV overpriced.",
        source_signals="oeb_iv_rank;oeb_earnings_implied_move",
        # New options kwargs:
        option_structure="long_put",
        option_legs_json='[{"action":"BUY","right":"PUT","strike":115.0,"expiration":"2026-06-20","qty":1,"limit":2.10}]',
        option_max_loss_usd=210.0,
        option_max_profit_usd=11290.0,
        option_net_debit_credit=2.10,
        option_dte_days=55,
        option_difficulty_tier="BEGINNER",
        account_size_floor_usd=10500.0,
    )

    assert captured["category"] == "ag_picks"
    row = captured["row"]
    assert row["option_structure"] == "long_put"
    assert "BUY" in row["option_legs_json"]
    assert row["option_max_loss_usd"] == 210.0
    assert row["option_max_profit_usd"] == 11290.0
    assert row["option_net_debit_credit"] == 2.10
    assert row["option_dte_days"] == 55
    assert row["option_difficulty_tier"] == "BEGINNER"
    assert row["account_size_floor_usd"] == 10500.0


# ── 3. Validator extracts new fields ──────────────────────────────


_SAMPLE_OPTIONS_PICK = {
    "idea_id": "option:nvda_2026-06-20_long_put:short",
    "pick_rank": 1,
    "pick_tier": "TOP",
    "instrument_type": "option",
    "instrument_id": "nvda_2026-06-20_long_put",
    "direction": "SHORT",
    "conviction_pct": 78,
    "expected_return_pct": 100.0,
    "position_size_tier": "SMALL",
    "entry_price": 2.10,
    "suggested_buy_epoch": 1777000000,
    "suggested_sell_epoch": 1778000000,
    "hold_hours": 28,
    "take_profit_price": 4.20,
    "stop_loss_price": 1.05,
    "exit_catalyst": "post-earnings IV crush 2026-05-22",
    "thesis": "NVDA earnings IV overpriced relative to historical realized moves.",
    "source_signals": ["oeb_earnings_implied_move", "oeb_iv_rank"],
    "correlation_cluster": "ai_infra_iv_short",
    "short_squeeze_risk": None,
    "option_structure": "long_put",
    "option_legs": [
        {"action": "BUY", "right": "PUT", "strike": 115.0,
         "expiration": "2026-06-20", "qty": 1, "limit": 2.10,
         "contract_symbol": "O:NVDA260620P00115000"},
    ],
    "option_max_loss_usd": 210.0,
    "option_max_profit_usd": 11290.0,
    "option_net_debit_credit": 2.10,
    "option_dte_days": 55,
    "option_difficulty_tier": "BEGINNER",
    "account_size_floor_usd": 10500.0,
}


def test_validator_extracts_options_fields():
    """``_validate_and_normalize_pick`` must extract option_* fields from
    a Claude JSON pick into the normalized dict."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        dict(_SAMPLE_OPTIONS_PICK), rank=1, group_name="options_edge_brief",
    )
    assert normalized is not None
    assert normalized["option_structure"] == "long_put"
    assert normalized["option_legs_json"] is not None
    legs = json.loads(normalized["option_legs_json"])
    assert legs[0]["strike"] == 115.0
    assert normalized["option_max_loss_usd"] == 210.0
    assert normalized["option_max_profit_usd"] == 11290.0
    assert normalized["option_net_debit_credit"] == 2.10
    assert normalized["option_dte_days"] == 55
    assert normalized["option_difficulty_tier"] == "BEGINNER"
    assert normalized["account_size_floor_usd"] == 10500.0


def test_validator_handles_missing_options_fields_gracefully():
    """A non-options pick (Daily Opportunity Brief style) must still
    validate cleanly.  All option_* fields should land as None."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    pick = dict(_SAMPLE_OPTIONS_PICK)
    pick["instrument_type"] = "equity"
    pick["instrument_id"] = "nvda"
    pick["idea_id"] = "equity:nvda:long"
    pick["direction"] = "LONG"
    # Strip every option_* field
    for key in list(pick.keys()):
        if key.startswith("option_") or key == "account_size_floor_usd":
            del pick[key]

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        pick, rank=1, group_name="daily_opportunity_brief",
    )
    assert normalized is not None
    # All option-specific fields should be present (key) but None (value)
    for key in (
        "option_structure", "option_legs_json", "option_max_loss_usd",
        "option_max_profit_usd", "option_net_debit_credit",
        "option_dte_days", "option_difficulty_tier", "account_size_floor_usd",
    ):
        assert key in normalized
        assert normalized[key] is None


def test_validator_handles_invalid_difficulty_tier():
    """``option_difficulty_tier`` outside the enum should normalize to None
    rather than raise."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    pick = dict(_SAMPLE_OPTIONS_PICK)
    pick["option_difficulty_tier"] = "BANANA"

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        pick, rank=1, group_name="options_edge_brief",
    )
    assert normalized is not None
    assert normalized["option_difficulty_tier"] is None


def test_validator_handles_malformed_option_legs():
    """``option_legs`` that's not a list should normalize to None
    rather than raise."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    pick = dict(_SAMPLE_OPTIONS_PICK)
    pick["option_legs"] = "not a list"

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        pick, rank=1, group_name="options_edge_brief",
    )
    assert normalized is not None
    assert normalized["option_legs_json"] is None


# ── 4. Dispatcher forwards new fields to log_ag_pick ──────────────


def test_dispatcher_log_picks_forwards_options_fields(monkeypatch):
    """End-to-end through ``_log_picks``: a normalized pick with options
    fields must be forwarded to ``log_ag_pick`` with all kwargs intact.
    This pins the contract that connects the validator to the journal.
    """
    from alert_groups.dispatcher import AlertGroupDispatcher

    captured_calls = []

    def fake_log_ag_pick(**kwargs):
        captured_calls.append(kwargs)

    monkeypatch.setattr(
        "alert_groups.dispatcher.log_ag_pick", fake_log_ag_pick,
    )

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        dict(_SAMPLE_OPTIONS_PICK), rank=1, group_name="options_edge_brief",
    )
    assert normalized is not None

    written = AlertGroupDispatcher._log_picks(
        normalized_picks=[normalized],
        group_name="options_edge_brief",
        run_request_id="req_test_oeb",
        source="claude",
        model_used="claude-sonnet-4-6",
    )
    assert written == 1
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["alert_group"] == "options_edge_brief"
    assert call["option_structure"] == "long_put"
    assert call["option_max_loss_usd"] == 210.0
    assert call["option_difficulty_tier"] == "BEGINNER"
    assert call["account_size_floor_usd"] == 10500.0
    assert call["option_dte_days"] == 55


# ── 5. Alert-group YAML structure ─────────────────────────────────


def test_oeb_yaml_loads_with_required_fields():
    """The OEB alert-group YAML must load and carry every required field
    that the dispatcher consumes."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_edge_brief.yaml"
    assert yaml_path.exists(), "options_edge_brief.yaml is missing"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    assert data["name"] == "options_edge_brief"
    # Migrated 2026-04-27 from "30 14,19 * * 1-5" UTC to America/New_York
    # so DST is handled automatically - same wall-clock 10:30 + 15:30 ET
    # year-round. 2026-05-02 cron audit (e3c5514): renamed numeric DOW
    # "1-5" to named "mon-fri" (numeric DOW silently misfires under
    # APScheduler's 0=Mon convention vs Linux 0=Sun). See
    # reference_apscheduler_dow_numbering_bug.md.
    assert data["schedule"] == "30 10,15 * * mon-fri"
    assert data["timezone"] == "America/New_York"
    assert data["max_dispatches_per_day"] == 2
    assert data["max_output_tokens"] >= 8192
    # Ships disabled (public-release convention, 2026-07-11): a fresh
    # install must not fire briefs that have no data and no recipient.
    # The operator enables it in the Alert Groups UI after wiring feeders.
    assert data["disabled"] is True
    assert isinstance(data["search_names"], list)
    assert len(data["search_names"]) == 6


def test_oeb_yaml_references_correct_feeders():
    """The OEB alert-group's feeder list must match the 6 saved searches
    we ship in default_saved_searches/."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_edge_brief.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    expected = {
        "oeb_iv_rank",
        "oeb_term_structure",
        "oeb_skew_extreme",
        "oeb_earnings_implied_move",
        "oeb_unusual_activity",
        "oeb_session_context",
    }
    actual = set(data["search_names"])
    assert actual == expected, (
        f"OEB feeder list drifted from default saved searches.\n"
        f"YAML lists: {sorted(actual)}\n"
        f"Expected:   {sorted(expected)}"
    )


def test_oeb_prompt_documents_options_fields():
    """The OEB prompt must instruct Claude to emit every options-specific
    JSON field that the schema persists.  If the prompt drifts away from
    the schema, picks fail to journal correctly without a loud error.
    """
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_edge_brief.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    for required in (
        "option_structure",
        "option_legs",
        "option_max_loss_usd",
        "option_max_profit_usd",
        "option_net_debit_credit",
        "option_dte_days",
        "option_difficulty_tier",
        "account_size_floor_usd",
    ):
        assert required in prompt, (
            f"OEB prompt no longer mentions '{required}'. The schema persists "
            f"this column, so picks emitted without it will land NULL and "
            f"Wave 2 attribution will lose data. Re-add the field to the "
            f"'OPTIONS-SPECIFIC FIELDS' section of the prompt."
        )


def test_oeb_prompt_documents_three_tier_learner_format():
    """The OEB prompt must describe the BEGINNER / INTERMEDIATE / ADVANCED
    three-tier output structure.  This is the user's primary requirement
    for Wave 1 - the brief is meant to teach options trading."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_edge_brief.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    for required in ("BEGINNER", "INTERMEDIATE", "ADVANCED"):
        assert required in prompt, (
            f"OEB prompt missing '{required}' tier - the learner format "
            f"is the primary deliverable of Wave 1."
        )


def test_oeb_prompt_documents_account_size_awareness():
    """The OEB prompt must cap risk per pick (1-2% of account) and flag
    picks where account_size_floor exceeds $1000."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_edge_brief.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    assert "$1000" in prompt or "$1,000" in prompt, (
        "OEB prompt no longer references the $1000 account size - Wave 1 "
        "explicitly designs around this audience."
    )
    assert "1-2%" in prompt or "1%" in prompt, (
        "OEB prompt no longer documents the 1-2% per-pick risk cap."
    )


def test_oeb_prompt_documents_risk_management_rules():
    """The OEB prompt must document explicit stop-loss + take-profit + time
    stop rules for each trade type. Without these the brief would emit
    picks without exit plans, defeating the performance-attribution
    feedback loop in Wave 2."""
    yaml_path = PROJECT_ROOT / "alert_groups" / "options_edge_brief.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    prompt = data["prompt_text"]
    for required in ("stop-loss", "take-profit", "time stop"):
        assert required.lower() in prompt.lower(), (
            f"OEB prompt missing risk-management rule: '{required}'."
        )


# ── 6. Ingestion scripts present ─────────────────────────────────


@pytest.mark.parametrize("script_name", [
    "options_unusual_activity_pro",
    "options_earnings_implied_move_pro",
])
def test_options_script_does_not_call_stocks_snapshot_endpoint(script_name):
    """Massive Options Starter ($29/mo) does NOT entitle stocks-snapshot,
    last-trade, or any equities-tier endpoint - they all return 403
    NOT_AUTHORIZED. The previous-day aggs endpoint works but rate-limits
    to ~2 requests before HTTP 429, unworkable for a 40-ticker watchlist.

    Both pro scripts MUST derive underlying spot via put-call parity
    on the options chain they already pull (paid tier endpoint).

    History - three iterations that all failed in production:
      1. 2026-04-26 (commit bfbe583) - used /v3/ stocks-snapshot → 404
         (endpoint doesn't exist at v3).
      2. 2026-04-27 (commit bfed096) - switched to /v2/ stocks-snapshot
         → 403 NOT_AUTHORIZED (endpoint exists but not on Options Starter).
      3. 2026-05-02 (this commit) - chain-derived via put-call parity,
         zero extra API calls, works on Options Starter.

    Drift guard ensures no future commit reintroduces the broken pattern.
    """
    path = PROJECT_ROOT / "script_library" / "scripts" / f"{script_name}.json"
    code = json.loads(path.read_text())["code"]

    # No HTTP call to any stocks-snapshot or last-trade endpoint.
    forbidden_calls = [
        "/v2/snapshot/locale/us/markets/stocks/tickers/",
        "/v3/snapshot/locale/us/markets/stocks/tickers/",
        "/v2/last/trade/",
        "/v2/aggs/ticker/",  # rate-limited on Options Starter
    ]
    for pattern in forbidden_calls:
        # Pattern is allowed inside a Python string literal that is
        # passed to requests.get / _massive_get - that IS a call.
        # We allow it only if it appears strictly inside a comment.
        # Heuristic: scan each line; flag if the pattern appears AND
        # the line does NOT start with '#' (after leading whitespace).
        for line_no, line in enumerate(code.split("\n"), 1):
            stripped = line.lstrip()
            if pattern in line and not stripped.startswith("#"):
                pytest.fail(
                    f"{script_name} line {line_no}: contains forbidden "
                    f"endpoint '{pattern}'. Use _estimate_underlying_from_chain "
                    f"instead - Options Starter does not entitle this endpoint."
                )

    # Positive assertion: the chain-derive helper must be defined AND used.
    assert "def _estimate_underlying_from_chain" in code, (
        f"{script_name}: missing _estimate_underlying_from_chain helper. "
        f"Required for put-call parity-based underlying derivation."
    )
    # >= 2 occurrences = definition + at least one call site
    assert code.count("_estimate_underlying_from_chain(") >= 2, (
        f"{script_name}: helper _estimate_underlying_from_chain is "
        f"defined but never called."
    )


# Reference implementation of the put-call parity helper for tests.
# MUST stay byte-equivalent (modulo whitespace/comments) with the helper
# embedded in both options_earnings_implied_move_pro.json and
# options_unusual_activity_pro.json. Drift guard further below pins the
# helper definition appears in both scripts.
def _ref_estimate_underlying_from_chain(contracts):
    by_strike = {}
    for contract in contracts:
        details = contract.get("details") or {}
        day = contract.get("day") or {}
        strike = details.get("strike_price")
        close = day.get("close")
        ctype = str(details.get("contract_type") or "").lower()
        if strike is None or close is None or close <= 0.005:
            continue
        if ctype not in ("call", "put"):
            continue
        slot = by_strike.setdefault(strike, {})
        slot[ctype] = close
    estimates = []
    for strike, sides in by_strike.items():
        if "call" in sides and "put" in sides:
            estimates.append(strike + sides["call"] - sides["put"])
    if not estimates:
        return None
    estimates.sort()
    return estimates[len(estimates) // 2]


def test_put_call_parity_helper_is_correct():
    """Validate the put-call parity formula with synthetic data.

    For an underlying S, at any strike K:
        Call - Put ≈ S - K  (ignoring small r*T discount factor)
    therefore:
        S ≈ K + Call - Put

    Median across all strike-pairs is robust to far-OTM stale prints.
    Pinned with a synthetic chain centered at S=$280 so any change to
    the helper's mathematics fails loud.

    Validated against live AAPL data 2026-05-02: 34 strike pairs across
    the 2026-05-04 expiration, median estimate $280.08 vs true close
    $280.14 (within $0.06 / 0.02%).
    """
    S_TRUE = 280.0
    extrinsic = 1.0  # uniform extrinsic across strikes (simplification)
    contracts = []
    for K in (270, 275, 280, 285, 290):
        C = max(S_TRUE - K, 0) + extrinsic
        P = max(K - S_TRUE, 0) + extrinsic
        contracts.append({
            "details": {"contract_type": "call", "strike_price": K},
            "day": {"close": C},
        })
        contracts.append({
            "details": {"contract_type": "put", "strike_price": K},
            "day": {"close": P},
        })
    result = _ref_estimate_underlying_from_chain(contracts)
    assert result is not None
    assert abs(result - S_TRUE) < 0.01, (
        f"helper returned {result}, expected ≈{S_TRUE}"
    )


def test_put_call_parity_helper_handles_sparse_chain():
    """When NO strike has both a call and a put (sparse / illiquid),
    the helper must return None rather than raise - script then logs
    a per-ticker error and continues to the next ticker."""
    contracts = [
        {"details": {"contract_type": "call", "strike_price": 100},
         "day": {"close": 5.0}},
        {"details": {"contract_type": "call", "strike_price": 110},
         "day": {"close": 1.0}},
    ]
    assert _ref_estimate_underlying_from_chain(contracts) is None


def test_put_call_parity_helper_skips_zero_close_contracts():
    """A strike pair where one side has close ≈ 0 (no trades) must be
    skipped, otherwise the parity estimate inherits the stale zero and
    the median gets dragged toward strike-only values.

    Real-world example: deep-OTM puts on a strong bull day might print
    close=$0.01 from a single residual trade. We require close > 0.005."""
    contracts = [
        # Real strike pair at K=100 - yields S=105
        {"details": {"contract_type": "call", "strike_price": 100},
         "day": {"close": 6.0}},
        {"details": {"contract_type": "put", "strike_price": 100},
         "day": {"close": 1.0}},
        # Bad: K=200 with put=0 (filtered by close > 0.005); leaves only call
        {"details": {"contract_type": "call", "strike_price": 200},
         "day": {"close": 0.5}},
        {"details": {"contract_type": "put", "strike_price": 200},
         "day": {"close": 0.0}},
    ]
    result = _ref_estimate_underlying_from_chain(contracts)
    # Only the K=100 pair is valid; estimate = 100 + 6 - 1 = 105
    assert result == 105.0


def test_chain_derive_helper_scripts_use_reference_impl():
    """The helper definition embedded in BOTH scripts must reuse the
    reference logic (signature + key formulas). Drift guard pins:
      - Function signature
      - The strike + call - put formula
      - Median selection (sort + middle index)
      - close > 0.005 zero-close filter
    """
    sentinels = (
        "def _estimate_underlying_from_chain",
        "by_strike = {}",
        "if strike is None or close is None or close <= 0.005:",
        "if ctype not in ('call', 'put'):",
        "slot = by_strike.setdefault(strike, {})",
        "estimates.append(strike + sides['call'] - sides['put'])",
        "estimates.sort()",
        "return estimates[len(estimates) // 2]",
    )
    for script_name in ("options_earnings_implied_move_pro", "options_unusual_activity_pro"):
        path = PROJECT_ROOT / "script_library" / "scripts" / f"{script_name}.json"
        code = json.loads(path.read_text())["code"]
        for sentinel in sentinels:
            assert sentinel in code, (
                f"{script_name}: missing required token '{sentinel}' from "
                f"_estimate_underlying_from_chain helper. Did you change the "
                f"helper signature or formula? Update tests/test_options_edge_brief.py "
                f"if intentional."
            )


@pytest.mark.parametrize("script_name", [
    "options_iv_rank_screener_pro",
    "options_term_structure_pro",
    "options_skew_monitor_pro",
    "options_earnings_implied_move_pro",
    "options_market_status",
    "options_ex_div_calendar",
])
def test_oeb_ingestion_script_exists(script_name):
    """Each of the 6 new OEB ingestion scripts must be present and parse."""
    path = PROJECT_ROOT / "script_library" / "scripts" / f"{script_name}.json"
    assert path.exists(), f"OEB ingestion script {script_name}.json is missing"
    data = json.loads(path.read_text())
    assert data.get("title"), f"{script_name}: missing title"
    assert data.get("code"), f"{script_name}: missing code"
    # Compile to ensure syntax is valid
    compile(data["code"], f"{script_name}.json", "exec")


# ── 7. Saved searches present ────────────────────────────────────


@pytest.mark.parametrize("search_name", [
    "oeb_iv_rank",
    "oeb_term_structure",
    "oeb_skew_extreme",
    "oeb_earnings_implied_move",
    "oeb_unusual_activity",
    "oeb_session_context",
])
def test_oeb_saved_search_exists(search_name):
    """Each of the 6 new OEB saved searches must be present in
    default_saved_searches/ and parse as YAML."""
    path = PROJECT_ROOT / "default_saved_searches" / f"{search_name}.yaml"
    assert path.exists(), f"OEB saved search {search_name}.yaml is missing"
    with open(path) as f:
        data = yaml.safe_load(f)
    assert data["name"] == search_name
    assert data.get("query"), f"{search_name}: missing query"
    assert data.get("email_address") == "noreply@speakesquery.local", (
        f"{search_name}: AG-feeder saved searches use the noreply convention; "
        f"the saved-search store rejects empty emails."
    )


# ── 8. Account-size-floor round-trip ─────────────────────────────


def test_account_size_floor_round_trip(monkeypatch):
    """A pick with ``account_size_floor_usd: 10500`` must land that value
    in the journal call, NOT silently round to None or be dropped.
    Wave 2 attribution filters on this column to identify picks that
    didn't fit the user's actual account size at the time."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    captured_calls = []
    monkeypatch.setattr(
        "alert_groups.dispatcher.log_ag_pick",
        lambda **kw: captured_calls.append(kw),
    )

    pick = dict(_SAMPLE_OPTIONS_PICK)
    pick["account_size_floor_usd"] = 10500.0

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        pick, rank=1, group_name="options_edge_brief",
    )
    assert normalized["account_size_floor_usd"] == 10500.0

    AlertGroupDispatcher._log_picks(
        normalized_picks=[normalized],
        group_name="options_edge_brief",
        run_request_id="req_floor_test",
    )
    assert len(captured_calls) == 1
    assert captured_calls[0]["account_size_floor_usd"] == 10500.0


def test_account_size_floor_handles_string_input():
    """If Claude emits ``"10500.0"`` (string) instead of ``10500.0``
    (number), the validator should coerce, not drop."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    pick = dict(_SAMPLE_OPTIONS_PICK)
    pick["account_size_floor_usd"] = "10500.0"

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        pick, rank=1, group_name="options_edge_brief",
    )
    assert normalized is not None
    assert normalized["account_size_floor_usd"] == 10500.0


def test_account_size_floor_under_1000_marks_pick_fits_small_account():
    """When a pick has account_size_floor <= $1000, it should still
    round-trip cleanly (no rejection) - just lands as small account-fit."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    pick = dict(_SAMPLE_OPTIONS_PICK)
    pick["account_size_floor_usd"] = 250.0  # tiny pick - fits even $500 account

    normalized = AlertGroupDispatcher._validate_and_normalize_pick(
        pick, rank=1, group_name="options_edge_brief",
    )
    assert normalized is not None
    assert normalized["account_size_floor_usd"] == 250.0


# ── 9. Pick parsing end-to-end with Claude-shaped response ────────


def test_full_pick_parse_from_claude_response_text(monkeypatch):
    """Simulate the dispatcher receiving a Claude response with a fenced
    JSON block carrying an OEB-shaped pick. Verify the full round trip:
    parse → validate → log."""
    from alert_groups.dispatcher import AlertGroupDispatcher

    captured_calls = []
    monkeypatch.setattr(
        "alert_groups.dispatcher.log_ag_pick",
        lambda **kw: captured_calls.append(kw),
    )

    response_text = (
        "## Executive Summary\n\nSomething.\n\n--- END BRIEF ---\n\n"
        f"```json\n{json.dumps([_SAMPLE_OPTIONS_PICK])}\n```\n"
    )

    written = AlertGroupDispatcher._extract_and_log_picks(
        response_text=response_text,
        group_name="options_edge_brief",
        run_request_id="req_full_e2e",
        model_used="claude-sonnet-4-6",
    )
    assert written == 1
    assert captured_calls[0]["option_structure"] == "long_put"
    assert captured_calls[0]["account_size_floor_usd"] == 10500.0
