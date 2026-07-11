#!/usr/bin/env python3
"""
Slice B (2026-06-23): "GitHub Hot Repos" daily alert group.

Pieces under test:

1. **Ingestion invariant** - github_trending_repos emits all rows of a run
   with ONE shared `_epoch`. The feeder's first-seen dedup depends on this;
   if a future edit makes `_epoch` per-row, the dedup silently breaks.
2. **Dedup feeder (the heart)** - the SHIPPED feeder query
   (github_hot_repos_today.yaml) surfaces only repos appearing for the
   FIRST time in the window (no-repeat-for-30-days), ranked by stars,
   capped at 10. Loaded from the YAML so the test catches query drift.
3. **Clean logs** - the feeder runs with ZERO "Error while applying Pandas
   query" logs. A `where` on a derived (eventstats/eval) column logs a
   spurious error every run; the feeder must filter on the BASE _epoch
   column only. This guard fails loudly if someone reintroduces a
   derived-column where.
4. **Empty/quiet day** - no new entrants yields 0 rows, no error.
5. **AG config** - the shipped AG pins model_id to the local 122B, ships
   disabled, sets no output_kind (so the digest never pollutes the OEB
   ag_picks journal), and round-trips through AlertGroupStore validation.
6. **Allowlist** - github.com is allow-listed in BOTH global_settings.py
   DEFAULTS and global_settings.defaults.yaml.
"""

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

LOCAL_MODEL_ID = "llamacpp-qwen35-122b-a10b"
SCRIPT_PATH = PROJECT_ROOT / "script_library" / "scripts" / "github_trending_repos.json"
FEEDER_YAML = PROJECT_ROOT / "default_saved_searches" / "github_hot_repos_today.yaml"
AG_YAML = PROJECT_ROOT / "default_alert_groups" / "github_hot_repos_brief.yaml"

TRENDING_COLUMNS = [
    "repo_full_name", "owner", "name", "html_url", "description",
    "language", "stars_total", "stars_today", "source", "snapshot_date",
    "_epoch",
]


def _trending_row(full, stars_today, epoch):
    return {
        "repo_full_name": full, "owner": full.split("/")[0],
        "name": full.split("/")[1], "html_url": "https://github.com/" + full,
        "description": "desc", "language": "Go", "stars_total": stars_today * 10,
        "stars_today": stars_today, "source": "trending",
        "snapshot_date": "2026-06-23", "_epoch": epoch,
    }


@pytest.fixture
def trending_index():
    """Seed indexes/<tmp>/ with two daily runs: run1 ~25h ago (prior),
    run2 now (latest). cc/r3 repeats across both; dd/r4, ee/r5 are new."""
    test_dir = PROJECT_ROOT / "indexes" / "_slice_b_test"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True)
    t_prior = int(time.time()) - 90000   # ~25h ago - outside the -1d window
    t_now = int(time.time())
    pd.DataFrame(
        [_trending_row("aa/r1", 100, t_prior), _trending_row("bb/r2", 200, t_prior),
         _trending_row("cc/r3", 300, t_prior)],
        columns=TRENDING_COLUMNS,
    ).to_parquet(test_dir / "run1.parquet", index=False)
    pd.DataFrame(
        [_trending_row("cc/r3", 500, t_now), _trending_row("dd/r4", 400, t_now),
         _trending_row("ee/r5", 350, t_now)],
        columns=TRENDING_COLUMNS,
    ).to_parquet(test_dir / "run2.parquet", index=False)
    yield "indexes/_slice_b_test"
    shutil.rmtree(test_dir, ignore_errors=True)


def _feeder_query():
    data = yaml.safe_load(FEEDER_YAML.read_text())
    return data["query"]


# ===========================================================================
# 1 - Ingestion single-_epoch invariant
# ===========================================================================


class TestIngestionEpochInvariant:

    def test_all_rows_share_one_epoch(self):
        """The dedup design REQUIRES one _epoch per run. Run the script as
        plain Python against a small trending fixture and assert it."""
        spec = json.load(open(SCRIPT_PATH))
        html = (
            "<html><body>"
            + "".join(
                f'<article class="Box-row">'
                f'<h2 class="h3 lh-condensed"><a href="/o{i}/r{i}">o{i} / r{i}</a></h2>'
                f'<p class="col-9">repo {i}</p>'
                f'<span itemprop="programmingLanguage">Python</span>'
                f'<a href="/o{i}/r{i}/stargazers">{1000 - i * 10}</a>'
                f'<span class="float-sm-right">{500 - i * 10} stars today</span>'
                f'</article>'
                for i in range(6)
            )
            + "</body></html>"
        )
        captured = {}

        def gen(df):
            captured["df"] = df

        def fake_get(url, **kw):
            m = MagicMock()
            m.raise_for_status = lambda: None
            m.text = html if "github.com/trending" in url else ""
            m.json = lambda: {"items": []}
            return m

        ns = {"GENERATE_RESULTS": gen}
        with patch("requests.get", side_effect=fake_get):
            exec(compile(spec["code"], "github_trending_repos", "exec"), ns)
        df = captured["df"]
        assert len(df) == 6
        assert df["_epoch"].nunique() == 1, (
            "all rows of a run MUST share one _epoch - the feeder's "
            "first-seen dedup breaks otherwise"
        )
        assert (df["source"] == "trending").all()
        assert df["stars_today"].is_monotonic_decreasing


