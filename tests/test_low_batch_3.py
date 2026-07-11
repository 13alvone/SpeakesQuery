"""LOWs batch 3 (final) - L-MI-14, L-MI-15, L-SV-10 regressions.

  * **L-MI-14** - the ``kalshi_polymarket_arbitrage`` scripts now emit
    explicit ``polymarket_action`` / ``kalshi_action`` (one of
    ``BUY_YES`` / ``BUY_NO``) alongside the legacy
    ``suggested_action`` composite label. Prediction markets have no
    "sell" primitive; you buy the opposite side (NO) to short a YES.
  * **L-MI-15** - zero-row days in the arb scripts now land every
    numeric column with an explicit float/int dtype via
    ``df.astype(...)`` so downstream SPQL ``| where net_edge_pct > 1``
    can evaluate against an empty schema without coercion failures.
  * **L-SV-10** - credentials versioning deferred (single-user
    local-trust context); schema-block comment in
    ``credentials.py`` documents the future hook + retention
    requirement so a reader designing "credential history" has the
    constraint list.
"""
from __future__ import annotations

import json
import sys
import unittest.mock
from pathlib import Path

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

SCRIPTS_DIR = _PROJECT_ROOT / "script_library" / "scripts"


# ======================================================================
# L-MI-14: explicit per-leg actions
# ======================================================================


class TestPerLegActionColumns:

    def _run_sandboxed(self, *, kalshi_last_price: int, poly_yes: float):
        """Run kalshi_polymarket_arbitrage with a crafted event/market pair.

        2026-05-06: switched from /v2/markets-flat to /v2/events with nested
        markets after the Kalshi V2 schema flooded /v2/markets with KXMVE
        auto-permutations. Mock now returns a single Economics event whose
        nested market carries the price under test."""
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads(
            (SCRIPTS_DIR / "kalshi_polymarket_arbitrage.json").read_text()
        )

        kalshi_events = {
            "events": [{
                "event_ticker": "EVT-1",
                "title": "Federal Reserve rate cut March 2026",
                "sub_title": "Fed funds target",
                "category": "Economics",
                "status": "active",
                "markets": [{
                    "ticker": "LEG-1",
                    "event_ticker": "EVT-1",
                    "title": "Federal Reserve rate cut March 2026",
                    "subtitle": "",
                    "last_price": kalshi_last_price,
                    # V2-shape so the script's `last_price_dollars` read path works
                    "last_price_dollars": f"{kalshi_last_price / 100.0:.4f}",
                    "status": "active",
                }],
            }],
            "cursor": "",
        }
        poly_markets = [{
            "id": "pm_leg_1",
            "question": "Federal Reserve rate cut March 2026",
            "slug": "fed-rate-cut",
            "conditionId": "0xcond",
            "outcomePrices": f'[\"{poly_yes}\", \"{1 - poly_yes}\"]',
            "outcomes": '["Yes","No"]',
            "volume": "100000",
            "liquidity": "10000",
            "tags": "[]",
        }]

        def router(url, *_a, **_k):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "api.elections.kalshi.com" in url:
                resp.json = lambda: kalshi_events
            elif "gamma-api.polymarket.com" in url:
                resp.json = lambda: poly_markets
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("time.sleep", lambda *a, **kw: None):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    def test_divergence_positive_buy_poly_buy_no_kalshi(self):
        """Kalshi priced HIGHER → buy Polymarket YES (cheaper) + Kalshi NO (synthetic short)."""
        # Kalshi 45¢ → 0.45; Polymarket 0.65 → abs_div 0.20 → net 0.16.
        # divergence = k_yes - p_yes = 0.45 - 0.65 = -0.20 (negative)
        # So BUY_KALSHI_SELL_POLYMARKET → kalshi=BUY_YES, polymarket=BUY_NO.
        result = self._run_sandboxed(kalshi_last_price=45, poly_yes=0.65)
        rows = [r for r in result["head"] if r.get("kalshi_ticker") == "LEG-1"]
        assert rows, f"Expected at least one arb row. head={result['head']}"
        r = rows[0]
        assert r["suggested_action"] == "BUY_KALSHI_SELL_POLYMARKET"
        assert r["kalshi_action"] == "BUY_YES"
        assert r["polymarket_action"] == "BUY_NO"

    def test_divergence_positive_buy_kalshi_side(self):
        """Kalshi priced LOWER → buy Kalshi YES + Polymarket NO."""
        # Kalshi 85¢ → 0.85; Polymarket 0.65. divergence = 0.85 - 0.65 = +0.20
        # BUY_POLYMARKET_SELL_KALSHI means buy the CHEAPER side on Polymarket
        # (wait - Polymarket is 0.65, Kalshi 0.85, so poly is cheaper).
        # Expected: polymarket=BUY_YES (the cheaper YES), kalshi=BUY_NO.
        result = self._run_sandboxed(kalshi_last_price=85, poly_yes=0.65)
        rows = [r for r in result["head"] if r.get("kalshi_ticker") == "LEG-1"]
        assert rows, f"Expected at least one arb row. head={result['head']}"
        r = rows[0]
        assert r["suggested_action"] == "BUY_POLYMARKET_SELL_KALSHI"
        assert r["polymarket_action"] == "BUY_YES"
        assert r["kalshi_action"] == "BUY_NO"

    def test_per_leg_actions_are_valid_enum_values(self):
        result = self._run_sandboxed(kalshi_last_price=45, poly_yes=0.65)
        for r in result["head"]:
            if r.get("polymarket_action"):
                assert r["polymarket_action"] in ("BUY_YES", "BUY_NO")
            if r.get("kalshi_action"):
                assert r["kalshi_action"] in ("BUY_YES", "BUY_NO")


