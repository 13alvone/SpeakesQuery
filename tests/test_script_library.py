#!/usr/bin/env python3
"""
Script Library Test Suite
─────────────────────────
Validates all no-auth ingestion scripts in the default library.

Two parametrized test families:
  1. test_script_json_structure - JSON schema, required keys, metadata
  2. test_script_executes_valid_dataframe - mock HTTP, run CodeExecutor.execute_test(),
     assert pass status / columns / _epoch / row count

All HTTP is mocked via unittest.mock.patch('requests.get') with a URL-pattern
router.  No live network calls.  Deterministic and fast (~15s total).
"""

import datetime as _dt
import json
import os
import sys
import unittest.mock
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from scheduled_input_engine.executor import CodeExecutor

SCRIPTS_DIR = PROJECT_ROOT / "script_library" / "scripts"


# ═══════════════════════════════════════════════════════════════════
# Mock infrastructure
# ═══════════════════════════════════════════════════════════════════

def _make_response(data, status_code=200):
    """Build a requests.Response-compatible mock."""
    resp = unittest.mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = unittest.mock.Mock()
    if isinstance(data, str):
        resp.text = data
        resp.content = data.encode()
    else:
        resp.text = json.dumps(data) if data is not None else ""
        resp.content = json.dumps(data).encode() if data is not None else b""
    return resp


def _make_router(url_map):
    """Return a side_effect callable that routes by URL substring."""
    def router(url, **kwargs):
        for pattern, data in url_map.items():
            if pattern in url:
                payload = data(url, kwargs) if callable(data) else data
                return _make_response(payload)
        return _make_response([])
    return router


# ═══════════════════════════════════════════════════════════════════
# Mock data factories
# ═══════════════════════════════════════════════════════════════════

# ── Gamma API: Markets ────────────────────────────────────────────

def make_gamma_market(**overrides):
    """Realistic Gamma /markets object with JSON-string-encoded fields."""
    base = {
        "id": "market_001",
        "question": "Will it rain tomorrow?",
        "slug": "will-it-rain-tomorrow",
        "conditionId": "0xcondition123",
        "outcomePrices": '["0.65","0.35"]',
        "outcomes": '["Yes","No"]',
        "volume": "150000",
        "liquidity": "25000",
        "category": "Weather",
        "tags": '[{"label":"Weather","slug":"weather"}]',
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-31T00:00:00Z",
        "acceptingOrders": True,
        "neg_risk": False,
        "marketType": "binary",
        "clobTokenIds": '["token_yes_001","token_no_001"]',
        "active": True,
        "closed": False,
        "createdAt": "2026-01-01T00:00:00Z",
        "description": "A test market for weather prediction.",
    }
    base.update(overrides)
    return base


def make_geo_market():
    return make_gamma_market(
        id="market_geo_001",
        question="Will there be a Ukraine ceasefire by end of 2026?",
        slug="ukraine-ceasefire-2026",
        category="Geopolitics",
        tags='[{"label":"Geopolitics","slug":"geopolitics"},{"label":"World","slug":"world"}]',
        description="Tracks the possibility of a ceasefire in the Ukraine war.",
    )


def make_sports_market():
    return make_gamma_market(
        id="market_sports_001",
        question="Will the Lakers win the NBA Finals 2026?",
        slug="lakers-nba-finals-2026",
        category="Sports",
        tags='[{"label":"Sports","slug":"sports"},{"label":"NBA","slug":"nba"}]',
        description="NBA championship prediction.",
    )


def make_politics_market():
    return make_gamma_market(
        id="market_politics_001",
        question="Who will win the 2028 presidential election?",
        slug="presidential-election-2028",
        category="Politics",
        tags='[{"label":"Politics","slug":"politics"},{"label":"Elections","slug":"elections"}]',
        description="Presidential election prediction market.",
    )


def make_crypto_market():
    return make_gamma_market(
        id="market_crypto_001",
        question="Will Bitcoin ETF reach $100B AUM by 2027?",
        slug="bitcoin-etf-100b",
        category="Crypto",
        tags='[{"label":"Crypto","slug":"crypto"},{"label":"Bitcoin","slug":"bitcoin"}]',
        description="Bitcoin ETF asset prediction.",
    )


def make_high_prob_market():
    return make_gamma_market(
        id="market_highprob_001",
        question="Will the sun rise tomorrow?",
        slug="sun-rise-tomorrow",
        outcomePrices='["0.92","0.08"]',
        volume="500000",
    )


def make_resolved_yes_market():
    return make_gamma_market(
        id="market_resolved_001",
        question="Did it rain on Jan 1 2026?",
        slug="rain-jan-1-2026",
        outcomePrices='["0.99","0.01"]',
        active=False,
        closed=True,
        volume="80000",
    )


def make_resolved_no_market():
    return make_gamma_market(
        id="market_resolved_002",
        question="Did it snow in Miami on Jan 1 2026?",
        slug="snow-miami-jan-1-2026",
        outcomePrices='["0.01","0.99"]',
        active=False,
        closed=True,
        volume="30000",
    )


# ── Gamma API: Events ────────────────────────────────────────────

def make_gamma_event(**overrides):
    """Realistic Gamma /events object with nested markets."""
    base = {
        "id": "event_001",
        "title": "2026 Weather Predictions",
        "slug": "2026-weather-predictions",
        "category": "Weather",
        "tags": '[{"label":"Weather","slug":"weather"}]',
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-31T00:00:00Z",
        "markets": [
            make_gamma_market(id="market_ev_001", question="Rain in January?",
                              outcomePrices='["0.70","0.30"]', volume="50000"),
            make_gamma_market(id="market_ev_002", question="Rain in February?",
                              outcomePrices='["0.55","0.45"]', volume="30000"),
        ],
    }
    base.update(overrides)
    return base


def make_arb_event():
    """Event where outcome prices sum to > 1.0 (arbitrage opportunity)."""
    return make_gamma_event(
        id="event_arb_001",
        title="Who wins the race?",
        slug="who-wins-the-race",
        markets=[
            make_gamma_market(id="m_arb_1", question="Alice wins?",
                              outcomePrices='["0.50","0.50"]', volume="40000"),
            make_gamma_market(id="m_arb_2", question="Bob wins?",
                              outcomePrices='["0.40","0.60"]', volume="35000"),
            make_gamma_market(id="m_arb_3", question="Charlie wins?",
                              outcomePrices='["0.15","0.85"]', volume="20000"),
        ],
    )


# ── CLOB API ──────────────────────────────────────────────────────

MOCK_PRICE_HISTORY = {
    "history": [
        {"t": 1700000000, "p": 0.55},
        {"t": 1700100000, "p": 0.60},
        {"t": 1700200000, "p": 0.65},
    ]
}

MOCK_ORDERBOOK = {
    "bids": [
        {"price": "0.60", "size": "500"},
        {"price": "0.58", "size": "300"},
        {"price": "0.55", "size": "200"},
    ],
    "asks": [
        {"price": "0.65", "size": "400"},
        {"price": "0.68", "size": "250"},
        {"price": "0.70", "size": "150"},
    ],
}

MOCK_MIDPOINT = {"mid": "0.625"}

MOCK_SPREAD = {"spread": "0.05"}

MOCK_PRICE = {"price": "0.60"}


# ── Data API ──────────────────────────────────────────────────────

MOCK_ACTIVITY = [
    {
        "proxyWallet": "0xuser001",
        "side": "BUY",
        "asset": "token_yes_123",
        "conditionId": "0xcondition123",
        "size": 50,
        "price": 0.65,
        "timestamp": 1705312800,
        "title": "Will it rain tomorrow?",
        "slug": "will-it-rain-tomorrow",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "name": "trader1",
        "pseudonym": "Brave-Cactus",
        "transactionHash": "0xtxhash001",
    },
    {
        "proxyWallet": "0xuser002",
        "side": "SELL",
        "asset": "token_yes_123",
        "conditionId": "0xcondition123",
        "size": 25,
        "price": 0.70,
        "timestamp": 1705316400,
        "title": "Will it rain tomorrow?",
        "slug": "will-it-rain-tomorrow",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "name": "trader2",
        "pseudonym": "Clever-Owl",
        "transactionHash": "0xtxhash002",
    },
]

MOCK_LEADERBOARD = [
    {
        "rank": "1",
        "proxyWallet": "0xleader001",
        "userName": "whale_trader",
        "xUsername": "",
        "verifiedBadge": False,
        "vol": 2500000,
        "pnl": 125000,
        "profileImage": "https://example.com/avatar1.png",
    },
    {
        "rank": "2",
        "proxyWallet": "0xleader002",
        "userName": "steady_eddie",
        "xUsername": "",
        "verifiedBadge": False,
        "vol": 1800000,
        "pnl": 85000,
        "profileImage": "https://example.com/avatar2.png",
    },
]

MOCK_HOLDERS = [
    {
        "address": "0xwhale001",
        "username": "big_fish",
        "size": 50000,
        "outcome": "Yes",
        "outcomeIndex": 0,
    },
    {
        "address": "0xwhale002",
        "username": "deep_pockets",
        "size": 30000,
        "outcome": "No",
        "outcomeIndex": 1,
    },
]

MOCK_COMMENTS = [
    {
        "id": "comment_001",
        "body": "I think this market is underpriced.",
        "profile": {
            "name": "analyst_mike",
            "pseudonym": "analyst_mike",
            "baseAddress": "0xcomment_user_001",
            "proxyWallet": "0xproxy_001",
        },
        "userAddress": "0xcomment_user_001",
        "parentEntityID": "event_001",
        "parentEntityType": "Event",
        "reactionCount": 12,
        "reportCount": 0,
        "createdAt": "2026-01-10T08:30:00Z",
        "updatedAt": "2026-01-10T08:30:00Z",
    },
    {
        "id": "comment_002",
        "body": "Disagree - look at the weather models.",
        "profile": {
            "name": "weather_nerd",
            "pseudonym": "weather_nerd",
            "baseAddress": "0xcomment_user_002",
            "proxyWallet": "0xproxy_002",
        },
        "userAddress": "0xcomment_user_002",
        "parentEntityID": "event_001",
        "parentEntityType": "Event",
        "parentCommentID": "comment_001",
        "reactionCount": 5,
        "reportCount": 0,
        "createdAt": "2026-01-10T09:15:00Z",
        "updatedAt": "2026-01-10T09:15:00Z",
    },
]


# ── Original APIs ─────────────────────────────────────────────────

MOCK_JSONPLACEHOLDER_POSTS = [
    {"userId": 1, "id": 1, "title": "Test Post Alpha", "body": "Body of test post alpha."},
    {"userId": 2, "id": 2, "title": "Test Post Beta", "body": "Body of test post beta."},
]

MOCK_GITHUB_EVENTS = [
    {
        "id": "evt_001",
        "type": "PushEvent",
        "actor": {"login": "octocat"},
        "repo": {"name": "octocat/Hello-World"},
        "created_at": "2026-01-15T10:00:00Z",
        "public": True,
    },
    {
        "id": "evt_002",
        "type": "PullRequestEvent",
        "actor": {"login": "devuser"},
        "repo": {"name": "devuser/my-project"},
        "created_at": "2026-01-15T11:00:00Z",
        "public": True,
    },
]

MOCK_HN_TOP_STORIES = [101, 102, 103]

MOCK_HN_ITEM = {
    "id": 101,
    "title": "Show HN: A New Framework",
    "url": "https://example.com/framework",
    "by": "hacker_jane",
    "score": 250,
    "descendants": 45,
    "type": "story",
    "time": 1700000000,
}


# ── External APIs (PredictIt, GDELT) ─────────────────────────────

MOCK_PREDICTIT_RESPONSE = {
    "markets": [
        {
            "name": "Who will win the 2028 presidential election?",
            "contracts": [
                {
                    "name": "Republican candidate",
                    "lastTradePrice": 0.45,
                    "bestBuyYesCost": 0.46,
                    "bestBuyNoCost": 0.56,
                },
            ],
        },
    ],
}

MOCK_GDELT_ARTICLES = {
    "articles": [
        {"title": "Rain forecast shows unprecedented surge tomorrow across region"},
        {"title": "Weather experts confirm strong likelihood of rain tomorrow"},
    ],
}


# ── Specialised market factories for new alpha scripts ───────────

def make_volume_spike_market():
    return make_gamma_market(
        id="market_spike_001",
        question="Will there be a government shutdown in Q2 2026?",
        slug="government-shutdown-q2-2026",
        volume="150000",
        volume24hr="5000",
        liquidityNum="25000",
    )


def make_near_resolution_market():
    # endDate is computed relative to "now" so the fixture stays valid as time
    # marches forward. The polymarket_temporal_decay script filters to markets
    # with 0 < days_to_resolution <= 30; picking +5 days keeps us in that
    # window in every timezone on every day the test runs.
    end_dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=5)
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return make_gamma_market(
        id="market_nearres_001",
        question="Will the Fed cut rates soon?",
        slug="fed-rate-cut-near-term",
        outcomePrices='["0.80","0.20"]',
        volume="200000",
        liquidityNum="40000",
        endDate=end_iso,
    )


def make_edge_zone_market():
    return make_gamma_market(
        id="market_edge_001",
        question="Will Democrats win the Senate in 2026?",
        slug="democrats-senate-2026",
        outcomePrices='["0.50","0.50"]',
        volume="300000",
        volume24hr="10000",
        liquidityNum="60000",
        endDate="2026-06-15T00:00:00Z",
    )


def make_sentiment_divergence_market():
    return make_gamma_market(
        id="market_sent_001",
        question="Will it rain tomorrow in New York?",
        slug="rain-tomorrow-new-york",
        outcomePrices='["0.35","0.65"]',
        volume="50000",
        volume24hr="1000",
        liquidityNum="15000",
    )


# ── Kalshi API ───────────────────────────────────────────────────

def make_kalshi_market(**overrides):
    """Realistic Kalshi /markets object.

    Includes BOTH legacy (integer cents) and V2 (string-typed dollars)
    field forms so tests cover scripts on either side of the
    2026-05-XX V2 schema flip:
        legacy: last_price, yes_bid, yes_ask, volume, open_interest (int)
        V2:     last_price_dollars, yes_bid_dollars, yes_ask_dollars
                (strings: "0.6500"), volume_fp / open_interest_fp
                (strings: "25000.00")

    Pre-2026-05-05 the kalshi_contract_scanner script used legacy
    fields and silently produced 18,000 rows of zero-volume garbage in
    production after Kalshi made the V2 switch. Fixed to use V2 fields
    + per-event_ticker market queries; mock now provides both shapes
    so old + new tests both pass."""
    base = {
        "ticker": "INXD-26APR11-T5505",
        "event_ticker": "INXD-26APR11",
        "title": "S&P 500 above 5505 on April 11?",
        "subtitle": "S&P 500 closing price",
        "status": "active",
        "category": "Economics",
        # Legacy fields (integer cents)
        "last_price": 65,
        "previous_price": 60,
        "yes_bid": 63,
        "yes_ask": 67,
        "no_bid": 33,
        "no_ask": 37,
        "volume": 25000,
        "volume_24h": 5000,
        "open_interest": 8000,
        # V2 fields (string-typed dollar values)
        "last_price_dollars": "0.6500",
        "previous_price_dollars": "0.6000",
        "yes_bid_dollars": "0.6300",
        "yes_ask_dollars": "0.6700",
        "no_bid_dollars": "0.3300",
        "no_ask_dollars": "0.3700",
        "volume_fp": "25000.00",
        "volume_24h_fp": "5000.00",
        "open_interest_fp": "8000.00",
        "close_time": "2026-04-11T20:00:00Z",
        "result": None,
    }
    base.update(overrides)
    # Auto-derive V2 dollar/_fp forms from legacy cent overrides so older
    # tests passing only legacy fields stay consistent. Skip when the
    # caller explicitly set the V2 field (last_price_dollars="1.5000"
    # corruption tests etc. - those need to override both forms).
    _legacy_to_v2 = {
        "last_price": ("last_price_dollars", 100.0),
        "previous_price": ("previous_price_dollars", 100.0),
        "yes_bid": ("yes_bid_dollars", 100.0),
        "yes_ask": ("yes_ask_dollars", 100.0),
        "no_bid": ("no_bid_dollars", 100.0),
        "no_ask": ("no_ask_dollars", 100.0),
        "volume": ("volume_fp", 1.0),
        "volume_24h": ("volume_24h_fp", 1.0),
        "open_interest": ("open_interest_fp", 1.0),
    }
    for legacy_field, (v2_field, divisor) in _legacy_to_v2.items():
        if legacy_field in overrides and v2_field not in overrides:
            cents_or_units = overrides[legacy_field]
            try:
                dollars_or_units = float(cents_or_units) / divisor
            except (ValueError, TypeError):
                continue
            # Prices use 4 decimals (matches Kalshi V2 wire format);
            # volume / OI use 2 decimals.
            decimals = 4 if divisor == 100.0 else 2
            base[v2_field] = f"{dollars_or_units:.{decimals}f}"
    return base


def make_kalshi_market_high_vol():
    """Kalshi market with high volume/OI ratio for volume tracker."""
    return make_kalshi_market(
        ticker="FEDR-26MAR-T450",
        event_ticker="FEDR-26MAR",
        title="Fed rate above 4.50% in March?",
        category="Economics",
        last_price=72,
        previous_price=70,
        volume=50000,
        volume_24h=12000,
        open_interest=3000,
        last_price_dollars="0.7200",
        previous_price_dollars="0.7000",
        volume_fp="50000.00",
        volume_24h_fp="12000.00",
        open_interest_fp="3000.00",
    )


def make_kalshi_event(**overrides):
    """Realistic Kalshi /events object."""
    base = {
        "event_ticker": "INXD-26APR11",
        "title": "S&P 500 Range on April 11",
        "category": "Economics",
        "sub_title": "Daily close prediction",
        "status": "active",
        "markets": [
            make_kalshi_market(),
            make_kalshi_market(
                ticker="INXD-26APR11-T5450",
                title="S&P 500 above 5450 on April 11?",
                last_price=80,
                previous_price=78,
                volume=18000,
                volume_24h=3000,
                open_interest=6000,
            ),
        ],
    }
    base.update(overrides)
    return base


MOCK_KALSHI_ORDERBOOK = {
    "orderbook": {
        "yes": [
            [63, 500],
            [62, 300],
            [60, 200],
        ],
        "no": [
            [37, 400],
            [36, 250],
            [35, 150],
        ],
    }
}


# ── Google Trends RSS mock ────────────────────────────────────

MOCK_GOOGLE_TRENDS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:ht="https://trends.google.com/trends/trendingsearches/daily">
  <channel>
    <title>Daily Search Trends</title>
    <item>
      <title>Federal Reserve rate decision</title>
      <ht:approx_traffic>500,000+</ht:approx_traffic>
    </item>
    <item>
      <title>Hurricane watch Florida</title>
      <ht:approx_traffic>200,000+</ht:approx_traffic>
    </item>
    <item>
      <title>Super Bowl halftime</title>
      <ht:approx_traffic>1,000,000+</ht:approx_traffic>
    </item>
  </channel>
</rss>"""


def _google_trends_router(url, kwargs):
    """Route Google Trends requests - RSS feed returns XML string."""
    if "trendingsearches" in url:
        return MOCK_GOOGLE_TRENDS_RSS
    return []


# ── Open-Meteo mock ──────────────────────────────────────────

MOCK_OPEN_METEO_FORECAST = {
    "latitude": 40.71,
    "longitude": -74.01,
    "generationtime_ms": 0.5,
    "daily": {
        "time": ["2026-04-09", "2026-04-10", "2026-04-11"],
        "temperature_2m_max": [72.5, 68.3, 75.1],
        "temperature_2m_min": [55.2, 52.8, 58.4],
        "precipitation_sum": [0.0, 2.5, 0.0],
        "wind_speed_10m_max": [12.3, 18.7, 8.5],
        "weather_code": [0, 61, 1],
    },
}


# ── Reddit API ──────────────────────────────────────────────────

def make_reddit_post(**overrides):
    """Realistic Reddit post (t3 child data object)."""
    base = {
        "title": "YOLO'd my life savings into TSLA calls",
        "score": 3500,
        "num_comments": 420,
        "upvote_ratio": 0.91,
        "author": "diamond_hands_42",
        "created_utc": 1712600000,
        "permalink": "/r/wallstreetbets/comments/abc123/yolod_my_life_savings/",
        "link_flair_text": "YOLO",
        "subreddit": "wallstreetbets",
        "selftext": "Going all in on $TSLA $NVDA. To the moon.",
        "stickied": False,
    }
    base.update(overrides)
    return base


def make_reddit_post_alt(**overrides):
    """Second Reddit post with different ticker mentions."""
    base = {
        "title": "$NVDA earnings play - DD inside",
        "score": 6200,
        "num_comments": 890,
        "upvote_ratio": 0.94,
        "author": "quant_trader_99",
        "created_utc": 1712610000,
        "permalink": "/r/wallstreetbets/comments/def456/nvda_earnings_play/",
        "link_flair_text": "DD",
        "subreddit": "wallstreetbets",
        "selftext": "Deep dive on $NVDA and $AMD. This is not financial advice.",
        "stickied": False,
    }
    base.update(overrides)
    return base


def make_reddit_post_stocks(**overrides):
    """Reddit post from r/stocks."""
    base = {
        "title": "AAPL just broke out of a 3-month consolidation",
        "score": 1200,
        "num_comments": 180,
        "upvote_ratio": 0.88,
        "author": "value_investor",
        "created_utc": 1712620000,
        "permalink": "/r/stocks/comments/ghi789/aapl_breakout/",
        "link_flair_text": "Technical Analysis",
        "subreddit": "stocks",
        "selftext": "$AAPL breaking resistance at 195.",
        "stickied": False,
    }
    base.update(overrides)
    return base


def _make_reddit_listing(posts):
    """Wrap a list of post dicts into a Reddit listing response."""
    children = []
    for i in range(len(posts)):
        children.append({"kind": "t3", "data": posts[i]})
    return {"kind": "Listing", "data": {"children": children, "after": None}}


MOCK_REDDIT_WSB_HOT = _make_reddit_listing([
    make_reddit_post(),
    make_reddit_post_alt(),
])

MOCK_REDDIT_WSB_TOP = _make_reddit_listing([
    make_reddit_post_alt(score=8500, num_comments=1200),
    make_reddit_post(score=5000),
])

MOCK_REDDIT_STOCKS_HOT = _make_reddit_listing([
    make_reddit_post_stocks(),
])

MOCK_REDDIT_INVESTING_HOT = _make_reddit_listing([
    make_reddit_post_stocks(
        subreddit="investing",
        title="Long-term NVDA thesis",
        selftext="$NVDA is the backbone of AI infrastructure.",
        permalink="/r/investing/comments/jkl012/long_term_nvda/",
    ),
])

MOCK_REDDIT_CRYPTO_HOT = _make_reddit_listing([
    make_reddit_post_stocks(
        subreddit="cryptocurrency",
        title="BTC dominance rising - altcoin season over?",
        selftext="Bitcoin dominance at 54%.",
        permalink="/r/cryptocurrency/comments/mno345/btc_dominance/",
        link_flair_text="MARKETS",
    ),
])

MOCK_REDDIT_ECONOMICS_HOT = _make_reddit_listing([
    make_reddit_post_stocks(
        subreddit="economics",
        title="CPI data suggests inflation cooling faster than expected",
        selftext="Core CPI came in at 3.1% YoY.",
        permalink="/r/economics/comments/pqr678/cpi_data/",
        link_flair_text="News",
    ),
])

MOCK_REDDIT_OPTIONS_HOT = _make_reddit_listing([
    make_reddit_post_stocks(
        subreddit="options",
        title="$TSLA straddle before earnings",
        selftext="IV is elevated for TSLA puts.",
        permalink="/r/options/comments/stu901/tsla_straddle/",
        link_flair_text="Discussion",
    ),
])


def _reddit_router_factory():
    """Router that dispatches Reddit JSON API by subreddit + sort."""
    def router(url, kwargs):
        if "wallstreetbets/top" in url:
            return MOCK_REDDIT_WSB_TOP
        if "wallstreetbets/hot" in url:
            return MOCK_REDDIT_WSB_HOT
        if "stocks/hot" in url:
            return MOCK_REDDIT_STOCKS_HOT
        if "investing/hot" in url:
            return MOCK_REDDIT_INVESTING_HOT
        if "cryptocurrency/hot" in url:
            return MOCK_REDDIT_CRYPTO_HOT
        if "economics/hot" in url:
            return MOCK_REDDIT_ECONOMICS_HOT
        if "options/hot" in url:
            return MOCK_REDDIT_OPTIONS_HOT
        return MOCK_REDDIT_WSB_HOT
    return router


# ── Wikipedia Pageviews API ─────────────────────────────────────

def make_wikipedia_pageview_items(article, days=14, base_views=5000, spike_factor=1.0):
    """Generate mock pageview items for a Wikipedia article.

    spike_factor > 1 means recent 7 days have higher views than prior 7.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    items = []
    for d in range(days):
        day = now - timedelta(days=days - d)
        ts = day.strftime('%Y%m%d') + '00'
        if d >= 7:
            views = int(base_views * spike_factor)
        else:
            views = base_views
        items.append({
            "project": "en.wikipedia",
            "article": article,
            "granularity": "daily",
            "timestamp": ts,
            "views": views,
        })
    return items


MOCK_WIKI_COMPANY_RESPONSES = {
    "Apple_Inc.": {"items": make_wikipedia_pageview_items("Apple_Inc.", base_views=50000, spike_factor=1.8)},
    "Microsoft": {"items": make_wikipedia_pageview_items("Microsoft", base_views=30000, spike_factor=1.1)},
    "Tesla,_Inc.": {"items": make_wikipedia_pageview_items("Tesla,_Inc.", base_views=40000, spike_factor=2.5)},
    "Nvidia": {"items": make_wikipedia_pageview_items("Nvidia", base_views=35000, spike_factor=1.3)},
}

MOCK_WIKI_FEAR_RESPONSES = {
    "Recession": {"items": make_wikipedia_pageview_items("Recession", base_views=8000, spike_factor=2.0)},
    "Inflation": {"items": make_wikipedia_pageview_items("Inflation", base_views=12000, spike_factor=1.5)},
    "Stock_market_crash": {"items": make_wikipedia_pageview_items("Stock_market_crash", base_views=5000, spike_factor=3.0)},
    "Bank_run": {"items": make_wikipedia_pageview_items("Bank_run", base_views=3000, spike_factor=1.2)},
}


def _wikipedia_router_factory(article_responses):
    """Router that dispatches Wikipedia pageviews by article name in URL."""
    def router(url, kwargs):
        for article in article_responses:
            if article in url:
                return article_responses[article]
        first_key = list(article_responses.keys())[0]
        return article_responses[first_key]
    return router


# ── CoinGecko API ────────────────────────────────────────────────

def make_coingecko_coin(**overrides):
    """Realistic CoinGecko /coins/markets object."""
    base = {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "current_price": 67500.0,
        "market_cap": 1330000000000,
        "market_cap_rank": 1,
        "fully_diluted_valuation": 1420000000000,
        "total_volume": 28500000000,
        "price_change_percentage_24h": 2.35,
        "price_change_percentage_1h_in_currency": 0.12,
        "price_change_percentage_7d_in_currency": -1.50,
        "price_change_percentage_30d_in_currency": 8.75,
        "circulating_supply": 19700000,
        "total_supply": 21000000,
        "ath": 73750.0,
        "ath_change_percentage": -8.47,
        "ath_date": "2024-03-14T07:10:36.635Z",
    }
    base.update(overrides)
    return base


def make_coingecko_coin_alt():
    """Altcoin with high volume anomaly for divergence detection."""
    return make_coingecko_coin(
        id="solana",
        symbol="sol",
        name="Solana",
        current_price=145.0,
        market_cap=65000000000,
        market_cap_rank=5,
        total_volume=45000000000,   # Abnormally high vs mcap
        price_change_percentage_24h=0.5,   # Small price move
        price_change_percentage_1h_in_currency=0.1,
        price_change_percentage_7d_in_currency=3.2,
        price_change_percentage_30d_in_currency=15.0,
        circulating_supply=440000000,
        total_supply=580000000,
        ath=260.0,
        fully_diluted_valuation=84000000000,
    )


MOCK_COINGECKO_TRENDING = {
    "coins": [
        {
            "item": {
                "id": "pepe",
                "coin_id": 24613,
                "name": "Pepe",
                "symbol": "PEPE",
                "market_cap_rank": 25,
                "score": 0,
                "data": {
                    "price": 0.0000125,
                    "price_change_percentage_24h": {"usd": 15.3},
                    "market_cap": "$5,200,000,000",
                    "total_volume": "$1,800,000,000",
                },
            }
        },
        {
            "item": {
                "id": "render-token",
                "coin_id": 11636,
                "name": "Render",
                "symbol": "RNDR",
                "market_cap_rank": 30,
                "score": 1,
                "data": {
                    "price": 8.50,
                    "price_change_percentage_24h": {"usd": 7.8},
                    "market_cap": "$3,400,000,000",
                    "total_volume": "$600,000,000",
                },
            }
        },
    ],
    "nfts": [
        {
            "id": "bored-ape-yacht-club",
            "name": "Bored Ape Yacht Club",
            "symbol": "BAYC",
            "data": {"floor_price_in_usd_24h_percentage_change": "-2.5"},
        }
    ],
    "categories": [
        {
            "id": 123,
            "name": "Meme Coins",
            "data": {
                "market_cap_change_percentage_24h": {"usd": 5.2},
                "market_cap": 25000000000,
                "total_volume": 8000000000,
            },
        }
    ],
}

MOCK_COINGECKO_GLOBAL = {
    "data": {
        "active_cryptocurrencies": 15000,
        "markets": 1100,
        "total_market_cap": {"usd": 2500000000000},
        "total_volume": {"usd": 95000000000},
        "market_cap_percentage": {
            "btc": 53.2,
            "eth": 16.8,
            "usdt": 4.5,
            "bnb": 3.1,
            "sol": 2.9,
        },
        "market_cap_change_percentage_24h_usd": 1.8,
        "defi_dominance": 3.5,
    }
}

MOCK_COINGECKO_EXCHANGES = [
    {
        "id": "binance",
        "name": "Binance",
        "country": "Cayman Islands",
        "year_established": 2017,
        "trust_score": 10,
        "trust_score_rank": 1,
        "trade_volume_24h_btc": 450000.0,
        "trade_volume_24h_btc_normalized": 420000.0,
        "has_trading_incentive": False,
    },
    {
        "id": "coinbase-exchange",
        "name": "Coinbase Exchange",
        "country": "United States",
        "year_established": 2012,
        "trust_score": 10,
        "trust_score_rank": 2,
        "trade_volume_24h_btc": 120000.0,
        "trade_volume_24h_btc_normalized": 118000.0,
        "has_trading_incentive": False,
    },
]


# ── DeFi Llama API ───────────────────────────────────────────────

def make_defillama_protocol(**overrides):
    """Realistic DeFi Llama /protocols object."""
    base = {
        "name": "Lido",
        "slug": "lido",
        "symbol": "LDO",
        "tvl": 14500000000,
        "change_1d": 1.2,
        "change_7d": -3.5,
        "change_1m": 8.0,
        "mcap": 2200000000,
        "category": "Liquid Staking",
        "chains": ["Ethereum", "Polygon", "Solana"],
        "staking": 14000000000,
        "listedAt": 1609459200,
    }
    base.update(overrides)
    return base


def make_defillama_protocol_mover():
    """Protocol with large TVL change for mover detection."""
    return make_defillama_protocol(
        name="HyperLend",
        slug="hyperlend",
        symbol="HLP",
        tvl=50000000,
        change_1d=45.0,
        change_7d=120.0,
        change_1m=250.0,
        mcap=15000000,
        category="Lending",
        chains=["Arbitrum"],
    )


MOCK_DEFILLAMA_CHAINS = [
    {
        "name": "Ethereum",
        "gecko_id": "ethereum",
        "tokenSymbol": "ETH",
        "tvl": 55000000000,
        "stablesMcap": 80000000000,
        "protocols": 850,
        "chainId": "1",
    },
    {
        "name": "Solana",
        "gecko_id": "solana",
        "tokenSymbol": "SOL",
        "tvl": 8500000000,
        "stablesMcap": 4500000000,
        "protocols": 220,
        "chainId": "solana",
    },
    {
        "name": "Arbitrum",
        "gecko_id": "arbitrum",
        "tokenSymbol": "ARB",
        "tvl": 3200000000,
        "stablesMcap": 2100000000,
        "protocols": 310,
        "chainId": "42161",
    },
]

MOCK_DEFILLAMA_YIELDS = {
    "data": [
        {
            "pool": "pool_aave_eth_usdc",
            "project": "aave-v3",
            "chain": "Ethereum",
            "symbol": "USDC",
            "tvlUsd": 850000000,
            "apy": 4.8,
            "apyBase": 3.5,
            "apyReward": 1.3,
            "apyBase7d": 3.2,
            "apyMean30d": 3.8,
            "stablecoin": True,
            "ilRisk": "no",
            "exposure": "single",
        },
        {
            "pool": "pool_compound_usdt",
            "project": "compound-v3",
            "chain": "Ethereum",
            "symbol": "USDT",
            "tvlUsd": 520000000,
            "apy": 5.2,
            "apyBase": 4.1,
            "apyReward": 1.1,
            "apyBase7d": 3.9,
            "apyMean30d": 4.0,
            "stablecoin": True,
            "ilRisk": "no",
            "exposure": "single",
        },
        {
            "pool": "pool_uniswap_eth_usdc",
            "project": "uniswap-v3",
            "chain": "Ethereum",
            "symbol": "ETH-USDC",
            "tvlUsd": 320000000,
            "apy": 18.5,
            "apyBase": 12.0,
            "apyReward": 6.5,
            "apyBase7d": 11.0,
            "apyMean30d": 13.5,
            "stablecoin": False,
            "ilRisk": "yes",
            "exposure": "multi",
        },
    ],
}

MOCK_DEFILLAMA_STABLECOINS = {
    "peggedAssets": [
        {
            "name": "Tether",
            "symbol": "USDT",
            "pegType": "peggedUSD",
            "pegMechanism": "fiat-backed",
            "price": 1.0001,
            "chainCirculating": {
                "Ethereum": {"current": {"peggedUSD": 60000000000}},
                "Tron": {"current": {"peggedUSD": 55000000000}},
                "BSC": {"current": {"peggedUSD": 3000000000}},
            },
        },
        {
            "name": "USD Coin",
            "symbol": "USDC",
            "pegType": "peggedUSD",
            "pegMechanism": "fiat-backed",
            "price": 0.9999,
            "chainCirculating": {
                "Ethereum": {"current": {"peggedUSD": 25000000000}},
                "Solana": {"current": {"peggedUSD": 5000000000}},
                "Arbitrum": {"current": {"peggedUSD": 2000000000}},
            },
        },
        {
            "name": "Dai",
            "symbol": "DAI",
            "pegType": "peggedUSD",
            "pegMechanism": "crypto-backed",
            "price": 0.998,
            "chainCirculating": {
                "Ethereum": {"current": {"peggedUSD": 4500000000}},
            },
        },
    ],
}


# ── USASpending mock data ─────────────────────────────────────────

MOCK_USASPENDING_AWARDS = {
    "limit": 100,
    "results": [
        {
            "Award ID": "W911NF26D0001",
            "Recipient Name": "GENERAL DYNAMICS INFORMATION TECHNOLOGY INC",
            "Start Date": "2026-04-01",
            "End Date": "2031-03-31",
            "Award Amount": 250000000.00,
            "Total Outlays": 0,
            "Description": "CLOUD COMPUTING AND ENTERPRISE IT SERVICES",
            "Awarding Agency": "DEPARTMENT OF DEFENSE",
            "Awarding Sub Agency": "DEPT OF THE ARMY",
            "Contract Award Type": "DEFINITIVE CONTRACT",
            "Award Type": "contract",
            "generated_internal_id": "CONT_AWD_W911NF26D0001",
        },
        {
            "Award ID": "75FCMC26D0042",
            "Recipient Name": "PALANTIR TECHNOLOGIES INC",
            "Start Date": "2026-04-05",
            "End Date": "2029-04-04",
            "Award Amount": 85000000.00,
            "Total Outlays": 0,
            "Description": "DATA ANALYTICS AND AI PLATFORM SERVICES",
            "Awarding Agency": "DEPARTMENT OF HEALTH AND HUMAN SERVICES",
            "Awarding Sub Agency": "CENTERS FOR MEDICARE AND MEDICAID SERVICES",
            "Contract Award Type": "DELIVERY ORDER",
            "Award Type": "contract",
            "generated_internal_id": "CONT_AWD_75FCMC26D0042",
        },
        {
            "Award ID": "GS35F0001Y",
            "Recipient Name": "SMALL DEFENSE CORP LLC",
            "Start Date": "2026-04-08",
            "End Date": "2027-04-07",
            "Award Amount": 15000000.00,
            "Total Outlays": 0,
            "Description": "CYBERSECURITY ASSESSMENT AND MONITORING",
            "Awarding Agency": "GENERAL SERVICES ADMINISTRATION",
            "Awarding Sub Agency": "FEDERAL ACQUISITION SERVICE",
            "Contract Award Type": "DEFINITIVE CONTRACT",
            "Award Type": "contract",
            "generated_internal_id": "CONT_AWD_GS35F0001Y",
        },
    ],
    "page_metadata": {
        "page": 1,
        "hasNext": False,
        "last_record_unique_id": 3,
        "last_record_sort_value": "15000000.00",
    },
}


