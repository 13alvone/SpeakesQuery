#!/usr/bin/env python3
"""
Script Library Smoke Tests - Live API Contract Validation
──────────────────────────────────────────────────────────
Hits every real API endpoint used by the no-auth ingestion scripts
and validates HTTP status + response shape (expected keys present).

These tests make LIVE HTTP calls.  They are:
  - Marked @pytest.mark.smoke - never run automatically
  - Run explicitly:  pytest -m smoke -v
  - Network-dependent, may fail on rate limits or API downtime

Purpose: catch API contract changes (wrong params → 422, renamed
fields, removed endpoints) that mocked tests cannot detect.
"""

import json
import os
import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

TIMEOUT = 30

pytestmark = pytest.mark.smoke


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def assert_status_ok(resp, context=""):
    """Assert 2xx status and return parsed JSON."""
    assert resp.status_code == 200, (
        f"{context}: expected 200, got {resp.status_code}. "
        f"Body: {resp.text[:500]}"
    )
    return resp.json()


def assert_list_response(data, min_items=1, context=""):
    """Assert response is a non-empty list."""
    assert isinstance(data, list), (
        f"{context}: expected list, got {type(data).__name__}"
    )
    assert len(data) >= min_items, (
        f"{context}: expected >= {min_items} items, got {len(data)}"
    )


def assert_keys_present(obj, keys, context=""):
    """Assert all expected keys are present in a dict."""
    missing = [k for k in keys if k not in obj]
    assert not missing, (
        f"{context}: missing keys {missing}. Got: {sorted(obj.keys())}"
    )


# ═══════════════════════════════════════════════════════════════════
# Fixtures - fetch live data needed by downstream tests
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def gamma_market():
    """Fetch a single active market from Gamma API.

    Returns the full market dict. Provides conditionId and clobTokenIds
    for downstream CLOB / Data API tests.
    """
    resp = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"limit": 5, "offset": 0, "active": "true", "closed": "false"},
        timeout=TIMEOUT,
    )
    data = assert_status_ok(resp, "fixture:gamma_market")
    assert_list_response(data, min_items=1, context="fixture:gamma_market")
    # Find a market with both conditionId and clobTokenIds
    for m in data:
        condition_id = m.get("conditionId", "")
        token_ids = m.get("clobTokenIds", "[]")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if condition_id and token_ids:
            m["_parsed_token_ids"] = token_ids
            return m
    pytest.skip("No active market found with conditionId and clobTokenIds")


@pytest.fixture(scope="module")
def gamma_event():
    """Fetch a single active event from Gamma API.

    Returns the full event dict. Provides event id for comment tests.
    """
    resp = requests.get(
        "https://gamma-api.polymarket.com/events",
        params={"limit": 5, "offset": 0, "active": "true", "closed": "false"},
        timeout=TIMEOUT,
    )
    data = assert_status_ok(resp, "fixture:gamma_event")
    assert_list_response(data, min_items=1, context="fixture:gamma_event")
    return data[0]


# ═══════════════════════════════════════════════════════════════════
# Gamma API - gamma-api.polymarket.com
# ═══════════════════════════════════════════════════════════════════

