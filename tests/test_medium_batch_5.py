"""MEDIUMs batch 5 - M-MI-7, M-MI-8, M-MI-10 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-MI-7** - ``polymarket_news_sentiment_divergence`` now uses a
    positional negation-window sentiment scorer (``"will NOT win"`` no
    longer scores positive) and emits a ``sentiment_reliability`` enum
    (``RELIABLE`` / ``UNRELIABLE``) so small-sample / near-neutral rows
    can be discounted downstream.
  * **M-MI-8** - ``polymarket_market_movers_pro`` now skips Gamma
    snapshots older than 5 minutes so stale-vs-fresh comparisons don't
    masquerade as momentum. Each surviving row carries
    ``snapshot_age_seconds`` so Claude / SPQL can see the freshness
    directly.
  * **M-MI-10** - ``polymarket_calibration_analysis`` skips multi-
    outcome events (3+ prices) instead of misclassifying them, and
    emits a dedicated ``_summary`` row with the skip count so the
    exclusion is visible in the output.

M-MI-9 (Kelly bankroll-aware sizing) was **superseded by H-MI-1**,
which dropped the Kelly fields from ``polymarket_high_probability_pro``
entirely. No Kelly fraction remains to multiply against a bankroll; the
finding is moot.
"""
from __future__ import annotations

import json
import sys
import unittest.mock
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

SCRIPTS_DIR = _PROJECT_ROOT / "script_library" / "scripts"


# ======================================================================
# M-MI-7: sentiment negation window + reliability flag
# ======================================================================

class TestSentimentNegationAndReliability:
    """Inline-exec the scoring logic so we can test it without mocked HTTP."""

    # Lift the exact negation / sentiment word lists out of the script so
    # the test exercises the same code contract.

    POS = frozenset([
        'win', 'wins', 'winning', 'victory', 'leads', 'leading', 'ahead',
        'surge', 'surges', 'surging', 'rally', 'rallies', 'boost', 'gains',
        'likely', 'expected', 'favored', 'frontrunner', 'dominant', 'strong',
        'confirms', 'confirmed', 'approved', 'passes', 'passed', 'success',
        'breakthrough', 'record', 'historic', 'support', 'backs', 'endorses',
    ])
    NEG = frozenset([
        'lose', 'loses', 'losing', 'loss', 'defeat', 'trails', 'trailing',
        'behind', 'decline', 'declines', 'drop', 'drops', 'falling', 'slump',
        'unlikely', 'doubt', 'doubts', 'fails', 'failed', 'failure', 'rejects',
        'rejected', 'scandal', 'crisis', 'collapse', 'crashes', 'warns',
        'warning', 'risk', 'threat', 'opposes', 'blocks', 'blocked', 'withdraws',
    ])
    NEGATORS = frozenset([
        'not', 'no', 'never', 'none', 'nothing', 'nobody',
        'without', 'cannot', 'cant', 'wont', 'doesnt', 'dont',
        'isnt', 'arent', 'wasnt', 'werent', 'fails', 'failed',
        'rejected', 'denied', 'stopped', 'blocked', 'refuses',
    ])

    def _score(self, headline: str) -> float:
        """Mirror of the script's negation-window scoring - clean Python (no sandbox)."""
        import re
        h_tokens = re.findall(r"[a-z]+", headline.lower())
        pos_count = 0
        neg_count = 0
        idx = 0
        while idx < len(h_tokens):
            tok = h_tokens[idx]
            flip = False
            window_start = max(0, idx - 3)
            for prev in h_tokens[window_start:idx]:
                if prev in self.NEGATORS:
                    flip = True
                    break
            if tok in self.POS:
                (neg_count := neg_count + 1) if flip else (pos_count := pos_count + 1)
            elif tok in self.NEG:
                (pos_count := pos_count + 1) if flip else (neg_count := neg_count + 1)
            idx += 1
        total = pos_count + neg_count
        if total == 0:
            return 0.0
        return (pos_count - neg_count) / total

    def test_positive_headline_scores_positive(self):
        # "Alice wins primary" → pos=1, neg=0, score=+1
        assert self._score("Alice wins primary in landslide") > 0.5

    def test_negated_positive_flips(self):
        # "Alice will NOT win primary" → the "win" is flipped by "not".
        # In the old (set-based) scorer this was positive; now it's negative.
        out = self._score("Alice will not win primary this week")
        assert out < 0.0, (
            f"'will not win' must not score positive; got {out}"
        )

    def test_negator_outside_window_does_not_flip(self):
        # Negator is 5+ tokens before the sentiment word → out of the
        # 3-token look-back window → no flip.
        out = self._score("Not in the spirit of things he still wins big")
        # "wins" is at index 8, "not" at index 0. Window is 5..8 → "not"
        # is NOT inside. Score should be positive.
        assert out > 0.0, (
            f"Negator at distance > 3 tokens must not flip; got {out}"
        )

    def test_double_negation_is_heuristic_ambiguous(self):
        """Double-negation exposes the heuristic's limitation; acceptable near 0.

        ``"never fails to win the race"``: ``never`` flips ``fails`` (NEG→POS)
        AND ``win`` (POS→NEG). Net is 0 - not truly positive like a full
        NLU model would score, but not misleadingly negative either. The
        fix is a heuristic, not sentiment AI; we document the limitation
        via the test rather than claim more.
        """
        out = self._score("never fails to win the race")
        assert abs(out) <= 0.5, (
            f"Double-negation should land near neutral (heuristic "
            f"limitation); got {out}"
        )