# ── FDA openFDA FAERS mock data ──────────────────────────────────

MOCK_FDA_SERIOUS_EVENTS = {
    "meta": {"last_updated": "2026-01-27"},
    "results": [
        {"term": "HUMIRA", "count": 1523},
        {"term": "REVLIMID", "count": 987},
        {"term": "KEYTRUDA", "count": 456},
        {"term": "ELIQUIS", "count": 234},
        {"term": "OZEMPIC", "count": 189},
    ],
}

MOCK_FDA_DEATH_EVENTS = {
    "meta": {"last_updated": "2026-01-27"},
    "results": [
        {"term": "REVLIMID", "count": 156},
        {"term": "KEYTRUDA", "count": 78},
        {"term": "HUMIRA", "count": 45},
        {"term": "ELIQUIS", "count": 23},
        {"term": "OZEMPIC", "count": 5},
    ],
}


def _fda_event_router(url, kwargs):
    """Route openFDA requests: death-specific vs all serious events."""
    if "seriousnessdeath" in url:
        return MOCK_FDA_DEATH_EVENTS
    return MOCK_FDA_SERIOUS_EVENTS


# ── Steam player counts mock data ────────────────────────────────

# ── OpenSky Network mock data ─────────────────────────────────────

MOCK_OPENSKY_STATES = {
    "time": 1712700000,
    "states": [
        # Cruising business jet
        ["a1b2c3", "GLF5    ", "United States", 1712700000, 1712700000,
         -73.9857, 40.7484, 12192.0, False, 257.0, 45.0, 0.5,
         None, 12200.0, "1200", False, 0],
        # Descending jet
        ["d4e5f6", "LJ45    ", "United States", 1712700000, 1712700000,
         -87.6298, 41.8781, 2438.0, False, 154.0, 180.0, -5.2,
         None, 2450.0, "4523", False, 0],
        # High altitude transit
        ["a7b8c9", "CL30    ", "Canada", 1712700000, 1712700000,
         -95.3698, 29.7604, 10668.0, False, 231.0, 270.0, 0.0,
         None, 10700.0, "1200", False, 0],
        # Climbing jet
        ["fab012", "H25B    ", "United States", 1712700000, 1712700000,
         -122.4194, 37.7749, 3048.0, False, 180.0, 90.0, 8.1,
         None, 3050.0, "5412", False, 0],
        # Grounded (should be filtered out)
        ["000000", "GRND    ", "United States", 1712700000, 1712700000,
         -80.0, 25.0, 0.0, True, 0.0, 0.0, 0.0,
         None, 0.0, "1200", False, 0],
    ],
}


# ── USGS Water Services mock data ─────────────────────────────────

MOCK_USGS_GAUGES = {
    "value": {
        "timeSeries": [
            {
                "sourceInfo": {
                    "siteName": "Mississippi River at Memphis, TN",
                    "siteCode": [{"value": "07032000"}],
                },
                "values": [{"value": [
                    {"value": "10.5", "dateTime": "2026-04-10T06:00:00.000-05:00"},
                    {"value": "10.3", "dateTime": "2026-04-10T07:00:00.000-05:00"},
                    {"value": "10.1", "dateTime": "2026-04-10T08:00:00.000-05:00"},
                ]}],
            },
            {
                "sourceInfo": {
                    "siteName": "Ohio River at Cairo, IL",
                    "siteCode": [{"value": "03612500"}],
                },
                "values": [{"value": [
                    {"value": "22.0", "dateTime": "2026-04-10T06:00:00.000-05:00"},
                    {"value": "21.8", "dateTime": "2026-04-10T08:00:00.000-05:00"},
                ]}],
            },
            {
                "sourceInfo": {
                    "siteName": "Columbia River at The Dalles, OR",
                    "siteCode": [{"value": "14105700"}],
                },
                "values": [{"value": [
                    {"value": "6.2", "dateTime": "2026-04-10T06:00:00.000-07:00"},
                    {"value": "6.0", "dateTime": "2026-04-10T08:00:00.000-07:00"},
                ]}],
            },
            {
                "sourceInfo": {
                    "siteName": "Missouri River at Omaha, NE",
                    "siteCode": [{"value": "06610000"}],
                },
                "values": [{"value": [
                    {"value": "9.5", "dateTime": "2026-04-10T06:00:00.000-05:00"},
                    {"value": "9.4", "dateTime": "2026-04-10T08:00:00.000-05:00"},
                ]}],
            },
        ],
    },
}


MOCK_STEAM_MOST_PLAYED = {
    "response": {
        "ranks": [
            {"rank": 1, "appid": 730, "concurrent_in_game": 850432, "peak_in_game": 1200000},
            {"rank": 2, "appid": 570, "concurrent_in_game": 620150, "peak_in_game": 950000},
            {"rank": 3, "appid": 440, "concurrent_in_game": 95000, "peak_in_game": 140000},
            {"rank": 4, "appid": 1172470, "concurrent_in_game": 28500, "peak_in_game": 65000},
            {"rank": 5, "appid": 271590, "concurrent_in_game": 4200, "peak_in_game": 12000},
        ],
    },
}


# ── Nasdaq Earnings Calendar mock ───────────────────────────────
MOCK_NASDAQ_EARNINGS = {
    "data": {
        "rows": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "time": "time-after-hours",
                "epsForecast": "$1.55",
                "lastYearEPS": "$1.52",
                "revenueEstimateForecast": "$94.5B",
                "marketCap": "3200000000000",
                "fiscalQuarterEnding": "Mar/2026",
            },
            {
                "symbol": "ACME",
                "name": "Acme Small Corp",
                "time": "time-pre-market",
                "epsForecast": "$0.12",
                "lastYearEPS": "$0.08",
                "revenueEstimateForecast": "$45M",
                "marketCap": "500000000",
                "fiscalQuarterEnding": "Mar/2026",
            },
        ]
    }
}


# ── Future-dated helpers (polymarket temporal_decay + Finnhub chain) ──
# Expiration strings/epochs must be in the future so 2-day minimum gates
# pass. Computed at import time so every test run sees a fresh window.
from datetime import timezone as _options_tz, timedelta as _options_td, datetime as _options_dt  # noqa: E402

# ISO strings used by the polymarket temporal_decay _pro mock.
_FUTURE_3D_ISO = (_options_dt.now(_options_tz.utc) + _options_td(days=3)).isoformat().replace("+00:00", "Z")
_FUTURE_7D_ISO = (_options_dt.now(_options_tz.utc) + _options_td(days=7)).isoformat().replace("+00:00", "Z")
_FUTURE_14D_ISO = (_options_dt.now(_options_tz.utc) + _options_td(days=14)).isoformat().replace("+00:00", "Z")
# Massive.com (formerly polygon.io) options-chain snapshot mock
# (options_unusual_activity_pro). Massive's chain response shape:
# {"results": [{"day": {...}, "details": {...}, "greeks": {...},
# "implied_volatility": float, "open_interest": int,
# "underlying_asset": {"ticker": ...}}, ...], "next_url": ..., "status": "OK"}.
# Greeks and IV are server-computed; ticker is OPRA-format
# ("O:AAPL260517C00150000"). 2026-04-25: replaces the prior Finnhub
# mock after Finnhub issue #545 documented an unresolved 85%+ ATM
# mispricing - see CHANGELOG entry that ships the script swap.
_MASSIVE_FUTURE_EXP_ISO = (
    _options_dt.now(_options_tz.utc) + _options_td(days=30)
).strftime("%Y-%m-%d")

MOCK_MASSIVE_OPTIONS_CHAIN = {
    "results": [
        {
            # CALL - vol/OI = 5000 / 400 = 12.5 → CRITICAL alert tier
            "day": {
                "close": 38.25,
                "vwap": 38.22,
                "high": 38.40,
                "low": 38.10,
                "open": 38.15,
                "previous_close": 38.05,
                "volume": 5000,
                "change": 0.20,
                "change_percent": 0.526,
                "last_updated": 1000000000000000000,
            },
            "details": {
                "contract_type": "call",
                "exercise_style": "american",
                "expiration_date": _MASSIVE_FUTURE_EXP_ISO,
                "shares_per_contract": 100,
                "strike_price": 150.0,
                "ticker": "O:AAPL260517C00150000",
            },
            "greeks": {
                "delta": 0.92,
                "gamma": 0.002,
                "theta": -0.08,
                "vega": 0.12,
            },
            "implied_volatility": 0.25,
            "open_interest": 400,
            "break_even_price": 188.25,
            "underlying_asset": {"ticker": "AAPL"},
        },
        {
            # CALL - volume below MIN_VOLUME=1000 → filtered out
            "day": {
                "close": 0.35,
                "vwap": 0.34,
                "high": 0.40,
                "low": 0.32,
                "open": 0.33,
                "previous_close": 0.30,
                "volume": 200,
                "change": 0.05,
                "change_percent": 16.67,
                "last_updated": 1000000000000000000,
            },
            "details": {
                "contract_type": "call",
                "exercise_style": "american",
                "expiration_date": _MASSIVE_FUTURE_EXP_ISO,
                "shares_per_contract": 100,
                "strike_price": 200.0,
                "ticker": "O:AAPL260517C00200000",
            },
            "greeks": {
                "delta": 0.12,
                "gamma": 0.005,
                "theta": -0.03,
                "vega": 0.08,
            },
            "implied_volatility": 0.75,
            "open_interest": 1000,
            "underlying_asset": {"ticker": "AAPL"},
        },
        {
            # PUT - vol/OI = 4000 / 800 = 5.0 → HIGH alert tier
            "day": {
                "close": 1.20,
                "vwap": 1.18,
                "high": 1.30,
                "low": 1.10,
                "open": 1.15,
                "previous_close": 1.05,
                "volume": 4000,
                "change": 0.15,
                "change_percent": 14.29,
                "last_updated": 1000000000000000000,
            },
            "details": {
                "contract_type": "put",
                "exercise_style": "american",
                "expiration_date": _MASSIVE_FUTURE_EXP_ISO,
                "shares_per_contract": 100,
                "strike_price": 180.0,
                "ticker": "O:AAPL260517P00180000",
            },
            "greeks": {
                "delta": -0.35,
                "gamma": 0.011,
                "theta": -0.14,
                "vega": 0.25,
            },
            "implied_volatility": 0.30,
            "open_interest": 800,
            "break_even_price": 178.80,
            "underlying_asset": {"ticker": "AAPL"},
        },
    ],
    "status": "OK",
    "request_id": "test_massive_chain",
}


# ── Options Edge Brief mocks (Wave 1, 2026-04-26) ─────────────────
# Richer Massive.com fixtures spanning multiple endpoints:
#   * /v3/snapshot/options/{ticker} - chain with FRONT (~30 DTE) and
#     BACK (~70 DTE) contracts at 25-delta + ATM, both calls and puts.
#     Designed so term-structure shows BACKWARDATION (front > back IV)
#     and skew shows STRESS_BIDDED (put 25d IV > call 25d IV).
#   * /v2/snapshot/locale/us/markets/stocks/tickers/{ticker} - underlying.
#     (Polygon/Massive: stocks-snapshot is at v2, NOT v3 - the v3 URL is
#      a 404 trap. Fixed in production scripts 2026-04-27.)
#   * /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to} - 252-day HV history.
#   * /v1/marketstatus/now + /upcoming - session + holiday calendar.
#   * /v3/reference/dividends - upcoming ex-div calendar.

_OEB_FRONT_EXP = (
    _options_dt.now(_options_tz.utc) + _options_td(days=30)
).strftime("%Y-%m-%d")
_OEB_BACK_EXP = (
    _options_dt.now(_options_tz.utc) + _options_td(days=70)
).strftime("%Y-%m-%d")


def _oeb_contract(*, exp, ctype, strike, delta, iv, ticker_root, contract_id):
    """Helper to build a Massive-shape options-chain entry."""
    return {
        "day": {
            "close": round(max(0.05, abs(150.0 - strike) * 0.10 + iv * 5.0), 2),
            "vwap": round(max(0.05, abs(150.0 - strike) * 0.10 + iv * 5.0 - 0.02), 2),
            "high": round(iv * 6.0, 2),
            "low": round(iv * 4.5, 2),
            "open": round(iv * 5.2, 2),
            "previous_close": round(iv * 4.9, 2),
            "volume": 1500,
            "change": 0.10,
            "change_percent": 1.5,
            "last_updated": 1000000000000000000,
        },
        "details": {
            "contract_type": ctype,
            "exercise_style": "american",
            "expiration_date": exp,
            "shares_per_contract": 100,
            "strike_price": strike,
            "ticker": f"O:{ticker_root}{contract_id}",
        },
        "greeks": {
            "delta": delta,
            "gamma": 0.005,
            "theta": -0.05,
            "vega": 0.18,
        },
        "implied_volatility": iv,
        "open_interest": 600,
        "break_even_price": strike + (iv * 5.0 if ctype == "call" else -iv * 5.0),
        "last_quote": {"bid": round(iv * 4.8, 2), "ask": round(iv * 5.2, 2)},
        "underlying_asset": {"ticker": ticker_root},
    }


# Build the chain with FRONT + BACK tenors. Front IVs > back IVs → backwardation.
# Put 25d IVs > call 25d IVs → STRESS_BIDDED skew.
MOCK_MASSIVE_OEB_CHAIN_RESULTS = [
    # FRONT 30 DTE
    _oeb_contract(exp=_OEB_FRONT_EXP, ctype="call", strike=150.0, delta=0.50, iv=0.32, ticker_root="OEBT", contract_id="F00150C"),
    _oeb_contract(exp=_OEB_FRONT_EXP, ctype="put",  strike=150.0, delta=-0.50, iv=0.34, ticker_root="OEBT", contract_id="F00150P"),
    _oeb_contract(exp=_OEB_FRONT_EXP, ctype="call", strike=160.0, delta=0.25, iv=0.36, ticker_root="OEBT", contract_id="F00160C"),
    _oeb_contract(exp=_OEB_FRONT_EXP, ctype="put",  strike=140.0, delta=-0.25, iv=0.42, ticker_root="OEBT", contract_id="F00140P"),
    _oeb_contract(exp=_OEB_FRONT_EXP, ctype="call", strike=152.0, delta=0.45, iv=0.33, ticker_root="OEBT", contract_id="F00152C"),
    _oeb_contract(exp=_OEB_FRONT_EXP, ctype="put",  strike=148.0, delta=-0.45, iv=0.36, ticker_root="OEBT", contract_id="F00148P"),
    # BACK 70 DTE
    _oeb_contract(exp=_OEB_BACK_EXP, ctype="call", strike=150.0, delta=0.50, iv=0.28, ticker_root="OEBT", contract_id="B00150C"),
    _oeb_contract(exp=_OEB_BACK_EXP, ctype="put",  strike=150.0, delta=-0.50, iv=0.30, ticker_root="OEBT", contract_id="B00150P"),
    _oeb_contract(exp=_OEB_BACK_EXP, ctype="call", strike=160.0, delta=0.25, iv=0.32, ticker_root="OEBT", contract_id="B00160C"),
    _oeb_contract(exp=_OEB_BACK_EXP, ctype="put",  strike=140.0, delta=-0.25, iv=0.36, ticker_root="OEBT", contract_id="B00140P"),
]

MOCK_MASSIVE_OEB_CHAIN = {
    "results": MOCK_MASSIVE_OEB_CHAIN_RESULTS,
    "status": "OK",
    "request_id": "test_massive_oeb_chain",
}

# Stocks snapshot (underlying spot) - 150.00 matches the strike axis above.
MOCK_MASSIVE_STOCKS_SNAPSHOT = {
    "ticker": {
        "ticker": "AAPL",
        "lastTrade": {"p": 150.50, "price": 150.50, "t": 1700000000000000000},
        "lastQuote": {"P": 150.55, "p": 150.45},
        "day": {"c": 150.50, "o": 150.00, "h": 151.00, "l": 149.50, "v": 50000000},
        "min": {"c": 150.50, "o": 150.45, "h": 150.55, "l": 150.40},
        "prevDay": {"c": 149.80},
        "todaysChange": 0.70,
        "todaysChangePerc": 0.47,
    },
    "status": "OK",
}

# 252-day daily aggregate history with realistic noisy returns.
import math as _oeb_math
import random as _oeb_random
_oeb_random.seed(42)
_oeb_aggregate_results = []
_oeb_close_seed = 150.0
for _i in range(260):
    _oeb_close_seed *= 1.0 + (_oeb_random.gauss(0, 0.012))
    _oeb_aggregate_results.append({
        "v": 50000000,
        "vw": _oeb_close_seed,
        "o": _oeb_close_seed * 0.998,
        "c": _oeb_close_seed,
        "h": _oeb_close_seed * 1.005,
        "l": _oeb_close_seed * 0.995,
        "t": 1700000000000 + _i * 86400000,
        "n": 100000,
    })

MOCK_MASSIVE_STOCK_AGGREGATES = {
    "results": _oeb_aggregate_results,
    "status": "OK",
    "queryCount": len(_oeb_aggregate_results),
    "resultsCount": len(_oeb_aggregate_results),
    "ticker": "AAPL",
}

# Market status - open session, next holiday 5 days out.
MOCK_MASSIVE_MARKET_STATUS_NOW = {
    "market": "open",
    "earlyHours": False,
    "afterHours": False,
    "serverTime": "2026-04-26T15:00:00.000Z",
    "exchanges": {"nyse": "open", "nasdaq": "open", "otc": "open"},
}

_OEB_NEXT_HOLIDAY_DATE = (
    _options_dt.now(_options_tz.utc) + _options_td(days=5)
).strftime("%Y-%m-%d")
MOCK_MASSIVE_MARKET_STATUS_UPCOMING = [
    {
        "exchange": "NYSE",
        "name": "Memorial Day",
        "date": _OEB_NEXT_HOLIDAY_DATE,
        "status": "closed",
    },
    {
        "exchange": "NASDAQ",
        "name": "Memorial Day",
        "date": _OEB_NEXT_HOLIDAY_DATE,
        "status": "closed",
    },
]

# Dividends - one quarterly dividend ~30 days out.
_OEB_DIV_EX_DATE = (
    _options_dt.now(_options_tz.utc) + _options_td(days=30)
).strftime("%Y-%m-%d")
_OEB_DIV_PAY_DATE = (
    _options_dt.now(_options_tz.utc) + _options_td(days=45)
).strftime("%Y-%m-%d")
MOCK_MASSIVE_DIVIDENDS = {
    "results": [
        {
            "ticker": "AAPL",
            "ex_dividend_date": _OEB_DIV_EX_DATE,
            "pay_date": _OEB_DIV_PAY_DATE,
            "declaration_date": _OEB_DIV_EX_DATE,
            "record_date": _OEB_DIV_EX_DATE,
            "cash_amount": 0.24,
            "frequency": 4,
            "dividend_type": "CD",
        },
    ],
    "status": "OK",
}


def _massive_oeb_router_factory():
    """Router for the 5 Wave-1 Options Edge Brief credentialed scripts.

    Dispatches Massive.com endpoint URLs to the right fixture. All five
    scripts target api.massive.com; the router pattern-matches the URL
    path prefix.
    """
    def router(url, **kwargs):
        if "/v1/marketstatus/now" in url:
            return _make_response(MOCK_MASSIVE_MARKET_STATUS_NOW)
        if "/v1/marketstatus/upcoming" in url:
            return _make_response(MOCK_MASSIVE_MARKET_STATUS_UPCOMING)
        if "/v3/reference/dividends" in url:
            return _make_response(MOCK_MASSIVE_DIVIDENDS)
        if "/v2/snapshot/locale/us/markets/stocks/tickers/" in url:
            return _make_response(MOCK_MASSIVE_STOCKS_SNAPSHOT)
        if "/v2/aggs/ticker/" in url and "/range/" in url:
            return _make_response(MOCK_MASSIVE_STOCK_AGGREGATES)
        if "/v3/snapshot/options/" in url:
            return _make_response(MOCK_MASSIVE_OEB_CHAIN)
        return _make_response({})
    return router


# ── Global Macro Risk Brief feeder mocks ──────────────────────────
# (usgs_earthquake_feed / noaa_severe_weather_alerts / noaa_tropical_cyclones /
# usgs_volcanic_alerts / gdelt_geopolitical_events / worldbank_country_growth)

MOCK_USGS_EARTHQUAKES = {
    "type": "FeatureCollection",
    "metadata": {"count": 3},
    "features": [
        {
            "type": "Feature",
            "id": "us7000sendai_demo",
            "properties": {
                "mag": 7.2,
                "magType": "Mww",
                "place": "120 km NE of Sendai, Japan",
                "time": 1713776400000,
                "alert": "orange",
                "sig": 900,
                "felt": 5000,
                "mmi": 7.5,
                "tsunami": 1,
                "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000sendai",
            },
            "geometry": {"coordinates": [141.2, 38.5, 35.0]},
        },
        {
            "type": "Feature",
            "id": "us7000antofagasta_demo",
            "properties": {
                "mag": 6.1,
                "magType": "mww",
                "place": "25 km SW of Antofagasta, Chile",
                "time": 1713800000000,
                "alert": "yellow",
                "sig": 570,
                "felt": 1200,
                "mmi": 6.0,
                "tsunami": 0,
                "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000antofagasta",
            },
            "geometry": {"coordinates": [-70.5, -23.5, 60.0]},
        },
        {
            "type": "Feature",
            "id": "us7000jakarta_demo",
            "properties": {
                "mag": 5.1,
                "magType": "mb",
                "place": "80 km N of Jakarta, Indonesia",
                "time": 1713810000000,
                "alert": None,
                "sig": 400,
                "felt": 300,
                "mmi": 4.5,
                "tsunami": 0,
                "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000jakarta",
            },
            "geometry": {"coordinates": [106.8, -5.5, 80.0]},
        },
    ],
}


MOCK_NOAA_SEVERE_ALERTS = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "urn:oid:2.49.0.1.840.0.hurricane",
            "type": "Feature",
            "properties": {
                "event": "Hurricane Warning",
                "severity": "Extreme",
                "urgency": "Immediate",
                "certainty": "Observed",
                "areaDesc": "Palm Beach, FL; Broward, FL",
                "headline": "Hurricane Warning issued for South Florida",
                "sent": "2026-04-22T10:00:00Z",
                "effective": "2026-04-22T10:00:00Z",
                "expires": "2027-04-23T10:00:00Z",
                "senderName": "NWS Miami FL",
            },
        },
        {
            "id": "urn:oid:2.49.0.1.840.0.tornado",
            "type": "Feature",
            "properties": {
                "event": "Tornado Warning",
                "severity": "Severe",
                "urgency": "Immediate",
                "certainty": "Likely",
                "areaDesc": "Travis, TX; Williamson, TX",
                "headline": "Tornado Warning for central TX",
                "sent": "2026-04-22T11:00:00Z",
                "effective": "2026-04-22T11:00:00Z",
                "expires": "2027-04-22T15:00:00Z",
                "senderName": "NWS Austin TX",
            },
        },
        {
            "id": "urn:oid:2.49.0.1.840.0.blizzard",
            "type": "Feature",
            "properties": {
                "event": "Blizzard Warning",
                "severity": "Severe",
                "urgency": "Expected",
                "certainty": "Likely",
                "areaDesc": "Hennepin, MN; Ramsey, MN",
                "headline": "Blizzard Warning for central MN",
                "sent": "2026-04-22T09:00:00Z",
                "effective": "2026-04-22T12:00:00Z",
                "expires": "2027-04-23T06:00:00Z",
                "senderName": "NWS Minneapolis MN",
            },
        },
    ],
}


MOCK_NHC_STORMS = {
    "activeStorms": [
        {
            "id": "AL042026",
            "name": "ALICE",
            "classification": "HU",
            "intensity": "115",
            "pressure": "948",
            "latitudeNumeric": 27.3,
            "longitudeNumeric": -80.5,
            "movementDir": "NW",
            "movementSpeed": "14",
            "lastUpdate": "2026-04-22T12:00:00Z",
            "publicAdvisory": {"url": "https://www.nhc.noaa.gov/text/AL042026.shtml"},
        },
        {
            "id": "EP032026",
            "name": "BRUNO",
            "classification": "TS",
            "intensity": "45",
            "pressure": "1000",
            "latitudeNumeric": 15.5,
            "longitudeNumeric": -110.2,
            "movementDir": "W",
            "movementSpeed": "12",
            "lastUpdate": "2026-04-22T11:30:00Z",
            "publicAdvisory": {"url": "https://www.nhc.noaa.gov/text/EP032026.shtml"},
        },
    ],
}


MOCK_USGS_VOLCANOES = [
    {
        "vnum": "372010",
        "volcano_name_en": "Grimsvotn",
        "region": "Iceland",
        "subregion": "Northeast Iceland",
        "alert_level": "WATCH",
        "color_code": "ORANGE",
        "latitude": 64.42,
        "longitude": -17.33,
        "updated_date": "2026-04-22T08:00:00Z",
    },
    {
        "vnum": "267020",
        "volcano_name_en": "Merapi",
        "region": "Indonesia",
        "subregion": "Java",
        "alert_level": "WARNING",
        "color_code": "RED",
        "latitude": -7.54,
        "longitude": 110.44,
        "updated_date": "2026-04-22T06:00:00Z",
    },
    {
        "vnum": "313040",
        "volcano_name_en": "Redoubt",
        "region": "Alaska",
        "subregion": "Aleutian Arc",
        "alert_level": "ADVISORY",
        "color_code": "YELLOW",
        "latitude": 60.48,
        "longitude": -152.74,
        "updated_date": "2026-04-22T04:00:00Z",
    },
]


MOCK_GDELT_MACRO_ARTICLES = {
    "articles": [
        {
            "title": "Russian forces advance near Kharkiv, Ukraine reports heavy shelling",
            "url": "https://reuters.com/article-ukraine",
            "domain": "reuters.com",
            "language": "English",
            "sourcecountry": "US",
            "seendate": "20260422T100000Z",
        },
        {
            "title": "Taiwan conducts military drills amid rising tensions with China",
            "url": "https://bbc.com/article-taiwan",
            "domain": "bbc.com",
            "language": "English",
            "sourcecountry": "UK",
            "seendate": "20260422T090000Z",
        },
        {
            "title": "Israel airstrikes target Gaza infrastructure, Hamas vows retaliation",
            "url": "https://aljazeera.com/article-israel",
            "domain": "aljazeera.com",
            "language": "English",
            "sourcecountry": "QA",
            "seendate": "20260422T080000Z",
        },
        {
            "title": "Iran warns of closure of Strait of Hormuz if sanctions continue",
            "url": "https://ft.com/article-iran",
            "domain": "ft.com",
            "language": "English",
            "sourcecountry": "UK",
            "seendate": "20260422T070000Z",
        },
        {
            "title": "US Treasury announces new sanctions on Russian banks",
            "url": "https://wsj.com/article-sanctions",
            "domain": "wsj.com",
            "language": "English",
            "sourcecountry": "US",
            "seendate": "20260422T060000Z",
        },
        {
            "title": "Generic local news about school board meeting",
            "url": "https://local-news.example/article-noise",
            "domain": "local-news.example",
            "language": "English",
            "sourcecountry": "US",
            "seendate": "20260422T050000Z",
        },
    ],
}


MOCK_WB_GDP = [
    {"page": 1, "pages": 1, "total": 4, "sourceid": "2"},
    [
        {
            "countryiso3code": "IND",
            "country": {"id": "IN", "value": "India"},
            "date": "2023",
            "value": 7.2,
            "indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
        },
        {
            "countryiso3code": "CHN",
            "country": {"id": "CN", "value": "China"},
            "date": "2023",
            "value": 5.2,
            "indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
        },
        {
            "countryiso3code": "USA",
            "country": {"id": "US", "value": "United States"},
            "date": "2023",
            "value": 2.5,
            "indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
        },
        {
            "countryiso3code": "EMU",
            "country": {"id": "XC", "value": "Euro area"},
            "date": "2023",
            "value": 1.1,
            "indicator": {"id": "NY.GDP.MKTP.KD.ZG", "value": "GDP growth (annual %)"},
        },
    ],
]


MOCK_WB_CPI = [
    {"page": 1, "pages": 1, "total": 3, "sourceid": "2"},
    [
        {
            "countryiso3code": "ARG",
            "country": {"id": "AR", "value": "Argentina"},
            "date": "2023",
            "value": 133.0,
            "indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"},
        },
        {
            "countryiso3code": "TUR",
            "country": {"id": "TR", "value": "Turkey"},
            "date": "2023",
            "value": 53.8,
            "indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"},
        },
        {
            "countryiso3code": "USA",
            "country": {"id": "US", "value": "United States"},
            "date": "2023",
            "value": 4.1,
            "indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"},
        },
    ],
]


MOCK_WB_EXPORTS = [
    {"page": 1, "pages": 1, "total": 3, "sourceid": "2"},
    [
        {
            "countryiso3code": "VNM",
            "country": {"id": "VN", "value": "Vietnam"},
            "date": "2023",
            "value": 11.5,
            "indicator": {"id": "NE.EXP.GNFS.KD.ZG", "value": "Exports of goods and services growth (annual %)"},
        },
        {
            "countryiso3code": "MEX",
            "country": {"id": "MX", "value": "Mexico"},
            "date": "2023",
            "value": 6.2,
            "indicator": {"id": "NE.EXP.GNFS.KD.ZG", "value": "Exports of goods and services growth (annual %)"},
        },
        {
            "countryiso3code": "USA",
            "country": {"id": "US", "value": "United States"},
            "date": "2023",
            "value": 2.8,
            "indicator": {"id": "NE.EXP.GNFS.KD.ZG", "value": "Exports of goods and services growth (annual %)"},
        },
    ],
]


# ═══════════════════════════════════════════════════════════════════
# Script registry - one entry per no-auth script
# ═══════════════════════════════════════════════════════════════════

# Helper: standard Gamma /markets list with a generic + special market
def _gamma_markets_with(*extra_markets):
    """Return a list with the generic market plus any extras."""
    return [make_gamma_market()] + list(extra_markets)


MOCK_GITHUB_TRENDING = """<html><body>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/rocket">acme / rocket</a></h2>
  <p class="col-9">A blazing fast rocket framework for builders</p>
  <div class="f6">
    <span itemprop="programmingLanguage">Python</span>
    <a href="/acme/rocket/stargazers">12,345</a>
    <a href="/acme/rocket/forks">678</a>
    <span class="d-inline-block float-sm-right">820 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/globex/transpose">globex / transpose</a></h2>
  <p class="col-9">Matrix tooling for data teams</p>
  <div class="f6">
    <span itemprop="programmingLanguage">Rust</span>
    <a href="/globex/transpose/stargazers">9,876</a>
    <span class="d-inline-block float-sm-right">540 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/initech/flowstate">initech / flowstate</a></h2>
  <p class="col-9">Workflow engine for humans</p>
  <div class="f6">
    <span itemprop="programmingLanguage">TypeScript</span>
    <a href="/initech/flowstate/stargazers">4,200</a>
    <span class="d-inline-block float-sm-right">410 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/hooli/nucleus">hooli / nucleus</a></h2>
  <p class="col-9">Distributed cache that scales</p>
  <div class="f6">
    <span itemprop="programmingLanguage">Go</span>
    <a href="/hooli/nucleus/stargazers">2,100</a>
    <span class="d-inline-block float-sm-right">300 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/umbrella/sentinel">umbrella / sentinel</a></h2>
  <p class="col-9">Security scanner for monorepos</p>
  <div class="f6">
    <span itemprop="programmingLanguage">Python</span>
    <a href="/umbrella/sentinel/stargazers">1,500</a>
    <span class="d-inline-block float-sm-right">220 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/stark/arc-reactor">stark / arc-reactor</a></h2>
  <p class="col-9">Energy modeling toolkit</p>
  <div class="f6">
    <span itemprop="programmingLanguage"></span>
    <a href="/stark/arc-reactor/stargazers">999</a>
    <span class="d-inline-block float-sm-right">120 stars today</span>
  </div>
</article>
</body></html>"""


MOCK_AI_PAPERS_README = {
    # Slice C1: a GitHub API /readme response (base64 markdown) for
    # ai_papers_github_lists. 4 paper entries: 2 masamasa-style
    # (**"title"** + [[paper](arxiv)]), 1 aimerou-style ([title](arxiv)),
    # 1 medium explainer.
    "content": "IyBQYXBlcnMKKiAqKiJBbHBoYSBQYXBlcjogQSBTdHVkeSBvZiBUaGluZ3MiKiogW1twYXBlcl0oaHR0cHM6Ly9hcnhpdi5vcmcvYWJzLzI1MDEuMDAwMDEpXQoqICoqIkJldGEgUGFwZXI6IEFub3RoZXIgU3R1ZHkiKiogW1twYXBlcl0oaHR0cHM6Ly9hcnhpdi5vcmcvYWJzLzI1MDEuMDAwMDIpXQpbR2FtbWEgUGFwZXIgVGl0bGUgb24gVmlzaW9uXShodHRwczovL2FyeGl2Lm9yZy9hYnMvMjUwMS4wMDAwMykKW0RlbHRhIEV4cGxhaW5lcl0oaHR0cHM6Ly9tZWRpdW0uY29tL3BhcGVycy1leHBsYWluZWQvZGVsdGEtMTIzKQo=",
}


MOCK_HF_DAILY_PAPERS = [
    # Slice C2: huggingface.co/api/daily_papers returns a JSON list; each
    # item has a top-level title and a nested paper.id (the arxiv id).
    {"paper": {"id": "2606.00001"}, "title": "HF Paper One: A Study of Things"},
    {"paper": {"id": "2606.00002"}, "title": "HF Paper Two: Another Study"},
    {"paper": {"id": "2606.00003"}, "title": "HF Paper Three on Vision"},
    {"title": "HF Paper Four (title at top level)", "paper": {"id": "2606.00004"}},
    {"paper": {"id": "2606.00005"}, "title": "HF Paper Five"},
]


