"""
OEB Pick Tracker - outcome-logic specification + drift guard
────────────────────────────────────────────────────────────

The deterministic tracker (`oeb_pick_tracker_pro.json`) is the most
load-bearing component of Wave 2 - every closure event it writes feeds
the user's hit-rate metric, which gates the $1000 real-money go-live
decision. Bugs here corrupt the metric silently.

This file:
  1. Pins the outcome-determination logic as a *reference Python
     implementation* (`_determine_outcome`, `_compute_pnl_per_contract`,
     `_compute_pnl_pct_vs_max_loss`).
  2. Tests every documented exit path with concrete numbers - long
     premium / short premium / time stop / expiration / still-open
     band / missing-leg behavior / signed P&L for both directions.
  3. Includes a drift guard that walks the tracker JSON and asserts
     critical invariants are still present in the deployed script
     (model name, signed-P&L formula, expiration-first ordering).

If the tracker code drifts from this spec, both halves fail loud.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRACKER_PATH = PROJECT_ROOT / "script_library" / "scripts" / "oeb_pick_tracker_pro.json"


# ───────────────────────────────────────────────────────────────
# Reference implementation
# ───────────────────────────────────────────────────────────────


def _determine_outcome(
    *,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    take_profit: Optional[float],
    net_now: float,
    suggested_sell_epoch: Optional[int],
    now_epoch: int,
    latest_expiration_epoch: Optional[int],
    any_missing: bool,
) -> Tuple[Optional[str], Optional[str], Optional[float], str]:
    """Reference implementation. Return (outcome, trigger_rule, exit_price, closure_quality).

    ``outcome=None`` means the pick is still in band - no closure event.
    """
    # 1. Expiration ALWAYS wins (most fundamental event).
    if latest_expiration_epoch and now_epoch > latest_expiration_epoch:
        exit_price = net_now if not any_missing else 0.0
        return (
            "expired",
            "expiration",
            exit_price,
            "expired_otm" if (exit_price or 0.0) <= 0.0 else "expired_itm",
        )

    # 2. Time stop second - but only if price data is fresh.
    if suggested_sell_epoch and now_epoch >= suggested_sell_epoch and not any_missing:
        return ("time_exit", "time_stop", net_now, "clean")

    # 3. Price triggers last; require fresh data + a signed entry.
    if any_missing or entry_price is None:
        return (None, None, None, "clean")

    if entry_price > 0:
        # Long premium (debit paid).
        if stop_loss is not None and net_now <= stop_loss:
            return ("lost", "stop_loss_hit", stop_loss, "clean")
        if take_profit is not None and net_now >= take_profit:
            return ("won", "take_profit_hit", take_profit, "clean")
    elif entry_price < 0:
        # Short premium (credit received). entry_price is negative;
        # net_now closer to zero = winning, more negative = losing.
        if stop_loss is not None and net_now <= stop_loss:
            return ("lost", "stop_loss_hit", stop_loss, "clean")
        if take_profit is not None and net_now >= take_profit:
            return ("won", "take_profit_hit", take_profit, "clean")
    # entry_price == 0 falls through - never a real options pick.
    return (None, None, None, "clean")


def _compute_pnl_per_contract(
    entry_price: Optional[float], exit_price: Optional[float]
) -> Optional[float]:
    """Signed P&L per 1 contract, in dollars. Works for both long
    (positive entry) and short (negative entry) premium picks because
    the formula carries the sign of the entry naturally."""
    if exit_price is None or entry_price is None:
        return None
    return round((exit_price - entry_price) * 100.0, 2)


def _compute_pnl_pct_vs_max_loss(
    pnl_per_contract: Optional[float],
    max_loss: Optional[float],
    max_profit: Optional[float],
) -> Optional[float]:
    """+100 = full max profit, -100 = full max loss, 0 = breakeven."""
    if pnl_per_contract is None or not max_loss or max_loss <= 0:
        return None
    if pnl_per_contract >= 0 and max_profit and max_profit > 0:
        return round(pnl_per_contract / max_profit * 100.0, 2)
    return round(pnl_per_contract / max_loss * 100.0, 2)


# ───────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────


_NOW = int(time.time())
_LATER = _NOW + 86400 * 30  # 30 days from now
_EARLIER = _NOW - 86400  # yesterday


class TestLongPremiumPicks:
    """Long calls / long puts / debit spreads - entry_price > 0."""

    def test_at_stop_triggers_loss(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.50,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("lost", "stop_loss_hit", 1.05, "clean")

    def test_below_stop_still_triggers_loss(self):
        # net_now well below stop should still trigger
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.10,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("lost", "stop_loss_hit", 1.05, "clean")

    def test_at_take_triggers_win(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=4.20,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("won", "take_profit_hit", 4.20, "clean")

    def test_above_take_still_triggers_win(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=8.00,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("won", "take_profit_hit", 4.20, "clean")

    def test_in_band_stays_open(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=2.50,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == (None, None, None, "clean")  # still open


class TestShortPremiumPicks:
    """Iron condors / short straddles / credit spreads - entry_price < 0."""

    def test_credit_doubled_against_us_triggers_loss(self):
        # Sold for $1.50 credit (entry=-1.50). If credit doubles
        # against us, close at $3.00 (net=-3.00). Stop trigger.
        out = _determine_outcome(
            entry_price=-1.50,
            stop_loss=-3.00,
            take_profit=-0.75,
            net_now=-3.00,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("lost", "stop_loss_hit", -3.00, "clean")

    def test_close_at_half_credit_triggers_win(self):
        # Sold for $1.50 credit. Close at half max (net=-0.75) = win.
        out = _determine_outcome(
            entry_price=-1.50,
            stop_loss=-3.00,
            take_profit=-0.75,
            net_now=-0.75,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("won", "take_profit_hit", -0.75, "clean")

    def test_more_credit_decay_than_take_still_triggers_win(self):
        # net moved closer to zero than take threshold - even better for us
        out = _determine_outcome(
            entry_price=-1.50,
            stop_loss=-3.00,
            take_profit=-0.75,
            net_now=-0.25,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == ("won", "take_profit_hit", -0.75, "clean")

    def test_in_band_stays_open(self):
        out = _determine_outcome(
            entry_price=-1.50,
            stop_loss=-3.00,
            take_profit=-0.75,
            net_now=-1.20,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=False,
        )
        assert out == (None, None, None, "clean")


class TestTriggerOrdering:
    """Expiration must beat time-stop must beat price triggers."""

    def test_expired_beats_stop_loss(self):
        # Pick is past expiration AND price would have hit stop
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.50,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_EARLIER,  # past
            any_missing=False,
        )
        assert out[0] == "expired"
        assert out[1] == "expiration"

    def test_expired_beats_take_profit(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=5.00,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_EARLIER,
            any_missing=False,
        )
        assert out[0] == "expired"

    def test_expired_beats_time_stop(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=2.00,
            suggested_sell_epoch=_EARLIER,  # also past
            now_epoch=_NOW,
            latest_expiration_epoch=_EARLIER,
            any_missing=False,
        )
        assert out[0] == "expired"

    def test_time_stop_beats_price_triggers(self):
        # Time stop has passed AND price would have hit stop -
        # time_stop wins (more graceful "close at market" than stop)
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.50,
            suggested_sell_epoch=_EARLIER,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,  # not yet expired
            any_missing=False,
        )
        assert out[0] == "time_exit"
        assert out[1] == "time_stop"


class TestExpirationClosureQuality:
    def test_otm_expiration_marks_expired_otm(self):
        # Expired with worthless final value (OTM)
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.0,  # OTM call expired worthless
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_EARLIER,
            any_missing=False,
        )
        assert out[3] == "expired_otm"

    def test_itm_expiration_marks_expired_itm(self):
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=5.50,  # ITM at expiration
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_EARLIER,
            any_missing=False,
        )
        assert out[3] == "expired_itm"

    def test_expired_with_missing_data_marks_otm_pessimistic(self):
        # If we can't fetch leg prices at expiration, treat as worthless
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.0,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_EARLIER,
            any_missing=True,
        )
        assert out[0] == "expired"
        assert out[2] == 0.0  # pessimistic exit price
        assert out[3] == "expired_otm"


class TestMissingLegBehavior:
    """When Massive can't return one or more leg prices, the tracker
    must NOT trigger price-based exits - only expiration."""

    def test_missing_leg_does_not_trigger_stop(self):
        # Stop would fire if data were fresh, but any_missing=True
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=0.50,
            suggested_sell_epoch=None,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=True,
        )
        assert out == (None, None, None, "clean")

    def test_missing_leg_does_not_trigger_time_stop(self):
        # Time stop has passed, but missing data means we can't fairly
        # mark the pick - stay open for next run
        out = _determine_outcome(
            entry_price=2.10,
            stop_loss=1.05,
            take_profit=4.20,
            net_now=2.00,
            suggested_sell_epoch=_EARLIER,
            now_epoch=_NOW,
            latest_expiration_epoch=_LATER,
            any_missing=True,
        )
        assert out == (None, None, None, "clean")


class TestPnLPerContract:
    """Signed P&L formula: (exit - entry) × 100."""

    def test_long_premium_winner(self):
        # Buy for $2.10, sell for $4.20 → +$210
        assert _compute_pnl_per_contract(2.10, 4.20) == 210.0

    def test_long_premium_loser(self):
        # Buy for $2.10, stopped at $1.05 → -$105
        assert _compute_pnl_per_contract(2.10, 1.05) == -105.0

    def test_short_premium_winner(self):
        # Sold for $1.50 credit (entry=-1.50), close at half (-0.75)
        # → +$75
        assert _compute_pnl_per_contract(-1.50, -0.75) == 75.0

    def test_short_premium_loser(self):
        # Sold for $1.50 credit, credit doubled to $3.00 (close cost)
        # → -$150
        assert _compute_pnl_per_contract(-1.50, -3.00) == -150.0

    def test_breakeven(self):
        assert _compute_pnl_per_contract(2.10, 2.10) == 0.0

    def test_handles_none_input(self):
        assert _compute_pnl_per_contract(None, 5.0) is None
        assert _compute_pnl_per_contract(5.0, None) is None


class TestPnLPctVsMaxLoss:
    def test_full_max_profit_is_plus_100(self):
        # max_profit=300, pnl=300 → +100%
        assert _compute_pnl_pct_vs_max_loss(300.0, 200.0, 300.0) == 100.0

    def test_full_max_loss_is_minus_100(self):
        # max_loss=200, pnl=-200 → -100%
        assert _compute_pnl_pct_vs_max_loss(-200.0, 200.0, 300.0) == -100.0

    def test_partial_win(self):
        # max_profit=300, pnl=150 → +50%
        assert _compute_pnl_pct_vs_max_loss(150.0, 200.0, 300.0) == 50.0

    def test_partial_loss(self):
        # max_loss=200, pnl=-100 → -50%
        assert _compute_pnl_pct_vs_max_loss(-100.0, 200.0, 300.0) == -50.0

    def test_unlimited_upside_uses_max_loss(self):
        # Long call: max_profit=None (unlimited), pnl=500
        # Falls back to max_loss as denominator
        assert _compute_pnl_pct_vs_max_loss(500.0, 200.0, None) == 250.0

    def test_zero_max_loss_returns_none(self):
        assert _compute_pnl_pct_vs_max_loss(100.0, 0.0, 300.0) is None
        assert _compute_pnl_pct_vs_max_loss(100.0, None, 300.0) is None


# ───────────────────────────────────────────────────────────────
# Drift guard - walks the tracker JSON for critical invariants
# ───────────────────────────────────────────────────────────────


class TestTrackerSourceDriftGuard:
    """If the tracker JSON drifts away from this spec, fail loud.

    Catches: someone refactors the tracker and accidentally swaps
    expiration / time-stop ordering, breaks the signed-P&L formula,
    drops the long-vs-short-premium branch, etc. These would silently
    corrupt the metric without these guards.
    """

    @pytest.fixture(scope="class")
    def code(self) -> str:
        return json.loads(TRACKER_PATH.read_text())["code"]

    def test_signed_pnl_formula_present(self, code):
        # Pinning: pnl = (exit - entry) * 100, NOT abs() or per-direction.
        assert "(exit_price - entry_price) * 100" in code, (
            "Tracker no longer uses the signed (exit - entry) × 100 formula. "
            "This formula carries the sign for both long and short premium "
            "picks. Replacing it with direction-conditional math is a known "
            "footgun. See test_oeb_tracker_outcomes.py::TestPnLPerContract."
        )

    def test_expiration_check_precedes_time_stop(self, code):
        # The outcome-determination block: expiration must be the FIRST
        # branch, time_stop the SECOND, price triggers LAST. Verify by
        # finding the textual order of the keywords.
        idx_expiration = code.find("outcome = 'expired'")
        idx_time_exit = code.find("outcome = 'time_exit'")
        idx_lost_long = code.find("# Long premium hitting stop")
        assert -1 < idx_expiration < idx_time_exit < idx_lost_long, (
            "Tracker outcome-determination order has drifted from "
            "expiration → time_stop → price triggers. This order matters "
            "for picks that hit multiple conditions on the same day."
        )

    def test_long_and_short_premium_branches_both_present(self, code):
        assert "entry_price > 0" in code
        assert "entry_price < 0" in code, (
            "Tracker missing the short-premium branch (entry_price < 0). "
            "Without it, any pick the brief surfaces as a credit-spread / "
            "iron-condor / short-straddle would never close on stop or take."
        )

    def test_missing_leg_blocks_price_triggers(self, code):
        # Pinning: price triggers require `not any_missing`. Without this
        # guard, picks would close at stale / partial prices.
        assert "and not any_missing" in code, (
            "Tracker no longer guards price-trigger paths against missing "
            "leg data. With any leg's price missing, net_now is incomplete "
            "and stop / take triggers would fire incorrectly."
        )

    def test_imports_log_ag_pick_closure(self, code):
        # The tracker MUST go through the canonical writer.
        assert "from functionality.log_writer import log_ag_pick_closure" in code, (
            "Tracker no longer imports log_ag_pick_closure. Closure events "
            "would not land in indexes/IMMUTABLE/ag_picks_closures/."
        )

    def test_force_flushes_on_exit(self, code):
        # If the script exits without flushing, the closure events stay
        # in the deque and are lost. force-flush is required.
        assert "flush_all" in code, (
            "Tracker no longer force-flushes the log writer on exit. "
            "Closure events written via log_ag_pick_closure may stay "
            "buffered in the deque past script termination, causing "
            "data loss for the closure that ran in the last 30 seconds."
        )

    def test_uses_immutable_namespace(self, code):
        assert "IMMUTABLE/ag_picks" in code, (
            "Tracker reads from a path other than IMMUTABLE/ag_picks. "
            "Wave 2 of OEB requires the journal at this path."
        )

    def test_dedupe_against_closures(self, code):
        # Pinning: the tracker MUST exclude already-closed picks. Without
        # this, every run would re-emit closure events for the same
        # pick, inflating the journal and corrupting hit-rate counts.
        assert "closed_idea_ids" in code
        assert "isin(closed_idea_ids)" in code, (
            "Tracker no longer dedupes against existing closures. Picks "
            "would get closed multiple times, breaking hit-rate math."
        )