class TestGammaAPI:
    """Live contract tests for the Polymarket Gamma API."""

    MARKET_KEYS = ["id", "question", "slug", "conditionId", "outcomePrices", "volume"]

    def test_markets_active(self):
        """GET /markets?active=true - used by 7+ scripts."""
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 5, "offset": 0, "active": "true", "closed": "false"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/markets?active=true")
        assert_list_response(data, context="/markets?active=true")
        assert_keys_present(data[0], self.MARKET_KEYS, "/markets[0]")

    def test_markets_closed(self):
        """GET /markets?closed=true - used by resolved_markets, calibration_analysis."""
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 5, "offset": 0, "active": "false", "closed": "true"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/markets?closed=true")
        assert_list_response(data, context="/markets?closed=true")
        assert_keys_present(data[0], self.MARKET_KEYS, "/markets[0] closed")

    def test_markets_sorted_by_volume(self):
        """GET /markets?order=volume - used by open_interest, recent_trades, etc."""
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 5, "offset": 0, "active": "true", "order": "volume", "ascending": "false"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/markets?order=volume")
        assert_list_response(data, context="/markets?order=volume")

    def test_markets_sorted_by_created(self):
        """GET /markets?order=createdAt - used by new_markets."""
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 5, "offset": 0, "active": "true", "closed": "false", "order": "createdAt", "ascending": "false"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/markets?order=createdAt")
        assert_list_response(data, context="/markets?order=createdAt")

    def test_events_active(self):
        """GET /events?active=true - used by events_catalog, arbitrage, cross_market, comments."""
        resp = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"limit": 5, "offset": 0, "active": "true", "closed": "false"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/events?active=true")
        assert_list_response(data, context="/events?active=true")
        event = data[0]
        assert_keys_present(event, ["id", "title", "slug"], "/events[0]")
        # Events should have nested markets
        assert "markets" in event, "/events[0] missing 'markets' key"

    def test_comments_with_entity(self, gamma_event):
        """GET /comments?parent_entity_id=...&parent_entity_type=Event - used by comments_sentiment."""
        event_id = gamma_event.get("id", "")
        assert event_id, "gamma_event fixture missing id"
        resp = requests.get(
            "https://gamma-api.polymarket.com/comments",
            params={
                "parent_entity_id": event_id,
                "parent_entity_type": "Event",
                "limit": 5,
                "order": "createdAt",
                "ascending": "false",
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, f"/comments?entity={event_id}")
        # Comments may be empty for some events - that's OK
        if isinstance(data, list) and len(data) > 0:
            comment = data[0]
            assert_keys_present(
                comment,
                ["id", "body", "createdAt", "userAddress"],
                "/comments[0]",
            )

    def test_market_response_field_types(self, gamma_market):
        """Validate field types/shapes that scripts depend on for parsing."""
        m = gamma_market
        # outcomePrices should be a JSON-encoded string or a list
        prices = m.get("outcomePrices", "")
        if isinstance(prices, str):
            parsed = json.loads(prices)
            assert isinstance(parsed, list), "outcomePrices should parse to a list"
            assert len(parsed) >= 2, "outcomePrices should have at least 2 elements"
        # clobTokenIds should be a JSON-encoded string or a list
        tokens = m.get("clobTokenIds", "")
        if isinstance(tokens, str):
            parsed = json.loads(tokens)
            assert isinstance(parsed, list), "clobTokenIds should parse to a list"
            assert len(parsed) >= 1, "clobTokenIds should have at least 1 token"


# ═══════════════════════════════════════════════════════════════════
# Data API - data-api.polymarket.com
# ═══════════════════════════════════════════════════════════════════

class TestDataAPI:
    """Live contract tests for the Polymarket Data API."""

    def test_leaderboard(self):
        """GET /v1/leaderboard - used by polymarket_leaderboard."""
        resp = requests.get(
            "https://data-api.polymarket.com/v1/leaderboard",
            params={"limit": 5, "offset": 0},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/v1/leaderboard")
        # Response is a list of leader entries
        leaders = data
        if isinstance(data, dict):
            leaders = data.get("data", data.get("leaderboard", data.get("results", [])))
        assert_list_response(leaders, context="/v1/leaderboard")
        entry = leaders[0]
        assert "proxyWallet" in entry, f"/v1/leaderboard[0] missing proxyWallet. Keys: {sorted(entry.keys())}"
        assert "pnl" in entry, f"/v1/leaderboard[0] missing pnl. Keys: {sorted(entry.keys())}"

    def test_trades(self, gamma_market):
        """GET /trades?conditionId=... - used by polymarket_recent_trades."""
        condition_id = gamma_market.get("conditionId", "")
        assert condition_id, "gamma_market fixture missing conditionId"
        resp = requests.get(
            "https://data-api.polymarket.com/trades",
            params={"conditionId": condition_id, "limit": 5},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, f"/trades?conditionId={condition_id[:12]}...")
        assert isinstance(data, list), f"/trades expected list, got {type(data).__name__}"
        if data:
            entry = data[0]
            assert "proxyWallet" in entry, f"/trades[0] missing proxyWallet. Keys: {sorted(entry.keys())}"
            assert "side" in entry, f"/trades[0] missing side. Keys: {sorted(entry.keys())}"

    def test_holders(self, gamma_market):
        """GET /holders?market=... - used by polymarket_whale_tracker."""
        condition_id = gamma_market.get("conditionId", "")
        assert condition_id, "gamma_market fixture missing conditionId"
        resp = requests.get(
            "https://data-api.polymarket.com/holders",
            params={"market": condition_id, "limit": 5},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, f"/holders?market={condition_id[:12]}...")
        holders = data
        if isinstance(data, dict):
            holders = data.get("data", data.get("holders", []))
        assert isinstance(holders, list), f"/holders expected list, got {type(holders).__name__}"


# ═══════════════════════════════════════════════════════════════════
# CLOB API - clob.polymarket.com
# ═══════════════════════════════════════════════════════════════════

class TestClobAPI:
    """Live contract tests for the Polymarket CLOB API."""

    def _get_yes_token(self, gamma_market):
        tokens = gamma_market.get("_parsed_token_ids", [])
        assert tokens, "gamma_market fixture missing parsed token IDs"
        return tokens[0]

    def test_book(self, gamma_market):
        """GET /book?token_id=... - used by polymarket_orderbook_depth."""
        token_id = self._get_yes_token(gamma_market)
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/book")
        assert_keys_present(data, ["bids", "asks"], "/book")
        assert isinstance(data["bids"], list), "/book bids should be a list"
        assert isinstance(data["asks"], list), "/book asks should be a list"

    def test_midpoint(self, gamma_market):
        """GET /midpoint?token_id=... - used by polymarket_market_movers."""
        token_id = self._get_yes_token(gamma_market)
        resp = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": token_id},
            timeout=15,
        )
        data = assert_status_ok(resp, "/midpoint")
        has_mid = any(k in data for k in ["mid", "midpoint"])
        assert has_mid, f"/midpoint missing 'mid' key. Keys: {sorted(data.keys())}"

    def test_spread(self, gamma_market):
        """GET /spread?token_id=... - used by polymarket_liquidity_gaps."""
        token_id = self._get_yes_token(gamma_market)
        resp = requests.get(
            "https://clob.polymarket.com/spread",
            params={"token_id": token_id},
            timeout=15,
        )
        data = assert_status_ok(resp, "/spread")
        assert "spread" in data, f"/spread missing 'spread' key. Keys: {sorted(data.keys())}"

    def test_price(self, gamma_market):
        """GET /price?token_id=...&side=buy - used by polymarket_liquidity_gaps."""
        token_id = self._get_yes_token(gamma_market)
        resp = requests.get(
            "https://clob.polymarket.com/price",
            params={"token_id": token_id, "side": "buy"},
            timeout=15,
        )
        data = assert_status_ok(resp, "/price")
        assert "price" in data, f"/price missing 'price' key. Keys: {sorted(data.keys())}"

    def test_prices_history(self, gamma_market):
        """GET /prices-history?market=...&interval=max&fidelity=60 - used by polymarket_price_history."""
        token_id = self._get_yes_token(gamma_market)
        resp = requests.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": 60},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "/prices-history")
        # Response is a dict with 'history' key containing list of {t, p}
        history = data
        if isinstance(data, dict):
            history = data.get("history", data.get("data", []))
        assert isinstance(history, list), f"/prices-history expected list, got {type(history).__name__}"


# ═══════════════════════════════════════════════════════════════════
# Original APIs (non-Polymarket)
# ═══════════════════════════════════════════════════════════════════

class TestOriginalAPIs:
    """Live contract tests for non-Polymarket script library APIs."""

    def test_jsonplaceholder_posts(self):
        """GET /posts - used by jsonplaceholder_posts."""
        resp = requests.get(
            "https://jsonplaceholder.typicode.com/posts",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "jsonplaceholder /posts")
        assert_list_response(data, context="jsonplaceholder /posts")
        assert_keys_present(data[0], ["userId", "id", "title", "body"], "/posts[0]")

    def test_github_events(self):
        """GET /events - used by github_public_events."""
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "SpeakesQuery-Ingest"}
        resp = requests.get(
            "https://api.github.com/events",
            headers=headers,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "github /events")
        assert_list_response(data, context="github /events")
        event = data[0]
        assert_keys_present(event, ["id", "type", "actor", "repo"], "/events[0]")
        assert "login" in event["actor"], "/events[0].actor missing 'login'"

    def test_hackernews_topstories(self):
        """GET /topstories.json - used by hackernews_top_stories."""
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "hn /topstories")
        assert isinstance(data, list), "topstories should be a list of IDs"
        assert len(data) > 0, "topstories should not be empty"
        assert isinstance(data[0], int), "topstories[0] should be an int (story ID)"

    def test_hackernews_item(self):
        """GET /item/{id}.json - used by hackernews_top_stories (per-item fetch)."""
        # Get a story ID first
        top_resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=TIMEOUT,
        )
        stories = assert_status_ok(top_resp, "hn /topstories (for item)")
        assert len(stories) > 0, "Need at least one story ID"
        story_id = stories[0]

        resp = requests.get(
            f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
            timeout=15,
        )
        item = assert_status_ok(resp, f"hn /item/{story_id}")
        assert_keys_present(item, ["id", "type"], f"/item/{story_id}")