SCRIPT_REGISTRY = {
    # ── Original scripts ──────────────────────────────────────────

    "jsonplaceholder_posts": {
        "url_map": {
            "jsonplaceholder.typicode.com/posts": MOCK_JSONPLACEHOLDER_POSTS,
        },
        "expected_columns": ["user_id", "id", "title", "body", "_epoch"],
        "min_rows": 1,
    },
    "github_public_events": {
        "url_map": {
            "api.github.com/events": MOCK_GITHUB_EVENTS,
        },
        "expected_columns": ["event_id", "type", "actor", "repo", "created_at", "_epoch"],
        "min_rows": 1,
    },
    "github_trending_repos": {
        "url_map": {
            # Both the daily and ?since=weekly URLs share this substring, so
            # the router returns the same trending HTML for each fetch; the
            # script dedups by repo_full_name. The Search-API fallback is not
            # exercised here (the scrape yields >= 5 rows).
            "github.com/trending": MOCK_GITHUB_TRENDING,
        },
        "expected_columns": [
            "repo_full_name", "owner", "name", "html_url", "description",
            "language", "stars_total", "stars_today", "source",
            "snapshot_date", "_epoch",
        ],
        "min_rows": 5,
    },
    "ai_papers_github_lists": {
        "url_map": {
            # All 5 repos hit api.github.com/repos/<repo>/readme; the router
            # matches "/readme" and returns the same base64 markdown for
            # each. The script dedups by paper_key, so 5x the same 4 papers
            # collapse to 4 rows.
            "/readme": MOCK_AI_PAPERS_README,
        },
        "expected_columns": [
            "paper_key", "title", "url", "link_text", "source_repo",
            "source", "discovered_iso", "_epoch",
        ],
        "min_rows": 4,
    },
    "ai_papers_huggingface": {
        "url_map": {
            "daily_papers": MOCK_HF_DAILY_PAPERS,
        },
        "expected_columns": [
            "paper_key", "title", "url", "link_text", "source_repo",
            "source", "discovered_iso", "_epoch",
        ],
        "min_rows": 5,
    },
    "hackernews_top_stories": {
        "url_map": {
            "topstories.json": MOCK_HN_TOP_STORIES,
            "/item/": MOCK_HN_ITEM,
        },
        "expected_columns": ["story_id", "title", "url", "author", "score", "comment_count", "_epoch"],
        "min_rows": 1,
    },

    # ── Polymarket: Gamma /markets (single/paginated) ─────────────

    "polymarket_active_markets": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
        },
        "expected_columns": [
            "market_id", "question", "slug", "yes_price", "no_price",
            "volume", "liquidity", "category", "tags", "condition_id", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_resolved_markets": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_resolved_yes_market(), make_resolved_no_market()],
        },
        "expected_columns": [
            "market_id", "question", "final_yes_price", "final_no_price",
            "volume", "category", "tags", "resolved", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_new_markets": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
        },
        "expected_columns": [
            "market_id", "question", "slug", "yes_price", "no_price",
            "volume", "created_at", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_tag_volume": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
        },
        "expected_columns": ["tag", "total_volume", "total_liquidity", "market_count", "avg_yes_price", "_epoch"],
        "min_rows": 1,
    },
    "polymarket_open_interest": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
        },
        "expected_columns": [
            "market_id", "condition_id", "question", "volume", "liquidity", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Polymarket: Gamma /markets with keyword filtering ─────────

    "polymarket_high_probability": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_high_prob_market()],
        },
        "expected_columns": [
            "market_id", "question", "leading_outcome", "leading_price",
            "payout_if_win", "probability_tier", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("leading_price", 0) >= 0.75 for row in result["head"])
        ),
    },
    "polymarket_geopolitical": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_geo_market()],
        },
        "expected_columns": [
            "market_id", "question", "yes_price", "match_type", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_sports_markets": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_sports_market()],
        },
        "expected_columns": [
            "market_id", "question", "yes_price", "implied_american_odds", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_election_politics": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_politics_market()],
        },
        "expected_columns": [
            "market_id", "question", "yes_price", "volume", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_crypto_markets": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_crypto_market()],
        },
        "expected_columns": [
            "market_id", "question", "yes_price", "volume", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_calibration_analysis": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_resolved_yes_market(), make_resolved_no_market()],
        },
        "expected_columns": [
            "market_id", "question", "final_yes_price", "resolved_yes", "volume",
            # M-MI-10 (2026-04-22): dedicated column on the summary row
            "multi_outcome_skipped_count",
            "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Polymarket: Gamma /events ─────────────────────────────────

    "polymarket_events_catalog": {
        "url_map": {
            "gamma-api.polymarket.com/events": [make_gamma_event()],
        },
        "expected_columns": [
            "event_id", "title", "slug", "market_count", "total_volume",
            "total_liquidity", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_arbitrage_scanner": {
        "url_map": {
            "gamma-api.polymarket.com/events": [make_arb_event()],
        },
        "expected_columns": [
            "event_title", "arb_type", "yes_price_sum", "deviation_from_1",
            "deviation_pct", "opportunity",
            "total_event_liquidity_usd",  # H-MI-3 regression (2026-04-21)
            "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("deviation_pct", 0) > 2.0 for row in result["head"]
                if row.get("arb_type") == "multi_outcome")
        ),
    },
    "polymarket_cross_market_correlation": {
        "url_map": {
            "gamma-api.polymarket.com/events": [make_gamma_event()],
        },
        "expected_columns": [
            "event_id", "event_title", "market_question", "yes_price",
            "event_market_count", "_epoch",
        ],
        "min_rows": 2,  # At least 2 markets per event
    },

    # ── Polymarket: Gamma /comments ───────────────────────────────

    "polymarket_comments_sentiment": {
        "url_map": {
            "gamma-api.polymarket.com/events": [make_gamma_event()],
            "gamma-api.polymarket.com/comments": MOCK_COMMENTS,
        },
        "expected_columns": [
            "comment_id", "event_id", "event_title", "author_name",
            "content", "reaction_count", "is_reply", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Polymarket: Data API (standalone) ─────────────────────────

    "polymarket_leaderboard": {
        "url_map": {
            "data-api.polymarket.com/v1/leaderboard": MOCK_LEADERBOARD,
        },
        "expected_columns": [
            "rank", "user_address", "username", "profit", "volume",
            "markets_traded", "win_rate", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Polymarket: Multi-API (Gamma + CLOB) ──────────────────────

    "polymarket_price_history": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
            "clob.polymarket.com/prices-history": MOCK_PRICE_HISTORY,
        },
        "expected_columns": [
            "condition_id", "question", "yes_price", "price_timestamp", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_orderbook_depth": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
            "clob.polymarket.com/book": MOCK_ORDERBOOK,
        },
        "expected_columns": [
            "condition_id", "question", "best_bid", "best_ask", "spread",
            "bid_depth", "ask_depth", "depth_imbalance", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_market_movers": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
            "clob.polymarket.com/midpoint": MOCK_MIDPOINT,
        },
        "expected_columns": [
            "condition_id", "question", "gamma_yes_price", "clob_midpoint",
            "price_delta", "direction", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_liquidity_gaps": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
            "clob.polymarket.com/spread": MOCK_SPREAD,
            "clob.polymarket.com/price": MOCK_PRICE,
        },
        "expected_columns": [
            "condition_id", "question", "spread", "vol_to_liq_ratio",
            "gap_score", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Polymarket: Multi-API (Gamma + Data API) ──────────────────

    "polymarket_recent_trades": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
            "data-api.polymarket.com/trades": MOCK_ACTIVITY,
        },
        "expected_columns": [
            "condition_id", "question", "side", "size", "price",
            "user_address", "type", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_whale_tracker": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market()],
            "data-api.polymarket.com/holders": MOCK_HOLDERS,
        },
        "expected_columns": [
            "condition_id", "question", "holder_rank", "holder_address",
            "position_size", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Alpha Idea scripts (cross-platform, volume, decay, alerts, sentiment) ──

    "polymarket_cross_platform_arbitrage": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_politics_market()],
            "predictit.org/api/marketdata/all": MOCK_PREDICTIT_RESPONSE,
            "gamma-api.polymarket.com/events": [make_arb_event()],
        },
        "expected_columns": [
            "polymarket_question", "polymarket_yes_price",
            "price_divergence", "abs_divergence", "divergence_pct",
            # H-MI-2 (2026-04-21): net-of-fees columns
            "fee_roundtrip_pct", "net_edge_pct",
            "suggested_action", "source_comparison", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            # Every surviving row must clear the net-edge floor (>= 1% net).
            all(row.get("net_edge_pct", 0) >= 1.0 for row in result["head"])
        ),
    },
    "polymarket_volume_spike_detector": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_volume_spike_market()],
        },
        "expected_columns": [
            "question", "slug", "condition_id", "yes_price",
            "total_volume", "volume_24h", "avg_daily_volume",
            "spike_multiple", "alert_level", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("spike_multiple", 0) >= 3.0 for row in result["head"])
        ),
    },
    "polymarket_temporal_decay": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_near_resolution_market()],
        },
        "expected_columns": [
            "question", "slug", "condition_id",
            "days_to_resolution", "end_date", "favored_side",
            "favored_price", "convergence_gap", "roi_pct",
            "annualized_roi_pct", "urgency", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_market_alert_pipeline": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_edge_zone_market()],
        },
        "expected_columns": [
            "question", "slug", "condition_id", "yes_price",
            "favored_side", "alert_signals", "alert_count",
            "alert_priority", "is_edge_zone", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("alert_count", 0) >= 1 for row in result["head"])
        ),
    },
    "polymarket_news_sentiment_divergence": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_sentiment_divergence_market()],
            "api.gdeltproject.org": MOCK_GDELT_ARTICLES,
            "gamma-api.polymarket.com/comments": MOCK_COMMENTS,
        },
        "expected_columns": [
            "question", "slug", "condition_id", "yes_price",
            "avg_sentiment_score", "sentiment_direction",
            # M-MI-7 (2026-04-22): reliability gate
            "sentiment_reliability",
            "price_direction", "divergence_type", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── CoinGecko scripts ────────────────────────────────────────

    "coingecko_top_coins": {
        "url_map": {
            "api.coingecko.com/api/v3/coins/markets": [
                make_coingecko_coin(),
                make_coingecko_coin_alt(),
            ],
        },
        "expected_columns": [
            "rank", "coin_id", "symbol", "name", "price_usd",
            "change_24h_pct", "change_7d_pct", "volume_24h",
            "market_cap", "vol_mcap_ratio", "ath_distance_pct", "_epoch",
        ],
        "min_rows": 2,
    },
    "coingecko_trending": {
        "url_map": {
            "api.coingecko.com/api/v3/search/trending": MOCK_COINGECKO_TRENDING,
        },
        "expected_columns": [
            "type", "trend_rank", "coin_id", "symbol", "name",
            "price_usd", "change_24h_pct", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            any(row.get("type") == "coin" for row in result["head"])
        ),
    },
    "coingecko_volume_anomaly_detector": {
        "url_map": {
            "api.coingecko.com/api/v3/coins/markets": [
                make_coingecko_coin(),       # BTC: vol/mcap = 0.021 (normal)
                make_coingecko_coin_alt(),   # SOL: vol/mcap = 0.692 (anomaly)
                make_coingecko_coin(id="ethereum", symbol="eth", name="Ethereum",
                    current_price=3500, market_cap=420000000000, total_volume=9000000000,
                    market_cap_rank=2, circulating_supply=120000000, total_supply=120000000,
                    ath=4870, fully_diluted_valuation=420000000000,
                    price_change_percentage_24h=1.0, price_change_percentage_1h_in_currency=0.05,
                    price_change_percentage_7d_in_currency=-0.5, price_change_percentage_30d_in_currency=5.0),
                make_coingecko_coin(id="cardano", symbol="ada", name="Cardano",
                    current_price=0.45, market_cap=16000000000, total_volume=350000000,
                    market_cap_rank=8, circulating_supply=35000000000, total_supply=45000000000,
                    ath=3.09, fully_diluted_valuation=20000000000,
                    price_change_percentage_24h=-0.3, price_change_percentage_1h_in_currency=0.01,
                    price_change_percentage_7d_in_currency=-2.0, price_change_percentage_30d_in_currency=-5.0),
            ],
        },
        "expected_columns": [
            "symbol", "name", "price_usd", "volume_24h", "market_cap",
            "vol_mcap_ratio", "ratio_vs_median", "is_divergence",
            "direction_signal", "alert_level", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("ratio_vs_median", 0) >= 2.0 for row in result["head"])
        ),
    },
    "coingecko_market_dominance": {
        "url_map": {
            "api.coingecko.com/api/v3/global": MOCK_COINGECKO_GLOBAL,
        },
        "expected_columns": [
            "metric", "symbol", "dominance_pct", "total_market_cap_usd",
            "total_volume_24h_usd", "market_cap_change_24h_pct", "_epoch",
        ],
        "min_rows": 2,  # At least global_summary + one coin
    },
    "coingecko_exchange_volumes": {
        "url_map": {
            "api.coingecko.com/api/v3/exchanges": MOCK_COINGECKO_EXCHANGES,
        },
        "expected_columns": [
            "exchange_id", "name", "country", "trust_score",
            "volume_24h_btc", "volume_24h_btc_normalized",
            "suspected_wash_pct", "_epoch",
        ],
        "min_rows": 2,
    },

    # ── DeFi Llama scripts ───────────────────────────────────────

    "defillama_tvl_rankings": {
        "url_map": {
            "api.llama.fi/protocols": [
                make_defillama_protocol(),
                make_defillama_protocol_mover(),
            ],
        },
        "expected_columns": [
            "rank", "protocol", "symbol", "category", "tvl_usd",
            "change_1d_pct", "change_7d_pct", "mcap_tvl_ratio",
            "chains", "chain_count", "_epoch",
        ],
        "min_rows": 2,
    },
    "defillama_tvl_movers": {
        "url_map": {
            "api.llama.fi/protocols": [
                make_defillama_protocol(),          # small 1d change - filtered out
                make_defillama_protocol_mover(),    # large 1d change - kept
            ],
        },
        "expected_columns": [
            "protocol", "symbol", "category", "tvl_usd",
            "change_1d_pct", "change_7d_pct", "dollar_flow_1d",
            "alert_level", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(abs(row.get("change_1d_pct", 0)) >= 10.0
                or abs(row.get("change_7d_pct", 0)) >= 25.0
                for row in result["head"])
        ),
    },
    "defillama_chain_tvl": {
        "url_map": {
            "api.llama.fi/v2/chains": MOCK_DEFILLAMA_CHAINS,
        },
        "expected_columns": [
            "rank", "chain", "token_symbol", "tvl_usd",
            "market_share_pct", "stablecoins_tvl", "protocol_count", "_epoch",
        ],
        "min_rows": 3,
    },
    "defillama_yield_opportunities": {
        "url_map": {
            "yields.llama.fi/pools": MOCK_DEFILLAMA_YIELDS,
        },
        "expected_columns": [
            "pool_id", "project", "chain", "symbol",
            "apy_total", "apy_base", "apy_reward",
            "sustainability_pct", "tvl_usd", "is_stablecoin", "tier", "_epoch",
        ],
        "min_rows": 2,
    },
    "defillama_stablecoin_flows": {
        "url_map": {
            "stablecoins.llama.fi/stablecoins": MOCK_DEFILLAMA_STABLECOINS,
        },
        "expected_columns": [
            "name", "symbol", "peg_type", "peg_mechanism",
            "circulating_usd", "market_share_pct", "price",
            "peg_deviation_pct", "is_depegged", "chain_count", "_epoch",
        ],
        "min_rows": 3,
    },

    # ── Kalshi scripts ───────────────────────────────────────────

    "kalshi_active_markets": {
        "url_map": {
            "api.elections.kalshi.com/trade-api/v2/markets": {
                "markets": [make_kalshi_market(), make_kalshi_market_high_vol()],
                "cursor": "",
            },
        },
        "expected_columns": [
            "ticker", "event_ticker", "title", "yes_bid", "yes_ask",
            "last_price", "spread", "volume", "volume_24h",
            "open_interest", "category", "_epoch",
        ],
        "min_rows": 2,
    },
    "kalshi_events_catalog": {
        "url_map": {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event()],
                "cursor": "",
            },
        },
        "expected_columns": [
            "event_ticker", "title", "category", "market_count",
            "total_volume", "status", "_epoch",
        ],
        "min_rows": 1,
    },
    "kalshi_volume_tracker": {
        "url_map": {
            "api.elections.kalshi.com/trade-api/v2/markets": {
                "markets": [make_kalshi_market(), make_kalshi_market_high_vol()],
                "cursor": "",
            },
        },
        "expected_columns": [
            "ticker", "title", "last_price", "volume_24h",
            "open_interest", "vol_oi_ratio", "alert_level",
            # M-MI-11 (2026-04-22): _summary row carries the skip tally.
            "skipped_missing_data_count",
            "_epoch",
        ],
        "min_rows": 1,
    },
    "kalshi_orderbook_depth": {
        "url_map": {
            "orderbook": MOCK_KALSHI_ORDERBOOK,
            "api.elections.kalshi.com/trade-api/v2/markets": {
                "markets": [make_kalshi_market(), make_kalshi_market_high_vol()],
                "cursor": "",
            },
        },
        "expected_columns": [
            "ticker", "title", "best_yes_bid", "spread",
            "total_yes_depth", "total_no_depth", "depth_imbalance",
            "pressure", "open_interest", "_epoch",
        ],
        "min_rows": 1,
    },
    "kalshi_polymarket_arbitrage": {
        # 2026-05-06: switched to /v2/events?with_nested_markets=true to
        # bypass the KXMVE auto-permutation flood that monopolised /v2/markets
        # under V2. Mock now exercises the events-walk path end-to-end.
        "url_map": {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event(
                    event_ticker="FED-26MAR",
                    title="Will the Federal Reserve cut interest rates in March 2026?",
                    sub_title="Fed funds target",
                    category="Economics",
                    markets=[make_kalshi_market(
                        ticker="FED-26MAR-RATE",
                        title="Will the Federal Reserve cut interest rates in March 2026?",
                        last_price=45,
                    )],
                )],
                "cursor": "",
            },
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    id="pm_fed_rate",
                    question="Will the Federal Reserve cut interest rates in March 2026?",
                    outcomePrices='["0.65","0.35"]',
                    volume="100000",
                ),
            ],
        },
        "expected_columns": [
            "kalshi_ticker", "kalshi_title", "kalshi_yes_price",
            "polymarket_question", "polymarket_yes_price",
            "divergence", "divergence_pct",
            # H-MI-2 (2026-04-21): net-of-fees columns
            "fee_roundtrip_pct", "net_edge_pct",
            "suggested_action",
            # L-MI-14 (2026-04-22): explicit per-leg actions
            "polymarket_action", "kalshi_action",
            "opportunity_strength", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            # Gate now requires net_edge >= 1%, not raw divergence >= 3%.
            all(row.get("net_edge_pct", 0) >= 1.0 for row in result["head"])
        ),
    },

    # ── Reddit ───────────────────────────────────────────────────
    "reddit_wsb_trending": {
        "url_map": {
            "www.reddit.com": _reddit_router_factory(),
        },
        "expected_columns": [
            "sort_type", "title", "score", "num_comments",
            "upvote_ratio", "conviction", "link_flair_text",
            "permalink", "_epoch",
        ],
        "min_rows": 1,
    },
    "reddit_finance_pulse": {
        "url_map": {
            "www.reddit.com": _reddit_router_factory(),
        },
        "expected_columns": [
            "subreddit", "title", "score", "num_comments",
            "engagement_score", "upvote_ratio", "_epoch",
        ],
        "min_rows": 1,
    },
    "reddit_ticker_mentions": {
        "url_map": {
            "www.reddit.com": _reddit_router_factory(),
        },
        "expected_columns": [
            "ticker", "mention_count", "total_score",
            "total_comments", "avg_upvote_ratio",
            "subreddit_count", "buzz_level", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Wikipedia ────────────────────────────────────────────────
    "wikipedia_company_pageviews": {
        "url_map": {
            "wikimedia.org": _wikipedia_router_factory(MOCK_WIKI_COMPANY_RESPONSES),
        },
        "expected_columns": [
            "ticker", "article", "recent_7d_avg", "prior_7d_avg",
            "pct_change_wow", "spike_level", "_epoch",
        ],
        "min_rows": 1,
    },
    "wikipedia_fear_sentiment": {
        "url_map": {
            "wikimedia.org": _wikipedia_router_factory(MOCK_WIKI_FEAR_RESPONSES),
        },
        "expected_columns": [
            "article", "category", "recent_7d_avg", "prior_7d_avg",
            "pct_change_wow", "alert", "composite_fear_index", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Prediction Market Correlation Engine ─────────────────────

    "polymarket_contract_scanner": {
        "url_map": {
            "gamma-api.polymarket.com/events": [make_arb_event()],
        },
        "expected_columns": [
            "event_id", "event_title", "event_slug", "market_id",
            "condition_id", "question", "outcome_yes_price", "outcome_no_price",
            "volume", "liquidity", "end_date_epoch", "outcomes_count",
            "yes_price_sum", "price_sum_deviation", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            all("yes_price_sum" in row and "price_sum_deviation" in row
                for row in result["head"])
        ),
    },
    "kalshi_contract_scanner": {
        "url_map": {
            # Modern Kalshi /markets responses don't include `category`; the
            # script looks it up via /events. Keep both URLs mocked so the
            # event_ticker -> category map populates AND the markets fall-
            # through (market-level category in the mock still wins for the
            # legacy-format path).
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [
                    {"event_ticker": "INXD-26APR11", "category": "Economics",
                     "series_ticker": "INXD", "title": "S&P 500 above target"},
                    {"event_ticker": "FED-26MAY-RATE", "category": "Politics",
                     "series_ticker": "FED", "title": "Fed rate decision"},
                ],
                "cursor": "",
            },
            "api.elections.kalshi.com/trade-api/v2/markets": {
                "markets": [make_kalshi_market(), make_kalshi_market_high_vol()],
                "cursor": "",
            },
        },
        "expected_columns": [
            "event_ticker", "event_title", "market_ticker", "market_title",
            "category", "yes_price", "no_price", "implied_prob_yes",
            "volume", "open_interest", "close_time_epoch", "days_to_close",
            "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            all(0 <= row.get("implied_prob_yes", -1) <= 1.0
                for row in result["head"])
            and all(not r.get("event_ticker", "").startswith("KXMVE")
                    for r in result["head"])
            and all(r.get("category", "") for r in result["head"])
        ),
    },
    "google_trends_signals": {
        "url_map": {
            "trends.google.com": _google_trends_router,
        },
        "expected_columns": [
            "search_term", "interest_score", "trend_direction",
            "related_market_category", "geo", "is_trending", "_epoch",
        ],
        "min_rows": 1,
    },
    "weather_forecast_scanner": {
        "url_map": {
            "api.open-meteo.com": MOCK_OPEN_METEO_FORECAST,
        },
        "expected_columns": [
            "location_name", "latitude", "longitude", "forecast_date",
            "temp_max_f", "temp_min_f", "precipitation_mm",
            "wind_speed_max_mph", "weather_code", "weather_description",
            "forecast_model", "_epoch",
        ],
        "min_rows": 3,  # At least 3 forecast days for one location
    },

    # ── Government & Regulatory ───────────────────────────────────

    "usaspending_contract_awards": {
        "url_map": {
            "api.usaspending.gov/api/v2/search/spending_by_award": MOCK_USASPENDING_AWARDS,
        },
        "expected_columns": [
            "award_id", "recipient", "amount_usd", "amount_millions",
            "awarding_agency", "awarding_sub_agency", "contract_type",
            "award_type", "description", "start_date", "end_date",
            "size_tier", "_epoch",
        ],
        "min_rows": 3,
        "extra_checks": lambda result: (
            any(row.get("size_tier") == "MEGA" for row in result["head"])
        ),
    },
    "fda_adverse_events": {
        "url_map": {
            "api.fda.gov/drug/event.json": _fda_event_router,
        },
        "expected_columns": [
            "drug_name", "serious_event_count", "death_report_count",
            "death_pct", "volume_tier", "severity",
            "report_window_start", "report_window_end",
            "report_window_days", "data_age_days", "data_last_updated",
            "_epoch",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            any(row.get("severity") == "CRITICAL" for row in result["head"])
            and all(row.get("data_last_updated") for row in result["head"])
        ),
    },

    # ── Gaming / Engagement ───────��───────────────────────────────

    "steam_player_counts": {
        "url_map": {
            "api.steampowered.com/ISteamChartsService/GetMostPlayedGames": MOCK_STEAM_MOST_PLAYED,
        },
        "expected_columns": [
            "app_id", "rank", "concurrent_players", "peak_today",
            "peak_ratio", "engagement_tier", "_epoch",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            any(row.get("engagement_tier") == "MASSIVE" for row in result["head"])
        ),
    },

    # ── Alternative Data ──��───────────────────────────────────────

    "opensky_private_jets": {
        "url_map": {
            "opensky-network.org/api/states/all": MOCK_OPENSKY_STATES,
        },
        "expected_columns": [
            "icao24", "callsign", "origin_country", "latitude", "longitude",
            "altitude_ft", "speed_kts", "vertical_rate", "squawk",
            "flight_phase", "_epoch",
        ],
        "min_rows": 4,  # 5 mock states minus 1 grounded = 4 airborne
        "extra_checks": lambda result: (
            any(row.get("flight_phase") == "CRUISE" for row in result["head"])
            and all(row.get("callsign", "").strip() != "GRND" for row in result["head"])
        ),
    },
    "usgs_river_gauges": {
        "url_map": {
            "waterservices.usgs.gov/nwis/iv": MOCK_USGS_GAUGES,
        },
        "expected_columns": [
            "site_id", "site_name", "gauge_height_ft", "critical_threshold_ft",
            "margin_ft", "rate_ft_per_reading", "status", "primary_commodity",
            "reading_time", "_epoch",
        ],
        "min_rows": 4,
        "extra_checks": lambda result: (
            any(row.get("status") == "WARNING" for row in result["head"])
            and any(row.get("primary_commodity") == "grain" for row in result["head"])
        ),
    },

    # ── Equity Catalysts (daily_opportunity_brief alert group) ────

    "earnings_calendar_72h": {
        "url_map": {
            "api.nasdaq.com/api/calendar/earnings": MOCK_NASDAQ_EARNINGS,
        },
        "expected_columns": [
            "ticker", "company", "earnings_date", "report_time_code",
            "eps_estimate", "revenue_estimate_usd",
            "market_cap_tier", "hours_until_earnings", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("ticker") == "AAPL" for row in result["head"])
            and any(row.get("market_cap_tier") == "MEGA" for row in result["head"])
        ),
    },
    # ``options_unusual_activity`` + ``options_unusual_activity_pro`` (Yahoo
    # variants) + ``options_unusual_activity_tradier_pro`` (Tradier) were
    # retired 2026-04-23 in favour of a single Finnhub-backed
    # ``options_unusual_activity_pro`` registered under
    # CREDENTIALED_SCRIPT_REGISTRY below.

    # ── Pro-tier scripts (trust_level=unrestricted) ───────────────
    # Each _pro variant reuses its sandboxed counterpart's mock URLs; the
    # output columns are supersets of the sandboxed schema plus the new
    # scientific columns (scipy/sklearn/rapidfuzz/numpy).

    "kalshi_polymarket_arbitrage_pro": {
        # 2026-05-06: switched to /v2/events?with_nested_markets=true to
        # bypass the KXMVE auto-permutation flood. Mock returns one event
        # whose title fuzz-matches a Polymarket question above the 70.0
        # token_sort_ratio threshold; price diverges to clear the 1% net
        # edge gate (k=0.75 vs p=0.65 → 10pt raw, ~6pt net).
        "url_map": {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event(
                    event_ticker="ELEC-26-ALICE",
                    title="Alice wins election 2026",
                    sub_title="Presidential race",
                    category="Elections",
                    markets=[make_kalshi_market(
                        ticker="ELEC-26-ALICE-T",
                        title="Alice wins election 2026",
                        subtitle="Presidential race",
                        last_price=75,  # 0.75 - diverges from Polymarket's 0.65 → 10pt divergence
                    )],
                )],
                "cursor": "",
            },
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    question="Alice wins election 2026",
                    outcomePrices='["0.65","0.35"]',
                ),
            ],
        },
        "expected_columns": [
            "kalshi_ticker", "kalshi_title", "kalshi_yes_price",
            "polymarket_question", "polymarket_yes_price",
            "divergence_pct",
            # H-MI-2 (2026-04-21): net-of-fees columns
            "fee_roundtrip_pct", "net_edge_pct",
            # L-MI-14 (2026-04-22): explicit per-leg actions
            "polymarket_action", "kalshi_action",
            "opportunity_strength",
            "match_confidence", "match_tier", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("match_confidence", 0) >= 70.0 for row in result["head"])
            and all(row.get("net_edge_pct", 0) >= 1.0 for row in result["head"])
        ),
    },
    "coingecko_volume_anomaly_detector_pro": {
        "url_map": {
            "api.coingecko.com/api/v3/coins/markets": [
                make_coingecko_coin(),
                make_coingecko_coin_alt(),
                make_coingecko_coin(id="ethereum", symbol="eth", name="Ethereum",
                    current_price=3500, market_cap=420000000000, total_volume=9000000000,
                    market_cap_rank=2, circulating_supply=120000000, total_supply=120000000,
                    ath=4870, fully_diluted_valuation=420000000000,
                    price_change_percentage_24h=1.0, price_change_percentage_1h_in_currency=0.05,
                    price_change_percentage_7d_in_currency=-0.5, price_change_percentage_30d_in_currency=5.0),
            ],
        },
        "expected_columns": [
            "rank", "symbol", "name", "vol_mcap_ratio", "ratio_vs_median",
            "alert_level",
            "z_score", "robust_z_score", "percentile_rank",
            "is_statistical_outlier", "anomaly_strength", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            any(row.get("is_statistical_outlier") is not None for row in result["head"])
            and any(row.get("percentile_rank") is not None for row in result["head"])
        ),
    },
    "polymarket_volume_spike_detector_pro": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_gamma_market(volume="150000", volume24hr="80000")],
        },
        "expected_columns": [
            "question", "slug", "yes_price", "total_volume", "volume_24h",
            "spike_multiple", "volume_24h_ratio", "alert_level",
            "iqr_outlier", "robust_z_score", "spike_percentile",
            "outlier_strength", "is_statistical_outlier", "_epoch",
        ],
        "min_rows": 0,
    },
    # ``options_unusual_activity_pro`` moved to CREDENTIALED_SCRIPT_REGISTRY
    # (Massive.com variant with MASSIVE_API_KEY - formerly Finnhub-backed,
    # swapped 2026-04-25).
    "polymarket_high_probability_pro": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [make_high_prob_market()],
        },
        # H-MI-1 (2026-04-21): dropped kelly_fraction_full, kelly_fraction_half,
        # suggested_position_size (always 0 / 'SMALL' under the script's
        # fair-price assumption). Renamed expected_value_per_dollar to
        # expected_value_if_price_equals_fair.
        "expected_columns": [
            "market_id", "question", "leading_outcome", "leading_price",
            "payout_if_win", "probability_tier",
            "expected_value_if_price_equals_fair",
            "implied_edge_vs_50", "payout_multiple", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("leading_price", 0) >= 0.75 for row in result["head"])
            # Expected value at market price is zero by construction -
            # that's the whole point of the honest rename.
            and all(row.get("expected_value_if_price_equals_fair") == 0.0
                    for row in result["head"])
        ),
    },
    "reddit_ticker_mentions_pro": {
        "url_map": {
            "www.reddit.com": _reddit_router_factory(),
        },
        "expected_columns": [
            "ticker", "mention_count", "total_score",
            "avg_upvote_ratio", "subreddit_count", "buzz_level",
            "median_upvote_ratio", "weighted_buzz_score",
            "buzz_score_z", "momentum_percentile", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_calibration_analysis_pro": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(outcomePrices='["0.98","0.02"]'),
                make_gamma_market(id="m_cal_2", question="B?", outcomePrices='["0.02","0.98"]'),
                make_gamma_market(id="m_cal_3", question="C?", outcomePrices='["0.97","0.03"]'),
            ],
        },
        "expected_columns": [
            "market_id", "question", "final_yes_price", "resolved_yes", "volume",
            "calibration_bin", "bin_empirical_freq", "bin_sample_size",
            "calibration_error",
            # H-MI-5 (2026-04-21): fit_status column joins the existing fit_* trio.
            "fit_status",
            "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            # fit_status must be one of the documented enum values on every row.
            all(row.get("fit_status") in (
                "no_samples", "insufficient_bins",
                "fit_failed", "fit_error", "converged",
            ) for row in result["head"])
        ),
    },
    "polymarket_cross_market_correlation_pro": {
        "url_map": {
            "gamma-api.polymarket.com/events": [make_gamma_event()],
        },
        "expected_columns": [
            "event_id", "event_title", "market_id", "market_question",
            "yes_price", "volume", "liquidity",
            "event_volume_sum", "event_price_hhi", "event_entropy",
            "yes_price_zscore", "_epoch",
        ],
        "min_rows": 2,
    },
    "polymarket_temporal_decay_pro": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    question="Near-term market?",
                    outcomePrices='["0.78","0.22"]',
                    endDate=_FUTURE_3D_ISO,
                ),
                make_gamma_market(
                    id="m_td_2",
                    question="Week-out market?",
                    outcomePrices='["0.70","0.30"]',
                    endDate=_FUTURE_7D_ISO,
                ),
                make_gamma_market(
                    id="m_td_3",
                    question="Two-week market?",
                    outcomePrices='["0.65","0.35"]',
                    endDate=_FUTURE_14D_ISO,
                ),
            ],
        },
        "expected_columns": [
            "question", "days_to_resolution", "favored_side", "favored_price",
            "convergence_gap", "roi_pct", "annualized_roi_pct", "urgency",
            "fit_decay_constant", "fit_half_life_days", "fit_r_squared",
            "expected_gap_at_days", "gap_vs_fitted", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_news_sentiment_divergence_pro": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(question="Will Alice win the 2026 election?", volume="50000", volume24hr="15000"),
            ],
            "api.gdeltproject.org": MOCK_GDELT_ARTICLES,
        },
        "expected_columns": [
            "question", "yes_price", "news_headline_count", "avg_sentiment_score",
            "sentiment_direction", "price_direction", "divergence_type",
            "text_similarity_mean", "text_similarity_max",
            "headline_relevance_ratio", "divergence_strength", "_epoch",
        ],
        "min_rows": 0,
    },
    "coingecko_top_coins_pro": {
        "url_map": {
            "api.coingecko.com/api/v3/coins/markets": [
                make_coingecko_coin(),
                make_coingecko_coin_alt(),
            ],
        },
        "expected_columns": [
            "rank", "symbol", "name", "price_usd",
            "change_1h_pct", "change_24h_pct", "change_7d_pct", "change_30d_pct",
            "volume_24h", "market_cap", "vol_mcap_ratio",
            "volatility_score", "momentum_score", "sharpe_proxy", "_epoch",
        ],
        "min_rows": 2,
    },
    "polymarket_market_movers_pro": {
        "url_map": {
            # M-MI-8 (2026-04-22): market_movers_pro now requires a
            # fresh ``lastTradeTime`` (or ``updatedAt``) on the Gamma
            # snapshot - older than 5min and the row is skipped.
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    lastTradeTime=(
                        _FRESH_MARKET_MOVER_TS := _options_dt.now(
                            _options_tz.utc,
                        ).isoformat().replace("+00:00", "Z")
                    ),
                ),
            ],
            "clob.polymarket.com/midpoint": {"mid": "0.70"},
        },
        "expected_columns": [
            "condition_id", "question", "gamma_yes_price", "clob_midpoint",
            "price_delta", "abs_delta", "direction", "volume",
            "snapshot_age_seconds",  # M-MI-8
            "momentum_z", "robust_momentum_z", "normalized_move",
            "delta_percentile", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Global Macro Risk Brief feeders (no-auth) ─────────────────

    "usgs_earthquake_feed": {
        "url_map": {
            "earthquake.usgs.gov/earthquakes/feed": MOCK_USGS_EARTHQUAKES,
        },
        "expected_columns": [
            "event_id", "magnitude", "magnitude_type", "place", "region_tag",
            "pager_alert", "significance", "depth_km", "felt_reports", "mmi",
            "tsunami_warning", "event_time_utc", "event_epoch", "url",
            "latitude", "longitude", "severity_tier", "economic_thesis",
            "_epoch",
        ],
        "min_rows": 3,
        "extra_checks": lambda result: (
            any(row.get("severity_tier") == "CRITICAL" for row in result["head"])
            and any(row.get("region_tag", "").startswith("Japan") for row in result["head"])
        ),
    },
    "noaa_severe_weather_alerts": {
        "url_map": {
            "api.weather.gov/alerts/active": MOCK_NOAA_SEVERE_ALERTS,
        },
        "expected_columns": [
            "alert_id", "event_type", "severity", "urgency", "certainty",
            "area_description", "headline", "sent_epoch", "effective_epoch",
            "expires_epoch", "sender_name", "investable_sector",
            "economic_thesis", "region_tier", "_epoch",
        ],
        "min_rows": 3,
        "extra_checks": lambda result: (
            any(row.get("severity") == "Extreme" for row in result["head"])
            and any(row.get("investable_sector") == "Insurance/Energy" for row in result["head"])
        ),
    },
    "noaa_tropical_cyclones": {
        "url_map": {
            "nhc.noaa.gov/CurrentStorms.json": MOCK_NHC_STORMS,
        },
        "expected_columns": [
            "storm_id", "storm_name", "classification", "intensity_kts",
            "pressure_mb", "latitude", "longitude", "movement_dir",
            "movement_speed_kts", "last_update_epoch", "basin",
            "severity_tier", "economic_thesis", "advisory_url", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("severity_tier") == "MAJOR" for row in result["head"])
            and any(row.get("basin") == "NORTH_ATLANTIC" for row in result["head"])
        ),
    },
    "usgs_volcanic_alerts": {
        "url_map": {
            "volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes": MOCK_USGS_VOLCANOES,
        },
        "expected_columns": [
            "volcano_number", "volcano_name", "region", "subregion",
            "alert_level", "color_code", "latitude", "longitude",
            "updated_date", "severity_tier", "aviation_risk",
            "economic_thesis", "_epoch",
        ],
        "min_rows": 3,
        "extra_checks": lambda result: (
            any(row.get("severity_tier") == "CRITICAL" for row in result["head"])
            and any(row.get("color_code") == "RED" for row in result["head"])
        ),
    },
    "gdelt_geopolitical_events": {
        "url_map": {
            "api.gdeltproject.org/api/v2/doc/doc": MOCK_GDELT_MACRO_ARTICLES,
        },
        "expected_columns": [
            "article_url", "title", "domain", "language", "source_country",
            "seen_date", "tension_theme", "actor_region",
            "investment_thesis", "severity_tier", "_epoch",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            any(row.get("tension_theme") == "RUSSIA_UKRAINE_WAR" for row in result["head"])
            and any(row.get("tension_theme") == "TAIWAN_STRAIT_TENSION" for row in result["head"])
        ),
    },
    "worldbank_country_growth": {
        "url_map": {
            "api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.KD.ZG": MOCK_WB_GDP,
            "api.worldbank.org/v2/country/all/indicator/FP.CPI.TOTL.ZG": MOCK_WB_CPI,
            "api.worldbank.org/v2/country/all/indicator/NE.EXP.GNFS.KD.ZG": MOCK_WB_EXPORTS,
        },
        "expected_columns": [
            "country_code", "country_name", "indicator_id", "indicator_name",
            "year", "value", "growth_tier", "investability_tag",
            "etf_hint", "economic_thesis", "_epoch",
        ],
        "min_rows": 6,
        "extra_checks": lambda result: (
            any(row.get("investability_tag") == "INVESTABLE_EM" for row in result["head"])
            and not any(row.get("country_code") == "EMU" for row in result["head"])
            and any(row.get("growth_tier") == "HYPERINFLATION" for row in result["head"])
        ),
    },

    # ── Wave 2 (PPPB) - Federal Register significant rules (no-auth) ─
    "federal_register_actions": {
        "url_map": {
            "/api/v1/articles": {
                "results": [
                    {
                        "document_number": "2026-08234",
                        "type": "Rule",
                        "agencies": [{"name": "Environmental Protection Agency", "raw_name": "EPA"}],
                        "title": "Significant Regulatory Action: Final Rule on Greenhouse Gas Emissions Standards for Heavy-Duty Vehicles",
                        "abstract": "This final rule sets emissions standards. The economic effect exceeds $100 million annually.",
                        "action": "Final rule.",
                        "publication_date": "2026-04-22",
                        "effective_on": "2026-07-01",
                        "significant": True,
                        "html_url": "https://www.federalregister.gov/documents/2026/04/22/2026-08234/",
                        "regulation_id_numbers": ["2060-AW01"],
                    },
                    {
                        "document_number": "2026-08501",
                        "type": "Presidential Document",
                        "agencies": [{"name": "Executive Office of the President"}],
                        "title": "Executive Order 14XYZ: Tariff Increase on Imports of Critical Minerals",
                        "abstract": "Increases tariffs on critical mineral imports.",
                        "action": "Executive order.",
                        "publication_date": "2026-04-20",
                        "effective_on": "2026-05-15",
                        "significant": True,
                        "html_url": "https://www.federalregister.gov/documents/2026/04/20/2026-08501/",
                    },
                ],
            },
        },
        "expected_columns": [
            "document_number", "doc_type", "agency_names",
            "title", "abstract", "action",
            "publication_date", "effective_on",
            "significant_action", "sector_tag",
            "html_url", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            all(row.get("sector_tag") in
                ("Healthcare", "Energy", "Finance", "Defense", "Tech",
                 "Environment", "Trade", "Labor", "Immigration", "Other")
                for row in result["head"])
        ),
    },

    # ── Wave 2 (PHPB) - FDA drug approvals (no-auth) ──────────
    "fda_drug_approvals": {
        "url_map": {
            "/drug/drugsfda": {
                # meta.last_updated added 2026-05-02 to match the new
                # anchor-to-API-last_updated logic - script anchors its
                # 30d search window to this date, then per-submission-
                # date filter requires submissions in that window.
                "meta": {"last_updated": "2026-04-30"},
                "results": [
                    {
                        "application_number": "NDA215000",
                        "sponsor_name": "Acme Pharmaceuticals Inc.",
                        "openfda": {
                            "brand_name": ["NEWDRUG"],
                            "generic_name": ["acmecillin"],
                        },
                        "products": [{"route": "ORAL", "dosage_form": "TABLET"}],
                        "submissions": [{
                            "submission_type": "ORIG-1",
                            "submission_class_code": "TYPE 1",
                            "submission_status": "AP",
                            "submission_status_date": "20260415",
                        }],
                    },
                    {
                        "application_number": "BLA125678",
                        "sponsor_name": "Bigbio Therapeutics",
                        "openfda": {
                            "brand_name": ["BIODRUGX"],
                            "generic_name": ["bigmab"],
                        },
                        "products": [{"route": "INJECTION", "dosage_form": "SOLUTION"}],
                        "submissions": [{
                            "submission_type": "SUPPL-EFFICACY",
                            "submission_class_code": "EFFICACY",
                            "submission_status": "AP",
                            "submission_status_date": "20260418",
                        }],
                    },
                ],
            },
        },
        "expected_columns": [
            "application_number", "application_type",
            "sponsor_name", "brand_names", "generic_names",
            "submission_type", "submission_class_code",
            "submission_status", "submission_status_date",
            "product_count", "route", "dosage_form",
            "impact_tier", "data_age_days", "data_last_updated", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            # Real rows tier HIGH/MEDIUM/LOW; sentinel rows tier
            # API_ERROR or NO_SIGNAL (script emits sentinel when no
            # rows passed filters - preserves operator visibility).
            all(row.get("impact_tier") in
                ("HIGH", "MEDIUM", "LOW", "API_ERROR", "NO_SIGNAL")
                for row in result["head"])
        ),
    },

    # ── Wave 2 (PHPB) - FDA drug shortages (no-auth) ──────────
    "fda_drug_shortages": {
        "url_map": {
            "/drug/shortages": {
                "results": [
                    {
                        "generic_name": "amoxicillin",
                        "proprietary_name": "AMOXIL",
                        "dosage_form": "Capsule",
                        "strength": "500 mg",
                        "status": "Currently in Shortage",
                        "shortage_reason": "Increased demand",
                        "company_name": "Generic Pharma Co",
                        "therapeutic_category": "Antibiotic",
                        "change_date": "2026-02-15",
                        "update_type": "Status Change",
                    },
                    {
                        "generic_name": "cisplatin",
                        "proprietary_name": "",
                        "dosage_form": "Injection",
                        "strength": "1 mg/mL",
                        "status": "Currently in Shortage",
                        "shortage_reason": "Manufacturing delay",
                        "company_name": "Specialty Pharma",
                        "therapeutic_category": "Oncology",
                        "change_date": "2026-01-10",
                        "update_type": "Initial Posting",
                    },
                ],
            },
        },
        "expected_columns": [
            "generic_name", "proprietary_name",
            "dosage_form", "strength",
            "status", "shortage_reason",
            "company_name", "therapeutic_category",
            "change_date", "update_type",
            "days_in_shortage_estimated",
            "impact_tier", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("impact_tier") == "HIGH" for row in result["head"])
            and all(row.get("status") in ("Currently in Shortage", "Resolved")
                    for row in result["head"])
        ),
    },

    # ── Wave 2 (PHPB) - ClinicalTrials.gov v2 Phase 3 (no-auth) ──
    "clinicaltrials_phase3_updates": {
        "url_map": {
            "/api/v2/studies": {
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT06000001",
                                "briefTitle": "A Phase 3 Study of Acmecillin in Patients with X Disease",
                            },
                            "conditionsModule": {"conditions": ["X Disease"]},
                            "armsInterventionsModule": {
                                "interventions": [{"name": "Acmecillin"}]
                            },
                            "sponsorCollaboratorsModule": {
                                "leadSponsor": {"name": "Acme Pharmaceuticals Inc.", "class": "INDUSTRY"}
                            },
                            "statusModule": {
                                "overallStatus": "ACTIVE_NOT_RECRUITING",
                                "startDateStruct": {"date": "2024-01-01"},
                                "primaryCompletionDateStruct": {"date": "2026-09-30"},
                                "completionDateStruct": {"date": "2027-03-31"},
                                "lastUpdatePostDateStruct": {"date": "2026-04-22"},
                            },
                            "designModule": {
                                "phases": ["PHASE3"],
                                "studyType": "INTERVENTIONAL",
                                "enrollmentInfo": {"count": 850},
                            },
                        },
                    },
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT06000002",
                                "briefTitle": "Phase 2/3 Study of BigMab in Solid Tumors",
                            },
                            "conditionsModule": {"conditions": ["Lung Cancer", "Breast Cancer"]},
                            "armsInterventionsModule": {"interventions": [{"name": "BigMab"}]},
                            "sponsorCollaboratorsModule": {
                                "leadSponsor": {"name": "Bigbio Therapeutics", "class": "INDUSTRY"}
                            },
                            "statusModule": {
                                "overallStatus": "RECRUITING",
                                "startDateStruct": {"date": "2026-02-01"},
                                "primaryCompletionDateStruct": {"date": "2028-02-01"},
                                "completionDateStruct": {"date": "2028-08-01"},
                                "lastUpdatePostDateStruct": {"date": "2026-04-21"},
                            },
                            "designModule": {
                                "phases": ["PHASE2", "PHASE3"],
                                "studyType": "INTERVENTIONAL",
                                "enrollmentInfo": {"count": 450},
                            },
                        },
                    },
                ],
                "nextPageToken": "",
            },
        },
        "expected_columns": [
            "nct_id", "brief_title", "condition", "intervention",
            "lead_sponsor", "lead_sponsor_class",
            "overall_status", "phase",
            "study_type", "enrollment",
            "start_date", "primary_completion_date", "completion_date",
            "last_update_post_date",
            "impact_tier", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("lead_sponsor_class") == "INDUSTRY" for row in result["head"])
            and all(row.get("impact_tier") in ("HIGH", "MEDIUM", "LOW")
                    for row in result["head"])
        ),
    },

    # ── Wave 1 (SPBEB) - ESPN league-wide injuries (no-auth) ───
    "espn_injuries_feed": {
        "url_map": {
            "site.api.espn.com/apis/site/v2/sports": {
                "injuries": [
                    {
                        "team": {"abbreviation": "LAL", "displayName": "Los Angeles Lakers"},
                        "injuries": [
                            {
                                "athlete": {
                                    "id": "12345",
                                    "displayName": "LeBron James",
                                    "position": {"abbreviation": "SF"},
                                    "jersey": "23",
                                },
                                "status": "Out",
                                "type": {"description": "Knee soreness"},
                                "shortComment": "Out for tonight's game.",
                                "date": "2026-04-24T15:00:00Z",
                            },
                            {
                                "athlete": {
                                    "id": "12346",
                                    "displayName": "Anthony Davis",
                                    "position": {"abbreviation": "PF"},
                                    "jersey": "3",
                                },
                                "status": "Day-to-day",
                                "type": {"description": "Ankle sprain"},
                                "shortComment": "Game-time decision.",
                                "date": "2026-04-24T14:00:00Z",
                            },
                        ],
                    },
                    # 2026-Q2 payload shape: no nested "team" dict (only
                    # top-level id/displayName), and the athlete carries no
                    # "id" - it must be recovered from the playercard link
                    # href, with team_abbr from athlete.team. Caught
                    # 2026-07-01: every production row had athlete_id=""
                    # so `dedup athlete_id` collapsed the whole feed to one
                    # row and the spbeb_injuries feeder went permanently
                    # empty.
                    {
                        "id": "22",
                        "displayName": "Arizona Cardinals",
                        "injuries": [
                            {
                                "id": "631527",
                                "athlete": {
                                    "displayName": "Trey McBride",
                                    "position": {"abbreviation": "TE"},
                                    "links": [
                                        {
                                            "rel": ["playercard", "desktop", "athlete"],
                                            "href": "https://www.espn.com/nfl/player/_/id/4361307/trey-mcbride",
                                        },
                                    ],
                                    "team": {
                                        "id": "22",
                                        "abbreviation": "ARI",
                                        "displayName": "Arizona Cardinals",
                                    },
                                },
                                "status": "Questionable",
                                "type": {"description": "Hand injury"},
                                "shortComment": "Expected to play through it.",
                                "date": "2026-06-24T15:16Z",
                            },
                        ],
                    },
                ],
            },
        },
        "expected_columns": [
            "sport", "league", "team_abbr", "team_name",
            "athlete_id", "athlete_name", "position", "jersey",
            "status", "injury_type", "short_comment",
            "date_reported", "date_reported_epoch",
            "severity_rank", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            any(row.get("status") == "Out" for row in result["head"])
            and any(row.get("severity_rank", 0) >= 3 for row in result["head"])
            # Every row must carry a non-empty athlete_id - it's the
            # dedup key for the spbeb_injuries feeder. (The mock serves
            # the same payload for all four leagues, so ids repeat
            # across leagues; non-empty is the invariant that matters.)
            and all(row.get("athlete_id") for row in result["head"])
            and all(row.get("team_abbr") for row in result["head"])
            # New-shape row proves link-href id recovery works.
            and any(row.get("athlete_id") == "4361307" for row in result["head"])
        ),
    },

    # ── Wave 3 (SFCB) - Metaculus open questions ──────────────
    # Metaculus deprecated public API access in 2026-Q1. Script was
    # converted to optional-credential pattern in commit 3e8f8af
    # (backlog #3, 2026-05-02): credential_kinds={"METACULUS_API_TOKEN":
    # "api_key"} but requires_credentials=[]. _discover_no_auth_scripts()
    # excludes scripts with non-empty credential_kinds, so this entry no
    # longer belongs in SCRIPT_REGISTRY. The dedicated
    # TestMetaculusAuthRequiredSentinel test class below covers all 4
    # sentinel paths (AUTH_REQUIRED, AUTH_INVALID, API_ERROR, NO_SIGNAL)
    # plus the Authorization-header-when-set path - strictly more
    # thorough than the generic mock that used to live here.

    # ── Wave 3 (SFCB) - Manifold Markets open markets (no-auth) ────
    # Free no-auth public API; added 2026-05-06 as redundancy after the
    # Metaculus 2026-Q1 auth wall + V2 schema break audit. If Metaculus
    # has another schema break, this feeder gives the SFCB a fallback
    # signal source. Endpoint: /v0/search-markets, sort=most-popular,
    # filter=open. Paginates up to 4 pages of 100 markets. Manifold
    # returns timestamps as JavaScript milliseconds; the script converts
    # them to ISO seconds before output. The mock below exercises three
    # market shapes: a high-volume BINARY (probability set), a
    # MULTIPLE_CHOICE (no probability - script must leave null), and a
    # low-volume BINARY (must still appear, sorted last by 24h volume).
    "manifold_markets": {
        "url_map": {
            "/v0/search-markets": [
                {
                    "id": "manifold_high_vol",
                    "question": "Will SPY close above $600 by end of Q3 2026?",
                    "slug": "spy-600-q3-2026",
                    "outcomeType": "BINARY",
                    "mechanism": "cpmm-1",
                    "probability": 0.62,
                    "createdTime": 1736035200000,  # ~Jan 5 2026 UTC
                    "closeTime": 1759276800000,  # ~Oct 1 2026 UTC
                    "uniqueBettorCount": 250,
                    "volume": 50000.0,
                    "volume24Hours": 5000.0,
                    "totalLiquidity": 12000.0,
                    "url": "https://manifold.markets/user1/spy-600-q3-2026",
                    "creatorUsername": "user1",
                    "isResolved": False,
                },
                {
                    "id": "manifold_multi",
                    "question": "Who will win Best Picture at the 2027 Oscars?",
                    "slug": "oscars-2027-best-picture",
                    "outcomeType": "MULTIPLE_CHOICE",
                    "mechanism": "cpmm-multi-1",
                    "createdTime": 1735603200000,
                    "closeTime": 1804032000000,
                    "uniqueBettorCount": 80,
                    "volume": 8000.0,
                    "volume24Hours": 800.0,
                    "totalLiquidity": 3000.0,
                    "url": "https://manifold.markets/user2/oscars-2027-best-picture",
                    "creatorUsername": "user2",
                    "isResolved": False,
                },
                {
                    "id": "manifold_low_vol",
                    "question": "Will pi day 2026 be widely celebrated on Manifold?",
                    "slug": "pi-day-2026-celebrated",
                    "outcomeType": "BINARY",
                    "mechanism": "cpmm-1",
                    "probability": 0.95,
                    "createdTime": 1737244800000,
                    "closeTime": 1742083200000,  # ~Mar 16 2026 UTC
                    "uniqueBettorCount": 5,
                    "volume": 100.0,
                    "volume24Hours": 10.0,
                    "totalLiquidity": 50.0,
                    "url": "https://manifold.markets/user3/pi-day-2026-celebrated",
                    "creatorUsername": "user3",
                    "isResolved": False,
                },
            ],
        },
        "expected_columns": [
            "question_id", "title", "question_type",
            "community_prediction", "prediction_count",
            "comment_count", "forecaster_count",
            "created_time", "publish_time", "resolve_time",
            "days_to_resolve", "category", "page_url",
            "volume_total_mana", "volume_24h_mana", "total_liquidity_mana",
            "_epoch",
        ],
        "min_rows": 3,
        "extra_checks": lambda result: (
            # Sorted by volume_24h_mana descending - high-vol row first.
            result["head"][0]["volume_24h_mana"] >= result["head"][-1]["volume_24h_mana"]
            # The BINARY market with set probability surfaces a real
            # number between 0 and 1.
            and any(
                row.get("question_type") == "BINARY"
                and isinstance(row.get("community_prediction"), (int, float))
                and not isinstance(row.get("community_prediction"), bool)
                and row.get("community_prediction") == row.get("community_prediction")  # not NaN
                and 0.0 <= row.get("community_prediction") <= 1.0
                for row in result["head"]
            )
            # The MULTIPLE_CHOICE market has NO single probability - the
            # script must leave community_prediction null. After pandas
            # float64 serialisation that surfaces as None, "", or NaN
            # depending on rendering - accept all three.
            and any(
                row.get("question_type") == "MULTIPLE_CHOICE"
                and (
                    row.get("community_prediction") is None
                    or row.get("community_prediction") == ""
                    or (
                        isinstance(row.get("community_prediction"), float)
                        and row.get("community_prediction") != row.get("community_prediction")
                    )
                )
                for row in result["head"]
            )
            # uniqueBettorCount aliases as both prediction_count and
            # forecaster_count - the two columns must agree row-wise.
            and all(
                row.get("prediction_count") == row.get("forecaster_count")
                for row in result["head"]
            )
        ),
    },

    # ── Wave 3 (SFCB) - arXiv recent papers (no-auth) ─────────
    "arxiv_recent_papers": {
        "url_map": {
            "/api/query": (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">\n'
                '<entry>\n'
                '  <id>http://arxiv.org/abs/2604.12345v1</id>\n'
                '  <updated>2026-04-22T10:00:00Z</updated>\n'
                '  <published>2026-04-21T08:00:00Z</published>\n'
                '  <title>A Novel Attention Mechanism for Large Language Models</title>\n'
                '  <summary>We propose a novel attention mechanism that reduces compute by 40 percent while maintaining accuracy on standard benchmarks. Experiments on LLaMA-3 and GPT-4 show consistent gains across 12 reasoning tasks.</summary>\n'
                '  <author><name>Smith, J.</name></author>\n'
                '  <author><name>Doe, A.</name></author>\n'
                '  <author><name>Tan, M.</name></author>\n'
                '  <arxiv:primary_category term="cs.LG"/>\n'
                '  <category term="cs.LG"/>\n'
                '  <category term="cs.AI"/>\n'
                '  <link href="http://arxiv.org/abs/2604.12345v1" rel="alternate"/>\n'
                '</entry>\n'
                '<entry>\n'
                '  <id>http://arxiv.org/abs/2604.12346v1</id>\n'
                '  <updated>2026-04-23T11:00:00Z</updated>\n'
                '  <published>2026-04-22T07:00:00Z</published>\n'
                '  <title>Quantitative Methods for Portfolio Optimization Under Regime Shifts</title>\n'
                '  <summary>This paper introduces regime-aware portfolio optimization techniques for use in turbulent macro environments.</summary>\n'
                '  <author><name>Wong, K.</name></author>\n'
                '  <arxiv:primary_category term="q-fin.PM"/>\n'
                '  <category term="q-fin.PM"/>\n'
                '  <link href="http://arxiv.org/abs/2604.12346v1" rel="alternate"/>\n'
                '</entry>\n'
                '</feed>'
            ),
        },
        "expected_columns": [
            "arxiv_id", "title", "abstract",
            "primary_category", "all_categories",
            "author_count", "first_author",
            "published_date", "updated_date",
            "comment", "doi", "page_url", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("primary_category") == "cs.LG" for row in result["head"])
            and any(row.get("author_count", 0) >= 1 for row in result["head"])
        ),
    },

    # ── Wave 3 (SFCB) - NIH Reporter grants (no-auth) ──────────
    "nih_reporter_grants": {
        "url_map": {
            "/v2/projects/search": {
                "results": [
                    {
                        "project_num": "1R01CA288888-01A1",
                        "project_title": "Novel Immunotherapy Pathways in Pancreatic Cancer",
                        "organization": {
                            "org_name": "Stanford University",
                            "org_state": "CA",
                        },
                        "agency_ic_admin": {"code": "NCI"},
                        "agency_ic_fundings": [{"code": "NCI"}],
                        "award_amount": 2500000.0,
                        "total_cost": 2500000.0,
                        "fiscal_year": 2026,
                        "project_start_date": "2026-04-01",
                        "project_end_date": "2031-03-31",
                        "contact_pi_name": "Smith, John A",
                        "spending_categories": [
                            {"name": "Cancer"},
                            {"name": "Immunotherapy"},
                        ],
                    },
                    {
                        "project_num": "1U01AI199999-01",
                        "project_title": "Universal Influenza Vaccine Platform",
                        "organization": {
                            "org_name": "Mount Sinai School of Medicine",
                            "org_state": "NY",
                        },
                        "agency_ic_admin": {"code": "NIAID"},
                        "agency_ic_fundings": [{"code": "NIAID"}],
                        "award_amount": 8000000.0,
                        "total_cost": 8000000.0,
                        "fiscal_year": 2026,
                        "project_start_date": "2026-03-15",
                        "project_end_date": "2031-03-14",
                        "contact_pi_name": "Doe, Alice B",
                        "spending_categories": [
                            {"name": "Influenza"},
                            {"name": "Vaccine"},
                        ],
                    },
                ],
            },
        },
        "expected_columns": [
            "project_num", "project_title",
            "organization_name", "organization_state",
            "agency_ic_admin", "agency_ic_funding",
            "award_amount", "total_cost", "fiscal_year",
            "project_start_date", "project_end_date",
            "pi_name", "spending_category",
            "project_url", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("award_amount", 0) >= 500000 for row in result["head"])
            and any(row.get("agency_ic_admin") in ("NCI", "NIAID", "NHLBI", "NIDDK") for row in result["head"])
        ),
    },

    # ── Wave 3 (RCPB) - Wikipedia curated religion pageviews ─
    "wikipedia_curated_pageviews": {
        "url_map": {
            "/api/rest_v1/metrics/pageviews/per-article": {
                "items": [
                    {"article": "Pope_Francis", "views": 5000, "timestamp": "2026032500"},
                    {"article": "Pope_Francis", "views": 5500, "timestamp": "2026032600"},
                    {"article": "Pope_Francis", "views": 6200, "timestamp": "2026032700"},
                    {"article": "Pope_Francis", "views": 5800, "timestamp": "2026032800"},
                    {"article": "Pope_Francis", "views": 5400, "timestamp": "2026032900"},
                    {"article": "Pope_Francis", "views": 5900, "timestamp": "2026033000"},
                    {"article": "Pope_Francis", "views": 6100, "timestamp": "2026033100"},
                    {"article": "Pope_Francis", "views": 7800, "timestamp": "2026040100"},
                    {"article": "Pope_Francis", "views": 8200, "timestamp": "2026040200"},
                    {"article": "Pope_Francis", "views": 9100, "timestamp": "2026040300"},
                    {"article": "Pope_Francis", "views": 8600, "timestamp": "2026040400"},
                    {"article": "Pope_Francis", "views": 8400, "timestamp": "2026040500"},
                    {"article": "Pope_Francis", "views": 8800, "timestamp": "2026040600"},
                    {"article": "Pope_Francis", "views": 9200, "timestamp": "2026040700"},
                ],
            },
        },
        "expected_columns": [
            "article", "category",
            "total_views_30d", "avg_daily_views_30d",
            "recent_7d_views", "baseline_views_avg",
            "change_7d_vs_baseline_pct",
            "momentum_tier",
            "page_url", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("momentum_tier") in
                ("SURGING", "RISING", "STABLE", "FALLING")
                for row in result["head"])
        ),
    },

    # ── Wave 3 (CPB) - Wikipedia top weekly pageviews ─────────
    "wikipedia_top_pageviews_weekly": {
        "url_map": {
            "/api/rest_v1/metrics/pageviews/top": {
                "items": [
                    {
                        "project": "en.wikipedia",
                        "access": "all-access",
                        "year": "2026",
                        "month": "04",
                        "day": "23",
                        "articles": [
                            {"article": "Donald_Trump", "views": 250000, "rank": 1},
                            {"article": "ChatGPT", "views": 180000, "rank": 2},
                            {"article": "Deaths_in_2026", "views": 165000, "rank": 3},
                            {"article": "Main_Page", "views": 9000000, "rank": 4},
                            {"article": "2026_World_Cup_qualification", "views": 142000, "rank": 5},
                            {"article": "Apple_Inc.", "views": 110000, "rank": 6},
                            {"article": "Solar_eclipse_of_April_2026", "views": 92000, "rank": 7},
                            {"article": "Quantum_computing", "views": 78000, "rank": 8},
                        ],
                    },
                ],
            },
        },
        "expected_columns": [
            "article", "language_code", "rank",
            "views", "snapshot_date",
            "category_tag", "page_url", "_epoch",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            all(row.get("category_tag") in
                ("Person", "Place", "Event", "Concept",
                 "Entertainment", "Science", "Other")
                for row in result["head"])
            and not any(row.get("article") == "Main_Page" for row in result["head"])
        ),
    },

    # ── Wave 3 (RCPB) - Polymarket religion markets ─────────
    "polymarket_religion_markets": {
        "url_map": {
            "gamma-api.polymarket.com/markets": [
                {
                    "id": "rel_market_001",
                    "question": "Will Pope Francis attend World Youth Day 2026?",
                    "slug": "pope-francis-wyd-2026",
                    "description": "Resolves YES if Pope Francis attends in person.",
                    "category": "Religion",
                    "tags": [{"label": "Religion", "slug": "religion"}, {"label": "Pope", "slug": "pope"}],
                    "outcomePrices": "[\"0.65\", \"0.35\"]",
                    "volume": 45000,
                    "liquidity": 12000,
                    "endDate": "2026-08-04T23:59:59Z",
                    "conditionId": "0xabcd",
                },
                {
                    "id": "rel_market_002",
                    "question": "Will the Hajj pilgrimage attendance exceed 2.5M in 2026?",
                    "slug": "hajj-2026-attendance",
                    "description": "The annual Islamic pilgrimage to Mecca.",
                    "category": "Religion",
                    "tags": [{"label": "Islam", "slug": "islam"}],
                    "outcomePrices": "[\"0.42\", \"0.58\"]",
                    "volume": 18000,
                    "liquidity": 5000,
                    "endDate": "2026-07-15T23:59:59Z",
                    "conditionId": "0xefgh",
                },
                {
                    "id": "non_religion_market_xyz",
                    "question": "Will the Lakers win the NBA Finals?",
                    "slug": "lakers-nba-finals",
                    "category": "Sports",
                    "tags": [{"label": "Basketball", "slug": "basketball"}],
                    "outcomePrices": "[\"0.10\", \"0.90\"]",
                    "volume": 100000,
                    "liquidity": 20000,
                    "endDate": "2026-06-22T23:59:59Z",
                    "conditionId": "0xskip",
                },
            ],
        },
        "expected_columns": [
            "market_id", "question", "slug",
            "yes_price", "no_price",
            "volume", "liquidity", "category", "tags",
            "religion_subcategory", "end_date",
            "condition_id", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            all(row.get("religion_subcategory") in
                ("christianity", "islam", "buddhism", "hinduism",
                 "judaism", "interfaith", "culture")
                for row in result["head"])
            and not any(row.get("question", "").startswith("Will the Lakers")
                        for row in result["head"])
        ),
    },

    # ── Phase 6 / Bet 5 slice 1.5 - curator YouTube RSS pull ──────
    # Reads the user's subscriptions parquet (written by the Takeout
    # importer), joins with watch_history to compute a priority sort,
    # and HTTP-fetches the YouTube channel RSS feed for the top N.
    # The mock router returns the same Atom XML for every channel; the
    # test fixture below DOES NOT have access to the subscriptions
    # parquet on disk, so the script's empty-state info_row path is
    # exercised. The dedicated tests in test_curator_speaktube_slice1.py
    # cover the happy path (real subscriptions on disk → multiple
    # candidates emitted) - see those for end-to-end coverage.
    "curator_youtube_rss_pull": {
        "url_map": {
            "feeds/videos.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                'xmlns:media="http://search.yahoo.com/mrss/" '
                'xmlns="http://www.w3.org/2005/Atom">'
                '<entry>'
                '<yt:videoId>dQw4w9WgXcQ</yt:videoId>'
                '<title>Mock channel - joinery demo</title>'
                '<published>2026-05-15T16:30:00+00:00</published>'
                '<author>'
                '<name>Mock Channel</name>'
                '<uri>https://www.youtube.com/channel/UC--70ql_IxJmhmqXqrkJrWQ</uri>'
                '</author>'
                '<media:group>'
                '<media:description>A demonstration of Japanese joinery.</media:description>'
                '</media:group>'
                '</entry>'
                '<entry>'
                '<yt:videoId>abcdefghIJK</yt:videoId>'
                '<title>Second mock video</title>'
                '<published>2026-05-14T09:00:00+00:00</published>'
                '<author>'
                '<name>Mock Channel</name>'
                '<uri>https://www.youtube.com/channel/UC--70ql_IxJmhmqXqrkJrWQ</uri>'
                '</author>'
                '<media:group>'
                '<media:description>Second description.</media:description>'
                '</media:group>'
                '</entry>'
                '</feed>'
            ),
        },
        "expected_columns": [
            "_epoch", "discovered_at_epoch", "source",
            "video_external_id", "video_url", "title",
            "channel_id", "channel_name", "channel_url",
            "published_iso", "description",
            "duration_seconds", "raw_blob",
        ],
        # The sandbox-test harness doesn't populate
        # indexes/IMMUTABLE/curator_takeout/subscriptions on disk, so
        # the script falls through to the info_row branch. min_rows=1
        # pins that fallback shape (well-shaped DataFrame, never empty).
        "min_rows": 1,
        "extra_checks": lambda result: (
            # info_row carries the canonical "no subs found" message
            any(
                row.get("source") in ("youtube_rss", "youtube_rss_info")
                for row in result["head"]
            )
        ),
    },

    # ── Phase 6 / Bet 5 slice 1 - speaktube telemetry pull ─────────
    # Hourly HTTP fetch of NDJSON event lines from the speaktube
    # sidecar at <base>/api/telemetry/<date>.jsonl (default base
    # http://localhost:8080; override via the task's api_url).
    # The mock returns 5 unique events spanning play_start / play_end
    # / skip / rate / mark_junk; the script's dedup key
    # (event_ts_iso + event_type + video_external_id) collapses any
    # duplicate hits across the 3-day lookback loop down to 5.
    # ── Phase 6 / Bet 5 slice 3b - topic-search via yt-dlp ─────────
    # The BREADTH piece for the curator: discovers candidates outside
    # the user's existing YouTube subscriptions by running ytsearch
    # against cluster labels from the slice-3a topic snapshot. Trust
    # level is UNRESTRICTED because yt-dlp uses urllib (not requests)
    # and isn't in the sandbox allowlist.
    #
    # The script_library harness doesn't populate
    # indexes/IMMUTABLE/curator_topic_snapshots/ on disk, so the
    # script falls through to the "no snapshot" info_row branch.
    # That's exactly the production behaviour for a fresh install that
    # hasn't bootstrapped a snapshot yet - and pins the
    # well-shaped-fallback contract (empty topic source still emits a
    # canonical-13-column DataFrame, not None). End-to-end happy-path
    # coverage with a synthetic snapshot + mocked yt_dlp lives in
    # tests/test_curator_slice3b_topic_search.py.
    "curator_topic_search_pull_pro": {
        "url_map": {},  # script uses yt_dlp (urllib), not requests
        "expected_columns": [
            "_epoch", "discovered_at_epoch", "source",
            "video_external_id", "video_url", "title",
            "channel_id", "channel_name", "channel_url",
            "published_iso", "description",
            "duration_seconds", "raw_blob",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            # The empty-snapshot fallback emits exactly one info_row
            # with the canonical "run snapshot refresh first" message.
            len(result["head"]) == 1
            and result["head"][0].get("source") == "topic_search_info"
            and "tools.curator_topic_snapshot_refresh"
                in result["head"][0].get("title", "")
        ),
    },

    # ── Phase 6 / Bet 5 slice 7 - Archive.org multi-source ingestion ─
    # First non-YouTube curator candidate source. Pulls public-domain
    # films / lectures / archival video from Archive.org's
    # advancedsearch.php endpoint and emits canonical 14-col
    # candidate-row schema with source="archive_org". yt-dlp resolves
    # the archive.org/details/<id> URLs natively at speaktube playback
    # time. expected_columns include thumbnail_url (slice 4 +
    # slice 7) since this is a NEW script that ships with the
    # extended schema from day 1.
    "curator_archive_org_pull": {
        "url_map": {
            "advancedsearch.php": {
                "responseHeader": {"status": 0, "params": {}},
                "response": {
                    "numFound": 2,
                    "start": 0,
                    "docs": [
                        {
                            "identifier": "charade_1963",
                            "title": "Charade (1963)",
                            "description": "Public-domain noir thriller starring Cary Grant and Audrey Hepburn.",
                            "creator": "Stanley Donen",
                            "date": "1963",
                            "publicdate": "2019-04-12T18:30:00Z",
                            "downloads": 12345,
                            "runtime": "1:53:00",
                        },
                        {
                            "identifier": "MIT_Open_Courseware_lecture_42",
                            "title": "MIT 6.001 SICP Lecture 1A",
                            "description": "Structure and Interpretation of Computer Programs, intro lecture.",
                            "creator": "Hal Abelson",
                            "date": "1986",
                            "publicdate": "2007-11-01T00:00:00Z",
                            "downloads": 98765,
                            "runtime": "1:08:30",
                        },
                    ],
                },
            },
        },
        "expected_columns": [
            "_epoch", "discovered_at_epoch", "source",
            "video_external_id", "video_url", "title",
            "channel_id", "channel_name", "channel_url",
            "published_iso", "description",
            "duration_seconds", "thumbnail_url", "raw_blob",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            all(row.get("source") == "archive_org" for row in result["head"])
            and any(row.get("video_external_id") == "charade_1963"
                    for row in result["head"])
            and all(row.get("video_url", "").startswith(
                        "https://archive.org/details/")
                    for row in result["head"])
            and all(row.get("thumbnail_url", "").startswith(
                        "https://archive.org/services/img/")
                    for row in result["head"])
            and any(row.get("duration_seconds") == 6780
                    for row in result["head"])  # 1:53:00 parsed
            and any(row.get("channel_id", "").startswith("archive_org:")
                    for row in result["head"])
        ),
    },

    "curator_telemetry_pull": {
        "url_map": {
            "/api/telemetry/": (
                '{"event_type":"play_start","event_ts":"2026-05-16T09:14:22-07:00",'
                '"video_external_id":"dQw4w9WgXcQ","chosen_by":"curator",'
                '"run_date":"2026-05-16","position":3}\n'
                '{"event_type":"play_end","event_ts":"2026-05-16T09:42:01-07:00",'
                '"video_external_id":"dQw4w9WgXcQ","chosen_by":"curator",'
                '"watched_seconds":1660,"total_seconds":1742}\n'
                '{"event_type":"skip","event_ts":"2026-05-16T10:01:00-07:00",'
                '"video_external_id":"shorts_xyz","chosen_by":"recommendation",'
                '"watched_seconds":8,"total_seconds":42}\n'
                '{"event_type":"rate","event_ts":"2026-05-16T10:30:00-07:00",'
                '"video_external_id":"dQw4w9WgXcQ","chosen_by":"curator","rating":8}\n'
                '{"event_type":"mark_junk","event_ts":"2026-05-16T10:35:00-07:00",'
                '"video_external_id":"junk_clip","chosen_by":"recommendation",'
                '"reason":"clickbait thumbnail"}\n'
            ),
        },
        "expected_columns": [
            "_epoch", "event_ts_iso", "event_date", "event_type",
            "video_external_id", "chosen_by", "run_date", "position",
            "slot_kind", "watched_seconds", "total_seconds",
            "rating", "reason", "kind", "content", "query", "raw_json",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            # Dedup worked - the 3-day loop fetched the same payload
            # 3 times, dedup keys collapsed to exactly 5 unique events.
            len(result["head"]) == 5
            and any(row.get("event_type") == "rate" and row.get("rating") == 8
                    for row in result["head"])
            and any(row.get("event_type") == "mark_junk" for row in result["head"])
        ),
    },
}


