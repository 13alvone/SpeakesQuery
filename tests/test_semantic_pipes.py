"""
Tests for handlers/SemanticHandler.py and the | nearest / | dedup_semantic
SPQL pipes (slice 4 of Phase 1 / Bet 2).

Two layers of coverage:

  * Unit-level tests against SemanticHandler.nearest / dedup_semantic
    (directly callable Python functions). These cover edge cases that
    are awkward to express through YAML - bad threshold, missing field,
    no text columns, empty DataFrame, and the actual semantic ranking.

  * Integration tests through process_query that drive the full ANTLR →
    listener → handler stack. These prove the grammar + dispatch + handler
    composition works end-to-end on real fixture parquets.

A grammar-parity drift guard (`TestGrammarParity`) confirms that the new
pipes' tokens exist in `lexers/speakesQuery.g4` AND are dispatched by the
listener's `_command_map` - catches the very first regression mode if a
future grammar regen drops them.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from handlers.SemanticHandler import (
    SemanticPipeError,
    dedup_semantic,
    nearest,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def news_df() -> pd.DataFrame:
    """A tiny news-shaped DataFrame for ranking + dedup tests."""
    return pd.DataFrame({
        "title": [
            "Federal Reserve pauses interest rate hikes",
            "FOMC holds rates steady this month",
            "Apple announces new iPhone launch",
            "Nvidia GPU demand soars on AI hype",
            "iPhone 17 hits shelves next week",
        ],
        "_epoch": [1700000000, 1700000010, 1700000020, 1700000030, 1700000040],
    })


@pytest.fixture
def numeric_only_df() -> pd.DataFrame:
    """A DataFrame with no text columns - exercises the error path."""
    return pd.DataFrame({"x": [1, 2, 3], "_epoch": [10, 20, 30]})


# ── nearest: ranking behavior ────────────────────────────────────────

class TestNearestRanking:
    def test_adds_similarity_column(self, news_df):
        result = nearest(news_df, "fed pause")
        assert "_similarity" in result.columns
        assert len(result) == len(news_df)

    def test_sorts_descending_by_similarity(self, news_df):
        result = nearest(news_df, "fed pause")
        sims = result["_similarity"].tolist()
        assert sims == sorted(sims, reverse=True)

    def test_paraphrase_outranks_unrelated(self, news_df):
        # "fed pause" → top match is one of the two FOMC/Fed rows,
        # not an iPhone or GPU row.
        result = nearest(news_df, "federal reserve interest rate decision")
        top = result.iloc[0]["title"].lower()
        assert "fed" in top or "fomc" in top or "rate" in top

    def test_topk_limits_output(self, news_df):
        result = nearest(news_df, "fed pause", topk=2)
        assert len(result) == 2

    def test_topk_zero_returns_all(self, news_df):
        result = nearest(news_df, "fed pause", topk=0)
        assert len(result) == len(news_df)

    def test_topk_none_returns_all(self, news_df):
        result = nearest(news_df, "fed pause", topk=None)
        assert len(result) == len(news_df)

    def test_threshold_filters_below_cutoff(self, news_df):
        # A high threshold (0.95) should drop everything that isn't a
        # near-exact match.
        result = nearest(news_df, "completely off-topic xyz123", threshold=0.95)
        assert len(result) == 0

    def test_field_kwarg_uses_only_named_column(self, news_df):
        # When field=title is supplied, the _epoch column shouldn't
        # influence the embedding. Sanity check: result is the same as
        # the default (default already filters _epoch as numeric).
        a = nearest(news_df, "fed pause", field="title", topk=3)
        b = nearest(news_df, "fed pause", topk=3)
        # At minimum, the top-3 sets should overlap heavily; we don't
        # assert exact equality because pandas sorting is stable but
        # vector ties may break differently.
        assert set(a.iloc[:3]["title"]) == set(b.iloc[:3]["title"])


# ── nearest: edge cases ──────────────────────────────────────────────

class TestNearestEdgeCases:
    def test_empty_input_returns_empty_with_similarity_column(self):
        df = pd.DataFrame({"title": pd.Series([], dtype=object)})
        result = nearest(df, "anything")
        assert len(result) == 0
        assert "_similarity" in result.columns

    def test_empty_query_raises(self, news_df):
        with pytest.raises(SemanticPipeError):
            nearest(news_df, "")

    def test_none_query_raises(self, news_df):
        with pytest.raises(SemanticPipeError):
            nearest(news_df, None)  # type: ignore[arg-type]

    def test_threshold_out_of_range_raises(self, news_df):
        with pytest.raises(SemanticPipeError, match="out of range"):
            nearest(news_df, "x", threshold=1.5)
        with pytest.raises(SemanticPipeError, match="out of range"):
            nearest(news_df, "x", threshold=-2.0)

    def test_threshold_non_numeric_raises(self, news_df):
        with pytest.raises(SemanticPipeError, match="must be a number"):
            nearest(news_df, "x", threshold="high")  # type: ignore[arg-type]

    def test_missing_field_raises(self, news_df):
        with pytest.raises(SemanticPipeError, match="does not exist"):
            nearest(news_df, "x", field="bogus_column")

    def test_no_text_columns_raises(self, numeric_only_df):
        with pytest.raises(SemanticPipeError, match="No text columns"):
            nearest(numeric_only_df, "anything")


# ── dedup_semantic ───────────────────────────────────────────────────

class TestDedupSemantic:
    def test_drops_near_duplicates(self, news_df):
        # all-MiniLM-L6-v2 rates the Fed/FOMC paraphrase pair at ~0.41
        # and the iPhone pair at ~0.40 - moderate but real similarity.
        # At threshold=0.40 we expect to lose at least 1-2 rows.
        result = dedup_semantic(news_df, threshold=0.40)
        assert len(result) < len(news_df)
        assert len(result) >= 2  # at least Fed-cluster + iPhone-cluster reps

    def test_keeps_first_in_each_cluster(self, news_df):
        result = dedup_semantic(news_df, threshold=0.40)
        # The first row in the input is always kept (no prior rows).
        assert result.iloc[0]["title"] == news_df.iloc[0]["title"]

    def test_high_threshold_keeps_everything(self, news_df):
        # At 0.99, only literal duplicates collide.
        result = dedup_semantic(news_df, threshold=0.99)
        assert len(result) == len(news_df)

    def test_low_threshold_collapses_all(self, news_df):
        # At 0.0, everything is "similar enough" → only the first row
        # survives.
        result = dedup_semantic(news_df, threshold=0.0)
        assert len(result) == 1

    def test_empty_input(self):
        result = dedup_semantic(pd.DataFrame({"title": pd.Series([], dtype=object)}))
        assert len(result) == 0

    def test_threshold_out_of_range_raises(self, news_df):
        with pytest.raises(SemanticPipeError, match="out of range"):
            dedup_semantic(news_df, threshold=1.5)

    def test_missing_field_raises(self, news_df):
        with pytest.raises(SemanticPipeError, match="does not exist"):
            dedup_semantic(news_df, field="bogus")

    def test_field_kwarg_constrains_dedup_to_one_column(self, news_df):
        # Identical title path - the default extractor pulls all string
        # cols (title only here, since _epoch is int). field=title is
        # equivalent.
        a = dedup_semantic(news_df, threshold=0.7)
        b = dedup_semantic(news_df, threshold=0.7, field="title")
        assert len(a) == len(b)


# ── End-to-end through process_query ─────────────────────────────────

class TestEndToEndQuery:
    """Drive the full ANTLR → listener → handler stack on real fixture
    parquets. Exercises grammar acceptance, listener dispatch, and the
    handler in one shot.
    """

    @pytest.fixture
    def fixture_path(self):
        return "indexes/default_test/output_parquets/test0.parquet"

    def test_nearest_basic(self, fixture_path):
        from query_engine.CmdExecutionBackend import process_query
        q = f'index="{fixture_path}" | nearest "debug log" topk=3'
        df, _ = process_query(q)
        assert df is not None
        assert "_similarity" in df.columns
        assert len(df) == 3

    def test_nearest_with_threshold_drops_to_empty(self, fixture_path):
        # process_query returns (None, None) for an empty result by
        # project convention (CmdExecutionBackend line 135). The empty-
        # frame return shape is covered at the unit level; here we just
        # confirm the integration path doesn't crash and produces an
        # empty (None) result when the threshold is high enough.
        from query_engine.CmdExecutionBackend import process_query
        q = f'index="{fixture_path}" | nearest "totally unrelated zxy" threshold=0.95'
        df, _ = process_query(q)
        assert df is None  # empty result is collapsed to None upstream

    def test_nearest_with_field_kwarg(self, fixture_path):
        from query_engine.CmdExecutionBackend import process_query
        q = f'index="{fixture_path}" | nearest "debug" topk=2 field=message'
        df, _ = process_query(q)
        assert df is not None
        assert "_similarity" in df.columns
        assert len(df) <= 2

    def test_dedup_semantic_basic(self, fixture_path):
        from query_engine.CmdExecutionBackend import process_query
        q = f'index="{fixture_path}" | dedup_semantic threshold=0.99'
        df, _ = process_query(q)
        assert df is not None
        # Threshold 0.99 should keep most/all (no near-exact dupes in fixture)
        assert len(df) >= 4

    def test_pipe_composition_with_head(self, fixture_path):
        # nearest sorts; head trims further. Sorting should be preserved.
        from query_engine.CmdExecutionBackend import process_query
        q = f'index="{fixture_path}" | nearest "error tracking" topk=5 | head 2'
        df, _ = process_query(q)
        assert df is not None
        assert len(df) == 2
        sims = df["_similarity"].tolist()
        assert sims == sorted(sims, reverse=True)


# ── Grammar / dispatch parity drift guards ───────────────────────────

class TestGrammarParity:
    """If a future grammar regen drops or renames the new tokens, these
    tests fail loudly so the regression is caught at PR time, not in
    production.
    """

    def test_grammar_declares_nearest_token(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert re.search(r"\bNEAREST\s*:\s*'nearest'", g4)
        assert re.search(r"\bDEDUP_SEMANTIC\s*:\s*'dedup_semantic'", g4)
        assert re.search(r"\bTOPK\s*:\s*'topk'", g4)
        assert re.search(r"\bTHRESHOLD\s*:\s*'threshold'", g4)

    def test_grammar_has_directive_rules(self):
        g4 = (
            Path(__file__).parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text()
        assert "NEAREST DOUBLE_QUOTED_STRING" in g4
        assert "DEDUP_SEMANTIC " in g4 or "DEDUP_SEMANTIC\n" in g4

    def test_listener_dispatches_both_pipes(self):
        # Build a minimal listener and check the command map covers our keys.
        from lexers.speakesQueryListener import speakesQueryListener
        listener = speakesQueryListener("")
        assert "nearest" in listener._command_map
        assert "dedup_semantic" in listener._command_map

    def test_grammar_vocab_exposes_new_commands(self):
        # The grammar_vocab module parses speakesQuery.g4 and exposes the
        # command list to the autocomplete / UI. Both new pipes must appear
        # so the in-app `/api/grammar/vocab` endpoint surfaces them in the
        # console autocomplete.
        from lexers.grammar_vocab import get_vocab
        vocab = get_vocab()
        names = {c.get("name") for c in vocab.get("commands", [])}
        assert "nearest" in names, (
            "nearest missing from grammar_vocab - autocomplete won't surface "
            "it. Check lexers/grammar_vocab.py token-extraction logic."
        )
        assert "dedup_semantic" in names, (
            "dedup_semantic missing from grammar_vocab - same issue."
        )