# ═══════════════════════════════════════════════════════════════════
# External Data APIs - used by alpha idea scripts
# ═══════════════════════════════════════════════════════════════════

class TestExternalDataAPIs:
    """Live contract tests for external APIs used by alpha idea scripts."""

    def test_predictit_all_markets(self):
        """GET /api/marketdata/all - used by polymarket_cross_platform_arbitrage."""
        resp = requests.get(
            "https://www.predictit.org/api/marketdata/all",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "predictit /api/marketdata/all")
        assert "markets" in data, (
            f"PredictIt response missing 'markets' key. Keys: {sorted(data.keys())}"
        )
        markets = data["markets"]
        assert_list_response(markets, min_items=1, context="predictit markets")
        market = markets[0]
        assert_keys_present(market, ["name", "contracts"], "predictit markets[0]")
        if market["contracts"]:
            contract = market["contracts"][0]
            assert_keys_present(
                contract,
                ["name", "lastTradePrice"],
                "predictit markets[0].contracts[0]",
            )

    def test_gdelt_doc_api(self):
        """GET /api/v2/doc/doc - used by polymarket_news_sentiment_divergence."""
        resp = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": "election president",
                "mode": "artlist",
                "maxrecords": 5,
                "timespan": "72h",
                "format": "json",
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "gdelt /api/v2/doc/doc")
        assert "articles" in data, (
            f"GDELT response missing 'articles' key. Keys: {sorted(data.keys())}"
        )
        articles = data["articles"]
        assert isinstance(articles, list), "GDELT articles should be a list"
        if articles:
            assert_keys_present(articles[0], ["title"], "gdelt articles[0]")