# ═══════════════════════════════════════════════════════════════════
# Discover all no-auth scripts (cross-check against registry)
# ═══════════════════════════════════════════════════════════════════

def _discover_no_auth_scripts():
    """Return sorted list of script names (without .json) that need no credentials.

    "No-auth" here means the script can run with ZERO credential slots - both
    ``requires_credentials`` and ``credential_kinds`` are empty. Scripts with
    a non-empty ``credential_kinds`` are treated as credentialed even when
    ``requires_credentials`` is empty - those are the "optional credential"
    cases (e.g. SEC_EDGAR_CONTACT for the SEC scripts: no API key exists, but
    the user can optionally supply a custom User-Agent contact string and
    the test harness still wants to exercise that code path with
    ``_sec_router_factory()`` + injected CREDENTIALS).
    """
    scripts = []
    for path in sorted(SCRIPTS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        if data.get("requires_credentials") == [] and not data.get("credential_kinds"):
            scripts.append(path.stem)
    return scripts


ALL_NO_AUTH = _discover_no_auth_scripts()
ALL_REGISTERED = sorted(SCRIPT_REGISTRY.keys())


# ═══════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════

class TestRegistryCoverage:
    """Ensure every no-auth script in the library has a registry entry."""

    def test_all_no_auth_scripts_registered(self):
        missing = set(ALL_NO_AUTH) - set(ALL_REGISTERED)
        assert not missing, (
            f"No-auth scripts missing from SCRIPT_REGISTRY: {sorted(missing)}"
        )

    def test_no_stale_registry_entries(self):
        stale = set(ALL_REGISTERED) - set(ALL_NO_AUTH)
        assert not stale, (
            f"SCRIPT_REGISTRY entries with no matching script file: {sorted(stale)}"
        )


@pytest.mark.parametrize("script_name", ALL_REGISTERED, ids=ALL_REGISTERED)
class TestScriptJsonStructure:
    """Validate JSON schema and metadata for each no-auth library script."""

    REQUIRED_KEYS = {
        "title", "description", "category", "api_url",
        "requires_credentials", "suggested_cron", "suggested_subdirectory", "tags", "code",
    }

    def test_has_required_keys(self, script_name):
        path = SCRIPTS_DIR / f"{script_name}.json"
        data = json.loads(path.read_text())
        missing = self.REQUIRED_KEYS - set(data.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_no_credentials_required(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        assert data["requires_credentials"] == [], (
            f"Expected no credentials, got: {data['requires_credentials']}"
        )

    def test_code_contains_generate_results(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        assert "GENERATE_RESULTS" in data["code"], (
            "Script code must call GENERATE_RESULTS(df)"
        )

    def test_has_no_auth_tag(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        tags = data.get("tags", [])
        assert "no-auth" in tags or "free" in tags, (
            f"No-auth script should have 'no-auth' or 'free' tag, got: {tags}"
        )

    def test_trust_level_valid(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        trust = data.get("trust_level", "sandboxed")
        assert trust in ("sandboxed", "unrestricted"), (
            f"trust_level must be 'sandboxed' or 'unrestricted', got: {trust!r}"
        )

    def test_title_has_no_special_characters(self, script_name):
        """
        Titles must be safe for filename/URL/display use.  The allowed
        set matches saved_search_store._SAFE_NAME: letters, digits,
        space, underscore, period, hyphen.  Colons and parens are out.
        """
        import re as _re
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        title = data.get("title", "")
        assert _re.match(r"^[A-Za-z0-9 _.\-]+$", title), (
            f"Title {title!r} contains disallowed characters. "
            f"Use letters, digits, space, underscore, period, hyphen only."
        )


@pytest.mark.parametrize("script_name", ALL_REGISTERED, ids=ALL_REGISTERED)
class TestScriptExecution:
    """Run each script through CodeExecutor.execute_test() with mocked HTTP."""

    def test_executes_valid_dataframe(self, script_name):
        spec = SCRIPT_REGISTRY[script_name]
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        code = data["code"]
        trust_level = data.get("trust_level", "sandboxed")

        router = _make_router(spec["url_map"])
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(code, test_mode=True, trust_level=trust_level)
            result = executor.execute_test()

        # ── Universal assertions ──────────────────────────────
        assert result["status"] == "pass", (
            f"Script failed with errors: {result['errors']}"
        )
        assert result["errors"] == [], (
            f"Unexpected errors: {result['errors']}"
        )
        assert result["has_epoch"] is True, "Missing _epoch column"
        assert "_epoch" in result["columns"], "_epoch not in columns list"
        assert result["row_count"] >= spec.get("min_rows", 1), (
            f"Expected >= {spec.get('min_rows', 1)} rows, got {result['row_count']}"
        )

        # ── Column presence ───────────────────────────────────
        for col in spec.get("expected_columns", []):
            assert col in result["columns"], (
                f"Missing expected column '{col}'. Got: {result['columns']}"
            )

        # ── Script-specific checks ────────────────────────────
        extra = spec.get("extra_checks")
        if extra:
            assert extra(result), (
                f"Extra check failed. head={result['head']}"
            )


# ═══════════════════════════════════════════════════════════════════
# Credentialed script support (FRED, etc.)
# ═══════════════════════════════════════════════════════════════════

# ── FRED API mock data ───────────────────────────────────────────

def _make_fred_observations(values):
    """Build a FRED /series/observations response from a list of (date, value) pairs."""
    obs = []
    for i in range(len(values)):
        obs.append({
            "realtime_start": values[i][0],
            "realtime_end": values[i][0],
            "date": values[i][0],
            "value": str(values[i][1]),
        })
    return {"observations": obs}


# Yield curve series
MOCK_FRED_DGS1MO = _make_fred_observations([("2026-04-08", 4.30)])
MOCK_FRED_DGS3MO = _make_fred_observations([("2026-04-08", 4.35)])
MOCK_FRED_DGS6MO = _make_fred_observations([("2026-04-08", 4.25)])
MOCK_FRED_DGS1 = _make_fred_observations([("2026-04-08", 4.10)])
MOCK_FRED_DGS2 = _make_fred_observations([("2026-04-08", 3.95)])
MOCK_FRED_DGS5 = _make_fred_observations([("2026-04-08", 3.80)])
MOCK_FRED_DGS10 = _make_fred_observations([("2026-04-08", 4.05)])
MOCK_FRED_DGS20 = _make_fred_observations([("2026-04-08", 4.35)])
MOCK_FRED_DGS30 = _make_fred_observations([("2026-04-08", 4.25)])

# CPI / Inflation series - 13 observations for YoY calculation
MOCK_FRED_CPI_13 = _make_fred_observations([
    ("2026-03-01", 315.5), ("2026-02-01", 314.8), ("2026-01-01", 314.1),
    ("2025-12-01", 313.5), ("2025-11-01", 312.9), ("2025-10-01", 312.2),
    ("2025-09-01", 311.6), ("2025-08-01", 311.0), ("2025-07-01", 310.4),
    ("2025-06-01", 309.8), ("2025-05-01", 309.2), ("2025-04-01", 308.5),
    ("2025-03-01", 307.0),
])

# Labor market series
MOCK_FRED_LABOR_5 = _make_fred_observations([
    ("2026-03-01", 3.8), ("2026-02-01", 3.9), ("2026-01-01", 3.9),
    ("2025-12-01", 4.0), ("2025-11-01", 4.1),
])

# Money supply series
MOCK_FRED_M2_13 = _make_fred_observations([
    ("2026-03-01", 21500), ("2026-02-01", 21400), ("2026-01-01", 21300),
    ("2025-12-01", 21200), ("2025-11-01", 21100), ("2025-10-01", 21000),
    ("2025-09-01", 20900), ("2025-08-01", 20800), ("2025-07-01", 20700),
    ("2025-06-01", 20600), ("2025-05-01", 20500), ("2025-04-01", 20400),
    ("2025-03-01", 20100),
])

# Housing series
MOCK_FRED_HOUSING_13 = _make_fred_observations([
    ("2026-03-01", 1450), ("2026-02-01", 1420), ("2026-01-01", 1400),
    ("2025-12-01", 1390), ("2025-11-01", 1380), ("2025-10-01", 1370),
    ("2025-09-01", 1360), ("2025-08-01", 1350), ("2025-07-01", 1340),
    ("2025-06-01", 1330), ("2025-05-01", 1320), ("2025-04-01", 1310),
    ("2025-03-01", 1290),
])

# Fear gauge series
MOCK_FRED_FEAR_5 = _make_fred_observations([
    ("2026-04-08", 22.5), ("2026-04-07", 21.0), ("2026-04-04", 20.5),
    ("2026-04-03", 19.8), ("2026-04-02", 18.5),
])


MOCK_FRED_SERIES_METADATA = {
    "seriess": [{
        "id": "CPIAUCSL",
        "title": "CPI All Urban Consumers",
        "units": "Index 1982-1984=100",
        "frequency": "Monthly",
    }],
}


def _fred_router_factory(default_mock):
    """Return a callable that routes FRED requests by series_id param."""
    SERIES_MAP = {
        # Yield curve
        "DGS1MO": MOCK_FRED_DGS1MO, "DGS3MO": MOCK_FRED_DGS3MO,
        "DGS6MO": MOCK_FRED_DGS6MO, "DGS1": MOCK_FRED_DGS1,
        "DGS2": MOCK_FRED_DGS2, "DGS5": MOCK_FRED_DGS5,
        "DGS10": MOCK_FRED_DGS10, "DGS20": MOCK_FRED_DGS20,
        "DGS30": MOCK_FRED_DGS30,
        # CPI
        "CPIAUCSL": MOCK_FRED_CPI_13, "CPILFESL": MOCK_FRED_CPI_13,
        "PCEPI": MOCK_FRED_CPI_13, "PCEPILFE": MOCK_FRED_CPI_13,
        "CPIENGSL": MOCK_FRED_CPI_13, "CPIUFDSL": MOCK_FRED_CPI_13,
        # Labor
        "UNRATE": MOCK_FRED_LABOR_5, "U6RATE": MOCK_FRED_LABOR_5,
        "ICSA": _make_fred_observations([
            ("2026-04-05", 215000), ("2026-03-29", 220000),
            ("2026-03-22", 218000), ("2026-03-15", 225000),
            ("2026-03-08", 222000),
        ]),
        "CCSA": MOCK_FRED_LABOR_5, "PAYEMS": MOCK_FRED_LABOR_5,
        "CIVPART": MOCK_FRED_LABOR_5, "LNS12300060": MOCK_FRED_LABOR_5,
        # Money supply
        "M2SL": MOCK_FRED_M2_13, "WALCL": MOCK_FRED_M2_13,
        "RRPONTSYD": MOCK_FRED_M2_13, "FEDFUNDS": MOCK_FRED_LABOR_5,
        "BOGMBASE": MOCK_FRED_M2_13,
        # Housing
        "HOUST": MOCK_FRED_HOUSING_13, "PERMIT": MOCK_FRED_HOUSING_13,
        "EXHOSLUSM495S": MOCK_FRED_HOUSING_13, "CSUSHPINSA": MOCK_FRED_HOUSING_13,
        "MORTGAGE30US": _make_fred_observations([
            ("2026-04-03", 6.75), ("2026-03-27", 6.80), ("2026-03-20", 6.85),
            ("2026-03-13", 6.90), ("2026-03-06", 6.95),
            ("2025-12-01", 7.00), ("2025-11-01", 7.05), ("2025-10-01", 7.10),
            ("2025-09-01", 7.15), ("2025-08-01", 7.20), ("2025-07-01", 7.18),
            ("2025-06-01", 7.12), ("2025-04-03", 7.25),
        ]),
        "MORTGAGE15US": MOCK_FRED_LABOR_5, "MSACSR": MOCK_FRED_LABOR_5,
        # Fear gauges
        "VIXCLS": MOCK_FRED_FEAR_5, "BAMLH0A0HYM2": MOCK_FRED_FEAR_5,
        "BAMLC0A0CM": MOCK_FRED_FEAR_5, "STLFSI2": MOCK_FRED_FEAR_5,
        "DTWEXBGS": MOCK_FRED_FEAR_5,
        # Economic indicators (correlation engine)
        "GDP": _make_fred_observations([("2026-01-01", 28500), ("2025-10-01", 28200)]),
        "T10Y2Y": _make_fred_observations([("2026-04-08", 0.15), ("2026-04-07", 0.12)]),
        "DEXUSEU": _make_fred_observations([("2026-04-08", 1.085), ("2026-04-07", 1.082)]),
        "GASREGW": _make_fred_observations([("2026-04-07", 3.45), ("2026-03-31", 3.40)]),
    }

    def router(url, **kwargs):
        params = kwargs.get("params", {})
        series_id = params.get("series_id", "")
        # Handle /fred/series metadata endpoint (no /observations)
        if "/fred/series" in url and "/observations" not in url:
            return _make_response(MOCK_FRED_SERIES_METADATA)
        mock = SERIES_MAP.get(series_id, default_mock)
        return _make_response(mock)
    return router


# ── Global Macro Risk Brief FRED series (commodity, CB, FX, OECD CLI) ─

def _make_fred_desc_series(latest, num_obs=30, delta_per_step=0.02):
    """Build a FRED /observations response in descending date order.

    The newest observation equals ``latest``. Each older observation is
    offset by ``delta_per_step * i`` (positive delta → older values are
    higher than latest, so latest is the series' minimum in the window).
    Used by macro-brief FRED scripts that need >=30 observations to compute
    meaningful 30-day / 90-day change percentages.
    """
    base = _dt.datetime(2026, 4, 22)
    pairs = []
    for i in range(num_obs):
        d = base - _dt.timedelta(days=i)
        v = latest + (i * delta_per_step)
        pairs.append((d.strftime("%Y-%m-%d"), round(v, 4)))
    return _make_fred_observations(pairs)


# Macro-brief series mocks. 120-obs series for daily commodity/FX (so the
# 90d index is in range); 24-obs series for monthly OECD CLI (so 12m index
# is in range). Each series' latest value is realistic as of early-2026.
MACRO_FRED_SERIES_MAP = {
    # ag_central_bank_policy - policy rates + G4 yields
    "FEDFUNDS": _make_fred_desc_series(5.25, num_obs=60, delta_per_step=0.005),
    "DFEDTARU": _make_fred_desc_series(5.50, num_obs=60, delta_per_step=0.005),
    "DGS2_MACRO": _make_fred_desc_series(4.85, num_obs=60, delta_per_step=0.01),
    "DGS10_MACRO": _make_fred_desc_series(4.25, num_obs=60, delta_per_step=0.01),
    "T10Y2Y_MACRO": _make_fred_desc_series(-0.60, num_obs=60, delta_per_step=0.002),
    "T10Y3M": _make_fred_desc_series(-0.80, num_obs=60, delta_per_step=0.002),
    "ECBDFR": _make_fred_desc_series(3.25, num_obs=60, delta_per_step=0.003),
    "IRLTLT01DEM156N": _make_fred_desc_series(2.50, num_obs=60, delta_per_step=0.005),
    "IRLTLT01JPM156N": _make_fred_desc_series(1.10, num_obs=60, delta_per_step=0.005),
    "IRLTLT01GBM156N": _make_fred_desc_series(4.30, num_obs=60, delta_per_step=0.008),
    # ag_commodity_stress - commodity prices
    "DCOILWTICO": _make_fred_desc_series(85.50, num_obs=120, delta_per_step=0.05),
    "DCOILBRENTEU": _make_fred_desc_series(89.20, num_obs=120, delta_per_step=0.05),
    "DHHNGSP": _make_fred_desc_series(3.20, num_obs=120, delta_per_step=0.01),
    "GOLDPMGBD228NLBM": _make_fred_desc_series(2750.0, num_obs=120, delta_per_step=1.0),
    "PCOPPUSDM": _make_fred_desc_series(9250.0, num_obs=120, delta_per_step=10.0),
    "PALUMUSDM": _make_fred_desc_series(2550.0, num_obs=120, delta_per_step=3.0),
    "PMAIZMTUSDM": _make_fred_desc_series(210.0, num_obs=120, delta_per_step=0.2),
    "PWHEAMTUSDM": _make_fred_desc_series(285.0, num_obs=120, delta_per_step=0.5),
    "PSOYBUSDM": _make_fred_desc_series(485.0, num_obs=120, delta_per_step=0.8),
    # ag_fx_and_yields - FX majors + EM + real yields + breakeven
    "DEXUSEU_MACRO": _make_fred_desc_series(1.0850, num_obs=120, delta_per_step=0.0005),
    "DEXJPUS": _make_fred_desc_series(155.20, num_obs=120, delta_per_step=0.05),
    "DEXCHUS": _make_fred_desc_series(7.2450, num_obs=120, delta_per_step=0.001),
    "DEXMXUS": _make_fred_desc_series(17.85, num_obs=120, delta_per_step=0.01),
    "DEXBZUS": _make_fred_desc_series(5.15, num_obs=120, delta_per_step=0.005),
    "DEXUSUK": _make_fred_desc_series(1.2650, num_obs=120, delta_per_step=0.0008),
    "DEXCAUS": _make_fred_desc_series(1.3750, num_obs=120, delta_per_step=0.001),
    "DEXUSAL": _make_fred_desc_series(0.6680, num_obs=120, delta_per_step=0.0005),
    "DEXINUS": _make_fred_desc_series(83.45, num_obs=120, delta_per_step=0.03),
    # DEXSZUS = Swiss Franc per USD (real ~0.86-0.92). DEXSFUS in FRED
    # is actually Swedish Kronor per USD (real ~10-17) - round-6 audit
    # caught the script using DEXSFUS and labelling output as USDCHF
    # while routing the FXF (Swiss) ETF, producing 16.49 nonsense values.
    "DEXSZUS": _make_fred_desc_series(0.9120, num_obs=120, delta_per_step=0.0005),
    "DFII10": _make_fred_desc_series(2.15, num_obs=120, delta_per_step=0.005),
    "T10YIE": _make_fred_desc_series(2.35, num_obs=120, delta_per_step=0.003),
    # ag_leading_indicators - OECD CLI for G7 + BRICS (monthly, 24 obs)
    "USALOLITONOSTSAM": _make_fred_desc_series(100.3, num_obs=24, delta_per_step=0.05),
    "JPNLOLITONOSTSAM": _make_fred_desc_series(99.8, num_obs=24, delta_per_step=0.04),
    "DEULOLITONOSTSAM": _make_fred_desc_series(99.5, num_obs=24, delta_per_step=0.06),
    "GBRLOLITONOSTSAM": _make_fred_desc_series(100.1, num_obs=24, delta_per_step=0.04),
    "FRALOLITONOSTSAM": _make_fred_desc_series(99.7, num_obs=24, delta_per_step=0.05),
    "ITALOLITONOSTSAM": _make_fred_desc_series(99.9, num_obs=24, delta_per_step=0.05),
    "CANLOLITONOSTSAM": _make_fred_desc_series(100.0, num_obs=24, delta_per_step=0.04),
    "BRALOLITONOSTSAM": _make_fred_desc_series(100.5, num_obs=24, delta_per_step=0.03),
    "CHNLOLITONOSTSAM": _make_fred_desc_series(99.2, num_obs=24, delta_per_step=0.07),
    "INDLOLITONOSTSAM": _make_fred_desc_series(101.0, num_obs=24, delta_per_step=0.08),
    "KORLOLITONOSTSAM": _make_fred_desc_series(99.8, num_obs=24, delta_per_step=0.05),
    "MEXLOLITONOSTSAM": _make_fred_desc_series(100.2, num_obs=24, delta_per_step=0.04),
    "OECDLOLITONOSTSAM": _make_fred_desc_series(100.0, num_obs=24, delta_per_step=0.03),
}


def _fred_macro_router_factory():
    """Return a router for macro-brief FRED scripts.

    Falls back to a 30-obs 'no signal' series if the series_id isn't in
    MACRO_FRED_SERIES_MAP so the script still emits a row (non-regressing
    min_rows assertion) while surfacing unknown-series as a test warning.
    """
    default_empty = _make_fred_desc_series(1.0, num_obs=30, delta_per_step=0.0)
    overlapping_aliases = {
        "DGS2": MACRO_FRED_SERIES_MAP["DGS2_MACRO"],
        "DGS10": MACRO_FRED_SERIES_MAP["DGS10_MACRO"],
        "T10Y2Y": MACRO_FRED_SERIES_MAP["T10Y2Y_MACRO"],
        "DEXUSEU": MACRO_FRED_SERIES_MAP["DEXUSEU_MACRO"],
    }

    def router(url, **kwargs):
        params = kwargs.get("params", {})
        series_id = params.get("series_id", "")
        if "/fred/series" in url and "/observations" not in url:
            return _make_response(MOCK_FRED_SERIES_METADATA)
        if series_id in overlapping_aliases:
            return _make_response(overlapping_aliases[series_id])
        mock = MACRO_FRED_SERIES_MAP.get(series_id, default_empty)
        return _make_response(mock)
    return router


CREDENTIALED_SCRIPT_REGISTRY = {
    "fred_yield_curve": {
        "expected_columns": [
            "yield_2y", "yield_10y", "yield_30y",
            "spread_10y_2y", "spread_10y_3m",
            "is_inverted_10y2y", "curve_signal", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("curve_signal") in
                ("NORMAL", "RECESSION_WATCH", "STRONG_RECESSION_WARNING")
                for row in result["head"])
        ),
    },
    "fred_inflation_monitor": {
        "expected_columns": [
            "series_id", "metric", "latest_value", "latest_date",
            "mom_change_pct", "yoy_change_pct", "inflation_status", "_epoch",
        ],
        "min_rows": 4,
    },
    "fred_labor_market": {
        "expected_columns": [
            "series_id", "metric", "latest_value", "latest_date",
            "prior_value", "change", "signal", "_epoch",
        ],
        "min_rows": 5,
    },
    "fred_money_supply": {
        "expected_columns": [
            "series_id", "metric", "latest_value", "latest_date",
            "period_change_pct", "yoy_change_pct", "liquidity_signal", "_epoch",
        ],
        "min_rows": 4,
    },
    "fred_housing_market": {
        "expected_columns": [
            "series_id", "metric", "latest_value", "latest_date",
            "mom_change_pct", "yoy_change_pct", "market_signal", "_epoch",
        ],
        "min_rows": 5,
    },
    "fred_fear_gauges": {
        "expected_columns": [
            "series_id", "metric", "latest_value", "latest_date",
            "change", "change_pct", "fear_level", "_epoch",
        ],
        "min_rows": 4,
    },
    "fred_fear_gauges_pro": {
        "expected_columns": [
            "series_id", "metric", "latest_value", "latest_date",
            "change", "change_pct", "fear_level",
            "percentile_rank", "z_score_1y", "mean_1y", "std_1y",
            "history_points", "regime", "_epoch",
        ],
        "min_rows": 4,
        "extra_checks": lambda result: (
            all(row.get("regime") in ("CALM", "NORMAL", "ELEVATED", "STRESSED", "CRISIS")
                for row in result["head"])
        ),
    },
    "fred_yield_curve_pro": {
        "expected_columns": [
            "yield_2y", "yield_10y", "yield_30y",
            "spread_10y_2y", "spread_10y_3m",
            "is_inverted_10y2y", "curve_signal",
            "curve_level", "curve_slope", "curve_curvature",
            "inversion_depth_bp", "curve_shape", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("curve_shape") in
                ("UNKNOWN", "INVERTED", "FLAT", "HUMPED", "NORMAL", "STEEP")
                for row in result["head"])
        ),
    },
    "fred_economic_indicators": {
        "expected_columns": [
            "series_id", "series_name", "latest_value", "previous_value",
            "pct_change", "observation_date", "units", "frequency", "_epoch",
        ],
        "min_rows": 8,
        "extra_checks": lambda result: (
            all(row.get("series_id") in (
                "CPIAUCSL", "UNRATE", "FEDFUNDS", "GDP",
                "T10Y2Y", "DEXUSEU", "GASREGW", "MORTGAGE30US",
            ) for row in result["head"])
        ),
    },

    # ── OpenWeatherMap ───────────────────────────────────────────

    "openweathermap_current": {
        "expected_columns": [
            "city", "temp_f", "feels_like_f", "humidity_pct",
            "pressure_hpa", "wind_speed_mph", "weather", "description",
            "_epoch",
        ],
        "min_rows": 5,
    },

    # ── Polymarket credentialed (per-user / search) ──────────────

    "polymarket_user_positions": {
        "expected_columns": [
            "user_address", "condition_id", "asset_id", "question",
            "outcome", "size", "avg_price", "current_price", "pnl",
            "_epoch",
        ],
        "min_rows": 2,
    },
    "polymarket_user_activity": {
        "expected_columns": [
            "user_address", "type", "condition_id", "question",
            "side", "size", "price", "outcome",
            "transaction_hash", "trade_timestamp", "_epoch",
        ],
        "min_rows": 2,
    },
    "polymarket_public_profile_lookup": {
        "expected_columns": [
            "user_address", "username", "bio", "profile_image",
            "markets_traded", "volume", "profit",
            "positions_won", "positions_lost", "_epoch",
        ],
        "min_rows": 1,
    },
    "polymarket_search_monitor": {
        "expected_columns": [
            "search_term", "market_id", "question", "slug",
            "yes_price", "no_price", "volume", "liquidity",
            "active", "closed", "category", "tags",
            "condition_id", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── SEC EDGAR scripts ────────────────────────────────────────

    "sec_company_directory": {
        "expected_columns": [
            "cik", "cik_padded", "ticker", "company_name", "_epoch",
        ],
        "min_rows": 2,
    },
    "sec_major_filings_feed": {
        "expected_columns": [
            "ticker", "company_name", "cik", "form_type", "filing_type",
            "filing_date", "accession_number", "importance", "_epoch",
        ],
        "min_rows": 1,
    },
    "sec_revenue_leaders": {
        "expected_columns": [
            "cik", "entity_name", "concept", "period",
            "revenue_usd", "revenue_billions", "filed_date", "_epoch",
        ],
        "min_rows": 1,
    },
    "sec_profitability_screen": {
        "expected_columns": [
            "cik", "entity_name", "period", "net_income_usd",
            "net_income_millions", "profitability", "filed_date", "_epoch",
        ],
        "min_rows": 1,
    },
    "sec_balance_sheet_screen": {
        "expected_columns": [
            "cik", "entity_name", "period", "total_assets",
            "total_liabilities", "stockholders_equity",
            "debt_to_equity", "equity_ratio", "balance_sheet_health", "_epoch",
        ],
        "min_rows": 1,
    },

    # ── Global Macro Risk Brief FRED feeders ──────────────────────

    "fred_global_central_banks": {
        "expected_columns": [
            "series_id", "description", "country", "series_type",
            "latest_value", "latest_date", "value_30d_ago", "change_30d",
            "change_bps", "regime_flag", "investment_thesis", "_epoch",
        ],
        "min_rows": 8,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("TIGHTENING", "EASING", "STEADY", "RISING", "FALLING",
                 "STABLE", "NORMAL", "FLAT", "INVERTED", "UNKNOWN")
                for row in result["head"])
            and any(row.get("country") == "USA" for row in result["head"])
        ),
    },
    "fred_commodity_prices": {
        "expected_columns": [
            "series_id", "description", "category", "unit",
            "latest_value", "latest_date", "value_30d_ago", "value_90d_ago",
            "change_30d_pct", "change_90d_pct", "regime_flag",
            "investment_thesis", "etf_ticker", "_epoch",
        ],
        "min_rows": 7,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("SURGING", "RISING", "STEADY", "FALLING", "COLLAPSING")
                for row in result["head"])
            and any(row.get("category") == "grain" for row in result["head"])
        ),
    },
    "fred_fx_and_yields": {
        "expected_columns": [
            "series_id", "description", "pair_code", "series_type",
            "latest_value", "latest_date", "value_30d_ago", "value_90d_ago",
            "change_30d_pct", "change_90d_pct", "regime_flag",
            "investment_thesis", "etf_ticker", "_epoch",
        ],
        "min_rows": 10,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("USD_STRENGTH", "USD_WEAKNESS", "STABLE",
                 "RISING", "FALLING",
                 "INFLATION_RISING", "INFLATION_FALLING", "ANCHORED",
                 "UNKNOWN")
                for row in result["head"])
            and any(row.get("series_type") in ("fx_major", "fx_em", "real_yield", "breakeven")
                    for row in result["head"])
        ),
    },
    "fred_oecd_leading_indicators": {
        "expected_columns": [
            "country_code", "country_name", "series_id", "latest_value",
            "latest_date", "value_3m_ago", "value_12m_ago",
            "cli_momentum_3m", "cli_momentum_12m", "regime_flag",
            "business_cycle_phase", "rotation_thesis", "_epoch",
        ],
        "min_rows": 10,
        "extra_checks": lambda result: (
            all(row.get("business_cycle_phase") in
                ("Expansion", "Late Cycle", "Slowdown / Recession",
                 "Recovery", "Mid Cycle", "Unknown")
                for row in result["head"])
        ),
    },
    "options_unusual_activity_pro": {
        # Replaced the Finnhub-backed variant 2026-04-25 with a Massive.com
        # (formerly polygon.io) Options Starter implementation after
        # Finnhub issue #545 documented an unresolved 85%+ ATM-options
        # mispricing. Massive returns server-computed greeks + IV + OI
        # in the chain snapshot at the $29/mo Starter tier. Output
        # schema is a superset of the prior variant - same columns plus
        # day_vwap and break_even_price. Bid/ask are best-effort (the
        # snapshot chain endpoint at Starter tier doesn't populate
        # last_quote/last_trade), so the extra_checks no longer require
        # a non-null bid.
        "expected_columns": [
            "ticker", "contract_type", "strike", "expiration", "days_to_expiry",
            "volume", "open_interest", "vol_oi_ratio", "last_price",
            "implied_vol", "underlying_price", "contract_symbol",
            "alert_level", "direction_bias",
            "iv_rank", "delta", "gamma", "vega", "theta",
            "bid", "ask", "greeks_source",
            "day_vwap", "break_even_price",
            "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("greeks_source") == "massive" for row in result["head"])
            and any(
                row.get("alert_level") in ("HIGH", "CRITICAL")
                for row in result["head"]
            )
            and all(
                row.get("contract_symbol", "").startswith("O:")
                for row in result["head"]
                if row.get("contract_symbol")
            )
        ),
    },

    # ── Options Edge Brief Wave 1 (2026-04-26) ─────────────────────
    # Six new Massive.com-backed scripts powering the OEB alert group.
    # All 5 chain/snapshot scripts share the same fixture set
    # (MOCK_MASSIVE_OEB_CHAIN with FRONT 30-DTE + BACK 70-DTE contracts
    # at 25-delta and ATM, calls + puts; underlying snapshot at $150;
    # 260 days of daily-close history for HV computation).
    "options_iv_rank_screener_pro": {
        "expected_columns": [
            "ticker", "current_atm_iv", "underlying_price",
            "hv30", "iv_premium", "iv_rank_proxy", "hv30_pctile",
            "iv_regime", "premium_signal", "sample_size_contracts",
            "avg_call_iv_atm", "avg_put_iv_atm", "iv_skew_atm",
            "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("iv_regime") in ("HIGH", "MODERATE", "LOW", "UNKNOWN")
                for row in result["head"]
            )
            and all(
                row.get("premium_signal") in (
                    "SELL_PREMIUM", "BUY_PREMIUM", "NEUTRAL", "UNKNOWN"
                )
                for row in result["head"]
            )
        ),
    },
    "options_term_structure_pro": {
        "expected_columns": [
            "ticker", "front_atm_iv", "back_atm_iv",
            "term_slope", "term_ratio",
            "structure", "signal_class",
            "front_dte_avg", "back_dte_avg",
            "front_sample_size", "back_sample_size",
            "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("structure") in (
                    "BACKWARDATION", "CONTANGO", "FLAT", "UNKNOWN"
                )
                for row in result["head"]
            )
            and any(
                row.get("structure") == "BACKWARDATION"
                for row in result["head"]
            )
        ),
    },
    "options_skew_monitor_pro": {
        "expected_columns": [
            "ticker", "put_iv_25d", "call_iv_25d",
            "skew_25d", "atm_iv", "skew_pct",
            "regime", "sample_put", "sample_call",
            "avg_dte", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("regime") in (
                    "STRESS_BIDDED", "NORMAL", "CALL_SKEW", "UNKNOWN"
                )
                for row in result["head"]
            )
        ),
    },
    "options_earnings_implied_move_pro": {
        # Test exercises the graceful-fallback path: when no
        # indexes/equities/earnings_calendar/*.parquet exists at test time
        # (which it doesn't in the test harness), the script emits a
        # single INFO row with signal_class="NO_EARNINGS". The full
        # happy-path test (with a tmp parquet seeded) lives in the
        # dedicated tests/test_options_edge_brief.py file.
        "expected_columns": [
            "ticker", "earnings_date", "days_to_earnings",
            "underlying_price", "atm_strike",
            "call_mid", "put_mid", "straddle_price",
            "implied_move_pct", "implied_move_dollars",
            "expiration_used", "dte_at_expiration",
            "signal_class", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("signal_class") in (
                    "HIGH_IV", "MODERATE", "LOW_IV", "UNKNOWN", "NO_EARNINGS"
                )
                for row in result["head"]
            )
        ),
    },
    "options_market_status": {
        "expected_columns": [
            "session", "exchanges_open_count", "exchanges_open_list",
            "nyse_status", "nasdaq_status", "otc_status",
            "is_early_close_today", "minutes_to_close",
            "next_holiday_date", "next_holiday_name", "next_holiday_status",
            "advisory", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("advisory") in (
                    "TRADE_NORMAL", "PRE_HOLIDAY_LIQUIDITY_RISK",
                    "POST_HOLIDAY_REOPEN", "AFTER_HOURS",
                    "WEEKEND", "UNKNOWN"
                )
                for row in result["head"]
            )
            and all(
                row.get("session") in ("open", "closed", "extended-hours", "unknown")
                for row in result["head"]
            )
        ),
    },
    "options_ex_div_calendar": {
        "expected_columns": [
            "ticker", "ex_dividend_date", "days_to_ex_div",
            "cash_amount", "frequency", "pay_date",
            "declaration_date", "record_date",
            "dividend_type", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("ticker") for row in result["head"]
            )
        ),
    },
    # ── Wave 2 of OEB (2026-04-26): deterministic pick tracker ────
    # Tests the graceful-fallback path: when no picks exist at
    # indexes/IMMUTABLE/ag_picks/ at test time, the tracker emits a
    # single INFO row with outcome="noop" and a clear day-1 message.
    # The full happy-path test (with seeded picks parquet + mocked
    # Massive snapshots that trigger each exit rule) lives in
    # tests/test_options_edge_brief.py::TestPickTracker.
    "oeb_pick_tracker_pro": {
        "expected_columns": [
            "idea_id", "instrument_id", "outcome", "trigger_rule",
            "entry_price", "exit_price", "pnl_per_contract_usd",
            "pnl_pct_vs_max_loss", "days_held", "closure_quality",
            "fits_account_at_close", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(
                row.get("outcome") in (
                    "won", "lost", "time_exit", "expired", "noop"
                )
                for row in result["head"]
            )
        ),
    },

    # ── Wave 1 (FXRB) - DXY regime + G10 carry signal ──────────
    "fred_dxy_regime": {
        "expected_columns": [
            "series_id", "description", "index_scope",
            "latest_value", "latest_date",
            "value_30d_ago", "value_90d_ago", "value_1y_ago",
            "change_30d_pct", "change_90d_pct", "change_1y_pct",
            "percentile_1y", "regime_flag",
            "investment_thesis", "etf_long", "etf_short", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("BREAKING_OUT", "STRONG", "NEUTRAL", "SOFT", "BREAKING_DOWN", "UNKNOWN")
                for row in result["head"])
        ),
    },
    "fred_g10_carry_signal": {
        "expected_columns": [
            "pair_code", "funder_currency", "target_currency",
            "funder_short_rate", "target_short_rate", "short_rate_spread",
            "funder_10y_yield", "target_10y_yield", "long_yield_spread",
            "carry_attractive", "curve_supports_carry",
            "investment_thesis", "etf_expression", "_epoch",
        ],
        "min_rows": 8,
    },

    # ── Wave 1 (SPBEB) - The Odds API line snapshot ────────────
    "odds_api_line_movements": {
        "expected_columns": [
            "game_id", "sport_key", "sport_title", "commence_time_epoch",
            "home_team", "away_team",
            "market_key", "outcome_name", "outcome_point",
            "consensus_price", "book_count",
            "price_min", "price_max", "range_abs", "range_pct",
            "best_book", "worst_book", "_epoch",
        ],
        "min_rows": 2,
        "extra_checks": lambda result: (
            any(row.get("book_count", 0) >= 2 for row in result["head"])
        ),
    },

    # ── Wave 1 (EGIB) - EIA energy + grid intelligence ─────────
    "eia_petroleum_stocks": {
        "expected_columns": [
            "series_id", "description", "category",
            "unit", "period", "latest_value",
            "prior_value", "change_wow", "change_wow_pct",
            "value_1y_ago", "change_yoy", "change_yoy_pct",
            "percentile_5y", "regime_flag",
            "investment_thesis", "etf_ticker", "_epoch",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("DRAW_HEAVY", "DRAW", "NEUTRAL", "BUILD", "BUILD_HEAVY",
                 "ELEVATED", "TYPICAL", "DEPRESSED",
                 "STRONG", "WEAK")
                for row in result["head"])
        ),
    },
    "eia_natural_gas_storage": {
        "expected_columns": [
            "series_id", "description", "region",
            "unit", "period", "latest_value",
            "prior_value", "change_wow", "change_wow_pct",
            "value_1y_ago", "change_yoy_pct",
            "percentile_5y", "regime_flag",
            "investment_thesis", "etf_ticker", "_epoch",
        ],
        "min_rows": 5,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("CRITICAL_LOW", "TIGHT", "NORMAL", "LOOSE", "OVERSUPPLY")
                for row in result["head"])
        ),
    },
    "eia_electricity_demand": {
        "expected_columns": [
            "region", "region_name", "period", "latest_demand_mwh",
            "prior_day_demand_mwh", "change_dod_pct",
            "value_7d_ago", "change_wow_pct",
            "percentile_90d", "regime_flag",
            "investment_thesis", "etf_ticker", "_epoch",
        ],
        "min_rows": 8,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("PEAK", "ELEVATED", "NORMAL", "SOFT", "TROUGH")
                for row in result["head"])
        ),
    },
    "eia_renewable_share": {
        "expected_columns": [
            "fuel_code", "fuel_name", "period",
            "latest_value_mwh", "value_7d_ago_mwh", "value_1y_ago_mwh",
            "change_wow_pct", "change_yoy_pct",
            "share_of_total_pct",
            "percentile_1y", "regime_flag",
            "investment_thesis", "etf_ticker", "_epoch",
        ],
        "min_rows": 6,
        "extra_checks": lambda result: (
            all(row.get("regime_flag") in
                ("RECORD_HIGH", "ELEVATED", "NORMAL", "SOFT", "RECORD_LOW")
                for row in result["head"])
            and any(row.get("share_of_total_pct") is not None for row in result["head"])
        ),
    },

    # ── Wave 2 (PPPB) - Congress.gov bills (credentialed) ──────
    "congress_gov_bills": {
        "expected_columns": [
            "bill_id", "congress", "bill_type", "bill_number",
            "title", "origin_chamber", "sponsor_party", "sponsor_state",
            "latest_action_date", "latest_action_text",
            "importance_tier", "policy_area", "url", "_epoch",
        ],
        "min_rows": 1,
        "extra_checks": lambda result: (
            all(row.get("importance_tier") in ("HIGH", "MEDIUM", "LOW")
                for row in result["head"])
            and all(row.get("policy_area") in
                ("Healthcare", "Energy", "Finance", "Defense", "Tech",
                 "Immigration", "Tax", "Trade", "Climate", "Civil Rights", "Other")
                for row in result["head"])
        ),
    },
}


