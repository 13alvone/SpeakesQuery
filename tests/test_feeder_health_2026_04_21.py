#!/usr/bin/env python3
"""
Regression tests for the 2026-04-21 Feeder Health correctness pass.

User reported the Feeder Health modal showed the same misleading
"saved-search hasn't run recently (last: never, threshold: 48h)"
warning on 8 of 11 Daily Opportunity Brief feeders - even though those
feeders had fresh Parquet data AND were successfully returning rows
during AG dispatches. Root cause: the freshness check only read
``saved_search_history.db``, which is populated by the saved-search
cron only. AG dispatchers run the same queries on-demand and log to a
DIFFERENT store (``indexes/logs/search_runs/``), so feeders that are
alive-via-dispatcher looked dead-via-cron.

Additionally the reserved-picks feeder for the new pick-capture
pipeline showed the confusing message "No library script matches
subdirectory 'logs/ag_picks'" - technically correct but misleading
because that index is dispatcher-managed (populated by the AG
dispatcher itself, not by any ingestion task).

What this file pins:

1. **Fresh data = alive.** A feeder with ``last_data_epoch`` within the
   threshold is NOT flagged as dead, even when ``last_search_run_age_hours``
   is ``None`` (saved-search cron never fired).
2. **Stale data = dead.** Explicitly verify the dead-feeder flag still
   fires when the parquet data is old AND the saved-search never ran.
3. **Dispatcher-managed subdirs** (``logs/ag_picks``) show a clear
   message about being populated by the AG dispatcher, not a confusing
   "no library script matches" string. Day-1 empty → "pending", later
   with data → "live".
4. **Kalshi arb filter relaxed** - the shipped default query uses
   ``divergence_pct >= 3.0 AND match_confidence >= 70.0`` (not the
   prior 5.0 / 75.0 strict filter with opportunity_strength IN check).
5. **Options pro watchlist ≤ 10 tickers** - reduced so the task fits
   under the 120s default script timeout.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =====================================================================
# Part 1: Dead-feeder detection uses data freshness as primary signal
# =====================================================================

def _make_fake_parquet(tmp_path: Path, subdir: str, mtime_epoch: float) -> Path:
    """Write a 1-byte 'parquet' file at the given subdir + set its mtime."""
    full = tmp_path / subdir
    full.mkdir(parents=True, exist_ok=True)
    f = full / "test.parquet"
    f.write_bytes(b"0")
    os.utime(f, (mtime_epoch, mtime_epoch))
    return f


class TestFreshnessSourcePrimary:
    """``is_dead_feeder`` is based on data freshness (parquet mtime),
    NOT on ``saved_search_history.db`` alone."""

    def test_fresh_data_never_ran_saved_search_is_not_dead(self, tmp_path):
        """This is the EXACT scenario the user reported: parquet data
        present and fresh, but saved_search_history has no entry. Before
        the 2026-04-21 fix this was flagged dead with "last: never" -
        misleading since the AG dispatcher runs these on-demand and
        the data is fine."""
        from alert_groups.feeder_status import resolve_feeder

        # Fresh parquet - 1 hour old.
        _make_fake_parquet(tmp_path, "crypto/coingecko_volume_anomalies_pro",
                           time.time() - 3600)

        def loader(name):
            return {
                "name": name,
                "query": 'index="indexes/crypto/coingecko_volume_anomalies_pro/*.parquet" | head 1',
            }

        library_scripts = [{
            "id": "coingecko_volume_anomaly_detector_pro",
            "suggested_subdirectory": "crypto/coingecko_volume_anomalies_pro",
            "requires_credentials": [],
        }]
        scheduled_tasks = [{
            "id": 1,
            "library_script_id": "coingecko_volume_anomaly_detector_pro",
            "subdirectory": "crypto/coingecko_volume_anomalies_pro",
            "disabled": False,
        }]

        fs = resolve_feeder(
            "dob_crypto_anomalies",
            saved_search_loader=loader,
            library_scripts=library_scripts,
            scheduled_tasks=scheduled_tasks,
            credentials_lister=lambda _tid: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "live"
        assert fs.is_dead_feeder is False, (
            "Fresh data should NOT trigger dead-feeder flag. "
            f"Message: {fs.message!r}"
        )
        assert "data is stale" not in fs.message
        assert "hasn't run recently" not in fs.message

    def test_stale_data_with_no_cron_history_is_dead(self, tmp_path):
        """Both signals absent - correctly flagged dead."""
        from alert_groups.feeder_status import resolve_feeder

        # Very old parquet - 200 hours ago, well past 48h threshold.
        _make_fake_parquet(tmp_path, "crypto/coingecko_volume_anomalies_pro",
                           time.time() - 200 * 3600)

        def loader(name):
            return {
                "name": name,
                "query": 'index="indexes/crypto/coingecko_volume_anomalies_pro/*.parquet" | head 1',
            }

        library_scripts = [{
            "id": "coingecko_volume_anomaly_detector_pro",
            "suggested_subdirectory": "crypto/coingecko_volume_anomalies_pro",
            "requires_credentials": [],
        }]
        scheduled_tasks = [{
            "id": 1,
            "library_script_id": "coingecko_volume_anomaly_detector_pro",
            "subdirectory": "crypto/coingecko_volume_anomalies_pro",
            "disabled": False,
        }]

        fs = resolve_feeder(
            "dob_crypto_anomalies",
            saved_search_loader=loader,
            library_scripts=library_scripts,
            scheduled_tasks=scheduled_tasks,
            credentials_lister=lambda _tid: [],
            indexes_root=tmp_path,
        )
        assert fs.is_dead_feeder is True
        # Message should name the data-freshness reason, not the
        # saved-search-cron-never-ran red herring.
        assert "data is stale" in fs.message
        assert "ingestion task" in fs.message.lower()

    def test_message_does_not_blame_saved_search_cron_for_fresh_data(self, tmp_path):
        """Regression guard on the specific phrasing that confused the
        user. The phrase "saved-search hasn't run recently" should NOT
        appear when data is fresh."""
        from alert_groups.feeder_status import resolve_feeder

        _make_fake_parquet(tmp_path, "equities/earnings_calendar",
                           time.time() - 7200)  # 2h old

        def loader(name):
            return {
                "name": name,
                "query": 'index="indexes/equities/earnings_calendar/*.parquet" | head 1',
            }

        library_scripts = [{
            "id": "earnings_calendar_72h",
            "suggested_subdirectory": "equities/earnings_calendar",
            "requires_credentials": [],
        }]
        scheduled_tasks = [{
            "id": 2,
            "library_script_id": "earnings_calendar_72h",
            "subdirectory": "equities/earnings_calendar",
            "disabled": False,
        }]

        fs = resolve_feeder(
            "dob_earnings_72h",
            saved_search_loader=loader,
            library_scripts=library_scripts,
            scheduled_tasks=scheduled_tasks,
            credentials_lister=lambda _tid: [],
            indexes_root=tmp_path,
        )
        assert "saved-search hasn't run recently" not in fs.message, (
            "The misleading phrasing regressed - fresh parquet data "
            f"should not trigger it. Message: {fs.message!r}"
        )


# =====================================================================
# Part 2: Dispatcher-managed subdirs (ag_picks)
# =====================================================================

class TestDispatcherManagedSubdirs:

    def test_empty_ag_picks_shows_pending_with_clear_message(self, tmp_path):
        """Day-1: no data in indexes/logs/ag_picks/ yet."""
        from alert_groups.feeder_status import resolve_feeder

        def loader(name):
            return {
                "name": name,
                "query": (
                    'index="indexes/logs/ag_picks/*.parquet" '
                    '| where alert_group="daily_opportunity_brief" | head 25'
                ),
            }

        fs = resolve_feeder(
            "dob_reserved_picks",
            saved_search_loader=loader,
            library_scripts=[],  # no library script for logs/ag_picks
            scheduled_tasks=[],
            credentials_lister=lambda _tid: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "pending"
        assert "dispatcher-managed" in fs.message.lower() \
            or "alert group dispatcher" in fs.message.lower()
        # Should NOT say "no library script matches" - that was the
        # confusing message the user reported.
        assert "no library script matches" not in fs.message.lower()

    def test_populated_ag_picks_shows_live(self, tmp_path):
        """Once dispatches have captured picks, the feeder is live."""
        from alert_groups.feeder_status import resolve_feeder

        _make_fake_parquet(tmp_path, "logs/ag_picks", time.time() - 1800)

        def loader(name):
            return {
                "name": name,
                "query": 'index="indexes/logs/ag_picks/*.parquet" | head 25',
            }

        fs = resolve_feeder(
            "dob_reserved_picks",
            saved_search_loader=loader,
            library_scripts=[],
            scheduled_tasks=[],
            credentials_lister=lambda _tid: [],
            indexes_root=tmp_path,
        )
        assert fs.state == "live"
        assert "dispatcher-managed" in fs.message.lower() \
            or "alert group dispatcher" in fs.message.lower()


# =====================================================================
# Part 3: Kalshi arb filter is relaxed
# =====================================================================

class TestKalshiArbFilterRelaxed:

    def test_kalshi_filter_is_3pct_not_5pct(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_saved_searches" \
            / "dob_kalshi_poly_arb.yaml"
        spec = yaml.safe_load(p.read_text())
        q = spec["query"]
        assert "divergence_pct >= 3.0" in q, (
            "Kalshi arb filter should be >= 3.0 (was 5.0 pre-2026-04-21). "
            "That minimum was too strict for a rare-event feed - most "
            "days returned 0 rows for backtesting."
        )
        assert "divergence_pct >= 5.0" not in q, (
            "Old 5% threshold is still present. Relax to 3%."
        )

    def test_kalshi_filter_drops_opportunity_strength_IN_check(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_saved_searches" \
            / "dob_kalshi_poly_arb.yaml"
        spec = yaml.safe_load(p.read_text())
        q = spec["query"]
        # The opportunity_strength field is COMPUTED from divergence_pct
        # by the ingestion script (STRONG >= 15%, MODERATE >= 8%, WEAK
        # below). Gating on STRONG/MODERATE duplicates the divergence
        # gate and doubly-excludes WEAK (3-8%). We drop that check.
        assert 'opportunity_strength IN' not in q, (
            "opportunity_strength IN filter still present - it "
            "duplicates the divergence threshold and shuts out "
            "WEAK matches (3-8% divergence) which may still be "
            "useful for backtest. Drop it."
        )

    def test_kalshi_match_confidence_relaxed_to_70(self):
        import yaml
        p = Path(PROJECT_ROOT) / "default_saved_searches" \
            / "dob_kalshi_poly_arb.yaml"
        spec = yaml.safe_load(p.read_text())
        q = spec["query"]
        assert "match_confidence >= 70.0" in q, (
            "Match confidence should be >= 70 (was 75.0 pre-2026-04-21)."
        )


# =====================================================================
# Part 4: Options pro watchlist fits within Finnhub free-tier budget
# =====================================================================

class TestOptionsWatchlistSize:

    def test_options_pro_watchlist_size_reasonable(self):
        """Watchlist size sanity bound under Massive.com Options Starter.

        Evolution of this cap:
          * Yahoo era: ≤10 (tarpit / 100% 429 lockout).
          * Finnhub era: ≤30 (free tier 60 calls/min, 2 calls per ticker).
          * Massive era (2026-04-25): unlimited calls; cap raised to ≤80
            as a sanity ceiling so a typo doesn't accidentally add 500
            tickers and hammer the per-script timeout budget.
          * 2026-04-26 (Options Edge Brief Wave 1): expanded from 15 to
            40 - mega-caps + sector ETFs + vol/leveraged ETFs + high-vol
            movers - to match the OEB watchlist universe.
        """
        p = Path(PROJECT_ROOT) / "script_library" / "scripts" \
            / "options_unusual_activity_pro.json"
        spec = json.loads(p.read_text())
        code = spec["code"]
        # Extract the TICKERS list
        import re
        m = re.search(r"TICKERS\s*=\s*\[([^\]]+)\]", code)
        assert m is not None, "TICKERS list not found in options pro script"
        tickers = re.findall(r"['\"]([A-Z]+)['\"]", m.group(1))
        assert 1 <= len(tickers) <= 80, (
            f"Options pro watchlist has {len(tickers)} tickers; must be "
            f"≤80 as a sanity ceiling. Massive.com Starter has unlimited "
            f"calls but the per-script timeout (180-300s) limits how many "
            f"tickers can complete in one run. Split the watchlist into "
            f"multiple scripts if the workload legitimately needs more."
        )

    def test_options_pro_watchlist_keeps_most_liquid_tickers(self):
        """SPY, QQQ, AAPL, MSFT, NVDA, TSLA - the minimum useful set."""
        p = Path(PROJECT_ROOT) / "script_library" / "scripts" \
            / "options_unusual_activity_pro.json"
        spec = json.loads(p.read_text())
        code = spec["code"]
        import re
        m = re.search(r"TICKERS\s*=\s*\[([^\]]+)\]", code)
        tickers = set(re.findall(r"['\"]([A-Z]+)['\"]", m.group(1)))
        essentials = {"SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"}
        missing = essentials - tickers
        assert not missing, (
            f"Options watchlist dropped essential liquid tickers: {missing}"
        )
