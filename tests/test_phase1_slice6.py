"""
Tests for Phase 1 / Bet 2 slice 6 - sidecar fast path.

Covers:
  * _try_sidecar_lookup: applicable case returns the precomputed matrix
  * Fall-back conditions: missing _source_file, missing sidecar, model
    mismatch, dim mismatch, row-count mismatch (filter applied),
    source-file moved/deleted, stale sidecar, sidecar smaller than source
  * field= kwarg always uses slow path (sidecars embed the default
    text concatenation, not a single field)
  * Result equivalence: fast path's sims match slow path's within
    float tolerance - no behavioral drift
  * Multi-source case: index= over a glob across multiple files

Tests build a temp indexes/ tree per test, populate sidecars via the
real sweeper + embedder, then patch ``indexes_root`` to point at the
temp dir so _source_file relative paths resolve correctly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analyzers.embedder import get_embedder
from functionality import embedding_sidecar as sc
from functionality.embedding_sweeper import EmbeddingSweeper
from handlers.SemanticHandler import (
    _try_sidecar_lookup,
    dedup_semantic,
    nearest,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _build_news_index(root: Path, files: dict[str, list[str]]):
    """Write source parquets at ``root/<file>``; populate sidecars via
    the real sweeper. Returns the embedder instance used.
    """
    for fname, titles in files.items():
        path = root / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({
                "_epoch": pa.array(list(range(len(titles))), type=pa.int64()),
                "title": titles,
            }),
            path,
        )
    embedder = get_embedder()
    sweeper = EmbeddingSweeper(root, embedder=embedder)
    sweeper.sweep_once()
    return embedder


def _df_for_source(root: Path, fname: str, *, drop_first: bool = False) -> pd.DataFrame:
    """Build a DataFrame that matches what the index= loader would
    return for a single source. Optionally drop the first row to
    simulate an upstream filter.
    """
    df = pd.read_parquet(root / fname)
    df["_source_file"] = fname
    if drop_first:
        df = df.iloc[1:].reset_index(drop=True)
    return df


@pytest.fixture
def patched_indexes_root(tmp_path):
    """Point GlobalSettings.indexes_root at the test's tmp_path so
    _source_file relative paths resolve correctly during fast-path
    lookup. Restores the original on teardown.
    """
    import global_settings
    settings = global_settings.get_settings()
    orig = settings.get("indexes_root")
    settings.set("indexes_root", str(tmp_path))
    yield tmp_path.resolve()
    settings.set("indexes_root", orig)


# ── _try_sidecar_lookup direct tests ────────────────────────────────

class TestTrySidecarLookup:
    def test_applicable_returns_precomputed_matrix(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["Fed pauses", "Apple news", "Nvidia GPU"],
        })
        df = _df_for_source(root, "news/a.parquet")
        result = _try_sidecar_lookup(df, embedder)
        assert result is not None
        assert result.shape == (3, embedder.dim)
        assert result.dtype == np.float32
        # Vectors are L2-normalized
        norms = np.linalg.norm(result, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3)

    def test_no_source_file_column_returns_none(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y"],
        })
        df = pd.DataFrame({"title": ["x", "y"]})  # No _source_file
        assert _try_sidecar_lookup(df, embedder) is None

    def test_missing_sidecar_returns_none(self, patched_indexes_root):
        root = patched_indexes_root
        # Build source but DON'T sweep - no sidecar exists
        path = root / "news" / "lonely.parquet"
        path.parent.mkdir(parents=True)
        pq.write_table(
            pa.table({"_epoch": pa.array([1], type=pa.int64()), "title": ["x"]}),
            path,
        )
        df = _df_for_source(root, "news/lonely.parquet")
        embedder = get_embedder()
        assert _try_sidecar_lookup(df, embedder) is None

    def test_model_mismatch_returns_none(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y"],
        })
        df = _df_for_source(root, "news/a.parquet")
        # Stub embedder with a different model name
        stub = MagicMock()
        stub.model_name = "totally/different-model"
        stub.dim = embedder.dim
        assert _try_sidecar_lookup(df, stub) is None

    def test_dim_mismatch_returns_none(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y"],
        })
        df = _df_for_source(root, "news/a.parquet")
        stub = MagicMock()
        stub.model_name = embedder.model_name
        stub.dim = 768  # Mismatched
        assert _try_sidecar_lookup(df, stub) is None

    def test_row_count_mismatch_returns_none(self, patched_indexes_root):
        # Simulates an upstream filter dropping rows - fast path can't
        # tell which sidecar entries map to which df rows, so it bails.
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["row0", "row1", "row2"],
        })
        df = _df_for_source(root, "news/a.parquet", drop_first=True)
        assert len(df) == 2  # Source has 3, df has 2
        assert _try_sidecar_lookup(df, embedder) is None

    def test_missing_source_file_returns_none(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y"],
        })
        df = _df_for_source(root, "news/a.parquet")
        # Delete the source - sidecar still exists but source is gone
        (root / "news" / "a.parquet").unlink()
        assert _try_sidecar_lookup(df, embedder) is None

    def test_stale_sidecar_returns_none(self, patched_indexes_root):
        # Sidecar is_stale (source mtime > sidecar mtime) → fall back.
        # Simulates a source rewrite between sweeps.
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y"],
        })
        df = _df_for_source(root, "news/a.parquet")
        import os
        import time as _t
        sidecar_mtime = sc.sidecar_path_for(
            root / "news" / "a.parquet"
        ).stat().st_mtime
        # Bump source mtime past sidecar mtime
        future = sidecar_mtime + 100
        os.utime(root / "news" / "a.parquet", (future, future))
        # The mtime check inside is_stale will trip
        assert _try_sidecar_lookup(df, embedder) is None

    def test_none_source_file_value_returns_none(self, patched_indexes_root):
        # Mid-pipe operation could null out _source_file - defensive.
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y"],
        })
        df = _df_for_source(root, "news/a.parquet")
        df.loc[0, "_source_file"] = None
        assert _try_sidecar_lookup(df, embedder) is None

    def test_multi_source_case(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["Fed pauses", "Apple iPhone"],
            "news/b.parquet": ["Nvidia GPU", "AMD chips", "Intel CPU"],
        })
        df_a = _df_for_source(root, "news/a.parquet")
        df_b = _df_for_source(root, "news/b.parquet")
        df = pd.concat([df_a, df_b], ignore_index=True)
        result = _try_sidecar_lookup(df, embedder)
        assert result is not None
        assert result.shape == (5, embedder.dim)


# ── End-to-end: nearest() with fast path ────────────────────────────

class TestNearestFastPath:
    def test_fast_path_skips_encode_batch(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["Fed pauses rates", "Apple news", "Nvidia GPU"],
        })
        df = _df_for_source(root, "news/a.parquet")

        # Patch encode_batch to fail loud if the slow path is taken
        with patch.object(embedder, "encode_batch") as mock_encode:
            mock_encode.side_effect = AssertionError(
                "slow-path encode_batch should NOT be called when "
                "sidecars cover the input"
            )
            out = nearest(df, "federal reserve")
            mock_encode.assert_not_called()
            assert out is not None
            assert "_similarity" in out.columns

    def test_field_kwarg_forces_slow_path(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["Fed pauses rates", "Apple news", "Nvidia GPU"],
        })
        df = _df_for_source(root, "news/a.parquet")

        # field= forces the slow path (sidecar embeds default-concat,
        # not a specific field)
        with patch.object(embedder, "encode_batch", wraps=embedder.encode_batch) as mock_encode:
            nearest(df, "federal reserve", field="title")
            mock_encode.assert_called_once()

    def test_fast_path_results_match_slow_path(self, patched_indexes_root):
        # Result equivalence - running both paths on the same input must
        # produce identical (within float tolerance) similarity rankings.
        # Catches the most dangerous regression class: silently wrong
        # rankings from a misaligned fast path.
        root = patched_indexes_root
        _build_news_index(root, {
            "news/a.parquet": [
                "Federal Reserve pauses interest rate hikes",
                "Apple announces new iPhone launch",
                "Nvidia GPU demand soars",
                "FOMC holds rates steady this month",
                "Polymarket traders bet on rate cut",
            ],
        })
        df_fast = _df_for_source(root, "news/a.parquet")
        # Build an identical df WITHOUT _source_file → forces slow path
        df_slow = df_fast.drop(columns=["_source_file"]).copy()

        result_fast = nearest(df_fast, "fed rate decision", topk=10)
        result_slow = nearest(df_slow, "fed rate decision", topk=10)

        # Same titles in the same order
        assert (
            result_fast["title"].tolist() == result_slow["title"].tolist()
        )
        # Similarities match within a tight tolerance (the same vectors
        # produce the same dot product modulo float32 jitter)
        assert np.allclose(
            result_fast["_similarity"].to_numpy(),
            result_slow["_similarity"].to_numpy(),
            atol=1e-5,
        )


# ── End-to-end: dedup_semantic() with fast path ─────────────────────

class TestDedupSemanticFastPath:
    def test_fast_path_skips_encode_batch(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x1", "x2", "x3"],
        })
        df = _df_for_source(root, "news/a.parquet")

        with patch.object(embedder, "encode_batch") as mock_encode:
            mock_encode.side_effect = AssertionError(
                "slow-path encode_batch should NOT be called when "
                "sidecars cover the input"
            )
            out = dedup_semantic(df, threshold=0.99)
            mock_encode.assert_not_called()
            assert out is not None

    def test_field_kwarg_forces_slow_path(self, patched_indexes_root):
        root = patched_indexes_root
        embedder = _build_news_index(root, {
            "news/a.parquet": ["x", "y", "z"],
        })
        df = _df_for_source(root, "news/a.parquet")
        with patch.object(embedder, "encode_batch", wraps=embedder.encode_batch) as mock_encode:
            dedup_semantic(df, threshold=0.99, field="title")
            mock_encode.assert_called_once()

    def test_fast_path_dedup_matches_slow_path(self, patched_indexes_root):
        # Same news as the nearest test; verify dedup result equivalence.
        root = patched_indexes_root
        _build_news_index(root, {
            "news/a.parquet": [
                "Federal Reserve pauses interest rate hikes",
                "Apple announces new iPhone launch",
                "Nvidia GPU demand soars",
                "FOMC holds rates steady this month",
                "Polymarket traders bet on rate cut",
            ],
        })
        df_fast = _df_for_source(root, "news/a.parquet")
        df_slow = df_fast.drop(columns=["_source_file"]).copy()

        # Use a moderate threshold so paraphrase pairs collapse
        result_fast = dedup_semantic(df_fast, threshold=0.40)
        result_slow = dedup_semantic(df_slow, threshold=0.40)

        # Same surviving titles in the same order
        assert (
            result_fast["title"].tolist() == result_slow["title"].tolist()
        )
