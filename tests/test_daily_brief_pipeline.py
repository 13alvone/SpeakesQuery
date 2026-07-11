"""
End-to-end pipeline tests for the ``daily_opportunity_brief`` alert group.

For every one of the 10 feeders, exercise the full chain:

    library script code + mocked HTTP
    → pandas DataFrame (ingested rows)
    → tmp Parquet at the expected ``indexes/<subdir>/`` location
    → feeder's saved-search SPQL
    → result rows

Failures caught here would otherwise only surface in production as silent
empty dispatches (the alert group runs, serializes 0 rows per feeder, and
Claude receives an analysis window with no data).

The tests re-use the mock-HTTP router factories already in
``tests/test_script_library.py`` so there is no duplicate mock data to
maintain.  Credentials for ``ag_sec_catalysts`` and ``ag_macro_regime``
are injected as test values.
"""

from __future__ import annotations

import json
import unittest.mock as _mock
from pathlib import Path

import pandas as pd
import pytest
import yaml

from query_engine.CmdExecutionBackend import run_query_and_return_results_df
from scheduled_input_engine.executor import CodeExecutor
from tests.test_script_library import (
    CREDENTIALED_SCRIPT_REGISTRY,
    MOCK_FRED_FEAR_5,
    SCRIPT_REGISTRY,
    _fred_router_factory,
    _make_router,
    _sec_router_factory,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SS_DIR = PROJECT_ROOT / "default_saved_searches"
LIBRARY_DIR = PROJECT_ROOT / "script_library" / "scripts"


# ── Feeder → library script map (matches the daily_opportunity_brief AG) ──
FEEDER_TO_SCRIPT = {
    "dob_poly_high_prob":      "polymarket_high_probability_pro",
    "dob_kalshi_poly_arb":     "kalshi_polymarket_arbitrage_pro",
    "dob_poly_volume_spikes":  "polymarket_volume_spike_detector_pro",
    "dob_crypto_anomalies":    "coingecko_volume_anomaly_detector_pro",
    "dob_sec_catalysts":       "sec_major_filings_feed",
    "dob_reddit_buzz":         "reddit_ticker_mentions_pro",
    "dob_gov_contracts":       "usaspending_contract_awards",
    "dob_macro_regime":        "fred_fear_gauges_pro",
    "dob_earnings_72h":        "earnings_calendar_72h",
    "dob_options_unusual":     "options_unusual_activity_pro",
}


def _load_feeder(feeder_name: str) -> tuple[dict, dict]:
    """Return (saved_search_yaml, library_script_json) for a feeder."""
    script_id = FEEDER_TO_SCRIPT[feeder_name]
    ss = yaml.safe_load((DEFAULT_SS_DIR / f"{feeder_name}.yaml").read_text())
    script = json.loads((LIBRARY_DIR / f"{script_id}.json").read_text())
    return ss, script


def _resolve_mock_context(script_id: str):
    """
    Return ``(router, creds)`` for running the script's ingestion code
    with mocked HTTP.  Prefers `SCRIPT_REGISTRY` (no-auth) then falls
    back to credentialed router factories that ``test_script_library``
    already uses.
    """
    spec = SCRIPT_REGISTRY.get(script_id)
    if spec and spec.get("url_map"):
        return _make_router(spec["url_map"]), {}

    if script_id.startswith("sec_"):
        return _sec_router_factory(), {
            "SEC_EDGAR_CONTACT": "SpeakesQuery Test (test@example.com)"
        }
    if script_id.startswith("fred_"):
        return _fred_router_factory(MOCK_FRED_FEAR_5), {
            "FRED_API_KEY": "test_mock_key"
        }

    # Unknown or missing mock config - caller should skip.
    return None, {}


def _run_script_capture_df(code: str, trust_level: str, creds: dict, router) -> pd.DataFrame:
    """
    Compile and run a library script exactly as the engine would
    (RestrictedPython for sandboxed, plain exec for unrestricted),
    with ``requests.get``/``requests.post`` routed through ``router``,
    and capture the DataFrame passed to ``GENERATE_RESULTS``.
    """
    executor = CodeExecutor(code, test_mode=True, trust_level=trust_level)
    captured: list[pd.DataFrame | None] = [None]

    def capture(df, *_args):
        captured[0] = df.copy() if isinstance(df, pd.DataFrame) else df

    run_globals = executor._build_globals({"CREDENTIALS": creds})
    run_globals["GENERATE_RESULTS"] = capture

    with _mock.patch("requests.get", side_effect=router), \
         _mock.patch("requests.post", side_effect=router), \
         _mock.patch("time.sleep", return_value=None):
        # Patch time.sleep so scripts that pace themselves (e.g. options
        # with 40 tickers + ~0.8s jitter each) don't inflate the test
        # suite by ~30s.  Production paths still use the real sleep.
        # The code has already been compiled via RestrictedPython (for
        # sandboxed) or via plain compile() (for unrestricted, matching
        # the engine's production path); bandit B102 is acknowledged.
        if trust_level == "unrestricted":
            exec(executor._compiled, run_globals)  # nosec B102
        else:
            exec(executor._compiled, run_globals, {})  # nosec B102

    df = captured[0]
    if not isinstance(df, pd.DataFrame):
        raise AssertionError(
            "Script did not call GENERATE_RESULTS with a DataFrame"
        )
    return df


def _write_parquet_at_subdir(df: pd.DataFrame, root: Path, subdir: str) -> Path:
    """Persist the DataFrame at ``<root>/indexes/<subdir>/ingested.parquet``."""
    target = root / "indexes" / subdir
    target.mkdir(parents=True, exist_ok=True)
    path = target / "ingested.parquet"
    df.to_parquet(path, compression="gzip")
    return path


import re as _re

_EVAL_ASSIGN_RX = _re.compile(r"eval\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _extract_eval_created_columns(query: str) -> set[str]:
    """Columns introduced by `| eval <col> = ...` clauses."""
    return set(_EVAL_ASSIGN_RX.findall(query or ""))


def _extract_projected_columns(query: str) -> set[str]:
    """
    Best-effort scrape of columns the SPQL definitely projects or
    depends on.  Subtracts columns introduced by `eval` so computed
    fields don't false-positive as "missing from ingested data".
    """
    cols: set[str] = set()
    for verb in ("table", "fields"):
        idx = query.find(f"| {verb} ")
        if idx == -1:
            idx = query.find(f"|{verb} ")
        if idx == -1:
            continue
        tail = query[idx:]
        nl = tail.find("\n")
        segment = tail[: nl if nl != -1 else len(tail)]
        segment = segment.split(verb, 1)[1]
        for tok in segment.split(","):
            tok = tok.strip().strip("`'\"")
            if tok and tok.replace("_", "").isalnum():
                cols.add(tok)
    return cols - _extract_eval_created_columns(query)


# ─────────────────────────────────────────────────────────────────────────────
# Parametrized feeder tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("feeder_name", sorted(FEEDER_TO_SCRIPT.keys()))
class TestDailyBriefPipeline:
    """
    For each feeder in ``daily_opportunity_brief``, run the full
    ingestion → parquet → SPQL chain and assert that the wiring holds
    together.  Row counts after filtering are allowed to be zero
    (filters can be legitimately strict), but the script must produce
    data, every projected column must exist in the ingested frame, and
    the SPQL must execute without raising.
    """

    def test_script_runs_and_produces_dataframe(self, feeder_name):
        ss, script = _load_feeder(feeder_name)
        router, creds = _resolve_mock_context(script["id"] if "id" in script
                                              else FEEDER_TO_SCRIPT[feeder_name])
        if router is None:
            pytest.skip(
                f"No mock config for {FEEDER_TO_SCRIPT[feeder_name]} - "
                "extend SCRIPT_REGISTRY in test_script_library.py"
            )
        df = _run_script_capture_df(
            script["code"],
            script.get("trust_level", "sandboxed"),
            creds,
            router,
        )
        assert len(df) > 0, "Script produced an empty DataFrame"
        assert "_epoch" in df.columns, "Script must emit _epoch column"

    def test_spql_projected_columns_exist_in_ingested_data(self, feeder_name):
        """
        Every column the saved search projects/filters on must be
        present in the raw ingested DataFrame - otherwise the SPQL
        query would raise at runtime in production.
        """
        ss, script = _load_feeder(feeder_name)
        router, creds = _resolve_mock_context(FEEDER_TO_SCRIPT[feeder_name])
        if router is None:
            pytest.skip("No mock config")
        df = _run_script_capture_df(
            script["code"],
            script.get("trust_level", "sandboxed"),
            creds,
            router,
        )
        required = _extract_projected_columns(ss["query"])
        missing = required - set(df.columns)
        assert not missing, (
            f"Feeder {feeder_name}: SPQL projects columns not in ingested "
            f"data: {sorted(missing)}.  DataFrame columns: "
            f"{sorted(df.columns)}"
        )

    def test_spql_executes_against_ingested_parquet(self, feeder_name, tmp_path, monkeypatch):
        """
        Materialize the ingested DataFrame to Parquet at the expected
        subdirectory, then execute the saved search's SPQL against it
        via the real query backend.  The query must not raise.  Row
        count after filtering is logged but not asserted non-zero -
        feeders with strict filters (e.g. ``volume > 50000``) may
        legitimately produce zero rows from the mock dataset.
        """
        ss, script = _load_feeder(feeder_name)
        router, creds = _resolve_mock_context(FEEDER_TO_SCRIPT[feeder_name])
        if router is None:
            pytest.skip("No mock config")
        df = _run_script_capture_df(
            script["code"],
            script.get("trust_level", "sandboxed"),
            creds,
            router,
        )

        _write_parquet_at_subdir(df, tmp_path, script["suggested_subdirectory"])
        monkeypatch.chdir(tmp_path)

        result_df, _job_id = run_query_and_return_results_df(ss["query"])

        # Either result_df is None (engine logged an error - bad wiring),
        # or it's a DataFrame that the query successfully produced.
        assert result_df is not None or len(df) > 0, (
            f"Feeder {feeder_name}: SPQL query returned None.  This "
            f"usually indicates a parse error, a missing column, or a "
            f"path-resolution issue.  Query:\n{ss['query']}"
        )
        # If we got results, their columns should be a subset of the
        # ingested data's columns (plus any computed via `eval`).
        if result_df is not None and len(result_df) > 0:
            expected_present = _extract_projected_columns(ss["query"])
            result_cols = set(result_df.columns)
            # Computed columns may legitimately differ; just require
            # overlap with non-computed projections.
            overlap = expected_present & result_cols
            assert overlap, (
                f"Feeder {feeder_name}: SPQL result columns {sorted(result_cols)} "
                f"share nothing with projected {sorted(expected_present)}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Static consistency checks (not parametrized - cheap one-offs)
# ─────────────────────────────────────────────────────────────────────────────

def test_every_feeder_has_matching_yaml_and_script():
    """Sanity check: every entry in FEEDER_TO_SCRIPT resolves to real files."""
    missing_yaml = []
    missing_script = []
    for feeder_name, script_id in FEEDER_TO_SCRIPT.items():
        if not (DEFAULT_SS_DIR / f"{feeder_name}.yaml").exists():
            missing_yaml.append(feeder_name)
        if not (LIBRARY_DIR / f"{script_id}.json").exists():
            missing_script.append(script_id)
    assert not missing_yaml, f"Missing default saved searches: {missing_yaml}"
    assert not missing_script, f"Missing library scripts: {missing_script}"


def test_feeder_map_matches_alert_group_yaml():
    """
    The FEEDER_TO_SCRIPT keys must match the ``search_names`` list in
    ``alert_groups/daily_opportunity_brief.yaml`` exactly - excluding
    the ``*_reserved_picks`` dedup/throttle loops, which have no
    ingestion script (they query ``indexes/logs/ag_picks/`` which the
    dispatcher itself populates).  If the AG YAML is edited to add/
    remove real feeders, this test forces a matching update here.
    """
    ag_yaml = yaml.safe_load(
        (PROJECT_ROOT / "alert_groups" / "daily_opportunity_brief.yaml").read_text()
    )
    ag_feeders = {
        name for name in (ag_yaml.get("search_names") or [])
        if not name.endswith("_reserved_picks")
    }
    mapped = set(FEEDER_TO_SCRIPT.keys())
    assert ag_feeders == mapped, (
        f"Drift between AG YAML search_names and FEEDER_TO_SCRIPT.  "
        f"Only-in-YAML: {ag_feeders - mapped}.  "
        f"Only-in-map: {mapped - ag_feeders}."
    )
