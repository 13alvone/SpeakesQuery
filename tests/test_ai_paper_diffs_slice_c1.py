#!/usr/bin/env python3
"""
Slice C1 (2026-06-23): "AI Paper Diffs" daily alert group + the
`skip_on_empty` dispatcher flag.

Pieces under test:

1. **Ingestion** - ai_papers_github_lists parses a GitHub-API README
   (base64 markdown) into paper rows: one shared `_epoch` per run (the
   dedup depends on it), titles never start with a stray bracket, and the
   masamasa-style `**"title"** [[paper](url)]` extracts the real title.
2. **Dedup feeder** - the SHIPPED feeder (ai_papers_new_today.yaml)
   surfaces only papers appearing for the FIRST time in the window (the
   daily diff), keyed on paper_key, with zero pandas-query errors.
3. **skip_on_empty (the new dispatcher flag)** - when set, an AG with all
   feeders empty returns status="skipped" with NO LLM call (money-leak
   canary), NO failure email, and NO circuit-breaker trip. When unset, the
   historical error behavior is preserved (so a quiet stretch can't
   silently change for other AGs).
4. **AG config + store round-trip** - model_id pins the local 122B,
   skip_on_empty is true, no output_kind, ships disabled, and round-trips
   through AlertGroupStore (incl. skip_on_empty persistence).
"""

import base64
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

from alert_groups.dispatcher import AlertGroupDispatcher

LOCAL_MODEL_ID = "llamacpp-qwen35-122b-a10b"
SCRIPT_PATH = PROJECT_ROOT / "script_library" / "scripts" / "ai_papers_github_lists.json"
FEEDER_YAML = PROJECT_ROOT / "default_saved_searches" / "ai_papers_new_today.yaml"
AG_YAML = PROJECT_ROOT / "default_alert_groups" / "ai_paper_diffs_brief.yaml"

PAPER_COLUMNS = [
    "paper_key", "title", "url", "link_text", "source_repo", "source",
    "discovered_iso", "_epoch",
]

_MOCK_MD = (
    "# Papers\n"
    '* **"Alpha Paper: A Study of Things"** [[paper](https://arxiv.org/abs/2501.00001)]\n'
    '* **"Beta Paper: Another Study"** [[paper](https://arxiv.org/abs/2501.00002)]\n'
    "[Gamma Paper Title on Vision](https://arxiv.org/abs/2501.00003)\n"
    "[Delta Explainer](https://medium.com/papers-explained/delta-123)\n"
)


def _paper_row(key, title, epoch):
    return {
        "paper_key": key, "title": title, "url": "https://arxiv.org/abs/" + key.split(":")[-1],
        "link_text": title, "source_repo": "test/list", "source": "github_ai_papers",
        "discovered_iso": "2026-06-23T00:00:00+00:00", "_epoch": epoch,
    }


def _feeder_query():
    return yaml.safe_load(FEEDER_YAML.read_text())["query"]


def _ag(skip_on_empty=True, name="ai_paper_diffs_test"):
    return {
        "name": name,
        "description": "AI paper diffs",
        "search_names": ["ai_papers_new_today_x"],
        "prompt_text": "ELI5 the new papers.",
        "schedule": "30 7 * * *",
        "max_rows": 25,
        "email_address": "",
        "disabled": False,
        "model_id": LOCAL_MODEL_ID,
        "skip_on_empty": skip_on_empty,
    }


# ===========================================================================
# 1 - Ingestion
# ===========================================================================


class TestIngestion:

    def _run_script_with_readme(self, md_text):
        import json
        spec = json.load(open(SCRIPT_PATH))
        b64 = base64.b64encode(md_text.encode()).decode()
        captured = {}

        def fake_get(url, **kw):
            m = MagicMock()
            m.raise_for_status = lambda: None
            m.json = lambda: {"content": b64}
            return m

        ns = {"GENERATE_RESULTS": lambda df: captured.__setitem__("df", df)}
        with patch("requests.get", side_effect=fake_get):
            exec(compile(spec["code"], "ai_papers_github_lists", "exec"), ns)
        return captured["df"]

    def test_single_epoch_and_clean_titles(self):
        df = self._run_script_with_readme(_MOCK_MD)
        # 4 unique papers (5 repos x same readme -> deduped by paper_key).
        assert len(df) == 4, f"expected 4 deduped papers, got {len(df)}"
        assert df["_epoch"].nunique() == 1, "all rows must share one _epoch"
        assert not df["title"].str.startswith("[").any(), (
            "a title started with a bracket - the double-bracket "
            "[[paper](url)] parse leaked into the title"
        )

    def test_masamasa_title_extracted_from_bold(self):
        df = self._run_script_with_readme(_MOCK_MD)
        titles = list(df["title"])
        assert "Alpha Paper: A Study of Things" in titles, (
            "masamasa-style **\"title\"** [[paper](url)] must extract the "
            f"bold title, not 'paper'. Got: {titles}"
        )
        # arxiv ids become the dedup key.
        assert "arxiv:2501.00001" in list(df["paper_key"])


# ===========================================================================
# 2 - Dedup feeder
# ===========================================================================


