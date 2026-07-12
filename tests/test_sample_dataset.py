"""Bundled sample dataset + first-run experience (weakness audit W12, 2026-07-12).

A fresh install must not be empty: indexes/sample/app_logs ships in git
and is baked into the Docker image (_default_indexes seeding), and the
Query page shows a dismissible "try these 5 queries" card whose chips
are guaranteed offline-safe.

Pins:
1. The parquet exists, is tracked (not gitignored), and has the schema
   the first-run queries depend on.
2. All five first-run queries run end-to-end through the real engine
   and return rows - a broken chip on someone's first minute with the
   app is the worst possible first impression.
3. The generator is deterministic (safe to regenerate).
4. The Docker image bakes the sample dir; the UI card contract holds.
"""

import re
import subprocess
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PARQUET = (
    PROJECT_ROOT / "indexes" / "sample" / "app_logs" / "1780272000_sample.parquet"
)
UI_HTML = PROJECT_ROOT / "desktop_app" / "ui.html"

# Keep in sync with the SAMPLE_QUERIES list in ui.html (the drift guard
# in TestUiCardContract compares against ui.html directly).
FIRST_RUN_QUERIES = [
    'index="indexes/sample/app_logs/*" | head 20',
    'index="indexes/sample/app_logs/*" | search level="ERROR" '
    "| stats count by service | sort -count",
    'index="indexes/sample/app_logs/*" | rename timestamp as _time '
    "| timechart span=1day count by level",
    'index="indexes/sample/app_logs/*" '
    '| eval speed=if_(response_ms > 750, "slow", "fast") '
    "| stats count, avg(response_ms) by speed",
    'index="indexes/sample/app_logs/*" '
    '| rex field=message "user (?<user>\\w+)" | search user!="" '
    "| stats count by user | sort -count | head 10",
]


class TestSampleParquet:
    def test_exists_and_is_tracked(self):
        assert SAMPLE_PARQUET.is_file(), (
            "indexes/sample/app_logs parquet missing - regenerate with "
            "`python -m tools.generate_sample_data`"
        )
        # git check-ignore exits 1 when the path is NOT ignored.
        result = subprocess.run(
            ["git", "check-ignore", str(SAMPLE_PARQUET)],
            cwd=PROJECT_ROOT, capture_output=True,
        )
        assert result.returncode != 0, (
            "the sample parquet is gitignored - the !/indexes/sample/ "
            "negation in .gitignore was lost"
        )

    def test_schema_matches_first_run_queries(self):
        df = pd.read_parquet(SAMPLE_PARQUET)
        expected = {
            "_epoch", "timestamp", "level", "service", "host", "path",
            "status_code", "response_ms", "client_ip", "message",
            "bytes_sent",
        }
        assert expected.issubset(set(df.columns)), (
            f"sample dataset lost columns the first-run queries use: "
            f"{expected - set(df.columns)}"
        )
        assert len(df) == 5000
        assert str(df["_epoch"].dtype) == "int64"
        # Every first-run query needs these to return non-trivial output
        assert (df["level"] == "ERROR").any()
        assert df["message"].str.contains("user ").any()

    def test_generator_is_deterministic(self):
        from tools.generate_sample_data import build_dataframe
        df_a = build_dataframe()
        df_b = build_dataframe()
        pd.testing.assert_frame_equal(df_a, df_b)


class TestFirstRunQueriesExecute:
    @pytest.mark.parametrize("query", FIRST_RUN_QUERIES)
    def test_query_returns_rows(self, query, run_query):
        df, job_id = run_query(query)
        assert df is not None, f"first-run query failed outright: {query}"
        assert len(df) > 0, f"first-run query returned zero rows: {query}"


class TestDockerSeeding:
    def test_dockerfile_bakes_sample_dir(self):
        dockerfile = (
            PROJECT_ROOT / "desktop_app" / "Dockerfile"
        ).read_text(encoding="utf-8")
        assert "indexes/sample _default_indexes/sample" in dockerfile, (
            "Dockerfile must bake indexes/sample into _default_indexes so "
            "the entrypoint seeds it into fresh volume mounts (W12)"
        )


class TestUiCardContract:
    def test_card_and_chips_present(self):
        ui = UI_HTML.read_text(encoding="utf-8")
        assert 'id="sample-queries-card"' in ui
        assert 'id="sample-query-chips"' in ui
        assert 'id="sample-queries-dismiss"' in ui
        assert "indexes/sample/app_logs" in ui

    def test_ui_queries_match_pinned_set(self):
        # Extract the query strings from the SAMPLE_QUERIES JS array and
        # verify each one (normalized) is in the pinned executable set -
        # so a UI edit can't ship an untested chip.
        ui = UI_HTML.read_text(encoding="utf-8")
        block = re.search(
            r"const SAMPLE_QUERIES = \[(.*?)\];", ui, re.DOTALL
        )
        assert block, "SAMPLE_QUERIES array missing from ui.html"
        ui_queries = re.findall(r"query: '([^']+)'", block.group(1))
        assert len(ui_queries) == len(FIRST_RUN_QUERIES)
        normalized_pinned = [
            re.sub(r"\s+", " ", q).strip() for q in FIRST_RUN_QUERIES
        ]
        for ui_query in ui_queries:
            flat = re.sub(
                r"\s+", " ", ui_query.replace("\\n", " ")
            ).replace("\\\\w", "\\w").strip()
            assert flat in normalized_pinned, (
                f"ui.html sample chip not covered by the executable "
                f"test set: {flat!r}"
            )


class TestSampleCardBrowser:
    """Playwright coverage (browser tier - runs nightly + on demand)."""

    def test_card_visible_chip_loads_query_and_dismiss_persists(self, page):
        card = page.locator("#sample-queries-card")
        card.wait_for(state="visible", timeout=5000)

        page.locator(".sample-query-chip").first.click()
        query_value = page.locator("#query").input_value()
        assert "indexes/sample/app_logs" in query_value

        page.locator("#sample-queries-dismiss").click()
        card.wait_for(state="hidden", timeout=5000)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".page.active", state="visible", timeout=10000)
        assert not page.locator("#sample-queries-card").is_visible()