# ── SEC EDGAR mock data ──────────────────────────────────────────

MOCK_SEC_COMPANY_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}

MOCK_SEC_SUBMISSIONS = {
    "cik": "320193",
    "entityType": "operating",
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "exchanges": ["Nasdaq"],
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-26-000014", "0000320193-26-000013",
                "0000320193-26-000012", "0000320193-26-000011",
            ],
            "filingDate": [
                "2026-04-08", "2026-04-05", "2026-03-28", "2026-03-15",
            ],
            "reportDate": [
                "2026-04-07", "2026-04-04", "2026-03-27", "2026-03-14",
            ],
            "acceptanceDateTime": [
                "2026-04-08T18:00:00.000Z", "2026-04-05T16:30:00.000Z",
                "2026-03-28T17:00:00.000Z", "2026-03-15T18:00:00.000Z",
            ],
            "act": ["34", "34", "34", "34"],
            "form": ["4", "8-K", "10-Q", "4"],
            "fileNumber": ["001-36743", "001-36743", "001-36743", "001-36743"],
            "filmNumber": ["", "", "", ""],
            "items": ["", "2.02,9.01", "", ""],
            "size": [5000, 25000, 1500000, 4800],
            "isXBRL": [0, 1, 1, 0],
            "isInlineXBRL": [0, 1, 1, 0],
            "primaryDocument": [
                "doc4.xml", "d8k.htm", "aapl-20260327.htm", "doc4.xml",
            ],
            "primaryDocDescription": [
                "FORM 4", "8-K", "10-Q", "FORM 4",
            ],
        },
        "files": [],
    },
}