# ═══════════════════════════════════════════════════════════════════
# CoinGecko API - used by crypto market scripts
# ═══════════════════════════════════════════════════════════════════

class TestCoinGeckoAPI:
    """Live contract tests for CoinGecko public API endpoints."""

    def test_coins_markets(self):
        """GET /coins/markets - used by coingecko_top_coins, coingecko_volume_anomaly_detector."""
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 5,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d,30d",
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "coingecko /coins/markets")
        assert_list_response(data, min_items=1, context="coingecko coins/markets")
        coin = data[0]
        assert_keys_present(
            coin,
            ["id", "symbol", "name", "current_price", "market_cap",
             "total_volume", "price_change_percentage_24h"],
            "coingecko coins/markets[0]",
        )

    def test_search_trending(self):
        """GET /search/trending - used by coingecko_trending."""
        resp = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "coingecko /search/trending")
        assert "coins" in data, (
            f"Trending response missing 'coins' key. Keys: {sorted(data.keys())}"
        )
        coins = data["coins"]
        assert_list_response(coins, min_items=1, context="coingecko trending coins")
        item = coins[0].get("item", {})
        assert_keys_present(item, ["id", "symbol", "name"], "coingecko trending.coins[0].item")

    def test_global(self):
        """GET /global - used by coingecko_market_dominance."""
        resp = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "coingecko /global")
        assert "data" in data, (
            f"Global response missing 'data' key. Keys: {sorted(data.keys())}"
        )
        gdata = data["data"]
        assert_keys_present(
            gdata,
            ["active_cryptocurrencies", "markets", "total_market_cap",
             "market_cap_percentage", "market_cap_change_percentage_24h_usd"],
            "coingecko global.data",
        )

    def test_exchanges(self):
        """GET /exchanges - used by coingecko_exchange_volumes."""
        resp = requests.get(
            "https://api.coingecko.com/api/v3/exchanges",
            params={"per_page": 5, "page": 1},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "coingecko /exchanges")
        assert_list_response(data, min_items=1, context="coingecko exchanges")
        ex = data[0]
        assert_keys_present(
            ex,
            ["id", "name", "trade_volume_24h_btc", "trust_score"],
            "coingecko exchanges[0]",
        )