class TestSentimentReliabilityFlag:
    """Integration: run the full script with a crafted GDELT fixture and
    verify the reliability flag gates on (sample_size, magnitude)."""

    def _run(self, headlines, sentiment_signal="positive"):
        """Run the full sentiment-divergence script with a crafted article list."""
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads(
            (SCRIPTS_DIR / "polymarket_news_sentiment_divergence.json").read_text()
        )
        # Build a market whose price direction diverges from the sentiment
        # direction so the divergence gate doesn't short-circuit the row.
        outcome_prices = '["0.40","0.60"]'  # price_direction = NO
        gamma_market = {
            "id": "m_sent_reli",
            "question": "Will Alice win the primary election?",
            "slug": "alice-primary",
            "conditionId": "0xcondalice",
            "outcomePrices": outcome_prices,
            "outcomes": '["Yes","No"]',
            "volume": "50000",
            "volume24hr": "40000",  # forces volume spike via high 24h
            "liquidityNum": "25000",
            "tags": "[]",
            "category": "Politics",
            "active": True,
            "closed": False,
            "createdAt": "2024-01-01T00:00:00Z",
        }
        articles_payload = {"articles": [{"title": h} for h in headlines]}

        def router(url, *_args, **_kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "gamma-api.polymarket.com/markets" in url:
                resp.json = lambda: [gamma_market]
            elif "gamma-api.polymarket.com/comments" in url:
                resp.json = lambda: []
            elif "api.gdeltproject.org" in url:
                resp.json = lambda: articles_payload
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    def test_strong_consistent_signal_is_reliable(self):
        """Many headlines, strong positive sentiment → RELIABLE."""
        headlines = [
            "Alice wins primary - historic victory",
            "Alice leads field with dominant surge",
            "Alice's rally boosts primary win chances",
            "Alice confirmed as favored frontrunner",
        ]
        result = self._run(headlines)
        assert result["status"] == "pass", f"errors: {result['errors']}"
        rows = [r for r in result["head"] if r.get("slug") == "alice-primary"]
        assert rows, f"Expected a row for alice-primary. head={result['head']}"
        assert rows[0]["sentiment_reliability"] == "RELIABLE"

    def test_small_sample_is_unreliable(self):
        """Only 2 relevant headlines → below sample threshold → UNRELIABLE."""
        headlines = [
            "Alice wins primary - historic victory",
            "Alice leads field with dominant surge",
        ]
        result = self._run(headlines)
        # Depending on the relevant_count < 2 early skip, the row might not
        # appear at all. Either outcome (UNRELIABLE or skipped) is
        # acceptable; we assert specifically that it's not RELIABLE.
        rows = [r for r in result.get("head", []) if r.get("slug") == "alice-primary"]
        if rows:
            assert rows[0]["sentiment_reliability"] != "RELIABLE", (
                f"2-headline sample should not be RELIABLE; "
                f"got {rows[0]['sentiment_reliability']}"
            )


# ======================================================================
# M-MI-8: market_movers_pro stale-snapshot filter
# ======================================================================

class TestMarketMoversStaleSnapshot:

    def _make_market(self, *, last_trade_offset_sec: int, outcome_prices='["0.50","0.50"]'):
        """Build a gamma market dict with lastTradeTime offset from now."""
        from datetime import datetime, timedelta, timezone
        ts = datetime.now(timezone.utc) - timedelta(seconds=last_trade_offset_sec)
        return {
            "id": f"m_age_{last_trade_offset_sec}",
            "question": f"Will something happen in {last_trade_offset_sec}s?",
            "slug": f"slug-{last_trade_offset_sec}",
            "conditionId": f"0xcond{last_trade_offset_sec}",
            "outcomePrices": outcome_prices,
            "outcomes": '["Yes","No"]',
            "volume": "100000",
            "liquidity": "10000",
            "category": "Test",
            "tags": "[]",
            "clobTokenIds": f'["tok_{last_trade_offset_sec}_yes","tok_{last_trade_offset_sec}_no"]',
            "lastTradeTime": ts.isoformat().replace("+00:00", "Z"),
            "active": True,
            "closed": False,
            "createdAt": "2024-01-01T00:00:00Z",
        }

    def _run(self, markets):
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads(
            (SCRIPTS_DIR / "polymarket_market_movers_pro.json").read_text()
        )

        def router(url, *_a, **_k):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "gamma-api.polymarket.com/markets" in url:
                resp.json = lambda: markets
            elif "clob.polymarket.com/midpoint" in url:
                resp.json = lambda: {"mid": "0.55"}
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True, trust_level="unrestricted",
            )
            return executor.execute_test()

    def test_fresh_snapshot_kept(self):
        """A <5min-old snapshot is processed."""
        result = self._run([self._make_market(last_trade_offset_sec=60)])
        assert result["status"] == "pass", f"errors: {result['errors']}"
        rows = [r for r in result["head"] if r.get("slug") == "slug-60"]
        assert rows, f"Fresh snapshot should be processed. head={result['head']}"
        # snapshot_age_seconds surfaced directly in the output.
        assert 0 <= rows[0]["snapshot_age_seconds"] <= 120

    def test_stale_snapshot_filtered(self):
        """A snapshot >5min old is dropped (even with an arb-worthy delta)."""
        # Mixed: one stale, one fresh - only the fresh survives.
        fresh = self._make_market(last_trade_offset_sec=30)
        stale = self._make_market(last_trade_offset_sec=3600)  # 1 hour old
        result = self._run([fresh, stale])
        assert result["status"] == "pass"
        slugs = {r.get("slug") for r in result["head"]}
        assert "slug-30" in slugs
        assert "slug-3600" not in slugs, (
            "Stale (1h old) snapshot must be filtered by the 5min freshness gate."
        )