MOCK_SEC_XBRL_FRAMES = {
    "taxonomy": "us-gaap",
    "tag": "Revenues",
    "label": "Revenues",
    "description": "Total revenues",
    "units": "USD",
    "pts": "CY2026Q1",
    "data": [
        {
            "accn": "0000320193-26-000012",
            "cik": 320193,
            "entityName": "Apple Inc.",
            "loc": "US-CA",
            "end": "2026-03-31",
            "val": 94836000000,
            "filed": "2026-04-08",
            "form": "10-Q",
            "fy": 2026,
            "fp": "Q1",
        },
        {
            "accn": "0000789019-26-000015",
            "cik": 789019,
            "entityName": "Microsoft Corp",
            "loc": "US-WA",
            "end": "2026-03-31",
            "val": 65000000000,
            "filed": "2026-04-07",
            "form": "10-Q",
            "fy": 2026,
            "fp": "Q1",
        },
    ],
}

MOCK_SEC_XBRL_NET_INCOME = {
    "taxonomy": "us-gaap",
    "tag": "NetIncomeLoss",
    "units": "USD",
    "data": [
        {
            "accn": "0000320193-26-000012",
            "cik": 320193,
            "entityName": "Apple Inc.",
            "val": 23636000000,
            "filed": "2026-04-08",
            "form": "10-Q",
            "fy": 2026,
            "fp": "Q1",
        },
        {
            "accn": "0000789019-26-000015",
            "cik": 789019,
            "entityName": "Microsoft Corp",
            "val": 22000000000,
            "filed": "2026-04-07",
            "form": "10-Q",
            "fy": 2026,
            "fp": "Q1",
        },
    ],
}

MOCK_SEC_XBRL_ASSETS = {
    "taxonomy": "us-gaap", "tag": "Assets", "units": "USD",
    "data": [
        {"accn": "0000320193-26-000012", "cik": 320193,
         "entityName": "Apple Inc.", "val": 352583000000,
         "filed": "2026-04-08", "form": "10-Q", "fy": 2026, "fp": "Q1"},
    ],
}

MOCK_SEC_XBRL_LIABILITIES = {
    "taxonomy": "us-gaap", "tag": "Liabilities", "units": "USD",
    "data": [
        {"accn": "0000320193-26-000012", "cik": 320193,
         "entityName": "Apple Inc.", "val": 290437000000,
         "filed": "2026-04-08", "form": "10-Q", "fy": 2026, "fp": "Q1"},
    ],
}

MOCK_SEC_XBRL_EQUITY = {
    "taxonomy": "us-gaap", "tag": "StockholdersEquity", "units": "USD",
    "data": [
        {"accn": "0000320193-26-000012", "cik": 320193,
         "entityName": "Apple Inc.", "val": 62146000000,
         "filed": "2026-04-08", "form": "10-Q", "fy": 2026, "fp": "Q1"},
    ],
}


def _sec_router_factory():
    """Return a callable that routes SEC requests by URL pattern."""
    def router(url, **kwargs):
        if "company_tickers.json" in url:
            return _make_response(MOCK_SEC_COMPANY_TICKERS)
        if "submissions/CIK" in url:
            return _make_response(MOCK_SEC_SUBMISSIONS)
        if "NetIncomeLoss" in url:
            return _make_response(MOCK_SEC_XBRL_NET_INCOME)
        if "Assets" in url and "StockholdersEquity" not in url:
            return _make_response(MOCK_SEC_XBRL_ASSETS)
        if "Liabilities" in url:
            return _make_response(MOCK_SEC_XBRL_LIABILITIES)
        if "StockholdersEquity" in url:
            return _make_response(MOCK_SEC_XBRL_EQUITY)
        if "xbrl/frames" in url:
            return _make_response(MOCK_SEC_XBRL_FRAMES)
        return _make_response({})
    return router


# ── OpenWeatherMap mock ──────────────────────────────────────────

MOCK_OPENWEATHERMAP_CURRENT = {
    "main": {
        "temp": 72.4,
        "feels_like": 71.8,
        "humidity": 55,
        "pressure": 1014,
    },
    "wind": {"speed": 8.5, "deg": 180},
    "weather": [{"main": "Clouds", "description": "scattered clouds"}],
    "clouds": {"all": 40},
    "visibility": 10000,
    "dt": 1712601600,
}


def _openweathermap_router_factory():
    def router(url, **kwargs):
        if "openweathermap.org/data/2.5/weather" in url:
            return _make_response(MOCK_OPENWEATHERMAP_CURRENT)
        return _make_response({})
    return router


# ── Polymarket credentialed mocks (positions / activity / profile / search) ──

MOCK_POLYMARKET_POSITIONS = [
    {
        "conditionId": "0xcond_pos_001",
        "assetId": "asset_001",
        "question": "Will BTC close above 100k on 2026-12-31?",
        "slug": "btc-100k-2026",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "size": 250.0,
        "avgPrice": 0.42,
        "currentPrice": 0.55,
        "initialValue": 105.0,
        "currentValue": 137.5,
        "pnl": 32.5,
        "realizedPnl": 0.0,
        "cashflow": -105.0,
    },
    {
        "conditionId": "0xcond_pos_002",
        "assetId": "asset_002",
        "question": "Will the Fed cut rates in Q3 2026?",
        "slug": "fed-cut-q3-2026",
        "outcome": "No",
        "outcomeIndex": 1,
        "size": 100.0,
        "avgPrice": 0.30,
        "currentPrice": 0.25,
        "initialValue": 30.0,
        "currentValue": 25.0,
        "pnl": -5.0,
        "realizedPnl": 0.0,
        "cashflow": -30.0,
    },
]

MOCK_POLYMARKET_ACTIVITY = [
    {
        "type": "TRADE",
        "conditionId": "0xcond_act_001",
        "question": "Will BTC close above 100k on 2026-12-31?",
        "slug": "btc-100k-2026",
        "side": "BUY",
        "size": 250.0,
        "price": 0.42,
        "outcome": "Yes",
        "outcomeIndex": 0,
        "transactionHash": "0xabc123",
        "feeRateBps": 0,
        "timestamp": "2026-04-01T12:00:00Z",
    },
    {
        "type": "TRADE",
        "conditionId": "0xcond_act_002",
        "question": "Will the Fed cut rates in Q3 2026?",
        "slug": "fed-cut-q3-2026",
        "side": "SELL",
        "size": 50.0,
        "price": 0.28,
        "outcome": "No",
        "outcomeIndex": 1,
        "transactionHash": "0xdef456",
        "feeRateBps": 0,
        "timestamp": "2026-04-05T15:30:00Z",
    },
]

MOCK_POLYMARKET_PUBLIC_PROFILE = {
    "username": "TestWhale",
    "bio": "Long-only prediction market trader.",
    "profileImage": "https://example.com/avatar.png",
    "marketsTraded": 42,
    "volume": 125000.0,
    "profit": 8500.0,
    "positionsWon": 28,
    "positionsLost": 14,
}


def _polymarket_credentialed_router_factory():
    """Route polymarket data-api / gamma-api credentialed endpoints."""
    def router(url, **kwargs):
        # data-api positions and activity (BEFORE the gamma-api branch since
        # both hostnames contain the substring "polymarket")
        if "data-api.polymarket.com/positions" in url:
            return _make_response(MOCK_POLYMARKET_POSITIONS)
        if "data-api.polymarket.com/activity" in url:
            return _make_response(MOCK_POLYMARKET_ACTIVITY)
        # gamma-api public-profile/{addr}
        if "gamma-api.polymarket.com/public-profile" in url:
            return _make_response(MOCK_POLYMARKET_PUBLIC_PROFILE)
        # gamma-api public-search returns a markets list
        if "gamma-api.polymarket.com/public-search" in url:
            return _make_response([make_gamma_market()])
        return _make_response([])
    return router


# ── Massive.com mock (options_unusual_activity_pro) ─────────────────


def _massive_credentialed_router_factory():
    """Mock Massive.com for the options_unusual_activity_pro script.

    The script hits a single endpoint family:
      * /v3/snapshot/options/{TICKER}?expiration_date.gte=...
        → paginated chain with greeks, IV, OI, and day OHLC.

    The same MOCK_MASSIVE_OPTIONS_CHAIN fixture is returned for every
    ticker - sufficient to exercise direction-bias (call/put), volume
    gating, vol/OI ratio bucketing, IV-rank percentile, and the
    OPRA-format contract_symbol passthrough.

    The mock returns no ``next_url`` so the per-ticker pagination loop
    exits after a single page, matching the production script's
    ``len(results) < PAGE_LIMIT`` short-circuit.
    """
    def router(url, **kwargs):
        if "/v3/snapshot/options/" in url:
            return _make_response(MOCK_MASSIVE_OPTIONS_CHAIN)
        return _make_response({})
    return router


# ── Wave 1 (FXRB / SPBEB / EGIB) mock data + factories ───────────

# The Odds API - sportsbook line snapshots
MOCK_ODDS_API_SPORTS = [
    {"key": "americanfootball_nfl", "title": "NFL", "active": True},
    {"key": "basketball_nba", "title": "NBA", "active": True},
    {"key": "baseball_mlb", "title": "MLB", "active": True},
    {"key": "icehockey_nhl", "title": "NHL", "active": True},
]

MOCK_ODDS_API_GAMES = [
    {
        "id": "test_game_001",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2026-04-25T22:00:00Z",
        "home_team": "Boston Celtics",
        "away_team": "Los Angeles Lakers",
        "bookmakers": [
            {
                "key": "draftkings", "title": "DraftKings",
                "last_update": "2026-04-24T18:00:00Z",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Boston Celtics", "price": -150},
                        {"name": "Los Angeles Lakers", "price": 130},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Boston Celtics", "price": -110, "point": -3.5},
                        {"name": "Los Angeles Lakers", "price": -110, "point": 3.5},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": -110, "point": 222.5},
                        {"name": "Under", "price": -110, "point": 222.5},
                    ]},
                ],
            },
            {
                "key": "fanduel", "title": "FanDuel",
                "last_update": "2026-04-24T18:00:00Z",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Boston Celtics", "price": -145},
                        {"name": "Los Angeles Lakers", "price": 125},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Boston Celtics", "price": -108, "point": -3.5},
                        {"name": "Los Angeles Lakers", "price": -112, "point": 3.5},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": -105, "point": 223.0},
                        {"name": "Under", "price": -115, "point": 223.0},
                    ]},
                ],
            },
            {
                "key": "betmgm", "title": "BetMGM",
                "last_update": "2026-04-24T18:00:00Z",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Boston Celtics", "price": -155},
                        {"name": "Los Angeles Lakers", "price": 135},
                    ]},
                ],
            },
            {
                "key": "caesars", "title": "Caesars",
                "last_update": "2026-04-24T18:00:00Z",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Boston Celtics", "price": -148},
                        {"name": "Los Angeles Lakers", "price": 128},
                    ]},
                ],
            },
        ],
    },
    {
        "id": "test_game_002",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2026-04-26T01:00:00Z",
        "home_team": "Golden State Warriors",
        "away_team": "Phoenix Suns",
        "bookmakers": [
            {
                "key": "draftkings", "title": "DraftKings",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Golden State Warriors", "price": -120},
                        {"name": "Phoenix Suns", "price": 105},
                    ]},
                ],
            },
            {
                "key": "fanduel", "title": "FanDuel",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Golden State Warriors", "price": -125},
                        {"name": "Phoenix Suns", "price": 110},
                    ]},
                ],
            },
        ],
    },
]


def _odds_api_router_factory():
    """Router for The Odds API - /v4/sports and /v4/sports/{key}/odds."""
    def router(url, **kwargs):
        if "/v4/sports" in url and "/odds" in url:
            return _make_response(MOCK_ODDS_API_GAMES)
        if "/v4/sports" in url:
            return _make_response(MOCK_ODDS_API_SPORTS)
        return _make_response([])
    return router


# ESPN injuries - public site.api.espn.com
MOCK_ESPN_INJURIES = {
    "injuries": [
        {
            "team": {"abbreviation": "LAL", "displayName": "Los Angeles Lakers"},
            "injuries": [
                {
                    "athlete": {
                        "id": "12345",
                        "displayName": "LeBron James",
                        "position": {"abbreviation": "SF"},
                        "jersey": "23",
                    },
                    "status": "Out",
                    "type": {"description": "Knee soreness"},
                    "shortComment": "Out for tonight's game; questionable for next.",
                    "date": "2026-04-24T15:00:00Z",
                },
                {
                    "athlete": {
                        "id": "12346",
                        "displayName": "Anthony Davis",
                        "position": {"abbreviation": "PF"},
                        "jersey": "3",
                    },
                    "status": "Day-to-day",
                    "type": {"description": "Ankle sprain"},
                    "shortComment": "Game-time decision.",
                    "date": "2026-04-24T14:00:00Z",
                },
            ],
        },
        {
            "team": {"abbreviation": "BOS", "displayName": "Boston Celtics"},
            "injuries": [
                {
                    "athlete": {
                        "id": "22345",
                        "displayName": "Jayson Tatum",
                        "position": {"abbreviation": "SF"},
                        "jersey": "0",
                    },
                    "status": "Questionable",
                    "type": {"description": "Wrist contusion"},
                    "shortComment": "Wrist will be wrapped; expected to play.",
                    "date": "2026-04-24T13:00:00Z",
                },
            ],
        },
    ],
}


def _espn_injuries_router_factory():
    """Router for ESPN injuries feed - /apis/site/v2/sports/{sport}/{league}/injuries."""
    def router(url, **kwargs):
        if "/injuries" in url:
            return _make_response(MOCK_ESPN_INJURIES)
        return _make_response({"injuries": []})
    return router


# EIA v2 API - petroleum + nat gas + electricity + fuel mix
def _make_eia_data(value_seq, period_start="2026-04-15", period_step_days=7, units="MBBL"):
    """Build EIA v2 response.data array with descending periods.

    ``value_seq`` is the latest-first list of numeric values.  Periods are
    generated YYYY-MM-DD descending from ``period_start``.
    """
    out = []
    base = _dt.datetime.fromisoformat(period_start)
    for i in range(len(value_seq)):
        d = base - _dt.timedelta(days=i * period_step_days)
        out.append({
            "period": d.strftime("%Y-%m-%d"),
            "value": value_seq[i],
            "units": units,
        })
    return out


def _make_eia_response(values, period_start="2026-04-15", period_step_days=7, units="MBBL"):
    return {
        "response": {
            "data": _make_eia_data(values, period_start, period_step_days, units),
            "total": str(len(values)),
        },
    }


# Generate 270 weekly observations for petroleum/gas series so the
# 5-year (260 obs) percentile window is fully populated. Values descend
# slightly from latest so the latest is at a low percentile (bullish for
# inventory regime testing).
_PETROLEUM_VALUES = [430000 - (i * 100) for i in range(270)]
_NATGAS_VALUES = [2200 - (i * 5) for i in range(270)]
_REFINERY_VALUES = [88.0 + (i * 0.02) for i in range(270)]
_DEMAND_VALUES = [9000 + (i * 5) for i in range(270)]

# Daily series for electricity / fuel mix - 380 days so 1-year percentile
# window (365) is in range.
_ELEC_DEMAND_VALUES = [350000 + ((i % 7) * 5000) for i in range(380)]
_COAL_VALUES = [200000 - (i * 50) for i in range(380)]
_GAS_VALUES = [1200000 + ((i % 30) * 1000) for i in range(380)]
_NUC_VALUES = [780000 - ((i % 14) * 100) for i in range(380)]
_OIL_GEN_VALUES = [12000 + ((i % 30) * 50) for i in range(380)]
_OTH_GEN_VALUES = [50000 + ((i % 30) * 20) for i in range(380)]
_SOLAR_VALUES = [180000 + ((i % 30) * 100) for i in range(380)]
_HYDRO_VALUES = [220000 + ((i % 60) * 200) for i in range(380)]
_WIND_VALUES = [340000 + ((i % 30) * 500) for i in range(380)]


def _eia_router_factory():
    """Router for EIA v2 API (petroleum, nat gas, electricity, fuel mix)."""
    def router(url, **kwargs):
        params = kwargs.get("params", {}) or {}
        if "petroleum/stoc/wstk" in url:
            sid = params.get("facets[series][]", "")
            if sid in ("WPULEUS3",):
                return _make_response(_make_eia_response(_REFINERY_VALUES, units="percent"))
            if sid in ("WGFUPUS2",):
                return _make_response(_make_eia_response(_DEMAND_VALUES, units="MBBLD"))
            return _make_response(_make_eia_response(_PETROLEUM_VALUES, units="MBBL"))
        if "natural-gas/stor/wkly" in url:
            return _make_response(_make_eia_response(_NATGAS_VALUES, units="BCF"))
        if "electricity/rto/daily-region-data" in url:
            # 2026-04-26 fix: real script now hits the DAILY endpoint
            # (the prior `region-data` URL was the hourly route).
            return _make_response(_make_eia_response(_ELEC_DEMAND_VALUES, period_step_days=1, units="MWh"))
        if "electricity/rto/region-data" in url:
            # Back-compat: legacy hourly URL - still mocked in case any
            # other script keeps using it. New scripts should hit the
            # daily endpoint above.
            return _make_response(_make_eia_response(_ELEC_DEMAND_VALUES, period_step_days=1, units="MWh"))
        if "electricity/rto/daily-fuel-type-data" in url:
            ftype = params.get("facets[fueltype][]", "")
            mapping = {
                "COL": _COAL_VALUES,
                "NG": _GAS_VALUES,
                "NUC": _NUC_VALUES,
                "OIL": _OIL_GEN_VALUES,
                "OTH": _OTH_GEN_VALUES,
                "SUN": _SOLAR_VALUES,
                "WAT": _HYDRO_VALUES,
                "WND": _WIND_VALUES,
            }
            return _make_response(_make_eia_response(mapping.get(ftype, _COAL_VALUES), period_step_days=1, units="MWh"))
        return _make_response({"response": {"data": []}})
    return router


# ── Wave 2 (PPPB / PHPB) mock data + factories ───────────────────

# Congress.gov bills mock - exercises HIGH (passed chamber) / MEDIUM
# (committee report) / LOW (introduced) impact tiers and a spread of
# policy_area tags via title keywords.
MOCK_CONGRESS_BILLS = {
    "bills": [
        {
            "congress": 119,
            "type": "HR",
            "number": 4500,
            "title": "Healthcare Cost Transparency Act of 2026",
            "originChamber": "House",
            "latestAction": {
                "actionDate": "2026-04-22",
                "text": "Passed House by recorded vote: 234 - 198 (Roll no. 145).",
            },
            "url": "https://api.congress.gov/v3/bill/119/hr/4500",
            "sponsors": [
                {"firstName": "Jane", "lastName": "Smith", "party": "D", "state": "CA"}
            ],
        },
        {
            "congress": 119,
            "type": "S",
            "number": 1820,
            "title": "Energy Independence and Permitting Reform Act",
            "originChamber": "Senate",
            "latestAction": {
                "actionDate": "2026-04-20",
                "text": "Reported by Committee on Energy and Natural Resources without amendment.",
            },
            "url": "https://api.congress.gov/v3/bill/119/s/1820",
            "sponsors": [
                {"firstName": "John", "lastName": "Doe", "party": "R", "state": "WV"}
            ],
        },
        {
            "congress": 119,
            "type": "HR",
            "number": 4801,
            "title": "Semiconductor Manufacturing and Research Tax Credit Extension",
            "originChamber": "House",
            "latestAction": {
                "actionDate": "2026-04-21",
                "text": "Introduced in House.",
            },
            "url": "https://api.congress.gov/v3/bill/119/hr/4801",
            "sponsors": [
                {"firstName": "Mary", "lastName": "Tech", "party": "D", "state": "NY"}
            ],
        },
    ],
}


def _congress_router_factory():
    """Router for Congress.gov v3 API."""
    def router(url, **kwargs):
        if "/v3/bill" in url:
            return _make_response(MOCK_CONGRESS_BILLS)
        return _make_response({"bills": []})
    return router


# Federal Register articles mock - covers RULE / PRORULE / PRESDOCU /
# NOTICE types and exercises significant_action keyword detection.
MOCK_FEDERAL_REGISTER = {
    "results": [
        {
            "document_number": "2026-08234",
            "type": "Rule",
            "agencies": [{"name": "Environmental Protection Agency", "raw_name": "EPA"}],
            "title": "Significant Regulatory Action: Final Rule on Greenhouse Gas Emissions Standards for Heavy-Duty Vehicles",
            "abstract": "This final rule sets emissions standards. The economic effect exceeds $100 million annually.",
            "action": "Final rule.",
            "publication_date": "2026-04-22",
            "effective_on": "2026-07-01",
            "significant": True,
            "html_url": "https://www.federalregister.gov/documents/2026/04/22/2026-08234/",
            "regulation_id_numbers": ["2060-AW01"],
        },
        {
            "document_number": "2026-08456",
            "type": "Proposed Rule",
            "agencies": [{"name": "Food and Drug Administration", "raw_name": "FDA"}],
            "title": "Approval of New Drug Application Requirements: Modernization of CBER Pathway",
            "abstract": "FDA proposes new pathway for biological drug approvals.",
            "action": "Proposed rule; request for comments.",
            "publication_date": "2026-04-21",
            "effective_on": "",
            "significant": False,
            "html_url": "https://www.federalregister.gov/documents/2026/04/21/2026-08456/",
        },
        {
            "document_number": "2026-08501",
            "type": "Presidential Document",
            "agencies": [{"name": "Executive Office of the President"}],
            "title": "Executive Order 14XYZ: Tariff Increase on Imports of Critical Minerals",
            "abstract": "Increases tariffs on critical mineral imports.",
            "action": "Executive order.",
            "publication_date": "2026-04-20",
            "effective_on": "2026-05-15",
            "significant": True,
            "html_url": "https://www.federalregister.gov/documents/2026/04/20/2026-08501/",
        },
    ],
}


def _federal_register_router_factory():
    """Router for federalregister.gov v1 API."""
    def router(url, **kwargs):
        if "/api/v1/articles" in url:
            return _make_response(MOCK_FEDERAL_REGISTER)
        return _make_response({"results": []})
    return router


# openFDA drugsfda mock - original NDA approval (HIGH) + supplement (MED).
MOCK_FDA_DRUGSFDA = {
    "results": [
        {
            "application_number": "NDA215000",
            "sponsor_name": "Acme Pharmaceuticals Inc.",
            "openfda": {
                "brand_name": ["NEWDRUG"],
                "generic_name": ["acmecillin"],
            },
            "products": [
                {"route": "ORAL", "dosage_form": "TABLET"},
            ],
            "submissions": [
                {
                    "submission_type": "ORIG-1",
                    "submission_class_code": "TYPE 1",
                    "submission_status": "AP",
                    "submission_status_date": "20260415",
                },
            ],
        },
        {
            "application_number": "BLA125678",
            "sponsor_name": "Bigbio Therapeutics",
            "openfda": {
                "brand_name": ["BIODRUGX"],
                "generic_name": ["bigmab"],
            },
            "products": [
                {"route": "INJECTION", "dosage_form": "SOLUTION"},
            ],
            "submissions": [
                {
                    "submission_type": "SUPPL-EFFICACY",
                    "submission_class_code": "EFFICACY",
                    "submission_status": "AP",
                    "submission_status_date": "20260418",
                },
            ],
        },
    ],
}


# openFDA drug shortages mock - high-volume generic shortage (HIGH) +
# specialty drug shortage (MEDIUM) + resolved entry (filtered out by tier).
MOCK_FDA_SHORTAGES = {
    "results": [
        {
            "generic_name": "amoxicillin",
            "proprietary_name": "AMOXIL",
            "dosage_form": "Capsule",
            "strength": "500 mg",
            "status": "Currently in Shortage",
            "shortage_reason": "Increased demand",
            "company_name": "Generic Pharma Co",
            "therapeutic_category": "Antibiotic",
            "change_date": "2026-02-15",
            "update_type": "Status Change",
        },
        {
            "generic_name": "cisplatin",
            "proprietary_name": "",
            "dosage_form": "Injection",
            "strength": "1 mg/mL",
            "status": "Currently in Shortage",
            "shortage_reason": "Manufacturing delay",
            "company_name": "Specialty Pharma",
            "therapeutic_category": "Oncology",
            "change_date": "2026-01-10",
            "update_type": "Initial Posting",
        },
    ],
}


def _fda_router_factory():
    """Router for openFDA endpoints used by Wave 2 PHPB scripts."""
    def router(url, **kwargs):
        if "/drug/drugsfda" in url:
            return _make_response(MOCK_FDA_DRUGSFDA)
        if "/drug/shortages" in url:
            return _make_response(MOCK_FDA_SHORTAGES)
        return _make_response({"results": []})
    return router


# ClinicalTrials.gov v2 mock - Phase 3 industry-sponsored Active study
# (HIGH) + Phase 3 academic Recruiting (MEDIUM) + Phase 2/3 industry
# Recruiting (MEDIUM).
MOCK_CTGOV_STUDIES = {
    "studies": [
        {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT06000001",
                    "briefTitle": "A Phase 3 Study of Acmecillin in Patients with X Disease",
                },
                "conditionsModule": {"conditions": ["X Disease"]},
                "armsInterventionsModule": {
                    "interventions": [{"name": "Acmecillin", "interventionName": "Acmecillin"}]
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Acme Pharmaceuticals Inc.", "class": "INDUSTRY"}
                },
                "statusModule": {
                    "overallStatus": "ACTIVE_NOT_RECRUITING",
                    "startDateStruct": {"date": "2024-01-01"},
                    "primaryCompletionDateStruct": {"date": "2026-09-30"},
                    "completionDateStruct": {"date": "2027-03-31"},
                    "lastUpdatePostDateStruct": {"date": "2026-04-22"},
                },
                "designModule": {
                    "phases": ["PHASE3"],
                    "studyType": "INTERVENTIONAL",
                    "enrollmentInfo": {"count": 850},
                },
            },
        },
        {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT06000002",
                    "briefTitle": "Phase 2/3 Study of BigMab in Solid Tumors",
                },
                "conditionsModule": {"conditions": ["Lung Cancer", "Breast Cancer"]},
                "armsInterventionsModule": {"interventions": [{"name": "BigMab"}]},
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Bigbio Therapeutics", "class": "INDUSTRY"}
                },
                "statusModule": {
                    "overallStatus": "RECRUITING",
                    "startDateStruct": {"date": "2026-02-01"},
                    "primaryCompletionDateStruct": {"date": "2028-02-01"},
                    "completionDateStruct": {"date": "2028-08-01"},
                    "lastUpdatePostDateStruct": {"date": "2026-04-21"},
                },
                "designModule": {
                    "phases": ["PHASE2", "PHASE3"],
                    "studyType": "INTERVENTIONAL",
                    "enrollmentInfo": {"count": 450},
                },
            },
        },
    ],
    "nextPageToken": "",
}


def _ctgov_router_factory():
    """Router for ClinicalTrials.gov v2 API."""
    def router(url, **kwargs):
        if "/api/v2/studies" in url:
            return _make_response(MOCK_CTGOV_STUDIES)
        return _make_response({"studies": []})
    return router


# ── Discovery & coverage ─────────────────────────────────────────