# ═══════════════════════════════════════════════════════════════════
# DeFi Llama API - used by DeFi protocol / yield / stablecoin scripts
# ═══════════════════════════════════════════════════════════════════

class TestDeFiLlamaAPI:
    """Live contract tests for DeFi Llama public API endpoints."""

    def test_protocols(self):
        """GET /protocols - used by defillama_tvl_rankings, defillama_tvl_movers."""
        resp = requests.get(
            "https://api.llama.fi/protocols",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "defillama /protocols")
        assert_list_response(data, min_items=10, context="defillama protocols")
        protocol = data[0]
        assert_keys_present(
            protocol,
            ["name", "tvl", "chains", "category"],
            "defillama protocols[0]",
        )

    def test_v2_chains(self):
        """GET /v2/chains - used by defillama_chain_tvl."""
        resp = requests.get(
            "https://api.llama.fi/v2/chains",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "defillama /v2/chains")
        assert_list_response(data, min_items=5, context="defillama chains")
        chain = data[0]
        assert_keys_present(
            chain,
            ["name", "tvl"],
            "defillama chains[0]",
        )

    def test_yields_pools(self):
        """GET /pools - used by defillama_yield_opportunities."""
        resp = requests.get(
            "https://yields.llama.fi/pools",
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "defillama yields /pools")
        assert "data" in data, (
            f"Yields response missing 'data' key. Keys: {sorted(data.keys())}"
        )
        pools = data["data"]
        assert_list_response(pools, min_items=10, context="defillama yield pools")
        pool = pools[0]
        assert_keys_present(
            pool,
            ["pool", "project", "chain", "symbol", "tvlUsd", "apy"],
            "defillama yield pools[0]",
        )

    def test_stablecoins(self):
        """GET /stablecoins - used by defillama_stablecoin_flows."""
        resp = requests.get(
            "https://stablecoins.llama.fi/stablecoins",
            params={"includePrices": "true"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "defillama /stablecoins")
        assert "peggedAssets" in data, (
            f"Stablecoins response missing 'peggedAssets' key. Keys: {sorted(data.keys())}"
        )
        assets = data["peggedAssets"]
        assert_list_response(assets, min_items=5, context="defillama stablecoins")
        asset = assets[0]
        assert_keys_present(
            asset,
            ["name", "symbol", "pegType", "chainCirculating"],
            "defillama stablecoins[0]",
        )


# ═══════════════════════════════════════════════════════════════════
# FRED API - used by macro economics scripts
# Requires FRED_API_KEY; skips if not set in environment.
# ═══════════════════════════════════════════════════════════════════

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")


@pytest.mark.skipif(not FRED_API_KEY, reason="FRED_API_KEY not set in environment")
class TestFredAPI:
    """Live contract tests for FRED API endpoints (requires API key)."""

    def test_series_observations_dgs10(self):
        """GET /series/observations - 10-Year Treasury yield."""
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "DGS10",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "fred DGS10")
        assert "observations" in data, (
            f"FRED response missing 'observations' key. Keys: {sorted(data.keys())}"
        )
        obs = data["observations"]
        assert_list_response(obs, min_items=1, context="fred DGS10 observations")
        assert_keys_present(obs[0], ["date", "value"], "fred DGS10 obs[0]")

    def test_series_observations_cpiaucsl(self):
        """GET /series/observations - CPI All Urban Consumers."""
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "CPIAUCSL",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 2,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "fred CPIAUCSL")
        assert "observations" in data, "Missing observations key"
        obs = data["observations"]
        assert_list_response(obs, min_items=2, context="fred CPIAUCSL")
        assert obs[0]["value"] != ".", "Latest CPI value should not be missing"

    def test_series_observations_unrate(self):
        """GET /series/observations - Unemployment Rate."""
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "UNRATE",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "fred UNRATE")
        obs = data.get("observations", [])
        assert len(obs) >= 1, "Expected at least 1 observation"
        assert_keys_present(obs[0], ["date", "value"], "fred UNRATE obs[0]")

    def test_series_observations_vixcls(self):
        """GET /series/observations - VIX."""
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "VIXCLS",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "fred VIXCLS")
        obs = data.get("observations", [])
        assert len(obs) >= 1, "Expected at least 1 VIX observation"

    def test_series_observations_m2sl(self):
        """GET /series/observations - M2 Money Supply."""
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "M2SL",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "fred M2SL")
        obs = data.get("observations", [])
        assert len(obs) >= 1, "Expected at least 1 M2 observation"

    def test_series_observations_mortgage30us(self):
        """GET /series/observations - 30-Year Mortgage Rate."""
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "MORTGAGE30US",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "fred MORTGAGE30US")
        obs = data.get("observations", [])
        assert len(obs) >= 1, "Expected at least 1 mortgage rate observation"