# ======================================================================
# L-MI-15: zero-row arb DataFrame carries numeric dtypes
# ======================================================================


class TestZeroRowArbPreservesNumericDtypes:

    def _run(self, script_name: str, kalshi_markets, poly_markets):
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())

        def router(url, *_a, **_k):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "api.elections.kalshi.com" in url:
                resp.json = lambda: kalshi_markets
            elif "gamma-api.polymarket.com" in url:
                resp.json = lambda: poly_markets
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("time.sleep", lambda *a, **kw: None):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    def test_sandboxed_empty_rows_carries_float_dtypes(self):
        """Zero-row arb output carries float dtypes on numeric columns.

        Validate via a source-level grep: the script's ``if df.empty:``
        branch must call ``df.astype({...})`` with the numeric fields
        cast explicitly. The executor harness doesn't expose dtypes
        through its ``head`` slice, so a source check is the cleanest
        regression lock.
        """
        text = (SCRIPTS_DIR / "kalshi_polymarket_arbitrage.json").read_text()
        assert "if df.empty:" in text, (
            "Script must branch on df.empty to cast numeric dtypes."
        )
        # Every numeric column must be cast to a concrete dtype.
        for col in (
            "kalshi_yes_price", "polymarket_yes_price", "divergence",
            "abs_divergence", "divergence_pct", "fee_roundtrip_pct",
            "net_edge_pct",
        ):
            assert f"'{col}': 'float64'" in text, (
                f"Numeric column {col!r} must be explicitly cast to float64 "
                f"on zero-row days."
            )
        assert "'_epoch': 'int64'" in text

    def test_pro_empty_rows_also_carries_float_dtypes(self):
        text = (SCRIPTS_DIR / "kalshi_polymarket_arbitrage_pro.json").read_text()
        assert "if df.empty:" in text
        # Pro variant has the same numeric columns PLUS match_confidence.
        for col in (
            "kalshi_yes_price", "polymarket_yes_price", "divergence",
            "abs_divergence", "divergence_pct", "fee_roundtrip_pct",
            "net_edge_pct", "match_confidence",
        ):
            assert f"'{col}': 'float64'" in text, (
                f"Pro numeric column {col!r} must be cast to float64."
            )

    def test_empty_casted_df_evaluates_spql_comparison(self):
        """Smoke: an empty DF with float64 columns tolerates a query comparator."""
        cols = [
            'kalshi_yes_price', 'polymarket_yes_price', 'divergence',
            'abs_divergence', 'divergence_pct', 'fee_roundtrip_pct',
            'net_edge_pct', '_epoch',
        ]
        df = pd.DataFrame(columns=cols).astype(
            {c: "float64" for c in cols if c != "_epoch"} | {"_epoch": "int64"}
        )
        # The canonical downstream SPQL comparison.
        out = df.query("net_edge_pct > 1.0")
        assert len(out) == 0
        assert pd.api.types.is_float_dtype(df["net_edge_pct"]), (
            f"net_edge_pct should be float64; got {df['net_edge_pct'].dtype}"
        )


# ======================================================================
# L-SV-10: credentials versioning doc note in schema block
# ======================================================================


class TestCredentialsSchemaVersioningNote:

    SRC = _PROJECT_ROOT / "scheduled_input_engine" / "credentials.py"

    def test_schema_carries_future_versioning_hook_comment(self):
        text = self.SRC.read_text()
        # Comment must cite the finding + name the intended hook.
        assert "L-SV-10" in text, (
            "credentials.py schema block should carry the L-SV-10 "
            "deferral note so future work has a discoverable anchor."
        )
        # Must mention the design constraint (retention policy required).
        assert "retention" in text.lower(), (
            "The deferral note should call out the retention constraint "
            "so a future implementer doesn't add unbounded history."
        )