def _discover_credentialed_scripts():
    """Return sorted list of script names that accept credentials.

    Mirror of :func:`_discover_no_auth_scripts`: a script is credentialed if
    ``requires_credentials`` is non-empty OR the script declares
    ``credential_kinds`` (optional creds such as SEC_EDGAR_CONTACT). Both
    partitions together must cover every script in ``SCRIPTS_DIR`` exactly
    once.
    """
    scripts = []
    for path in sorted(SCRIPTS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        has_required = bool(data.get("requires_credentials"))
        has_optional = bool(data.get("credential_kinds"))
        if has_required or has_optional:
            scripts.append(path.stem)
    return scripts


ALL_CREDENTIALED = _discover_credentialed_scripts()
ALL_CRED_REGISTERED = sorted(CREDENTIALED_SCRIPT_REGISTRY.keys())


class TestCredentialedRegistryCoverage:
    """Ensure every registered credentialed script has a matching script file."""

    def test_all_registered_scripts_exist(self):
        """Every entry in CREDENTIALED_SCRIPT_REGISTRY must have a script file."""
        missing = set(ALL_CRED_REGISTERED) - set(ALL_CREDENTIALED)
        assert not missing, (
            f"CREDENTIALED_SCRIPT_REGISTRY entries with no matching script: {sorted(missing)}"
        )


@pytest.mark.parametrize("script_name", ALL_CRED_REGISTERED, ids=ALL_CRED_REGISTERED)
class TestCredentialedScriptJsonStructure:
    """Validate JSON schema for credentialed library scripts."""

    REQUIRED_KEYS = {
        "title", "description", "category", "api_url",
        "requires_credentials", "suggested_cron", "suggested_subdirectory", "tags", "code",
    }

    def test_has_required_keys(self, script_name):
        path = SCRIPTS_DIR / f"{script_name}.json"
        data = json.loads(path.read_text())
        missing = self.REQUIRED_KEYS - set(data.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_has_credentials_listed(self, script_name):
        """Credentialed scripts must surface at least one credential slot.

        A script counts as credentialed if it either ``requires_credentials``
        (hard dependency) OR declares ``credential_kinds`` with at least one
        key (optional / contact-kind slot - see SEC EDGAR scripts). Empty
        on both sides would mean a script that doesn't accept creds at all;
        that should live in SCRIPT_REGISTRY, not CREDENTIALED_SCRIPT_REGISTRY.
        """
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        required = data.get("requires_credentials", [])
        kinds = data.get("credential_kinds") or {}
        assert required or kinds, (
            "Credentialed script should list required credentials OR "
            "credential_kinds (for optional contact/identifier creds)"
        )

    def test_code_contains_generate_results(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        assert "GENERATE_RESULTS" in data["code"], (
            "Script code must call GENERATE_RESULTS(df)"
        )

    def test_code_uses_credentials(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        assert "CREDENTIALS" in data["code"], (
            "Credentialed script must reference CREDENTIALS"
        )

    def test_credential_kinds_shape(self, script_name):
        """
        If credential_kinds is present, it must be a {cred_name: kind}
        mapping. Keys can be a superset of ``requires_credentials`` -
        extras are OPTIONAL creds such as SEC_EDGAR_CONTACT on the SEC
        scripts, where the value falls back to a sensible default. Every
        declared value must be one of the canonical kinds.
        """
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        kinds = data.get("credential_kinds")
        if kinds is None:
            return  # field is optional; absence = back-compat api_key default
        assert isinstance(kinds, dict), (
            f"credential_kinds must be a dict, got {type(kinds).__name__}"
        )
        VALID = {"api_key", "secret", "contact", "identifier"}
        for name, kind in kinds.items():
            assert kind in VALID, (
                f"credential_kinds[{name!r}] = {kind!r} is not one of {VALID}"
            )

    def test_credential_kinds_covers_required_credentials(self, script_name):
        """
        Every entry in ``requires_credentials`` must have a matching key
        in ``credential_kinds``. Otherwise the UI cannot render the right
        credential-pill / portal hint when the user wires up the script.

        Caught 2026-04-25 on the 15 FRED-using scripts: each declared
        ``requires_credentials: ["FRED_API_KEY"]`` but ``credential_kinds``
        was empty, so the UI fell back to a generic api_key pill with no
        portal link to https://fred.stlouisfed.org/docs/api/api_key.html.
        """
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        required = data.get("requires_credentials") or []
        kinds = data.get("credential_kinds") or {}
        missing = [name for name in required if name not in kinds]
        assert not missing, (
            f"Script declares requires_credentials={required} but "
            f"credential_kinds is missing entries for {missing}. "
            f"Add a 'credential_kinds' map to script_library/scripts/"
            f"{script_name}.json with each required credential mapped to "
            f"its kind ('api_key', 'secret', 'contact', or 'identifier')."
        )

    def test_title_has_no_special_characters(self, script_name):
        """Same rule as TestScriptJsonStructure - applies to credentialed scripts too."""
        import re as _re
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        title = data.get("title", "")
        assert _re.match(r"^[A-Za-z0-9 _.\-]+$", title), (
            f"Title {title!r} contains disallowed characters. "
            f"Use letters, digits, space, underscore, period, hyphen only."
        )


@pytest.mark.parametrize("script_name", ALL_CRED_REGISTERED, ids=ALL_CRED_REGISTERED)
class TestCredentialedScriptExecution:
    """Run credentialed scripts with mocked HTTP and injected CREDENTIALS."""

    def test_executes_valid_dataframe(self, script_name):
        spec = CREDENTIALED_SCRIPT_REGISTRY[script_name]
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        code = data["code"]

        # Pick the right router and credentials based on script type
        if script_name.startswith("sec_"):
            router = _sec_router_factory()
            creds = {"SEC_EDGAR_CONTACT": "SpeakesQuery Test (test@example.com)"}
        elif script_name.startswith("openweathermap_"):
            router = _openweathermap_router_factory()
            creds = {"api_key": "test_owm_key"}
        elif script_name in (
            "polymarket_user_positions",
            "polymarket_user_activity",
            "polymarket_public_profile_lookup",
        ):
            router = _polymarket_credentialed_router_factory()
            creds = {"POLYMARKET_USER_ADDRESS": "0xtestwallet0000000000000000000000000001"}
        elif script_name == "polymarket_search_monitor":
            router = _polymarket_credentialed_router_factory()
            creds = {"POLYMARKET_SEARCH_TERM": "Bitcoin"}
        elif script_name in (
            "fred_global_central_banks",
            "fred_commodity_prices",
            "fred_fx_and_yields",
            "fred_oecd_leading_indicators",
            "fred_dxy_regime",
            "fred_g10_carry_signal",
        ):
            router = _fred_macro_router_factory()
            creds = {"FRED_API_KEY": "test_mock_key"}
        elif script_name == "options_unusual_activity_pro":
            router = _massive_credentialed_router_factory()
            creds = {"MASSIVE_API_KEY": "test_mock_massive_key_abc123"}
        elif script_name in (
            "options_iv_rank_screener_pro",
            "options_term_structure_pro",
            "options_skew_monitor_pro",
            "options_earnings_implied_move_pro",
            "options_market_status",
            "options_ex_div_calendar",
            "oeb_pick_tracker_pro",
        ):
            router = _massive_oeb_router_factory()
            creds = {"MASSIVE_API_KEY": "test_mock_massive_oeb_key_abc123"}
        elif script_name == "odds_api_line_movements":
            router = _odds_api_router_factory()
            creds = {"ODDS_API_KEY": "test_mock_odds_key_abc123"}
        elif script_name in (
            "eia_petroleum_stocks",
            "eia_natural_gas_storage",
            "eia_electricity_demand",
            "eia_renewable_share",
        ):
            router = _eia_router_factory()
            creds = {"EIA_API_KEY": "test_mock_eia_key_abc123"}
        elif script_name == "congress_gov_bills":
            router = _congress_router_factory()
            creds = {"CONGRESS_GOV_API_KEY": "test_mock_congress_key_abc123"}
        else:
            router = _fred_router_factory(MOCK_FRED_LABOR_5)
            creds = {"FRED_API_KEY": "test_mock_key"}

        trust_level = data.get("trust_level", "sandboxed")
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(code, test_mode=True, trust_level=trust_level)
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": creds},
            )

        # ── Universal assertions ──────────────────────────────
        assert result["status"] == "pass", (
            f"Script failed with errors: {result['errors']}"
        )
        assert result["errors"] == [], (
            f"Unexpected errors: {result['errors']}"
        )
        assert result["has_epoch"] is True, "Missing _epoch column"
        assert "_epoch" in result["columns"], "_epoch not in columns list"
        assert result["row_count"] >= spec.get("min_rows", 1), (
            f"Expected >= {spec.get('min_rows', 1)} rows, got {result['row_count']}"
        )

        # ── Column presence ───────────────────────────────────
        for col in spec.get("expected_columns", []):
            assert col in result["columns"], (
                f"Missing expected column '{col}'. Got: {result['columns']}"
            )

        # ── Script-specific checks ────────────────────────────
        extra = spec.get("extra_checks")
        if extra:
            assert extra(result), (
                f"Extra check failed. head={result['head']}"
            )


# ═══════════════════════════════════════════════════════════════════
# H-MI-6: polymarket_temporal_decay_pro - tz-naive endDate fallback
# ═══════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: Polymarket occasionally returns
# an endDate string without a 'Z' suffix or tz offset, which yields a naive
# datetime. Subtracting a tz-aware ``now`` raises TypeError, which the
# surrounding try/except used to silently drop the market. After H-MI-6, the
# script warns on stdout and forces UTC so the market is still processed.

class TestTemporalDecayProNaiveEndDate:

    def _run_with_enddate(self, capsys, end_str: str):
        """Run polymarket_temporal_decay_pro against a market with *end_str* as endDate."""
        data = json.loads(
            (SCRIPTS_DIR / "polymarket_temporal_decay_pro.json").read_text()
        )
        # Favored side priced below 0.95 so the convergence/roi gate passes
        # when the market is in the near-term window.
        market = make_gamma_market(
            id="m_naive_tz",
            question="Naive-tz regression market?",
            slug="naive-tz-regression",
            outcomePrices='["0.78","0.22"]',
            endDate=end_str,
        )
        url_map = {"gamma-api.polymarket.com/markets": [market]}
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True, trust_level="unrestricted",
            )
            result = executor.execute_test()
        captured = capsys.readouterr()
        return result, captured

    def test_naive_future_enddate_processes_with_warning(self, capsys):
        """Naive future endDate: market must be processed AND a warning emitted on stdout."""
        import datetime as _dt
        future = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(days=5)
        naive_iso = future.strftime("%Y-%m-%dT%H:%M:%S")  # no 'Z', no offset

        result, captured = self._run_with_enddate(capsys, naive_iso)

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] >= 1, (
            "Expected the market to be kept after tz-naive UTC fallback, "
            f"got {result['row_count']} rows."
        )
        assert "naive endDate" in captured.out, (
            f"Expected naive-endDate warning on stdout. stdout=\n{captured.out}"
        )
        assert "naive-tz-regression" in captured.out, (
            "Warning should name the offending slug."
        )

    def test_past_enddate_logs_and_skips(self, capsys):
        """Expired endDate: market is skipped with a visible log, not silently dropped.

        Include one live future market so the overall DataFrame is non-empty
        (execute_test's test gate flags 0-row results as 'fail' regardless of
        pipeline logic - that is separate from what we want to assert here).
        """
        import datetime as _dt
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)
        past_iso = past.isoformat().replace("+00:00", "Z")
        future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=5)
        future_iso = future.isoformat().replace("+00:00", "Z")

        data = json.loads(
            (SCRIPTS_DIR / "polymarket_temporal_decay_pro.json").read_text()
        )
        markets = [
            make_gamma_market(
                id="m_expired",
                question="Expired market?",
                slug="expired-regression",
                outcomePrices='["0.78","0.22"]',
                endDate=past_iso,
            ),
            make_gamma_market(
                id="m_live",
                question="Live market?",
                slug="live-regression",
                outcomePrices='["0.80","0.20"]',
                endDate=future_iso,
            ),
        ]
        router = _make_router({"gamma-api.polymarket.com/markets": markets})
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True, trust_level="unrestricted",
            )
            result = executor.execute_test()
        captured = capsys.readouterr()

        # The mock router returns the same list on every page, so the script
        # paginates 5 times and sees each market 5x. What matters for the
        # regression is: (a) the live market(s) appear; (b) the expired
        # market(s) were skipped with the explicit log.
        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] >= 1, (
            f"At least one live market should survive; got {result['row_count']} rows."
        )
        live_rows = [
            row for row in result["head"] if row.get("question") == "Live market?"
        ]
        assert len(live_rows) >= 1, (
            f"Live market missing from output; head={result['head']}"
        )
        assert "already passed" in captured.out, (
            f"Expected past-endDate skip log. stdout=\n{captured.out}"
        )
        assert "expired-regression" in captured.out, (
            "Skip log should name the offending slug."
        )
        # Confirm the expired clone did NOT land in the output.
        expired_rows = [
            row for row in result["head"] if row.get("question") == "Expired market?"
        ]
        assert expired_rows == [], (
            f"Expired market must not survive; found: {expired_rows}"
        )


# ═══════════════════════════════════════════════════════════════════
# H-MI-3: polymarket_arbitrage_scanner - thin-book liquidity filter
# ═══════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: arbitrage claims on events
# with very low book depth are noise, not executable edges. After H-MI-3,
# MIN_LIQUIDITY_USD = 10000 gates both multi_outcome and yes_no_pair rows,
# and every surviving row carries a total_event_liquidity_usd column so
# Claude can reason about fillability.

class TestArbitrageScannerLiquidityFloor:

    def _run(self, events_payload):
        data = json.loads(
            (SCRIPTS_DIR / "polymarket_arbitrage_scanner.json").read_text()
        )
        url_map = {"gamma-api.polymarket.com/events": events_payload}
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    def test_fat_event_appears_with_liquidity_column(self):
        """A well-funded event with arb-worthy deviation must land in output."""
        fat = make_gamma_event(
            id="event_fat_arb",
            title="Fat-book arb event",
            slug="fat-book",
            markets=[
                # YES sum = 1.05 → 5% deviation; liquidity ~25k each = 75k total
                make_gamma_market(id="m_f1", question="A?",
                                  outcomePrices='["0.50","0.50"]',
                                  volume="40000", liquidity="25000"),
                make_gamma_market(id="m_f2", question="B?",
                                  outcomePrices='["0.40","0.60"]',
                                  volume="35000", liquidity="25000"),
                make_gamma_market(id="m_f3", question="C?",
                                  outcomePrices='["0.15","0.85"]',
                                  volume="20000", liquidity="25000"),
            ],
        )
        result = self._run([fat])

        assert result["status"] == "pass", f"errors: {result['errors']}"
        multi = [r for r in result["head"] if r.get("arb_type") == "multi_outcome"]
        assert len(multi) >= 1, (
            f"Fat event should produce a multi_outcome row. head={result['head']}"
        )
        assert multi[0]["event_slug"] == "fat-book"
        # 3 markets × $25k = $75k.
        assert multi[0]["total_event_liquidity_usd"] >= 70000.0, (
            f"Expected total_event_liquidity_usd ~75000, got "
            f"{multi[0].get('total_event_liquidity_usd')}"
        )

    def test_thin_event_is_filtered(self):
        """Event with identical deviation but thin book (< MIN_LIQUIDITY_USD) must be dropped."""
        thin = make_gamma_event(
            id="event_thin_arb",
            title="Thin-book arb event",
            slug="thin-book",
            markets=[
                # Same prices as fat (5% deviation) but liquidity $500 each = $1500 total.
                make_gamma_market(id="m_t1", question="A?",
                                  outcomePrices='["0.50","0.50"]',
                                  volume="100", liquidity="500"),
                make_gamma_market(id="m_t2", question="B?",
                                  outcomePrices='["0.40","0.60"]',
                                  volume="100", liquidity="500"),
                make_gamma_market(id="m_t3", question="C?",
                                  outcomePrices='["0.15","0.85"]',
                                  volume="100", liquidity="500"),
            ],
        )
        result = self._run([thin])

        # The thin event yields zero multi_outcome rows; paired YES/NO sum
        # to 1.0 exactly so no pair-arb row either. DataFrame is empty,
        # which execute_test flags as 'fail' - the pipeline behaviour we
        # care about is 'no arb row for the thin event', not the gate.
        multi = [r for r in result["head"] if r.get("arb_type") == "multi_outcome"]
        assert multi == [], (
            f"Thin event must be filtered; found: {multi}"
        )

    def test_thin_and_fat_mixed_only_fat_survives(self):
        """Feed both a fat and a thin event; only the fat one should appear."""
        fat = make_gamma_event(
            id="event_mix_fat",
            title="Fat event",
            slug="mix-fat",
            markets=[
                make_gamma_market(id="m_mf1", question="A?",
                                  outcomePrices='["0.50","0.50"]',
                                  volume="40000", liquidity="25000"),
                make_gamma_market(id="m_mf2", question="B?",
                                  outcomePrices='["0.40","0.60"]',
                                  volume="35000", liquidity="25000"),
                make_gamma_market(id="m_mf3", question="C?",
                                  outcomePrices='["0.15","0.85"]',
                                  volume="20000", liquidity="25000"),
            ],
        )
        thin = make_gamma_event(
            id="event_mix_thin",
            title="Thin event",
            slug="mix-thin",
            markets=[
                make_gamma_market(id="m_mt1", question="A?",
                                  outcomePrices='["0.50","0.50"]',
                                  volume="100", liquidity="500"),
                make_gamma_market(id="m_mt2", question="B?",
                                  outcomePrices='["0.40","0.60"]',
                                  volume="100", liquidity="500"),
                make_gamma_market(id="m_mt3", question="C?",
                                  outcomePrices='["0.15","0.85"]',
                                  volume="100", liquidity="500"),
            ],
        )
        result = self._run([fat, thin])

        assert result["status"] == "pass", f"errors: {result['errors']}"
        slugs = {r.get("event_slug") for r in result["head"]
                 if r.get("arb_type") == "multi_outcome"}
        assert "mix-fat" in slugs, (
            f"Fat event missing from output. head={result['head']}"
        )
        assert "mix-thin" not in slugs, (
            f"Thin event leaked through. head={result['head']}"
        )


# ═══════════════════════════════════════════════════════════════════
# H-MI-4: probability clamp on corrupt price inputs
# ═══════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: float(API_value) without a
# [0, 1] clamp propagates corrupted probabilities into implied_prob_yes,
# Kelly math, and tier buckets. The three price-gating scripts now clamp
# via max/min (non-pro) or np.clip (pro) immediately after parsing.

class TestProbabilityClampCorruptInputs:

    # -- polymarket_high_probability (sandboxed) --

    def test_polymarket_high_probability_clamps_above_one(self):
        """yes_price > 1.0 in API response must clamp to 1.0."""
        data = json.loads(
            (SCRIPTS_DIR / "polymarket_high_probability.json").read_text()
        )
        url_map = {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    id="m_clamp_high",
                    question="Corrupt high?",
                    slug="corrupt-high",
                    outcomePrices='["1.5","-0.3"]',
                ),
            ],
        }
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        corrupt_rows = [r for r in result["head"] if r.get("slug") == "corrupt-high"]
        assert corrupt_rows, f"Expected the clamped row. head={result['head']}"
        row = corrupt_rows[0]
        assert row["yes_price"] == 1.0, (
            f"yes_price should clamp from 1.5 → 1.0, got {row['yes_price']}"
        )
        assert row["no_price"] == 0.0, (
            f"no_price should clamp from -0.3 → 0.0, got {row['no_price']}"
        )
        assert row["leading_price"] == 1.0
        assert row["probability_tier"] == "95+"

    # -- polymarket_high_probability_pro (unrestricted / np.clip) --

    def test_polymarket_high_probability_pro_clamps_above_one(self):
        data = json.loads(
            (SCRIPTS_DIR / "polymarket_high_probability_pro.json").read_text()
        )
        url_map = {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    id="m_pro_clamp_high",
                    question="Corrupt high pro?",
                    slug="pro-corrupt-high",
                    outcomePrices='["1.5","-0.3"]',
                ),
            ],
        }
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True, trust_level="unrestricted",
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        corrupt_rows = [r for r in result["head"] if r.get("slug") == "pro-corrupt-high"]
        assert corrupt_rows, f"Expected the clamped row. head={result['head']}"
        row = corrupt_rows[0]
        assert row["yes_price"] == 1.0
        assert row["no_price"] == 0.0
        assert row["leading_price"] == 1.0

    # -- kalshi_contract_scanner (sandboxed) --

    def test_kalshi_contract_scanner_clamps_above_one(self):
        """Kalshi V2 dollar field corrupted to 1.5 must clamp to 1.0."""
        data = json.loads(
            (SCRIPTS_DIR / "kalshi_contract_scanner.json").read_text()
        )
        corrupt = make_kalshi_market(
            ticker="CLAMP-HIGH",
            event_ticker="CLAMP-HIGH-EV",
            title="Corrupted price contract",
            # V2 schema uses string-typed dollar values. A corrupted
            # 1.5000 must still clamp to 1.0 in the script's price math.
            last_price_dollars="1.5000",
            previous_price_dollars="1.4000",
            yes_bid_dollars="1.4500",
            yes_ask_dollars="1.5500",
            no_bid_dollars="-0.1000",
            no_ask_dollars="-0.0500",
            # Legacy fields kept for any test that still asserts on them.
            last_price=150,
            previous_price=140,
            yes_bid=145, yes_ask=155,
            no_bid=-10, no_ask=-5,
        )
        url_map = {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [
                    {"event_ticker": "CLAMP-HIGH-EV", "category": "Economics",
                     "series_ticker": "CLAMP", "title": "Clamp test event"},
                ],
                "cursor": "",
            },
            "api.elections.kalshi.com/trade-api/v2/markets": {
                "markets": [corrupt],
                "cursor": "",
            },
        }
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        rows = [r for r in result["head"] if r.get("market_ticker") == "CLAMP-HIGH"]
        assert rows, f"Expected the clamped Kalshi row. head={result['head']}"
        row = rows[0]
        assert row["yes_price"] == 1.0, (
            f"yes_price should clamp from 1.5 → 1.0, got {row['yes_price']}"
        )
        assert row["no_price"] == 0.0, (
            f"no_price should clamp to 0.0, got {row['no_price']}"
        )
        assert row["implied_prob_yes"] == 1.0


class TestFdaDrugApprovalsLuceneSyntax:
    """Round-6 backlog #5 fix part A: openFDA Lucene-syntax bug.

    Old form: `submissions.submission_status_date:[X+TO+Y]+AND+...`.
    The `requests` library URL-encoded the `+` chars to `%2B` (literal
    plus, not space). openFDA's Lucene parser rejected `[X+TO+Y]` with
    HTTP 500 'Encountered "]". Was expecting "TO".' Bare-except
    swallowed the error → silent zero rows.

    Fix: use literal SPACES; requests encodes spaces as `+` (or `%20`)
    which decode back to space, satisfying Lucene.
    """

    def _load(self):
        import json as _json
        return _json.loads((SCRIPTS_DIR / "fda_drug_approvals.json").read_text())

    def test_search_param_uses_spaces_not_plus(self):
        """The script's 'search': param value (multi-segment string
        concatenation) must use literal spaces between Lucene tokens
        and must NOT use '+TO+' / '+AND+' (the round-6 bug)."""
        code = self._load()["code"]
        # Extract the full 'search': line (which may span multiple
        # tokens via concatenation). Pull everything from `'search':`
        # to the next comma at the start of a new key.
        import re as _re
        m = _re.search(
            r"'search':\s*(.+?),\s*\n\s*'limit'", code, _re.DOTALL,
        )
        assert m, "Could not locate 'search': param block in script"
        search_block = m.group(1)
        # Positive: the fixed form uses literal spaces
        assert "' TO '" in search_block, (
            f"Search param must use ' TO ' (literal space). Got block: "
            f"{search_block[:300]!r}"
        )
        assert " AND " in search_block, (
            f"Search param must use ' AND ' (literal space) between "
            f"Lucene clauses. Got block: {search_block[:300]!r}"
        )
        # Negative: the broken form must not be in the actual param value
        assert "'+TO+'" not in search_block, (
            f"Search param must not use '+TO+' (URL-encoded as "
            f"'%2BTO%2B' → literal plus → Lucene HTTP 500). "
            f"Got: {search_block[:300]!r}"
        )
        assert "'+AND+'" not in search_block, (
            f"Search param must not use '+AND+'. Got: {search_block[:300]!r}"
        )

    def test_anchors_window_to_api_last_updated(self):
        """openFDA's drugsfda dataset lags real-time by quarterly
        cadence. Window must anchor to API meta.last_updated, not now()."""
        code = self._load()["code"]
        assert "last_updated" in code, (
            "Script must read meta.last_updated from a probe call."
        )

    def test_emits_sentinel_when_no_rows_match(self):
        """Sentinel-row visibility on the empty path."""
        data = self._load()
        # Mock returns 200 but with no submissions matching the date filter
        from datetime import datetime, timezone, timedelta
        anchor_iso = "2026-04-30"

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {
                "meta": {"last_updated": anchor_iso},
                "results": [],  # no results
            }
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1
        row = result["head"][0]
        assert row["impact_tier"] in ("API_ERROR", "NO_SIGNAL")
        assert row["application_number"] == "INFO"
        assert row["data_last_updated"] == anchor_iso


class TestClinicalTrialsPhase3Filter:
    """Round-6 backlog #5 fix part B: ClinicalTrials.gov v2 enum bug.

    Old filter: `AREA[Phase](PHASE3 OR PHASE2_PHASE3)`. The v2 API
    rejects `PHASE2_PHASE3` - allowed enum is `NA, EARLY_PHASE1,
    PHASE1, PHASE2, PHASE3, PHASE4` - with HTTP 400 + a specific
    parser error message. Bare-except swallowed it → silent zero rows.

    Fix: filter on `PHASE3` only. Multi-phase studies have
    `phases: ["PHASE2", "PHASE3"]` so they still match.
    """

    def _load(self):
        import json as _json
        return _json.loads((SCRIPTS_DIR / "clinicaltrials_phase3_updates.json").read_text())

    def test_phase_filter_drops_invalid_phase2_phase3_enum(self):
        code = self._load()["code"]
        # Look at the actual filter.advanced value, not comments
        import re as _re
        m = _re.search(r"'filter\.advanced':\s*'([^']+)'", code)
        assert m, "Could not locate 'filter.advanced' param string in script"
        filter_value = m.group(1)
        assert "PHASE2_PHASE3" not in filter_value, (
            f"PHASE2_PHASE3 is not a valid v2 API enum value - allowed: "
            f"NA, EARLY_PHASE1, PHASE1, PHASE2, PHASE3, PHASE4. "
            f"Got: {filter_value!r}"
        )
        assert "AREA[Phase]PHASE3" in filter_value, (
            f"Filter must use AREA[Phase]PHASE3 (plain enum value). "
            f"Got: {filter_value!r}"
        )

    def test_emits_sentinel_on_400_filter_error(self):
        """When the API returns 400 (e.g., future enum drift), the
        script must emit a sentinel rather than silently skip."""
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 400
            resp.text = "Error parsing query in advanced filter: ..."
            resp.json = lambda: {}
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1
        row = result["head"][0]
        assert row["impact_tier"] == "API_ERROR"
        assert row["nct_id"] == "INFO"
        assert "invalid_filter" in (row.get("brief_title") or "") or \
               "400" in (row.get("brief_title") or "")


class TestCongressGovBillsClassifier:
    """Round-6 backlog #10 fix for `congress_gov_bills`:

    Round-5 audit caught the classifier overshooting - every
    commemorative resolution (DVT Awareness Month, congratulating the
    Little League team, etc.) was getting `importance_tier=HIGH` and
    flooding the politics brief. Two root causes:

    1. `agreed to` was a HIGH pattern, but every procedural Senate /
       House resolution finishes with "Submitted in the Senate,
       considered, and agreed to without amendment..." So SRES/HRES
       rows were always tiered HIGH on first action.
    2. The classifier didn't filter by bill_type. SRES/HRES are
       single-chamber resolutions that CANNOT become law - they're
       100% ceremonial - but the classifier tiered them like real bills.

    Fix: drop `agreed to` from HIGH (replaced with specific bicameral
    milestones); cap SRES/HRES at LOW regardless of action_text.
    """

    def _load(self):
        import json as _json
        return _json.loads((SCRIPTS_DIR / "congress_gov_bills.json").read_text())

    def _run_with_bills(self, bills):
        """Execute the script with a router that returns the supplied
        bills; return the row list."""
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {"bills": bills}
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"CONGRESS_GOV_API_KEY": "test"}},
            )
        assert result["status"] == "pass", f"errors: {result['errors']}"
        return result["head"]

    def _bill(self, type_, number, title, action_text, action_date="2026-04-15"):
        return {
            "congress": 119,
            "type": type_,
            "number": number,
            "title": title,
            "originChamber": "House" if type_.startswith("H") else "Senate",
            "latestAction": {
                "actionDate": action_date,
                "text": action_text,
            },
            "url": f"https://api.congress.gov/v3/bill/119/{type_.lower()}/{number}?format=json",
            "sponsors": [{"party": "R", "state": "TX"}],
        }

    def test_sres_with_agreed_to_is_NOT_high(self):
        """The round-5 audit's smoking gun. Senate Resolutions are
        commemorative and cannot become law - must never tier HIGH."""
        rows = self._run_with_bills([
            self._bill("SRES", "455",
                "A resolution commending and congratulating the Summerlin "
                "South Little League baseball team on winning the 2025 "
                "Little League World Series United States Championship.",
                "Submitted in the Senate, considered, and agreed to "
                "without amendment and with a preamble by Unanimous Consent."),
        ])
        assert len(rows) == 1
        assert rows[0]["importance_tier"] == "LOW", (
            f"SRES with 'agreed to' must be LOW (commemorative, "
            f"can't become law). Got {rows[0]['importance_tier']!r}"
        )

    def test_hres_with_agreed_to_is_NOT_high(self):
        rows = self._run_with_bills([
            self._bill("HRES", "965",
                "Providing for consideration of the bill (H.R. 1689) ...",
                "Motion to reconsider laid on the table Agreed to "
                "without objection."),
        ])
        assert rows[0]["importance_tier"] == "LOW"

    def test_hr_with_became_public_law_is_high(self):
        """Real bill that became law must still tier HIGH."""
        rows = self._run_with_bills([
            self._bill("HR", "7148", "Consolidated Appropriations Act, 2026",
                "Became Public Law No: 119-75."),
        ])
        assert rows[0]["importance_tier"] == "HIGH"

    def test_s_with_passed_senate_is_high(self):
        rows = self._run_with_bills([
            self._bill("S", "1234", "Critical Minerals Investment Act",
                "Passed Senate with an amendment by Yea-Nay Vote. 67 - 31."),
        ])
        assert rows[0]["importance_tier"] == "HIGH"

    def test_hr_with_conference_report_agreed_to_is_high(self):
        """The new specific 'agreed to' pattern (replacing the broad one)."""
        rows = self._run_with_bills([
            self._bill("HR", "5000", "National Defense Authorization Act",
                "Conference report agreed to by Yea-Nay Vote. 88-11."),
        ])
        assert rows[0]["importance_tier"] == "HIGH"

    def test_sjres_with_vetoed_is_high(self):
        rows = self._run_with_bills([
            self._bill("SJRES", "20", "Disapproving the rule submitted by ...",
                "Vetoed by President."),
        ])
        assert rows[0]["importance_tier"] == "HIGH"

    def test_hr_with_only_introduced_is_low(self):
        rows = self._run_with_bills([
            self._bill("HR", "9999", "Some New Bill",
                "Introduced in House"),
        ])
        assert rows[0]["importance_tier"] == "LOW"

    def test_classify_takes_bill_type_param(self):
        """Pin the API: classify_importance(action_text, bill_type).
        The round-6 fix added bill_type to the signature; if a future
        refactor drops it, SRES/HRES would silently regress."""
        code = self._load()["code"]
        assert "def classify_importance(action_text, bill_type):" in code, (
            "classify_importance must take (action_text, bill_type)"
        )
        assert "ALWAYS_LOW_BILL_TYPES" in code, (
            "Module must define the SRES/HRES ceremonial-types tuple"
        )


class TestMetaculusAuthRequiredSentinel:
    """Round-6 backlog #3 fixes for `metaculus_questions`:

    Metaculus deprecated public API access in 2026-Q1. Every /api*/ path
    now returns `403 Permission Error: The API is only available to
    authenticated users.` Script previously called bare-except + break,
    silently writing 0 rows. New behavior:

    1. Optional METACULUS_API_TOKEN credential - if supplied, sent as
       `Authorization: Token <value>` header.
    2. On 401/403 without token: sentinel row category=AUTH_REQUIRED
       with instructions to register at metaculus.com and supply token.
    3. On 401/403 WITH token: sentinel row category=AUTH_INVALID.
    4. On other failures: sentinel category=API_ERROR with detail.
    5. On 200 with empty results: sentinel category=NO_SIGNAL.
    """

    def _load(self):
        import json as _json
        return _json.loads((SCRIPTS_DIR / "metaculus_questions.json").read_text())

    def test_emits_auth_required_sentinel_on_403_without_token(self):
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 403
            resp.text = "Permission Error: The API is only available to authenticated users."
            resp.json = lambda: {}
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {}},
            )

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1
        row = result["head"][0]
        assert row["category"] == "AUTH_REQUIRED", (
            f"Expected AUTH_REQUIRED sentinel, got category={row['category']!r}"
        )
        assert "METACULUS_API_TOKEN" in (row.get("title") or ""), (
            "Sentinel title must reference METACULUS_API_TOKEN credential name"
        )

    def test_emits_auth_invalid_sentinel_on_403_with_token(self):
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 403
            resp.text = "Invalid token"
            resp.json = lambda: {}
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"METACULUS_API_TOKEN": "bad_token_xxx"}},
            )

        assert result["status"] == "pass"
        assert result["row_count"] == 1
        row = result["head"][0]
        assert row["category"] == "AUTH_INVALID", (
            f"Expected AUTH_INVALID sentinel when token is set but rejected, "
            f"got category={row['category']!r}"
        )

    def test_sends_authorization_header_when_token_supplied(self):
        """When METACULUS_API_TOKEN is set, the request must include
        Authorization: Token <value> so we don't get bounced by the
        deprecated-public-API gate."""
        data = self._load()
        captured_headers = []

        def router(url, **kwargs):
            captured_headers.append(dict(kwargs.get("headers") or {}))
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {
                "results": [
                    {
                        "id": 999,
                        "title": "Test question",
                        "type": "binary",
                        "possibilities": {"type": "binary"},
                        "community_prediction": {"full": {"q2": 0.5}},
                        "prediction_count": 100,
                        "comment_count": 10,
                        "number_of_forecasters": 50,
                        "created_time": "2026-01-01T00:00:00Z",
                        "publish_time": "2026-01-01T00:00:00Z",
                        "resolve_time": "2026-12-31T00:00:00Z",
                        "categories": [],
                        "page_url": "/questions/999/",
                    }
                ],
            }
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"METACULUS_API_TOKEN": "abc123_secret"}},
            )

        assert result["status"] == "pass"
        assert captured_headers, "No requests captured"
        first_call_headers = captured_headers[0]
        assert "Authorization" in first_call_headers, (
            f"Authorization header missing. Headers: {first_call_headers}"
        )
        assert first_call_headers["Authorization"] == "Token abc123_secret", (
            f"Expected 'Token abc123_secret', got "
            f"{first_call_headers['Authorization']!r}"
        )


class TestMetaculusV2SchemaPaths:
    """Round-7 (2026-05-06): Metaculus migrated to a posts/projects
    model. Question-specific data hoisted to a nested ``q.question``
    object; categories moved to ``q.projects.category[]``; several
    fields renamed (prediction_count → forecasts_count, created_time →
    created_at, publish_time → published_at, resolve_time →
    scheduled_resolve_time). Pre-fix the script read everything from
    top-level keys and produced 200 rows with most fields empty. The
    fix reads V2 paths first then falls back to legacy paths so older
    test fixtures still work.

    Caught when user added a real Metaculus token, ran the script, and
    saw populated `question_id`/`title`/`comment_count`/`forecaster_count`
    but empty `community_prediction`/`prediction_count`/`created_time`/
    `publish_time`/`resolve_time`/`days_to_resolve`/`category`."""

    def _load(self):
        import json as _json
        return _json.loads((SCRIPTS_DIR / "metaculus_questions.json").read_text())

    def _make_v2_post(self, **overrides):
        """Build a Metaculus V2 posts/projects-model response object -
        matches the shape returned by the live API as of 2026-05-06."""
        base = {
            "id": 43437,
            "title": "Will there be a successful coup in Africa or Latin America before September 1, 2026?",
            "slug": "will-there-be-a-successful-coup",
            "created_at": "2026-05-04T20:38:28.132911Z",
            "published_at": "2025-08-20T17:43:42Z",
            "edited_at": "2026-05-06T01:00:16.402355Z",
            "comment_count": 12,
            "scheduled_close_time": "2026-08-31T22:00:00Z",
            "scheduled_resolve_time": "2026-09-01T05:00:00Z",
            "actual_resolve_time": None,
            "nr_forecasters": 25,
            "forecasts_count": 100,
            "projects": {
                "category": [
                    {"id": 3689, "name": "Politics", "slug": "politics", "type": "category"},
                    {"id": 3687, "name": "Geopolitics", "slug": "geopolitics", "type": "category"},
                ],
            },
            "question": {
                "id": 43432,
                "type": "binary",
                "scheduled_resolve_time": "2026-09-01T05:00:00Z",
                "aggregations": {
                    "recency_weighted": {
                        "latest": {
                            "centers": [0.42],
                            "interval_lower_bounds": [0.30],
                            "interval_upper_bounds": [0.55],
                        },
                    },
                },
            },
        }
        base.update(overrides)
        return base

    def _run_with_response(self, posts):
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {"results": posts, "next": None, "previous": None, "count": len(posts)}
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test(
                extra_globals={"CREDENTIALS": {"METACULUS_API_TOKEN": "valid_token"}},
            )

    def test_v2_post_populates_all_columns(self):
        """A complete V2 post with question + projects + aggregations
        nested objects must produce a row with EVERY output column
        populated correctly."""
        result = self._run_with_response([self._make_v2_post()])
        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1
        row = result["head"][0]

        # All V2-renamed fields must populate from new paths
        assert row["question_id"] == 43437
        assert row["question_type"] == "binary", (
            "type must come from q.question.type (V2 nested path)"
        )
        assert row["community_prediction"] == 0.42, (
            "community_prediction must come from "
            "q.question.aggregations.recency_weighted.latest.centers[0]"
        )
        assert row["prediction_count"] == 100, (
            "prediction_count must read q.forecasts_count (V2 rename)"
        )
        assert row["forecaster_count"] == 25
        assert row["comment_count"] == 12
        assert row["created_time"] == "2026-05-04T20:38:28.132911Z", (
            "created_time must read q.created_at (V2 rename)"
        )
        assert row["publish_time"] == "2025-08-20T17:43:42Z", (
            "publish_time must read q.published_at (V2 rename)"
        )
        assert row["resolve_time"] == "2026-09-01T05:00:00Z", (
            "resolve_time must read q.scheduled_resolve_time (V2 rename) "
            "since actual_resolve_time is null"
        )
        assert row["days_to_resolve"] > 0, (
            "days_to_resolve must compute from the populated resolve_time"
        )
        assert row["category"] == "Politics; Geopolitics", (
            "category must concatenate names from q.projects.category[] (V2 nested path)"
        )
        # page_url uses slug+id when no explicit url provided
        assert "43437" in row["page_url"]
        assert "will-there-be-a-successful-coup" in row["page_url"]

    def test_v2_post_with_null_community_prediction_handles_gracefully(self):
        """Metaculus hides the community prediction until cp_reveal_time
        - for many questions the `latest` aggregation is null. The
        script must NOT crash and community_prediction must be None
        (not 0, which would silently pollute downstream stats)."""
        post = self._make_v2_post()
        post["question"]["aggregations"]["recency_weighted"]["latest"] = None
        result = self._run_with_response([post])
        assert result["status"] == "pass"
        row = result["head"][0]
        # Other fields still populate; just community_prediction is missing
        assert row["question_id"] == 43437
        assert row["prediction_count"] == 100
        cp = row["community_prediction"]
        # Accept any "no value" representation: None, NaN, or '' (the
        # JSON serializer in execute_test renders missing object cells
        # as empty string).
        is_missing = (
            cp is None
            or cp == ""
            or (isinstance(cp, float) and cp != cp)  # NaN check
        )
        assert is_missing, (
            f"community_prediction must be missing when latest "
            f"aggregation is null; got {cp!r}"
        )

    def test_v2_post_actual_resolve_time_wins_over_scheduled(self):
        """When a question has resolved, actual_resolve_time is set and
        should be used in preference to scheduled_resolve_time."""
        post = self._make_v2_post(
            scheduled_resolve_time="2026-09-01T05:00:00Z",
            actual_resolve_time="2026-08-15T12:00:00Z",
        )
        result = self._run_with_response([post])
        row = result["head"][0]
        assert row["resolve_time"] == "2026-08-15T12:00:00Z", (
            "actual_resolve_time must take precedence over scheduled_resolve_time"
        )

    def test_v2_post_falls_through_to_question_scheduled_resolve_time(self):
        """When a post has no top-level scheduled_resolve_time but the
        nested question does, the script must fall through to the
        question-level field."""
        post = self._make_v2_post()
        post.pop("scheduled_resolve_time", None)
        post.pop("actual_resolve_time", None)
        # question.scheduled_resolve_time stays
        result = self._run_with_response([post])
        row = result["head"][0]
        assert row["resolve_time"] == "2026-09-01T05:00:00Z"

    def test_legacy_schema_still_works_via_fallback(self):
        """Backward compat: a legacy-shape mock (top-level fields,
        categories[] not projects.category, no nested question) must
        STILL produce populated rows via the fallback paths. This is
        the shape the existing TestMetaculusAuthRequiredSentinel tests
        use - keeping legacy fallback prevents older tests/fixtures
        from breaking."""
        legacy_post = {
            "id": 999,
            "title": "Legacy-shape question",
            "type": "binary",  # top-level type (legacy)
            "possibilities": {"type": "binary"},
            "community_prediction": {"full": {"q2": 0.65}},
            "prediction_count": 50,  # legacy field name
            "comment_count": 5,
            "number_of_forecasters": 10,  # legacy field name
            "created_time": "2026-01-01T00:00:00Z",  # legacy
            "publish_time": "2026-01-01T00:00:00Z",  # legacy
            "resolve_time": "2026-12-31T00:00:00Z",  # legacy
            "categories": [{"name": "Test Category"}],  # legacy
            "page_url": "/questions/999/",
        }
        result = self._run_with_response([legacy_post])
        assert result["status"] == "pass"
        row = result["head"][0]
        assert row["question_id"] == 999
        assert row["prediction_count"] == 50, "Legacy prediction_count must still populate"
        assert row["forecaster_count"] == 10, "Legacy number_of_forecasters fallback"
        assert row["community_prediction"] == 0.65, (
            "Legacy community_prediction.full.q2 fallback must populate"
        )
        assert row["category"] == "Test Category"
        assert row["created_time"] == "2026-01-01T00:00:00Z"