# ═══════════════════════════════════════════════════════════════════
# SEC EDGAR API - used by SEC filing and XBRL screening scripts
# No API key required; User-Agent header is mandatory.
# ═══════════════════════════════════════════════════════════════════

SEC_HEADERS = {"User-Agent": "SpeakesQuery SmokeTest (smoketest@speakesquery.local)"}


class TestSecEdgarAPI:
    """Live contract tests for SEC EDGAR public API endpoints."""

    def test_company_tickers(self):
        """GET /files/company_tickers.json - full ticker/CIK directory."""
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "sec company_tickers.json")
        assert isinstance(data, dict), "Expected dict keyed by index"
        assert len(data) > 1000, f"Expected >1000 companies, got {len(data)}"
        first = data.get("0", {})
        assert_keys_present(first, ["cik_str", "ticker", "title"], "company_tickers[0]")

    def test_submissions_apple(self):
        """GET /submissions/CIK{padded}.json - Apple Inc filings."""
        resp = requests.get(
            "https://data.sec.gov/submissions/CIK0000320193.json",
            headers=SEC_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "sec submissions AAPL")
        assert_keys_present(data, ["cik", "name", "filings"], "sec submissions AAPL")
        recent = data.get("filings", {}).get("recent", {})
        assert "form" in recent, "Missing 'form' in filings.recent"
        assert len(recent["form"]) > 0, "Expected at least 1 filing"

    def test_xbrl_frames_revenues(self):
        """GET /api/xbrl/frames/us-gaap/Revenues/USD/CY2024Q4.json - cross-company revenue."""
        resp = requests.get(
            "https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2024Q4.json",
            headers=SEC_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "sec xbrl frames Revenues")
        assert "data" in data, (
            f"XBRL frames response missing 'data' key. Keys: {sorted(data.keys())}"
        )
        entries = data["data"]
        assert_list_response(entries, min_items=10, context="sec xbrl revenues")
        assert_keys_present(
            entries[0],
            ["cik", "entityName", "val", "filed"],
            "sec xbrl revenues[0]",
        )

    def test_xbrl_frames_net_income(self):
        """GET /api/xbrl/frames/us-gaap/NetIncomeLoss/USD/CY2024Q4.json - cross-company profitability."""
        resp = requests.get(
            "https://data.sec.gov/api/xbrl/frames/us-gaap/NetIncomeLoss/USD/CY2024Q4.json",
            headers=SEC_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "sec xbrl frames NetIncomeLoss")
        assert "data" in data, "Missing 'data' key"
        entries = data["data"]
        assert_list_response(entries, min_items=10, context="sec xbrl net income")

    def test_xbrl_frames_assets(self):
        """GET /api/xbrl/frames/us-gaap/Assets/USD/CY2024Q4I.json - cross-company balance sheet."""
        resp = requests.get(
            "https://data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY2024Q4I.json",
            headers=SEC_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "sec xbrl frames Assets")
        assert "data" in data, "Missing 'data' key"
        entries = data["data"]
        assert_list_response(entries, min_items=10, context="sec xbrl assets")


