"""Live integration harness for default alert-group feeders.

Not a pytest file - import helpers from here into ``test_live_integration.py``
or invoke directly from a python -c one-liner. Provides:

* ``load_secrets`` - read ``secrets.txt`` into a structured dict
* ``FEEDERS`` - the canonical list of default feeder specs (script,
  expected columns, credentials)
* ``run_script_live`` - execute a library script against real APIs
* ``audit_columns`` - report null/empty ratios per column
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = PROJECT_ROOT / "secrets.txt"
SCRIPTS_DIR = PROJECT_ROOT / "script_library" / "scripts"


# ─────────────────────────────────────────────────────────────────
# secrets.txt parser
# ─────────────────────────────────────────────────────────────────


def load_secrets(path: Path = SECRETS_PATH) -> dict[str, list[str]]:
    """Parse ``[section]``-delimited secrets file.

    Returns ``{normalised_section_name: [non_empty_lines, ...]}``.

    Section names are case-folded and whitespace-normalised so both
    ``[FRED API Key]`` and ``[fred]`` resolve to the ``fred`` key. We
    also split on word boundaries and register every meaningful token
    as an alias (``[FRED API Key]`` → ``fred_api_key`` + ``fred``), so
    callers can look up by either the full key or the shortest name.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"secrets.txt not found at {path} - copy the template and fill in creds."
        )
    out: dict[str, list[str]] = {}
    current_aliases: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip()
            normalised = re.sub(r"[^a-z0-9]+", "_", section.lower()).strip("_")
            aliases = {normalised}
            # Also register the first token as a short alias ("fred" for
            # "fred api key", "sec" for "sec edgar contact" - but not
            # generic words like "api" or "key").
            first = normalised.split("_", 1)[0]
            if first not in {"api", "key", "token", "secret", "contact"}:
                aliases.add(first)
            current_aliases = sorted(aliases)
            for alias in current_aliases:
                out.setdefault(alias, [])
            continue
        if not current_aliases:
            continue
        for alias in current_aliases:
            out[alias].append(line)
    return out


# ─────────────────────────────────────────────────────────────────
# Feeder registry - canonical map of feeder → script → expected cols
# ─────────────────────────────────────────────────────────────────


@dataclass
class Feeder:
    name: str                              # saved-search name
    script: str                            # library script filename (no .json)
    subdirectory: str                      # indexes/<subdirectory>/<file>.parquet
    required_creds: dict[str, str] = field(default_factory=dict)
    expected_columns: list[str] = field(default_factory=list)
    min_rows: int = 1

    @property
    def script_path(self) -> Path:
        return SCRIPTS_DIR / f"{self.script}.json"


FEEDERS: list[Feeder] = [
    Feeder(
        name="dob_crypto_anomalies",
        script="coingecko_volume_anomaly_detector_pro",
        subdirectory="crypto/coingecko_volume_anomalies_pro",
        expected_columns=[
            "rank", "symbol", "name", "price_usd", "volume_24h", "market_cap",
            "ratio_vs_median", "robust_z_score", "percentile_rank",
            "anomaly_strength", "change_1h_pct", "change_24h_pct",
            "change_7d_pct", "is_divergence", "direction_signal",
            "alert_level", "is_statistical_outlier",
        ],
    ),
    Feeder(
        name="dob_earnings_72h",
        script="earnings_calendar_72h",
        subdirectory="equities/earnings_calendar",
        # ``revenue_estimate_usd`` was removed from Nasdaq's free calendar
        # API in early 2026 - the feeder YAML no longer projects it so the
        # default table is fully populated.
        expected_columns=[
            "ticker", "company", "earnings_date", "report_time_code",
            "eps_estimate", "eps_prior_year", "market_cap_tier",
            "market_cap_usd", "hours_until_earnings",
        ],
    ),
    Feeder(
        name="dob_gov_contracts",
        script="usaspending_contract_awards",
        subdirectory="government/contract_awards",
        expected_columns=[
            "recipient", "amount_millions", "awarding_agency",
            "awarding_sub_agency", "contract_type", "description",
            "start_date", "end_date", "size_tier",
        ],
    ),
    Feeder(
        name="dob_kalshi_poly_arb",
        script="kalshi_polymarket_arbitrage_pro",
        subdirectory="kalshi/cross_platform_arb_pro",
        expected_columns=[
            "kalshi_title", "kalshi_ticker", "kalshi_yes_price",
            "polymarket_question", "polymarket_slug", "polymarket_yes_price",
            "divergence_pct", "suggested_action", "opportunity_strength",
            "match_confidence", "match_tier",
        ],
    ),
    Feeder(
        name="dob_macro_regime",
        script="fred_fear_gauges_pro",
        subdirectory="macro/fred_fear_gauges_pro",
        required_creds={"FRED_API_KEY": "fred"},
        expected_columns=[
            "metric", "description", "latest_value", "latest_date",
            "prior_value", "change", "change_pct", "fear_level",
            "percentile_rank", "z_score_1y", "regime",
        ],
    ),
    Feeder(
        name="dob_options_unusual",
        script="options_unusual_activity_pro",
        subdirectory="equities/options_unusual_pro",
        expected_columns=[
            "ticker", "contract_type", "strike", "expiration", "volume",
            "open_interest", "vol_oi_ratio", "last_price", "implied_vol",
            "iv_rank", "delta", "gamma", "vega", "theta", "days_to_expiry",
            "alert_level", "direction_bias",
        ],
    ),
    Feeder(
        name="dob_poly_high_prob",
        script="polymarket_high_probability_pro",
        subdirectory="polymarket/high_probability_pro",
        # ``hours_to_expiry`` is computed by the feeder's eval clause at
        # query time (not emitted by the ingest script), and ``category``
        # was dropped from the upstream API - the corresponding YAML was
        # updated to match. See 2026-04-17 live feeder audit.
        # H-MI-1 (2026-04-21): dropped kelly_fraction_half and
        # suggested_position_size (always 0 / 'SMALL' under the script's
        # fair-price assumption); renamed expected_value_per_dollar to
        # expected_value_if_price_equals_fair. See
        # polymarket_high_probability_pro.json description.
        expected_columns=[
            "question", "slug", "leading_outcome", "leading_price",
            "payout_if_win", "volume", "liquidity",
            "probability_tier",
            "expected_value_if_price_equals_fair", "implied_edge_vs_50",
            "payout_multiple",
        ],
    ),
    Feeder(
        name="dob_poly_volume_spikes",
        script="polymarket_volume_spike_detector_pro",
        subdirectory="polymarket/volume_spikes_pro",
        expected_columns=[
            "question", "slug", "yes_price", "volume_24h", "spike_multiple",
            "volume_24h_ratio", "outlier_strength", "iqr_outlier",
            "robust_z_score", "spike_percentile", "market_age_days",
            "liquidity", "alert_level", "is_statistical_outlier",
        ],
    ),
    Feeder(
        name="dob_reddit_buzz",
        script="reddit_ticker_mentions_pro",
        subdirectory="reddit/ticker_mentions_pro",
        expected_columns=[
            "ticker", "mention_count", "total_score", "total_comments",
            "avg_upvote_ratio", "median_upvote_ratio", "subreddit_count",
            "subreddits", "buzz_level", "weighted_buzz_score",
            "buzz_score_z", "momentum_percentile",
        ],
    ),
    Feeder(
        name="dob_sec_catalysts",
        script="sec_major_filings_feed",
        subdirectory="sec/major_filings",
        required_creds={"SEC_EDGAR_CONTACT": "sec"},
        expected_columns=[
            "ticker", "company_name", "form_type", "filing_type",
            "filing_date", "description", "items", "importance",
        ],
    ),
]