# ===========================================================================
# 2 + 3 + 4 - Dedup feeder correctness, clean logs, quiet day
# ===========================================================================


class TestDedupFeeder:

    def test_surfaces_only_new_repos_sorted(self, trending_index, caplog):
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        query = _feeder_query().replace("indexes/github/trending", trending_index)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            df, _job, diag = process_query_with_diagnostics(query)
        assert diag is None
        got = list(df["repo_full_name"]) if df is not None else []
        assert got == ["dd/r4", "ee/r5"], (
            f"expected only NEW repos newest-hottest first, got {got}"
        )
        # cc/r3 repeated from the prior run - must NOT resurface.
        assert "cc/r3" not in got

    def test_feeder_runs_without_pandas_query_errors(self, trending_index, caplog):
        """A `where` on a derived column logs 'Error while applying Pandas
        query' every run. The feeder must filter on the BASE _epoch column
        only - zero such errors."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        query = _feeder_query().replace("indexes/github/trending", trending_index)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            process_query_with_diagnostics(query)
        offending = [
            r.getMessage() for r in caplog.records
            if "filter not applicable on this pass" in r.getMessage()
        ]
        assert not offending, (
            "feeder logged a pandas-query error - it is filtering on a "
            f"derived column. Use base-column filters only. Saw: {offending}"
        )

    def test_quiet_day_returns_empty_cleanly(self, caplog):
        """No new entrants today (only the old run exists) -> 0 rows, no error."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        test_dir = PROJECT_ROOT / "indexes" / "_slice_b_quiet"
        if test_dir.exists():
            shutil.rmtree(test_dir)
        test_dir.mkdir(parents=True)
        try:
            old = int(time.time()) - 90000
            pd.DataFrame(
                [_trending_row("aa/r1", 100, old), _trending_row("bb/r2", 200, old)],
                columns=TRENDING_COLUMNS,
            ).to_parquet(test_dir / "run1.parquet", index=False)
            query = _feeder_query().replace("indexes/github/trending", "indexes/_slice_b_quiet")
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                df, _job, diag = process_query_with_diagnostics(query)
            n = 0 if df is None else len(df)
            assert n == 0, f"quiet day should surface 0 new repos, got {n}"
            offending = [r for r in caplog.records if "Error while applying" in r.getMessage()]
            assert not offending
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


# ===========================================================================
# 5 - Alert group config + validation round-trip
# ===========================================================================


class TestAlertGroupConfig:

    def test_ag_pins_local_model_and_ships_disabled(self):
        data = yaml.safe_load(AG_YAML.read_text())
        assert data["model_id"] == LOCAL_MODEL_ID, "AG must route to the local 122B"
        assert data["disabled"] is True, "ship disabled - operator enables it"
        assert data["search_names"] == ["github_hot_repos_today"]
        # No output_kind => the digest never journals into the OEB ag_picks
        # trading record.
        assert not data.get("output_kind"), (
            "AG must not set output_kind - a repo digest is not a trading pick"
        )

    def test_prompt_asks_for_digest_not_json(self):
        data = yaml.safe_load(AG_YAML.read_text())
        pt = data["prompt_text"].lower()
        assert "no json" in pt, (
            "prompt must steer away from a JSON tail so pick extraction "
            "stays a clean no-op"
        )

    def test_ag_round_trips_through_store(self, tmp_path):
        from alert_group_store import AlertGroupStore
        empty_defaults = tmp_path / "_empty"
        empty_defaults.mkdir()
        store = AlertGroupStore()
        store._dir = tmp_path / "alert_groups"
        store._defaults_dir = empty_defaults
        store._db = str(tmp_path / "lc.sqlite")
        store._runs_db = str(tmp_path / "runs.sqlite")
        store.initialize()
        data = yaml.safe_load(AG_YAML.read_text())
        store.save_group(data, overwrite=True)
        loaded = store.get_group("github_hot_repos_brief")
        assert loaded["model_id"] == LOCAL_MODEL_ID
        assert loaded["disabled"] is True

    def test_feeder_yaml_is_a_feeder(self):
        data = yaml.safe_load(FEEDER_YAML.read_text())
        assert data["purpose"] == "alert_group_feeder"
        assert "dedup repo_full_name" in data["query"]
        assert 'relative_time("-30d")' in data["query"]


# ===========================================================================
# 6 - Allowlist
# ===========================================================================


class TestAllowlist:

    def test_github_com_in_defaults_dict(self):
        from global_settings import DEFAULTS
        patterns = DEFAULTS["allowed_api_domains"]
        assert any(p == r"^github\.com$" for p in patterns), (
            "github.com missing from allowed_api_domains DEFAULTS - the "
            "trending scrape will be blocked"
        )

    def test_github_com_in_defaults_yaml(self):
        # Parse the YAML so the comparison is against the real regex
        # (the file stores "^github\\.com$"; YAML unescapes it to one
        # backslash), not raw double-backslash text.
        data = yaml.safe_load(
            (PROJECT_ROOT / "global_settings.defaults.yaml").read_text()
        )
        assert r"^github\.com$" in data["allowed_api_domains"], (
            "github.com missing from global_settings.defaults.yaml - a fresh "
            "install won't have it"
        )