# ---------------------------------------------------------------------------
# Kalshi - regulated prediction market
# ---------------------------------------------------------------------------

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@pytest.mark.smoke
class TestKalshiAPI:
    """Live contract tests for the Kalshi prediction-market API."""

    def test_markets_list(self):
        """GET /trade-api/v2/markets - active markets list."""
        resp = requests.get(
            f"{KALSHI_BASE}/markets",
            params={"limit": 10, "status": "active"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "kalshi markets")
        assert "markets" in data, (
            f"Kalshi markets response missing 'markets' key. Keys: {sorted(data.keys())}"
        )
        markets = data["markets"]
        assert_list_response(markets, min_items=1, context="kalshi markets")
        assert_keys_present(
            markets[0],
            ["ticker", "title", "status", "last_price", "volume", "open_interest"],
            "kalshi markets[0]",
        )

    def test_events_list(self):
        """GET /trade-api/v2/events - active events list."""
        resp = requests.get(
            f"{KALSHI_BASE}/events",
            params={"limit": 10, "status": "active"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "kalshi events")
        assert "events" in data, (
            f"Kalshi events response missing 'events' key. Keys: {sorted(data.keys())}"
        )
        events = data["events"]
        assert_list_response(events, min_items=1, context="kalshi events")
        assert_keys_present(
            events[0],
            ["event_ticker", "title", "category"],
            "kalshi events[0]",
        )

    def test_orderbook(self):
        """GET /trade-api/v2/markets/{ticker}/orderbook - order book for a market."""
        # First fetch one active market ticker
        resp = requests.get(
            f"{KALSHI_BASE}/markets",
            params={"limit": 5, "status": "active"},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "kalshi markets (for orderbook)")
        markets = data.get("markets", [])
        assert len(markets) > 0, "No active Kalshi markets found for orderbook test"

        ticker = markets[0]["ticker"]
        resp_ob = requests.get(
            f"{KALSHI_BASE}/markets/{ticker}/orderbook",
            timeout=TIMEOUT,
        )
        ob_data = assert_status_ok(resp_ob, f"kalshi orderbook ({ticker})")
        assert "orderbook" in ob_data, (
            f"Orderbook response missing 'orderbook' key. Keys: {sorted(ob_data.keys())}"
        )


# ---------------------------------------------------------------------------
# Reddit - finance subreddit JSON API
# ---------------------------------------------------------------------------

REDDIT_HEADERS = {"User-Agent": "speakesQuery/1.0 smoke-test"}


@pytest.mark.smoke
class TestRedditAPI:
    """Live contract tests for Reddit's public JSON API."""

    def test_wsb_hot(self):
        """GET /r/wallstreetbets/hot.json - hot posts listing."""
        resp = requests.get(
            "https://www.reddit.com/r/wallstreetbets/hot.json",
            headers=REDDIT_HEADERS,
            params={"limit": 5, "raw_json": 1},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "reddit wsb hot")
        assert "data" in data, (
            f"Reddit response missing 'data' key. Keys: {sorted(data.keys())}"
        )
        children = data["data"].get("children", [])
        assert_list_response(children, min_items=1, context="reddit wsb hot children")
        post = children[0].get("data", {})
        assert_keys_present(
            post,
            ["title", "score", "num_comments", "upvote_ratio", "author"],
            "reddit wsb hot post",
        )

    def test_stocks_hot(self):
        """GET /r/stocks/hot.json - hot posts from r/stocks."""
        resp = requests.get(
            "https://www.reddit.com/r/stocks/hot.json",
            headers=REDDIT_HEADERS,
            params={"limit": 5, "raw_json": 1},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "reddit stocks hot")
        assert "data" in data, "Missing 'data' key"
        children = data["data"].get("children", [])
        assert_list_response(children, min_items=1, context="reddit stocks hot")

    def test_subreddit_top(self):
        """GET /r/wallstreetbets/top.json?t=day - top posts today."""
        resp = requests.get(
            "https://www.reddit.com/r/wallstreetbets/top.json",
            headers=REDDIT_HEADERS,
            params={"limit": 5, "t": "day", "raw_json": 1},
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "reddit wsb top")
        assert "data" in data, "Missing 'data' key"
        children = data["data"].get("children", [])
        assert_list_response(children, min_items=1, context="reddit wsb top")


# ---------------------------------------------------------------------------
# Wikipedia - Wikimedia Pageviews REST API
# ---------------------------------------------------------------------------

WIKI_HEADERS = {"User-Agent": "speakesQuery/1.0 smoke-test"}


@pytest.mark.smoke
class TestWikipediaPageviewsAPI:
    """Live contract tests for the Wikimedia Pageviews API."""

    def test_per_article_daily(self):
        """GET pageviews/per-article - daily pageviews for a company article."""
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        end = (now - timedelta(days=1)).strftime("%Y%m%d")
        start = (now - timedelta(days=8)).strftime("%Y%m%d")
        resp = requests.get(
            f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
            f"en.wikipedia/all-access/all-agents/Apple_Inc./daily/{start}/{end}",
            headers=WIKI_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "wikipedia pageviews Apple_Inc.")
        assert "items" in data, (
            f"Pageviews response missing 'items' key. Keys: {sorted(data.keys())}"
        )
        items = data["items"]
        assert_list_response(items, min_items=1, context="wikipedia pageviews items")
        assert_keys_present(
            items[0],
            ["article", "timestamp", "views"],
            "wikipedia pageviews item",
        )

    def test_fear_term_pageviews(self):
        """GET pageviews/per-article - daily pageviews for 'Recession'."""
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        end = (now - timedelta(days=1)).strftime("%Y%m%d")
        start = (now - timedelta(days=8)).strftime("%Y%m%d")
        resp = requests.get(
            f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
            f"en.wikipedia/all-access/all-agents/Recession/daily/{start}/{end}",
            headers=WIKI_HEADERS,
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "wikipedia pageviews Recession")
        assert "items" in data, "Missing 'items' key"
        items = data["items"]
        assert_list_response(items, min_items=1, context="wikipedia recession pageviews")


# ---------------------------------------------------------------------------
# Open-Meteo - free weather forecast API
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestOpenMeteoAPI:
    """Live contract tests for the Open-Meteo forecast API."""

    def test_daily_forecast(self):
        """GET /v1/forecast - daily forecast for NYC."""
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 40.71,
                "longitude": -74.01,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "mm",
                "timezone": "auto",
                "forecast_days": 3,
            },
            timeout=TIMEOUT,
        )
        data = assert_status_ok(resp, "open-meteo forecast")
        assert "daily" in data, (
            f"Open-Meteo response missing 'daily' key. Keys: {sorted(data.keys())}"
        )
        daily = data["daily"]
        assert_keys_present(
            daily,
            ["time", "temperature_2m_max", "temperature_2m_min",
             "precipitation_sum", "wind_speed_10m_max", "weather_code"],
            "open-meteo daily",
        )
        assert len(daily["time"]) >= 3, (
            f"Expected >= 3 forecast days, got {len(daily['time'])}"
        )


# ---------------------------------------------------------------------------
# Google Trends - RSS feed for daily trending searches
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestGoogleTrendsRSS:
    """Live contract tests for Google Trends daily trending RSS feed."""

    def test_daily_trending_rss(self):
        """GET /trends/trendingsearches/daily/rss - daily trending searches."""
        resp = requests.get(
            "https://trends.google.com/trends/trendingsearches/daily/rss",
            params={"geo": "US"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=TIMEOUT,
        )
        assert resp.status_code == 200, (
            f"Google Trends RSS: expected 200, got {resp.status_code}"
        )
        assert len(resp.text) > 100, "Google Trends RSS response too short"
        assert "<item>" in resp.text, "RSS response missing <item> elements"
        assert "<title>" in resp.text, "RSS response missing <title> elements"