# ─────────────────────────────────────────────────────────────────
# Script runner
# ─────────────────────────────────────────────────────────────────


def load_script_json(feeder: Feeder) -> dict:
    return json.loads(feeder.script_path.read_text())


def run_script_live(
    feeder: Feeder,
    creds: dict[str, str] | None = None,
    *,
    http_budget: int = 200,
    response_mb_budget: int = 20,
) -> pd.DataFrame:
    """Execute a feeder's library script against real APIs.

    Bypasses the scheduled-input-engine plumbing (it's scheduler-oriented)
    and calls the executor directly, injecting a real credentials dict and
    the live ``requests`` module. The resource budgets are lifted relative
    to the normal per-script defaults so occasional rate-limit retries
    don't abort a live test run.
    """
    from scheduled_input_engine.executor import CodeExecutor
    from scheduled_input_engine.cache import (
        reset_budget,
        BudgetAwareRequests,
        get_cached_or_fetch,
    )

    data = load_script_json(feeder)
    code = data["code"]
    trust_level = data.get("trust_level", "sandboxed")

    # Build the same allowed-domain list the engine uses; for live tests we
    # permit every domain referenced by the script. An empty list means
    # "no allowlist" - it does NOT mean "block everything".
    reset_budget(
        max_requests=http_budget,
        max_response_mb=response_mb_budget,
        allowed_domains=[],
    )

    extra = {
        "get_cached_or_fetch": get_cached_or_fetch,
        "requests": BudgetAwareRequests(),
    }
    if creds:
        extra["CREDENTIALS"] = creds

    executor = CodeExecutor(code, trust_level=trust_level)
    return executor.execute(extra_globals=extra)


# ─────────────────────────────────────────────────────────────────
# Column auditor
# ─────────────────────────────────────────────────────────────────


def _is_empty_cell(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def audit_columns(
    df: pd.DataFrame,
    expected: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Per-column audit: presence + empty ratio.

    Returns ``{col: {"present": bool, "n_rows": int, "n_empty": int,
    "empty_ratio": float, "sample": any}}`` so a caller can decide
    pass/fail policy.
    """
    report: dict[str, dict[str, Any]] = {}
    for col in expected:
        if col not in df.columns:
            report[col] = {
                "present": False,
                "n_rows": len(df),
                "n_empty": None,
                "empty_ratio": None,
                "sample": None,
            }
            continue
        series = df[col]
        n = len(series)
        empties = sum(1 for v in series if _is_empty_cell(v))
        first_non_empty = next(
            (v for v in series if not _is_empty_cell(v)),
            None,
        )
        report[col] = {
            "present": True,
            "n_rows": n,
            "n_empty": empties,
            "empty_ratio": (empties / n) if n else 1.0,
            "sample": first_non_empty,
        }
    return report
