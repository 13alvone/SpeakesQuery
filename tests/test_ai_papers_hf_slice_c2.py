#!/usr/bin/env python3
"""
Slice C2 (2026-06-23): Hugging Face as the 6th AI-paper source.

ai_papers_huggingface fetches Hugging Face's Daily Papers feed and emits the
SAME paper-row schema as ai_papers_github_lists, keyed by arxiv id. The
ai_papers_new_today feeder now globs `indexes/ai_papers/*/*` so BOTH sources
flow into one daily diff, and a paper appearing in a GitHub list AND on
Hugging Face cross-dedups by arxiv key.

Pieces under test:
1. **HF ingestion** - single _epoch per run, arxiv: keys (required for
   cross-dedup), title taken from the top-level or nested field,
   source="huggingface".
2. **Feeder spans both sources** - the shipped feeder reads github + hf
   subdirs and surfaces new-today papers from either.
3. **Cross-source dedup** - a paper first seen in a GitHub list on a prior
   day does NOT resurface when it later appears on Hugging Face today.
4. **Allowlist** - huggingface.co is in DEFAULTS and defaults.yaml.
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

HF_SCRIPT = PROJECT_ROOT / "script_library" / "scripts" / "ai_papers_huggingface.json"
FEEDER_YAML = PROJECT_ROOT / "default_saved_searches" / "ai_papers_new_today.yaml"

PAPER_COLUMNS = [
    "paper_key", "title", "url", "link_text", "source_repo", "source",
    "discovered_iso", "_epoch",
]


def _paper_row(key, title, source, epoch):
    return {
        "paper_key": key, "title": title,
        "url": "https://x/" + key, "link_text": title,
        "source_repo": source, "source": source,
        "discovered_iso": "2026-06-23T00:00:00+00:00", "_epoch": epoch,
    }


def _feeder_query():
    return yaml.safe_load(FEEDER_YAML.read_text())["query"]


# ===========================================================================
# 1 - HF ingestion
# ===========================================================================


class TestHFIngestion:

    def test_emits_arxiv_keyed_rows_single_epoch(self):
        spec = json.load(open(HF_SCRIPT))
        mock_items = [
            {"paper": {"id": "2606.10001"}, "title": "First HF Paper"},
            {"paper": {"id": "2606.10002"}, "title": "Second HF Paper"},
            {"title": "Third (top-level title)", "paper": {"id": "2606.10003"}},
        ]
        captured = {}

        def fake_get(url, **kw):
            m = MagicMock()
            m.raise_for_status = lambda: None
            m.json = lambda: mock_items
            return m

        ns = {"GENERATE_RESULTS": lambda df: captured.__setitem__("df", df)}
        with patch("requests.get", side_effect=fake_get):
            exec(compile(spec["code"], "ai_papers_huggingface", "exec"), ns)
        df = captured["df"]
        assert len(df) == 3
        assert df["_epoch"].nunique() == 1
        assert df["paper_key"].tolist() == [
            "arxiv:2606.10001", "arxiv:2606.10002", "arxiv:2606.10003",
        ], "HF rows must be keyed arxiv:<id> for cross-source dedup"
        assert (df["source"] == "huggingface").all()
        assert "Third (top-level title)" in df["title"].tolist()


# ===========================================================================
# 2 + 3 - Feeder spans both sources + cross-source dedup
# ===========================================================================


@pytest.fixture
def both_sources_index():
    base = PROJECT_ROOT / "indexes" / "_c2test" / "ai_papers"
    root = PROJECT_ROOT / "indexes" / "_c2test"
    if root.exists():
        shutil.rmtree(root)
    (base / "github").mkdir(parents=True)
    (base / "huggingface").mkdir(parents=True)
    t_prior = int(time.time()) - 90000   # ~25h ago
    t_now = int(time.time())
    # GitHub: A was seen on a prior day; C is newly added today.
    pd.DataFrame([_paper_row("arxiv:A", "Alpha (github, prior)", "github_ai_papers", t_prior)],
                 columns=PAPER_COLUMNS).to_parquet(base / "github" / "g1.parquet", index=False)
    pd.DataFrame([_paper_row("arxiv:C", "Charlie (github, new today)", "github_ai_papers", t_now)],
                 columns=PAPER_COLUMNS).to_parquet(base / "github" / "g2.parquet", index=False)
    # Hugging Face today: A re-appears (already seen via github -> NOT new);
    # B is genuinely new.
    pd.DataFrame([_paper_row("arxiv:A", "Alpha (now on HF)", "huggingface", t_now),
                  _paper_row("arxiv:B", "Bravo (HF, new today)", "huggingface", t_now)],
                 columns=PAPER_COLUMNS).to_parquet(base / "huggingface" / "h1.parquet", index=False)
    yield "indexes/_c2test/ai_papers"
    shutil.rmtree(root, ignore_errors=True)


class TestFeederBothSources:

    def test_surfaces_new_from_both_and_cross_dedups(self, both_sources_index, caplog):
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        query = _feeder_query().replace("indexes/ai_papers", both_sources_index)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            df, _job, diag = process_query_with_diagnostics(query)
        assert diag is None
        got = set(df["paper_key"]) if df is not None else set()
        # B (new on HF) and C (new on github) surface; A does NOT (it was
        # first seen via github on a prior day -> cross-source dedup).
        assert got == {"arxiv:B", "arxiv:C"}, (
            f"expected new-from-both with cross-dedup, got {got}"
        )
        assert "arxiv:A" not in got, (
            "arxiv:A was seen earlier via github; its HF re-appearance today "
            "must NOT count as new (cross-source dedup by arxiv key)"
        )
        offending = [r for r in caplog.records if "filter not applicable on this pass" in r.getMessage()]
        assert not offending


# ===========================================================================
# 4 - Allowlist + feeder glob contract
# ===========================================================================


class TestAllowlistAndGlob:

    def test_huggingface_in_defaults_dict(self):
        from global_settings import DEFAULTS
        assert r"^huggingface\.co$" in DEFAULTS["allowed_api_domains"]

    def test_huggingface_in_defaults_yaml(self):
        data = yaml.safe_load((PROJECT_ROOT / "global_settings.defaults.yaml").read_text())
        assert r"^huggingface\.co$" in data["allowed_api_domains"]

    def test_feeder_globs_both_sources(self):
        query = _feeder_query()
        assert 'index="indexes/ai_papers/*/*.parquet"' in query, (
            "feeder must glob ai_papers/*/* so it reads BOTH the github and "
            "huggingface subdirs"
        )