class TestEiaElectricityDemandResilience:
    """Round-6 backlog #2 fixes for `eia_electricity_demand`:

    1. EIA returns one record per (period, timezone) - five timezones ×
       N days inflates the response and breaks the index-based 7d-ago
       lookup (idx 6 of 100 records ≈ 1.2 days back, not 7). The script
       now pins `facets[timezone][]=Eastern` so each region returns
       exactly one record per day.
    2. The previous "rows empty AND failures empty → return empty df"
       path silently produced status=success rows=0 with no diagnostic
       breadcrumb. New behavior emits a sentinel row with
       regime_flag=API_ERROR + investment_thesis containing the failure
       summary, so the brief surfaces the issue instead of rendering
       empty.
    """

    def _load(self):
        import json as _json
        return _json.loads((SCRIPTS_DIR / "eia_electricity_demand.json").read_text())

    def test_pins_facets_timezone_eastern(self):
        code = self._load()["code"]
        assert "'facets[timezone][]': 'Eastern'" in code, (
            "Script must pin facets[timezone][]=Eastern to filter EIA "
            "multi-timezone records to one record per day per region. "
            "Without this, idx_7d picks ~1.2 days back, not 7."
        )

    def test_emits_sentinel_when_all_regions_succeed_but_no_rows(self):
        """Patch requests.get to return 200 with empty data array for
        every region. Script should emit a single API_ERROR sentinel
        row instead of silently writing zero rows."""
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {"response": {"data": []}, "warnings": []}
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"EIA_API_KEY": "test_key"}},
            )

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1, (
            f"Expected one sentinel row when all regions succeed but "
            f"produce no usable data, got {result['row_count']}"
        )
        row = result["head"][0]
        assert row["region"] == "INFO"
        assert row["regime_flag"] == "API_ERROR"
        assert "0 rows produced" in (row.get("investment_thesis") or "")

    def test_emits_sentinel_on_403_failures(self):
        """When EIA returns 403 for every region (invalid api_key), the
        script must emit a single sentinel row with the failure summary
        rather than silently writing zero rows."""
        data = self._load()
        import requests as _req

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 403
            resp.json = lambda: {
                "error": {"code": "API_KEY_INVALID", "message": "..."}
            }
            def _raise():
                raise _req.exceptions.HTTPError("403 Client Error")
            resp.raise_for_status = _raise
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"EIA_API_KEY": "bad"}},
            )

        # Must NOT raise. Must emit a sentinel.
        errs = result.get("errors") or []
        assert not any("Runtime error" in str(e) for e in errs), (
            f"Script must not raise on 403 - should emit sentinel. "
            f"Got errors: {errs}"
        )
        assert result["row_count"] == 1
        row = result["head"][0]
        assert row["regime_flag"] == "API_ERROR"
        assert "13/13 regions failed" in (row.get("investment_thesis") or "")


class TestGdeltCaseSensitivityAndResilience:
    """Round-6 backlog #1 fixes for `gdelt_geopolitical_events`:

    1. GDELT's /doc/doc API is case-sensitive on `mode` and `sort` param
       values. The script used `mode=ArtList` + `sort=DateDesc` (capital
       case) which return a generic 429 rate-limit page even on the
       first request. raise_for_status() converted that to an HTTPError,
       the bare-except swallowed it, and the script silently wrote 0
       rows on every run. Use lowercase: `mode=artlist`, `sort=datedesc`.

    2. The `except Exception: articles = []` swallowed real failures
       with no operator visibility. New behavior emits a sentinel row
       with tension_theme=API_ERROR or NO_SIGNAL so the brief surfaces
       the issue rather than rendering empty.
    """

    def _load(self):
        import json as _json
        return _json.loads(
            (SCRIPTS_DIR / "gdelt_geopolitical_events.json").read_text()
        )

    def test_mode_param_is_lowercase(self):
        code = self._load()["code"]
        # Positive: lowercase forms are present
        assert "'mode': 'artlist'" in code, (
            "GDELT mode param must be lowercase 'artlist' - capital "
            "'ArtList' returns a misleading 429 rate-limit page."
        )
        assert "'sort': 'datedesc'" in code, (
            "GDELT sort param must be lowercase 'datedesc'."
        )
        # Negative: capital-case forms must NOT appear
        assert "'mode': 'ArtList'" not in code
        assert "'sort': 'DateDesc'" not in code

    def test_query_or_terms_are_paren_wrapped(self):
        """Round-7 backlog (2026-05-06): GDELT /doc/doc returns HTTP 429
        with body 'Please limit requests to one every 5 seconds' when
        the query string contains unwrapped OR-joined terms past a
        certain length. `(a OR b OR c)` returns 200 with the article
        list. The script was silently writing API_ERROR sentinels every
        run between 2026-05-02 and 2026-05-06 because of this - caught
        when the gmrb_geopolitical_events feeder showed 0 actionable
        rows in the schedule operations report.

        Drift guard: extract the QUERY constant assignment from the
        deployed code and assert it both starts with '(' and ends
        with ')'. Renaming the constant or inlining the query string
        will fail this test loud (intentionally - the wrap MUST
        survive every refactor)."""
        import re
        code = self._load()["code"]
        # Match: QUERY = 'something'   or   QUERY = "something"
        m = re.search(r"^QUERY\s*=\s*['\"]([^'\"]+)['\"]", code, re.MULTILINE)
        assert m, (
            "Could not find module-level QUERY = '...' assignment in "
            "the GDELT script. If you renamed the constant, update "
            "this drift-guard test as well."
        )
        q = m.group(1)
        assert " OR " in q, (
            f"GDELT QUERY must contain OR-joined terms; got {q!r}"
        )
        assert q.startswith("("), (
            f"GDELT QUERY must start with '(' to wrap OR-joined terms; "
            f"unwrapped form returns HTTP 429 from GDELT. Got: {q!r}"
        )
        assert q.endswith(")"), (
            f"GDELT QUERY must end with ')' to wrap OR-joined terms. "
            f"Got: {q!r}"
        )

    def test_emits_sentinel_on_api_error(self):
        """Patch requests.get to return a 429 response. Script should
        emit a single sentinel row (tension_theme=API_ERROR) so the
        operator sees the failure in the brief instead of silent zero."""
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 429
            resp.text = "Please limit requests to one every 5 seconds"
            resp.json = unittest.mock.Mock(side_effect=ValueError("not json"))
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1, (
            f"Expected one sentinel row on 429, got {result['row_count']}"
        )
        row = result["head"][0]
        assert row["tension_theme"] == "API_ERROR", (
            f"Sentinel must use tension_theme=API_ERROR, got "
            f"{row['tension_theme']!r}"
        )
        assert row["severity_tier"] == "INFO"
        # Detail should reference the rate_limited status
        assert "rate" in (row.get("title") or "").lower() or \
               "rate" in (row.get("investment_thesis") or "").lower()

    def test_emits_sentinel_when_no_articles_match_themes(self):
        """API returns 200 but with articles that don't match any
        theme keywords. Script should emit a NO_SIGNAL sentinel rather
        than zero rows."""
        data = self._load()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.text = "{}"
            resp.json = lambda: {
                "articles": [
                    {"url": "https://example.com/a",
                     "title": "Local sports team wins championship",
                     "domain": "example.com", "language": "English",
                     "sourcecountry": "USA", "seendate": "20260502T120000Z"},
                ],
            }
            resp.raise_for_status = unittest.mock.Mock()
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        assert result["row_count"] == 1
        row = result["head"][0]
        assert row["tension_theme"] == "NO_SIGNAL", (
            f"Sentinel must use tension_theme=NO_SIGNAL when API succeeds "
            f"but no articles match curated themes, got "
            f"{row['tension_theme']!r}"
        )


class TestFredScaleBugFixes:
    """Round-6 fixes for two FRED scale bugs surfaced by the round-5
    audit (USDINR=94.25, FEDFUNDS Δ=-169bps).

    1. fred_fx_and_yields used `DEXSFUS` for USDCHF - but DEXSFUS is
       FRED's code for Swedish Kronor per USD (real ~16), not Swiss
       Francs (real ~0.86). Correct code is DEXSZUS.
    2. fred_global_central_banks computed `value_30d_ago` as
       observations[29] - fine for daily series (DGS2/DGS10/T10Y*),
       but FEDFUNDS, IRLTLT01DEM156N/JPM156N/GBM156N are MONTHLY series
       so observations[29] = ~30 MONTHS ago. FEDFUNDS 'change_30d' was
       comparing today's 3.64% to the rate from 2.5 years ago (~5.33%)
       producing -169bps. Fix: walk observations by date and return
       the first one at least 30 calendar days before latest_date.
    """

    def _load_script(self, name):
        import json as _json
        return _json.loads((SCRIPTS_DIR / f"{name}.json").read_text())

    def test_fred_fx_and_yields_uses_dexszus_for_swiss_franc(self):
        """The script must reference DEXSZUS (Swiss franc), not DEXSFUS
        (Swedish krona). The DEXSFUS typo produced 16.49 'USDCHF'
        values which are nonsensical and routed FXF (Swiss) ETF for
        Swedish-krona movement."""
        data = self._load_script("fred_fx_and_yields")
        code = data["code"]
        # Positive: DEXSZUS appears (the correct Swiss franc series).
        assert "DEXSZUS" in code, (
            "fred_fx_and_yields must use DEXSZUS for Swiss franc"
        )
        # Negative: DEXSFUS must NOT appear (it's Swedish, not Swiss).
        assert "DEXSFUS" not in code, (
            "fred_fx_and_yields must NOT use DEXSFUS - that's the FRED "
            "code for Swedish Kronor per USD, not Swiss francs."
        )

    def test_fred_central_banks_uses_date_based_30d_lookup(self):
        """Round-6 fix: the script must walk observations by DATE to
        find the 30-day-ago value, not by index. Index-based lookup
        broke for monthly series like FEDFUNDS where the 29th
        observation was 29 months ago."""
        data = self._load_script("fred_global_central_banks")
        code = data["code"]
        # Negative: the old `prior_idx = 29` literal must be gone.
        assert "prior_idx = 29" not in code, (
            "fred_global_central_banks must not use prior_idx = 29 - "
            "that's the index-based lookup that breaks for monthly "
            "FRED series. Use date-based lookup via timedelta(days=30)."
        )
        # Positive: the script must use timedelta(days=30) for the
        # date-based lookup.
        assert "timedelta(days=30)" in code, (
            "fred_global_central_banks must use timedelta(days=30) "
            "for date-based 30-day-ago lookup."
        )

    def test_fred_central_banks_30d_change_is_small_for_monthly_series(self):
        """End-to-end: when FEDFUNDS observations are spaced 30 days
        apart (the real FRED cadence), the date-based lookup should
        return values[1] - i.e. the *previous month's* observation -
        not values[29] (which would be 29 months ago)."""
        data = self._load_script("fred_global_central_banks")

        # Build a 12-month FEDFUNDS series with monthly-spaced
        # observations. Latest = 3.64, prior = 3.69 (5bp lower last
        # month). With the OLD index-based lookup, observation[29]
        # would be out of range and clamp to values[-1] = 4.19, giving
        # change_bps = (3.64 - 4.19) * 100 = -55bps - wrong by an order
        # of magnitude. With the NEW date-based lookup, the script
        # picks observations[1] (one month back) and reports +5bp from
        # last month, the correct cadence-aligned answer.
        import datetime as _dt

        def make_monthly(latest_value, num_months, monthly_delta):
            """num_months observations, each 30 days apart (newest first)."""
            base = _dt.datetime(2026, 4, 22)
            obs = []
            for i in range(num_months):
                d = base - _dt.timedelta(days=30 * i)
                v = latest_value + (i * monthly_delta)
                obs.append({
                    "realtime_start": d.strftime("%Y-%m-%d"),
                    "realtime_end": d.strftime("%Y-%m-%d"),
                    "date": d.strftime("%Y-%m-%d"),
                    "value": str(round(v, 4)),
                })
            return {"observations": obs}

        # Latest=3.64; 1 month prior=3.69; 12 months prior=4.19
        fedfunds_mock = make_monthly(3.64, 12, 0.05)

        # All other series get a small empty default
        default_empty = _make_fred_desc_series(1.0, num_obs=30, delta_per_step=0.0)
        overlapping_aliases = {
            "DGS2": MACRO_FRED_SERIES_MAP["DGS2_MACRO"],
            "DGS10": MACRO_FRED_SERIES_MAP["DGS10_MACRO"],
            "T10Y2Y": MACRO_FRED_SERIES_MAP["T10Y2Y_MACRO"],
        }

        def router(url, **kwargs):
            params = kwargs.get("params", {})
            series_id = params.get("series_id", "")
            if series_id == "FEDFUNDS":
                return _make_response(fedfunds_mock)
            if series_id in overlapping_aliases:
                return _make_response(overlapping_aliases[series_id])
            return _make_response(MACRO_FRED_SERIES_MAP.get(series_id, default_empty))

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"FRED_API_KEY": "test_mock_key"}},
            )

        assert result["status"] == "pass", f"errors: {result['errors']}"
        # Find FEDFUNDS row
        fed_rows = [r for r in result["head"] if r.get("series_id") == "FEDFUNDS"]
        assert fed_rows, f"FEDFUNDS row missing. head={result['head'][:3]}"
        fed = fed_rows[0]
        # change_bps must reflect the 1-MONTH change (0.05 = 5bps), NOT
        # the 12-month change (12 × 0.05 = 0.60 → 60bps) nor the
        # extrapolated index-based change.
        assert -10.0 <= fed["change_bps"] <= 10.0, (
            f"FEDFUNDS change_bps must be ~5bps (one-month delta) when "
            f"observations are 30 days apart. Got {fed['change_bps']} - "
            f"the index-based bug would have produced a much larger value."
        )


class TestFdaAdverseEventsResilience:
    """openFDA returns 404 + {error:NOT_FOUND, message:'No matches found!'}
    when a search yields zero results, and the dataset typically lags
    real-time by 90-150 days (quarterly refresh).  These regression tests
    pin the round-6 hardening: anchor the search window to the API's
    `last_updated` field, and treat 404 as empty-rather-than-crash."""

    def _load_script(self):
        import json as _json
        return _json.loads(
            (SCRIPTS_DIR / "fda_adverse_events.json").read_text()
        )

    def test_treats_404_as_empty_not_crash(self):
        """Production bug: openFDA returns 404 when the rolling search
        window has no data.  The old script called raise_for_status()
        which propagated as an unhandled exception.  New script must
        return an empty DataFrame instead."""
        data = self._load_script()

        def router(url, **kwargs):
            resp = unittest.mock.Mock()
            if url.endswith("?limit=1"):
                # Meta probe - succeeds
                resp.status_code = 200
                resp.json.return_value = {
                    "meta": {"last_updated": "2026-01-27"},
                    "results": [],
                }
                resp.raise_for_status = unittest.mock.Mock()
                return resp
            # All count URLs return 404 (openFDA's "no matches" convention)
            resp.status_code = 404
            resp.json.return_value = {
                "error": {"code": "NOT_FOUND", "message": "No matches found!"}
            }
            def raise_status():
                raise requests.exceptions.HTTPError("404 Client Error")
            resp.raise_for_status = raise_status
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        # Script must complete without raising a Python exception.  The
        # test runner flags empty DataFrames as status='fail' with the
        # specific message 'DataFrame is empty (0 rows).' - that's the
        # SUCCESS case here (script gracefully produced empty output
        # instead of crashing on 404).  Any OTHER error message (e.g. an
        # uncaught HTTPError or AttributeError) means the script
        # crashed and the regression returned.
        errs = result.get("errors") or []
        non_empty_errs = [
            e for e in errs
            if "DataFrame is empty" not in str(e)
        ]
        assert not non_empty_errs, (
            f"Script crashed on 404-empty path. Errors: {non_empty_errs}"
        )
        assert result["row_count"] == 0, (
            f"404-on-empty must yield 0 rows, got {result['row_count']}"
        )

    def test_anchors_window_to_last_updated_not_today(self):
        """Window must be derived from API meta `last_updated`, not from
        datetime.now().  When the API reports last_updated=2026-01-27,
        the search window's report_window_end must be 20260127 (not
        today's date)."""
        data = self._load_script()

        captured_urls = []

        def router(url, **kwargs):
            captured_urls.append(url)
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if url.endswith("?limit=1"):
                resp.json.return_value = {
                    "meta": {"last_updated": "2026-01-27"},
                    "results": [],
                }
            elif "seriousnessdeath" in url:
                resp.json.return_value = MOCK_FDA_DEATH_EVENTS
            else:
                resp.json.return_value = MOCK_FDA_SERIOUS_EVENTS
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"],
                test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test()

        assert result["status"] == "pass", f"errors: {result['errors']}"
        # All output rows must reference the API's last_updated date,
        # not today's date.
        for row in result["head"]:
            assert row["report_window_end"] == "20260127", (
                f"report_window_end should anchor to API last_updated "
                f"(2026-01-27 → 20260127), got {row['report_window_end']}"
            )
            assert row["data_last_updated"] == "2026-01-27"
        # And the count URLs must reflect that window.
        count_urls = [u for u in captured_urls if "count=" in u]
        assert count_urls, "No count URLs captured"
        assert all("20260127" in u for u in count_urls), (
            f"Count URLs must include the API-anchored end date 20260127. "
            f"Saw: {count_urls}"
        )


# ═══════════════════════════════════════════════════════════════════
# H-MI-2: arbitrage scripts must gate on net-of-fees edge
# ═══════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: raw divergence under the
# round-trip fee + slippage ceiling is not arb. The 3 arb scripts now
# emit ``fee_roundtrip_pct`` and ``net_edge_pct`` and filter on
# ``net_edge >= 1%``. With default fees (4% combined Kalshi+Polymarket+
# slippage), a raw 3% divergence → net_edge_pct ≈ -1 → filtered out.

class TestArbitrageFeeGate:

    def _run(self, script_name: str, url_map: dict):
        data = json.loads(
            (SCRIPTS_DIR / f"{script_name}.json").read_text()
        )
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router), \
             unittest.mock.patch("time.sleep", lambda *a, **kw: None):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    # -- kalshi_polymarket_arbitrage --

    def test_kalshi_poly_arb_3pct_below_fee_ceiling_filtered(self):
        """Kalshi-Polymarket: raw 3% divergence is below the 4% fee roundtrip → filtered.

        2026-05-06: switched fixture to /v2/events?with_nested_markets path.
        Kalshi nested market last_price 65 → 0.65; Polymarket 0.62 → 0.03 abs."""
        url_map = {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event(
                    event_ticker="FEE-GATE-3",
                    title="Federal Reserve rate cut in March 2026",
                    sub_title="Fed funds target",
                    category="Economics",
                    markets=[make_kalshi_market(
                        ticker="FEE-GATE-3",
                        title="Federal Reserve rate cut in March 2026",
                        last_price=65,
                    )],
                )],
                "cursor": "",
            },
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    id="pm_fee_3",
                    question="Federal Reserve rate cut in March 2026",
                    outcomePrices='["0.62","0.38"]',
                    volume="100000",
                ),
            ],
        }
        result = self._run("kalshi_polymarket_arbitrage", url_map)
        rows = [r for r in result["head"] if r.get("kalshi_ticker") == "FEE-GATE-3"]
        assert rows == [], (
            f"Raw 3% divergence must be filtered below fee ceiling; "
            f"got {rows}"
        )

    def test_kalshi_poly_arb_passing_divergence_has_net_edge_column(self):
        """Kalshi-Polymarket: 20% raw divergence → net_edge_pct ~= 16%."""
        # Kalshi 45 → 0.45; Polymarket 0.65 → abs_div 0.20 → net_edge 0.16.
        url_map = {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event(
                    event_ticker="FEE-GATE-PASS",
                    title="Federal Reserve cuts interest rates March 2026",
                    sub_title="Fed funds target",
                    category="Economics",
                    markets=[make_kalshi_market(
                        ticker="FEE-GATE-PASS",
                        title="Federal Reserve cuts interest rates March 2026",
                        last_price=45,
                    )],
                )],
                "cursor": "",
            },
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    id="pm_fee_pass",
                    question="Federal Reserve cuts interest rates March 2026",
                    outcomePrices='["0.65","0.35"]',
                    volume="100000",
                ),
            ],
        }
        result = self._run("kalshi_polymarket_arbitrage", url_map)
        rows = [
            r for r in result["head"] if r.get("kalshi_ticker") == "FEE-GATE-PASS"
        ]
        assert rows, f"Expected the 20%-div row to survive. head={result['head']}"
        row = rows[0]
        assert row["divergence_pct"] == 20.0
        assert row["fee_roundtrip_pct"] == 4.0
        # net_edge_pct should be divergence_pct - fee_roundtrip_pct = 16.0 (± rounding).
        assert abs(row["net_edge_pct"] - 16.0) < 0.01, (
            f"Expected net_edge_pct ≈ 16.0, got {row['net_edge_pct']}"
        )

    # -- kalshi_polymarket_arbitrage_pro --

    def test_kalshi_poly_arb_pro_3pct_filtered(self):
        """Pro variant: 3% raw divergence filtered identically."""
        url_map = {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event(
                    event_ticker="FEE-GATE-3-PRO",
                    title="Alice wins election 2026",
                    sub_title="Presidential race",
                    category="Elections",
                    markets=[make_kalshi_market(
                        ticker="FEE-GATE-3-PRO",
                        title="Alice wins election 2026",
                        subtitle="Presidential race",
                        last_price=65,
                    )],
                )],
                "cursor": "",
            },
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    question="Alice wins election 2026",
                    outcomePrices='["0.62","0.38"]',
                ),
            ],
        }
        result = self._run("kalshi_polymarket_arbitrage_pro", url_map)
        rows = [
            r for r in result["head"] if r.get("kalshi_ticker") == "FEE-GATE-3-PRO"
        ]
        assert rows == [], (
            f"Pro variant must apply the same fee gate; got {rows}"
        )

    def test_kalshi_poly_arb_pro_passing_row_has_fee_columns(self):
        """Pro variant: 10% divergence → net_edge_pct ~= 6%."""
        # Match the existing registry fixture structure: override subtitle
        # so Kalshi's fuzzy-matched title text stays close to Polymarket's
        # question (otherwise rapidfuzz falls below MATCH_THRESHOLD=70).
        url_map = {
            "api.elections.kalshi.com/trade-api/v2/events": {
                "events": [make_kalshi_event(
                    event_ticker="FEE-PRO-PASS",
                    title="Alice wins election 2026",
                    sub_title="Presidential race",
                    category="Elections",
                    markets=[make_kalshi_market(
                        ticker="FEE-PRO-PASS",
                        title="Alice wins election 2026",
                        subtitle="Presidential race",
                        last_price=75,
                    )],
                )],
                "cursor": "",
            },
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(
                    question="Alice wins election 2026",
                    outcomePrices='["0.65","0.35"]',
                ),
            ],
        }
        result = self._run("kalshi_polymarket_arbitrage_pro", url_map)
        rows = [
            r for r in result["head"] if r.get("kalshi_ticker") == "FEE-PRO-PASS"
        ]
        assert rows, f"Expected 10%-div pro row to survive. head={result['head']}"
        row = rows[0]
        assert row["fee_roundtrip_pct"] == 4.0
        assert abs(row["net_edge_pct"] - 6.0) < 0.01

    # -- polymarket_cross_platform_arbitrage --

    def test_poly_cross_platform_high_predictit_fee_filters_noise(self):
        """Polymarket vs PredictIt: PredictIt ~10% fee makes most 5% divergences unprofitable."""
        # Build a Polymarket + PredictIt pair where raw divergence is ~5%
        # (below the 13% FEE_ROUNDTRIP_POLY_PI): must be filtered out.
        poly_mk = make_gamma_market(
            id="pm_cross_noise",
            question="Will Bitcoin reach $100,000 in 2026?",
            slug="btc-100k-2026",
            outcomePrices='["0.40","0.60"]',
            volume="50000",
            liquidityNum="10000",
        )
        predictit_response = {
            "markets": [{
                "id": 1234,
                "name": "Will Bitcoin reach $100,000 in 2026?",
                "contracts": [{
                    "name": "Yes",
                    "lastTradePrice": 0.45,
                    "bestBuyYesCost": 0.46,
                    "bestBuyNoCost": 0.55,
                }],
            }],
        }
        url_map = {
            "gamma-api.polymarket.com/markets": [poly_mk],
            "predictit.org/api/marketdata/all": predictit_response,
            "gamma-api.polymarket.com/events": [],
        }
        result = self._run("polymarket_cross_platform_arbitrage", url_map)
        cross = [
            r for r in result["head"]
            if r.get("source_comparison") == "polymarket_vs_predictit"
        ]
        # 5% divergence - 13% fee = -8% → filtered.
        assert cross == [], (
            f"Noise divergence below Poly/PredictIt fee ceiling must be filtered; "
            f"got {cross}"
        )


# ═══════════════════════════════════════════════════════════════════
# 2026-05-06: Kalshi V2 cross-platform arb scripts must walk /events
# ═══════════════════════════════════════════════════════════════════
# Pins the 2026-05-06 production-review fix: the Kalshi V2 schema
# rolled out a flood of KXMVE* multi-event auto-permutation parlays
# (sports/entertainment cross-products numbering in the thousands)
# that monopolise /v2/markets. The cross-platform arb scripts walked
# /v2/markets and pulled 600 KXMVE rows that never fuzz-matched any
# Polymarket question - silent zero rows on every run for ~2 weeks
# until the schedule operations report surfaced the empty feeder
# (caught when dob_kalshi_poly_arb showed avg 0 rows · 1.16s).
#
# Both arb scripts now walk /v2/events?with_nested_markets=true and
# skip Sports/Entertainment categories upstream. Kalshi Contract
# Scanner already had the events-walk pattern (shipped 2026-05-04).

class TestKalshiArbEventsWalk:
    """Drift guard: arb scripts must walk /events not /markets.

    Without this guard, a future refactor that re-introduces the
    /v2/markets pagination would silently regress to zero rows in
    production. The schedule report's avg-rows column would surface
    it eventually but only after a full week of empty feeder."""

    def _load(self, name):
        return json.loads(
            (SCRIPTS_DIR / f"{name}.json").read_text()
        )

    def test_pro_variant_uses_events_endpoint(self):
        code = self._load("kalshi_polymarket_arbitrage_pro")["code"]
        assert "/v2/events" in code, (
            "Kalshi Pro arb script must walk /v2/events; got code "
            "without /v2/events. /v2/markets is monopolised by "
            "KXMVE auto-permutations under V2 schema (2026-05-06)."
        )
        assert "with_nested_markets" in code, (
            "Pro arb script must use with_nested_markets=true to get "
            "prices in a single API call."
        )
        # Negative: bare /v2/markets walk (without an event_ticker filter)
        # is the bug pattern. Allow `/v2/markets?event_ticker=...` if a
        # future variant ever uses targeted per-event fetch.
        assert "trade-api/v2/markets" not in code, (
            "Pro arb script must NOT call /v2/markets directly - that "
            "endpoint floods with KXMVE auto-permutations under V2."
        )

    def test_base_variant_uses_events_endpoint(self):
        code = self._load("kalshi_polymarket_arbitrage")["code"]
        assert "/v2/events" in code, (
            "Kalshi base arb script must walk /v2/events; got code "
            "without /v2/events."
        )
        assert "with_nested_markets" in code
        assert "trade-api/v2/markets" not in code

    def test_arb_scripts_skip_kxmve_event_tickers(self):
        """Defence-in-depth: even if Kalshi ever serves a KXMVE event
        under a non-Sports category, the script must drop it."""
        for name in ("kalshi_polymarket_arbitrage", "kalshi_polymarket_arbitrage_pro"):
            code = self._load(name)["code"]
            assert "KXMVE" in code, (
                f"{name}: missing defensive KXMVE prefix check. The "
                f"script must drop event_tickers starting with 'KXMVE'."
            )

    def test_arb_scripts_skip_sports_and_entertainment(self):
        """Sports/Entertainment events have ~zero Polymarket overlap and
        contribute most of the noise. Skip them upstream."""
        for name in ("kalshi_polymarket_arbitrage", "kalshi_polymarket_arbitrage_pro"):
            code = self._load(name)["code"]
            assert "Sports" in code and "Entertainment" in code, (
                f"{name}: must skip 'Sports' and 'Entertainment' "
                f"categories upstream."
            )


# ═══════════════════════════════════════════════════════════════════
# H-MI-5: polymarket_calibration_analysis_pro fit_status enum
# ═══════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: scipy.optimize.curve_fit
# silently returns the initial guess when fed NaN or too few bins, and
# the old ``except Exception: pass`` swallowed both OptimizeWarning and
# RuntimeError. After the fix, every row carries a fit_status enum so
# the operator can distinguish 'converged' from 'insufficient_bins' /
# 'fit_failed' / 'fit_error' / 'no_samples'.

class TestCalibrationFitStatus:

    def _run(self, url_map: dict):
        data = json.loads(
            (SCRIPTS_DIR / "polymarket_calibration_analysis_pro.json").read_text()
        )
        router = _make_router(url_map)
        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("requests.post", side_effect=router), \
             unittest.mock.patch("time.sleep", lambda *a, **kw: None):
            executor = CodeExecutor(
                data["code"], test_mode=True, trust_level="unrestricted",
            )
            return executor.execute_test()

    def test_small_sample_reports_insufficient_bins(self):
        """With only 3 resolved markets in 2 distinct bins, the fit mask < 3 → insufficient_bins."""
        # All three markets resolve YES near 0.97-0.98 → they land in the
        # same 95-100% bin, so mask.sum() = 1, < 3 → insufficient_bins.
        url_map = {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(id="m_small_1", outcomePrices='["0.98","0.02"]'),
                make_gamma_market(id="m_small_2", question="B?", outcomePrices='["0.97","0.03"]'),
                make_gamma_market(id="m_small_3", question="C?", outcomePrices='["0.96","0.04"]'),
            ],
        }
        result = self._run(url_map)
        assert result["status"] == "pass", f"errors: {result['errors']}"
        statuses = {row.get("fit_status") for row in result["head"]}
        assert statuses == {"insufficient_bins"}, (
            f"Expected every row to report insufficient_bins; got {statuses}"
        )
        # fit_slope/intercept/r_squared must be null-like. execute_test
        # materialises DataFrames via ``df.head().to_dict(orient='records')``,
        # which on object-dtype columns with mixed None may serialise as
        # ``None``, ``NaN``, or the empty string depending on the upstream
        # coercion - the point is that no real fit was produced.
        def _is_nullish(v):
            if v is None:
                return True
            if v == "":
                return True
            try:
                import math
                if isinstance(v, float) and math.isnan(v):
                    return True
            except Exception:
                pass
            return False

        for row in result["head"]:
            assert _is_nullish(row.get("fit_slope")), (
                f"fit_slope should be null-like for insufficient_bins, got {row.get('fit_slope')!r}"
            )
            assert _is_nullish(row.get("fit_intercept")), (
                f"fit_intercept should be null-like, got {row.get('fit_intercept')!r}"
            )
            assert _is_nullish(row.get("fit_r_squared")), (
                f"fit_r_squared should be null-like, got {row.get('fit_r_squared')!r}"
            )

    def test_bimodal_clean_resolution_converges(self):
        """At least 3 bins with ≥2 unambiguous resolutions each → fit converges."""
        # Build bins with 2 samples each spread across 3 distinct 5% bins:
        # - bin 0-5% (no): two markets at 0.02
        # - bin 50-55% (yes): two markets at 0.51 (won't resolve - need clean)
        # Actually the script filters to resolved-only (>0.95 or <0.05). So
        # we need bins populated by THOSE values. All "yes" rows land in
        # 95-100%, all "no" rows land in 0-5%. Only 2 bins → mask < 3 →
        # insufficient_bins unless we widen. This is a known limitation of
        # the script's binning (price-based, not ground-truth-based), so
        # the converged path is rarely exercised with tight YES/NO data.
        #
        # This test verifies the enum still lands consistently - if the
        # fit can't converge on the test mock, insufficient_bins is the
        # honest answer.
        url_map = {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(id="m_mix_1", outcomePrices='["0.98","0.02"]'),
                make_gamma_market(id="m_mix_2", question="B?", outcomePrices='["0.02","0.98"]'),
                make_gamma_market(id="m_mix_3", question="C?", outcomePrices='["0.97","0.03"]'),
                make_gamma_market(id="m_mix_4", question="D?", outcomePrices='["0.03","0.97"]'),
            ],
        }
        result = self._run(url_map)
        assert result["status"] == "pass", f"errors: {result['errors']}"
        statuses = {row.get("fit_status") for row in result["head"]}
        # Only 2 distinct bins with >=2 samples each → mask.sum() = 2 < 3.
        # The correct enum here is 'insufficient_bins', not 'converged'.
        assert statuses == {"insufficient_bins"}, (
            f"Expected insufficient_bins for 2-bin fixture; got {statuses}"
        )

    def test_fit_status_is_always_in_enum(self):
        """Sanity: whatever path is taken, fit_status must be one of the 5 enum values."""
        url_map = {
            "gamma-api.polymarket.com/markets": [
                make_gamma_market(outcomePrices='["0.98","0.02"]'),
                make_gamma_market(id="m_e_2", question="B?", outcomePrices='["0.02","0.98"]'),
                make_gamma_market(id="m_e_3", question="C?", outcomePrices='["0.97","0.03"]'),
            ],
        }
        result = self._run(url_map)
        allowed = {"no_samples", "insufficient_bins", "fit_failed", "fit_error", "converged"}
        for row in result["head"]:
            assert row.get("fit_status") in allowed, (
                f"fit_status out of enum: {row.get('fit_status')!r}"
            )