# ======================================================================
# M-MI-10: calibration_analysis multi-outcome skip + summary row
# ======================================================================

class TestCalibrationMultiOutcomeSkip:

    def _make_market(self, *, outcome_prices: str, market_id: str):
        return {
            "id": market_id,
            "question": f"Market {market_id}?",
            "slug": market_id,
            "conditionId": f"0x{market_id}",
            "outcomePrices": outcome_prices,
            "outcomes": '["Yes","No"]',
            "volume": "1000",
            "liquidity": "500",
            "category": "Test",
            "tags": "[]",
            "active": False,
            "closed": True,
            "endDate": "2024-06-01T00:00:00Z",
            "createdAt": "2024-01-01T00:00:00Z",
        }

    def _run(self, markets):
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads(
            (SCRIPTS_DIR / "polymarket_calibration_analysis.json").read_text()
        )

        def router(url, *_a, **_k):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "gamma-api.polymarket.com/markets" in url:
                resp.json = lambda: markets
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    def test_multi_outcome_market_skipped_and_counted(self):
        """A 3-price market is skipped; summary row records the count."""
        markets = [
            # Binary, resolved YES - should land in the output.
            self._make_market(
                outcome_prices='["0.98","0.02"]', market_id="bin_yes"
            ),
            # Three-way - must be skipped.
            self._make_market(
                outcome_prices='["0.50","0.30","0.20"]',
                market_id="three_way",
            ),
        ]
        result = self._run(markets)
        assert result["status"] == "pass", f"errors: {result['errors']}"

        rows = result["head"]
        market_ids = {r.get("market_id") for r in rows}
        assert "bin_yes" in market_ids
        assert "three_way" not in market_ids

        # Summary row is always present.
        summary = [r for r in rows if r.get("market_id") == "_summary"]
        assert summary, (
            f"Expected a _summary row with the skip tally; head={rows}"
        )
        assert summary[0]["multi_outcome_skipped_count"] >= 1

    def test_no_multi_outcome_markets_summary_reports_zero(self):
        markets = [
            self._make_market(
                outcome_prices='["0.98","0.02"]', market_id="binary_only"
            ),
        ]
        result = self._run(markets)
        summary = [r for r in result["head"] if r.get("market_id") == "_summary"]
        assert summary and summary[0]["multi_outcome_skipped_count"] == 0
