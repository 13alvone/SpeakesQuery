"""
Regression tests for the 2026-05-01 feeder iteration on fx_rate_brief +
politics_policy_prediction_brief.

Background: the Schedule PDF audit surfaced two AGs whose feeders were
silently empty:

* ``fxrb_carry_trade_signal`` had a strict ``where carry_attractive=true``
  filter that cuts everything when no G10 pair currently meets the >=1.5%
  spread threshold. Replaced with eventstats cohort tally + top-by-spread.
* ``pppb_federal_register`` filtered on ``significant_action=true`` which
  matched zero of 3,500 Federal Register rows. Replaced with doc_type
  filter + cohort tally.
* ``pppb_kalshi_economy_policy`` + ``pppb_kalshi_politics`` returned
  baseball player-prop markets because Kalshi's ``category`` field is
  empty across the dataset and the regex chain was matching "Brady House"
  → "House". Added ``volume >= 1000``, ``yes_price > 0``, and
  word-boundary ``\\b...\\b`` regexes.
* ``pppb_poly_politics`` returned 2028 vanity markets ("Will Oprah win",
  "Will LeBron win") at 0.5% probability. Bumped volume floor 25k → 100k,
  added liquidity floor 50k, yes_price band [0.05, 0.95], and limited to
  markets closing within 365 days.
* ``pppb_congress_bills`` tagged ceremonial resolutions (DVT Awareness
  Month, baseball-team commemorations) as importance_tier=HIGH. Added
  bill_type filter (drops SRES/HRES) and required real legislative action
  in latest_action_text.

Each test pins one regression - if these guards are removed, the silent-
empty / contaminated-data bugs return.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = REPO_ROOT / "default_saved_searches"
DEPLOYED = REPO_ROOT / "saved_searches"


def _load(folder: Path, name: str) -> dict:
    path = folder / f"{name}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _query(folder: Path, name: str) -> str:
    return _load(folder, name)["query"]


# ── fxrb_carry_trade_signal - cohort tally + drop strict filter ────────
class TestFxrbCarryTradeSignal:
    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_drops_strict_carry_attractive_filter(self, folder):
        """The naked `where carry_attractive=true` pre-filter cut every
        row when spreads were tight. The new query SHOULD include the
        flag in the output table (so Claude can read it) but MUST NOT
        gate the rows on it."""
        q = _query(folder, "fxrb_carry_trade_signal")
        # Negative: no `where carry_attractive=true` line as a filter step.
        # `carry_attractive` appearing inside `if_()` for the cohort
        # tally is fine - that's a counter, not a filter.
        for line in q.splitlines():
            stripped = line.strip()
            if stripped.startswith("| where ") and "carry_attractive" in stripped:
                pytest.fail(
                    f"fxrb_carry_trade_signal must not filter rows on "
                    f"carry_attractive (silently empties when no pair "
                    f"meets the threshold). Found: {stripped!r}"
                )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_emits_cohort_tally_columns(self, folder):
        q = _query(folder, "fxrb_carry_trade_signal")
        assert "eventstats" in q, "Must use eventstats for cohort tally"
        assert "total_pairs" in q, (
            "Must emit total_pairs cohort column so Claude sees scan size"
        )
        assert "n_attractive" in q, (
            "Must emit n_attractive cohort column so Claude sees how "
            "many pairs passed the threshold even on empty days"
        )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_does_not_use_broken_sum_eval_or_sum_if(self, folder):
        """Two unsupported aggregator-with-expression forms:
        - `sum(eval(if_(...)))` - Splunk-idiomatic, but SPQL rejects
          `eval` as an expression (it's a pipe command).
        - `sum(if_(...))` - parses OK but raises KeyError: None at
          runtime (caught 2026-05-04, prior memory advice was wrong).

        The supported form is eval-then-sum:
            | eval indicator=if_(field==value, 1, 0)
            | eventstats sum(indicator) as <name>"""
        q = _query(folder, "fxrb_carry_trade_signal")
        assert "sum(eval(" not in q, (
            "sum(eval(...)) is rejected by the SPQL grammar - use eval-then-sum"
        )
        assert "sum(if_(" not in q, (
            "sum(if_(...)) parses but raises KeyError: None at runtime "
            "(caught 2026-05-04). Use eval-then-sum: "
            "| eval x=if_(...) | eventstats sum(x) as <name>"
        )


# ── pppb_federal_register - doc_type filter + cohort ──────────────────
class TestPppbFederalRegister:
    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_no_naked_significant_action_filter(self, folder):
        """The naked `where significant_action=true` matched 0 of 3,500
        rows because the field is empty / wrong-type in the source data.
        New query keeps the column for visibility but doesn't gate."""
        q = _query(folder, "pppb_federal_register")
        for line in q.splitlines():
            stripped = line.strip()
            if (stripped.startswith("| where ")
                    and "significant_action" in stripped
                    and "doc_type" not in stripped):
                # A `where significant_action=true` that's the SOLE
                # condition of its where clause is the bug. If
                # significant_action appears in eventstats it's fine.
                if re.search(
                    r"^\|\s*where\s+significant_action\s*=\s*true\s*$",
                    stripped,
                ):
                    pytest.fail(
                        f"pppb_federal_register must not filter solely "
                        f"on significant_action=true (matches 0 rows). "
                        f"Found: {stripped!r}"
                    )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_filters_on_doc_type(self, folder):
        q = _query(folder, "pppb_federal_register")
        assert (
            'doc_type IN ("Rule"' in q
            or "doc_type IN ('Rule'" in q
        ), "Must filter by real doc_type (Rule / Proposed Rule / Presidential Document)"

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_emits_cohort_tally(self, folder):
        q = _query(folder, "pppb_federal_register")
        assert "eventstats" in q
        assert "total_docs" in q
        assert "n_significant" in q

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_lookback_covers_14_days(self, folder):
        cfg = _load(folder, "pppb_federal_register")
        assert cfg.get("lookback") == "-14d", (
            "Federal Register lookback should be 14d to match the "
            "description's 'past 14 days' window."
        )


# ── pppb_kalshi_* - defensive volume + word-boundary guards ───────────
class TestPppbKalshiDefensiveGuards:
    @pytest.mark.parametrize("name", [
        "pppb_kalshi_economy_policy",
        "pppb_kalshi_politics",
    ])
    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_volume_floor_filter(self, folder, name):
        """Kalshi data ships zero-volume player-prop markets that pollute
        the regex match. Volume floor drops them."""
        q = _query(folder, name)
        assert "volume >= 1000" in q, (
            f"{name} must include `volume >= 1000` to drop "
            f"zero-volume player-prop noise."
        )
        assert "yes_price > 0" in q, (
            f"{name} must include `yes_price > 0` to drop empty markets."
        )

    @pytest.mark.parametrize("name", [
        "pppb_kalshi_economy_policy",
        "pppb_kalshi_politics",
    ])
    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_uses_word_boundary_regex(self, folder, name):
        """Without word-boundary regex, "Brady House" matched "House"
        and baseball player-prop markets contaminated the politics feed.
        Pin the \\b...\\b pattern."""
        q = _query(folder, name)
        # Look for at least one \b word-boundary anchor in the regex
        # chain. We use \\b in the YAML which becomes \b in the parsed
        # query string.
        assert "\\b" in q, (
            f"{name} must use word-boundary regex (\\b...\\b) in "
            f"keyword matching - bare substring matches caused player-"
            f"name false positives."
        )


# ── pppb_poly_politics - bumped thresholds + days_to_close ────────────
class TestPppbPolyPolitics:
    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_volume_floor_bumped(self, folder):
        q = _query(folder, "pppb_poly_politics")
        assert "volume >= 100000" in q, (
            "Volume floor must be ≥ 100k (was 25k - let through the "
            "'Will Oprah win 2028' vanity-market noise)."
        )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_liquidity_floor_present(self, folder):
        q = _query(folder, "pppb_poly_politics")
        assert "liquidity >= 50000" in q, (
            "Liquidity floor must be ≥ 50k - volume alone wasn't "
            "enough to filter joke markets."
        )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_yes_price_band(self, folder):
        q = _query(folder, "pppb_poly_politics")
        assert "yes_price >= 0.05" in q, (
            "Yes-price floor must be ≥ 5% - drops 1%-probability "
            "vanity markets."
        )
        assert "yes_price <= 0.95" in q, (
            "Yes-price ceiling must be ≤ 95% - drops 99%-foregone "
            "markets that aren't actionable."
        )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_days_to_close_caps_to_one_year(self, folder):
        q = _query(folder, "pppb_poly_politics")
        assert "days_to_close <= 365" in q, (
            "Must cap markets at 365 days out - without this, 2028 "
            "presidential markets dominated the feed."
        )


# ── pppb_congress_bills - substantive bills only ──────────────────────
class TestPppbCongressBills:
    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_drops_ceremonial_resolutions(self, folder):
        """SRES/HRES are ceremonial Senate/House resolutions (commemorate
        DVT Awareness Month, congratulate Little League winners). They're
        tagged HIGH importance by the upstream classifier but aren't
        actionable for trading. Filter them out at the SPQL layer."""
        q = _query(folder, "pppb_congress_bills")
        assert 'bill_type IN ("S","HR","SJRES","HJRES")' in q or \
               "bill_type IN ('S','HR','SJRES','HJRES')" in q, (
            "Must filter to substantive bill types (drops SRES/HRES "
            "ceremonial resolutions)."
        )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_requires_real_legislative_action(self, folder):
        q = _query(folder, "pppb_congress_bills")
        assert "became public law" in q.lower(), (
            "Must require real legislative action in latest_action_text "
            "(became public law / passed chamber / veto / reported with)"
        )

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_lookback_30_days(self, folder):
        cfg = _load(folder, "pppb_congress_bills")
        assert cfg.get("lookback") == "-30d", (
            "Lookback must be 30d for legislation tracking (7d was too "
            "narrow given the bi-weekly Congress.gov update cadence)."
        )


# ── oeb_earnings_implied_move - sentinel-passing + NaN guard ─────────
class TestOebEarningsImpliedMove:
    """Round 6 fix (2026-05-01): the sentinel row's days_to_earnings is
    NaN, not 0 - JSON renders it as 0.0 but isnull() returns 1, and any
    comparison against NaN is False. So `where days_to_earnings >= 0`
    silently dropped the sentinel and the brief got 0 rows on sparse
    weeks. New form admits NaN-bearing sentinels via OR isnull(...) so
    Claude reads "1 row, signal_class=NO_EARNINGS, count_in_signal_class=1"
    and knows the difference between 'feed broken' and 'no earnings
    this week'."""

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_admits_nan_sentinel_via_isnull(self, folder):
        q = _query(folder, "oeb_earnings_implied_move")
        # Must use OR isnull() to admit NaN-bearing sentinels
        assert "isnull(days_to_earnings)" in q, (
            "Must include `isnull(days_to_earnings)` so the sentinel "
            "row (days_to_earnings=NaN on sparse weeks) reaches the "
            "output as a single 'no upcoming earnings' marker."
        )
        # Negative: must NOT use the round-4 explicit-text guard alone,
        # because it doesn't fix the underlying NaN-comparison bug for
        # downstream queries that depend on days_to_earnings >= 0.
        # The OR isnull(...) form is the canonical fix.

    @pytest.mark.parametrize("folder", [DEFAULTS, DEPLOYED])
    def test_where_clause_preserves_drop_already_reported(self, folder):
        """Negative days_to_earnings (already-reported tickers) must
        still be dropped - the new form preserves that semantic via the
        days_to_earnings >= 0 branch (NaN passes via the second branch,
        but negative numbers fail both)."""
        q = _query(folder, "oeb_earnings_implied_move")
        assert "days_to_earnings >= 0" in q, (
            "Must keep the >= 0 branch so already-reported tickers "
            "(negative days) are dropped."
        )


# ── Cross-cutting: defaults ↔ deployed parity ─────────────────────────
class TestDefaultsAndDeployedAreInSync:
    """When the user pulls + reinstalls a feeder via Feeder Health, the
    deployed copy gets overwritten by the default. To avoid surprises,
    keep them in sync at commit time - these regression tests assume
    both are equally up to date."""

    @pytest.mark.parametrize("name", [
        "fxrb_carry_trade_signal",
        "pppb_federal_register",
        "pppb_kalshi_economy_policy",
        "pppb_kalshi_politics",
        "pppb_poly_politics",
        "pppb_congress_bills",
        "oeb_earnings_implied_move",
    ])
    def test_query_matches_default(self, name):
        a = _load(DEFAULTS, name)["query"]
        b = _load(DEPLOYED, name)["query"]
        assert a == b, (
            f"saved_searches/{name}.yaml query has drifted from "
            f"default_saved_searches/{name}.yaml. Re-sync before commit."
        )