@pytest.fixture
def papers_index():
    test_dir = PROJECT_ROOT / "indexes" / "_slice_c1_test"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    # Write under a source subdir so the feeder's ai_papers/*/* glob matches.
    (test_dir / "github").mkdir(parents=True)
    t_prior = int(time.time()) - 90000   # ~25h ago
    t_now = int(time.time())
    pd.DataFrame(
        [_paper_row("arxiv:2501.00001", "Alpha", t_prior),
         _paper_row("arxiv:2501.00002", "Beta", t_prior),
         _paper_row("arxiv:2501.00003", "Gamma", t_prior)],
        columns=PAPER_COLUMNS,
    ).to_parquet(test_dir / "github" / "run1.parquet", index=False)
    pd.DataFrame(
        [_paper_row("arxiv:2501.00003", "Gamma", t_now),   # repeat
         _paper_row("arxiv:2501.00009", "Delta", t_now),   # new
         _paper_row("arxiv:2501.00010", "Epsilon", t_now)],  # new
        columns=PAPER_COLUMNS,
    ).to_parquet(test_dir / "github" / "run2.parquet", index=False)
    yield "indexes/_slice_c1_test"
    shutil.rmtree(test_dir, ignore_errors=True)


class TestDedupFeeder:

    def test_surfaces_only_new_papers(self, papers_index, caplog):
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics
        query = _feeder_query().replace("indexes/ai_papers", papers_index)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            df, _job, diag = process_query_with_diagnostics(query)
        assert diag is None
        got = set(df["paper_key"]) if df is not None else set()
        assert got == {"arxiv:2501.00009", "arxiv:2501.00010"}, (
            f"expected only the two NEW papers, got {got}"
        )
        assert "arxiv:2501.00003" not in got, "repeated paper must not resurface"
        offending = [r for r in caplog.records if "filter not applicable on this pass" in r.getMessage()]
        assert not offending, f"feeder logged a pandas-query error: {offending}"


# ===========================================================================
# 3 - skip_on_empty
# ===========================================================================


class TestSkipOnEmpty:

    def _run_empty(self, skip_on_empty):
        """Run an AG whose feeders all come back empty; return (result,
        flags) where flags records LLM + failure-email + breaker calls."""
        claude = {"n": 0}
        router = {"n": 0}

        def claude_fail(*a, **k):
            claude["n"] += 1
            raise AssertionError("MONEY LEAK: Claude called on an empty diff AG")

        def router_fail(*a, **k):
            router["n"] += 1
            raise AssertionError("MONEY LEAK: router called on an empty diff AG")

        fail_email = MagicMock()
        trip_breaker = MagicMock()
        with patch("alert_groups.dispatcher.call_messages_create", claude_fail), \
             patch("analyzers.llm_router.call_llm", router_fail), \
             patch.object(AlertGroupDispatcher, "_execute_feeder_query_now", return_value=None), \
             patch.object(AlertGroupDispatcher, "_maybe_send_failure_email", fail_email), \
             patch.object(AlertGroupDispatcher, "_maybe_trip_circuit_breaker", trip_breaker), \
             patch.object(AlertGroupDispatcher, "_send_html_email", MagicMock()), \
             patch.object(AlertGroupDispatcher, "_send_plain_email", MagicMock()):
            d = AlertGroupDispatcher()
            result = d.run(_ag(skip_on_empty=skip_on_empty), force=True)
        return result, claude, router, fail_email, trip_breaker

    def test_skip_on_empty_true_skips_cleanly(self):
        result, claude, router, fail_email, trip_breaker = self._run_empty(True)
        assert result.status == "skipped", f"expected skipped, got {result.status}"
        assert claude["n"] == 0 and router["n"] == 0, "no LLM call on an empty day"
        assert not fail_email.called, "skip_on_empty must NOT send a failure email"
        assert not trip_breaker.called, "skip_on_empty must NOT trip the breaker"

    def test_skip_on_empty_false_preserves_error_behavior(self):
        result, claude, router, fail_email, trip_breaker = self._run_empty(False)
        assert result.status == "error", (
            "without skip_on_empty the historical empty-feeder error must "
            f"remain, got {result.status}"
        )
        assert trip_breaker.called, "the error path must still tick the breaker"


# ===========================================================================
# 4 - AG config + store round-trip
# ===========================================================================


class TestAlertGroupConfig:

    def test_ag_pins_local_model_skip_disabled(self):
        data = yaml.safe_load(AG_YAML.read_text())
        assert data["model_id"] == LOCAL_MODEL_ID
        assert data["skip_on_empty"] is True
        assert data["disabled"] is True
        assert not data.get("output_kind"), "no output_kind - a digest is not a pick"
        assert "no json" in data["prompt_text"].lower()

    def test_ag_round_trips_with_skip_on_empty(self, tmp_path):
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
        loaded = store.get_group("ai_paper_diffs_brief")
        assert loaded["model_id"] == LOCAL_MODEL_ID
        assert loaded["skip_on_empty"] is True

    def test_feeder_is_a_feeder_and_keys_on_paper_key(self):
        data = yaml.safe_load(FEEDER_YAML.read_text())
        assert data["purpose"] == "alert_group_feeder"
        assert "dedup paper_key" in data["query"]
        assert 'relative_time("-30d")' in data["query"]
