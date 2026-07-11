"""
Tests for functionality/embedding_sweeper.py - slice 3 of Phase 1.

Covers:
  * Source discovery (excludes sidecars, IMMUTABLE/, logs/, hidden files)
  * Text-column discovery (string types only; _epoch et al. excluded)
  * Text extraction (multi-column join, None handling)
  * End-to-end embed_source against a temp parquet
  * Idempotence: second sweep_once is all-fresh
  * Empty source produces an empty sidecar (skipped_empty)
  * Numeric-only source returns skipped_no_text (no sidecar yet)
  * Failures don't stop the sweep - bad file lands in report.failures
  * Model swap forces a re-embed (via expected_model_name on is_stale)

The sweeper uses the real embedder for end-to-end tests and a stub
embedder for the loop-control tests so the suite stays fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from functionality import embedding_sidecar as sc
from functionality import embedding_sweeper as sw


# ── Fixtures + helpers ───────────────────────────────────────────────

@dataclass
class _StubEmbedder:
    """A deterministic stand-in for the real embedder.

    Returns a row-index-based vector so test assertions can verify which
    rows landed in the sidecar without mocking the model.
    """

    model_name: str = "stub/model"
    dim: int = 8

    def encode(self, text: str) -> np.ndarray:
        # Vector based on string hash so identical strings encode identically
        h = abs(hash(text)) % 10_000
        v = np.zeros(self.dim, dtype=np.float32)
        v[h % self.dim] = 1.0
        return v

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i] = self.encode(t)
        return out


@pytest.fixture
def stub_embedder():
    return _StubEmbedder()


def _write_news_parquet(path: Path, n: int = 3) -> None:
    titles = [f"Headline {i}" for i in range(n)]
    bodies = [f"Body content row {i}" for i in range(n)]
    epochs = [1700000000 + i for i in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "_epoch": pa.array(epochs, type=pa.int64()),
            "title": titles,
            "body": bodies,
        }),
        path,
    )


def _write_empty(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "_epoch": pa.array([], type=pa.int64()),
            "title": pa.array([], type=pa.string()),
        }),
        path,
    )


def _write_numeric_only(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "_epoch": pa.array([1, 2], type=pa.int64()),
            "price": [10.0, 20.0],
        }),
        path,
    )


# ── Discovery ────────────────────────────────────────────────────────

class TestDiscovery:
    def test_returns_empty_when_root_missing(self, tmp_path: Path):
        ghost = tmp_path / "no-such-dir"
        assert sw.discover_sources(ghost) == []

    def test_finds_all_parquets_under_root(self, tmp_path: Path):
        root = tmp_path.resolve()
        _write_news_parquet(root / "news/a.parquet", n=1)
        _write_news_parquet(root / "news/sub/b.parquet", n=1)
        _write_news_parquet(root / "other/c.parquet", n=1)
        sources = sw.discover_sources(root)
        rels = sorted(p.relative_to(root).as_posix() for p in sources)
        assert rels == ["news/a.parquet", "news/sub/b.parquet", "other/c.parquet"]

    def test_excludes_sidecar_files(self, tmp_path: Path):
        root = tmp_path.resolve()
        src = root / "news/a.parquet"
        _write_news_parquet(src, n=1)
        # Hand-write a sidecar so we can prove discovery skips it
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=np.zeros((1, 8), dtype=np.float32),
            model_name="stub/model",
        )
        sources = sw.discover_sources(root)
        assert all(not sc.is_sidecar_path(p) for p in sources)
        assert len(sources) == 1
        assert sources[0].name == "a.parquet"

    def test_excludes_immutable_subtree(self, tmp_path: Path):
        root = tmp_path.resolve()
        _write_news_parquet(root / "news/a.parquet", n=1)
        _write_news_parquet(root / "IMMUTABLE/picks.parquet", n=1)
        _write_news_parquet(root / "IMMUTABLE/sub/dir/closures.parquet", n=1)
        sources = sw.discover_sources(root)
        assert len(sources) == 1
        assert sources[0].name == "a.parquet"

    def test_excludes_logs_subtree(self, tmp_path: Path):
        root = tmp_path.resolve()
        _write_news_parquet(root / "news/a.parquet", n=1)
        _write_news_parquet(root / "logs/system/log.parquet", n=1)
        sources = sw.discover_sources(root)
        assert len(sources) == 1
        assert sources[0].name == "a.parquet"

    def test_excludes_hidden_files(self, tmp_path: Path):
        root = tmp_path.resolve()
        _write_news_parquet(root / "news/.tmp_partial.parquet", n=1)
        _write_news_parquet(root / "news/a.parquet", n=1)
        sources = sw.discover_sources(root)
        assert all(not p.name.startswith(".") for p in sources)
        assert len(sources) == 1


# ── Text-column discovery ───────────────────────────────────────────

class TestTextColumnDiscovery:
    def test_picks_string_columns(self, tmp_path: Path):
        src = tmp_path / "x.parquet"
        _write_news_parquet(src, n=2)
        table = pq.read_table(src)
        cols = sw.discover_text_columns(table)
        assert "title" in cols
        assert "body" in cols
        assert "_epoch" not in cols

    def test_skips_numeric_columns(self, tmp_path: Path):
        src = tmp_path / "y.parquet"
        _write_numeric_only(src)
        table = pq.read_table(src)
        cols = sw.discover_text_columns(table)
        assert cols == []


# ── Text extraction ─────────────────────────────────────────────────

class TestTextExtraction:
    def test_concatenates_columns_with_newline(self, tmp_path: Path):
        src = tmp_path / "z.parquet"
        _write_news_parquet(src, n=2)
        table = pq.read_table(src)
        texts = sw.extract_texts(table)
        assert len(texts) == 2
        assert texts[0] == "Headline 0\nBody content row 0"
        assert texts[1] == "Headline 1\nBody content row 1"

    def test_handles_none_cells(self, tmp_path: Path):
        src = tmp_path / "n.parquet"
        src.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({
                "_epoch": pa.array([1, 2], type=pa.int64()),
                "title": pa.array(["yes", None], type=pa.string()),
                "body": pa.array([None, "world"], type=pa.string()),
            }),
            src,
        )
        table = pq.read_table(src)
        texts = sw.extract_texts(table)
        assert texts[0] == "yes\n"
        assert texts[1] == "\nworld"

    def test_explicit_columns_override(self, tmp_path: Path):
        src = tmp_path / "o.parquet"
        _write_news_parquet(src, n=2)
        table = pq.read_table(src)
        texts = sw.extract_texts(table, columns=["title"])
        assert texts[0] == "Headline 0"
        assert texts[1] == "Headline 1"

    def test_returns_empty_strings_when_no_text_columns(self, tmp_path: Path):
        src = tmp_path / "p.parquet"
        _write_numeric_only(src)
        table = pq.read_table(src)
        texts = sw.extract_texts(table)
        assert texts == ["", ""]


# ── embed_source path ────────────────────────────────────────────────

class TestEmbedSource:
    def test_embeds_new_source(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        src = root / "news/a.parquet"
        _write_news_parquet(src, n=3)

        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        result = sweeper.embed_source(src)

        assert result.status == "embedded"
        assert result.rows == 3
        assert result.elapsed_ms >= 0

        frame = sc.read_sidecar(src)
        assert frame is not None
        assert frame.n_rows == 3
        assert frame.dim == stub_embedder.dim
        assert frame.model_name == stub_embedder.model_name
        assert frame.row_ids.tolist() == [0, 1, 2]
        # Epochs round-trip
        assert frame.epochs.tolist() == [1700000000, 1700000001, 1700000002]

    def test_skips_when_sidecar_fresh(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        src = root / "news/a.parquet"
        _write_news_parquet(src, n=2)
        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        first = sweeper.embed_source(src)
        assert first.status == "embedded"
        second = sweeper.embed_source(src)
        assert second.status == "skipped_fresh"
        assert second.rows == 0

    def test_re_embeds_after_model_swap(self, tmp_path: Path):
        root = tmp_path.resolve()
        src = root / "news/a.parquet"
        _write_news_parquet(src, n=2)

        first_embedder = _StubEmbedder(model_name="stub/v1", dim=8)
        sweeper_v1 = sw.EmbeddingSweeper(root, embedder=first_embedder)
        sweeper_v1.embed_source(src)
        frame_v1 = sc.read_sidecar(src)
        assert frame_v1.model_name == "stub/v1"

        # Swap the model - the dim mismatch alone forces a re-embed
        second_embedder = _StubEmbedder(model_name="stub/v2", dim=12)
        sweeper_v2 = sw.EmbeddingSweeper(root, embedder=second_embedder)
        result = sweeper_v2.embed_source(src)
        assert result.status == "embedded"
        frame_v2 = sc.read_sidecar(src)
        assert frame_v2.model_name == "stub/v2"
        assert frame_v2.dim == 12

    def test_empty_source_writes_empty_sidecar(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        src = root / "news/empty.parquet"
        _write_empty(src)
        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        result = sweeper.embed_source(src)
        assert result.status == "skipped_empty"
        # A sidecar IS written for empty sources so the next sweep skips
        # cheaply rather than reading the parquet again
        frame = sc.read_sidecar(src)
        assert frame is not None
        assert frame.n_rows == 0
        assert frame.dim == stub_embedder.dim

    def test_numeric_only_source_returns_no_text(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        src = root / "metrics/numbers.parquet"
        _write_numeric_only(src)
        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        result = sweeper.embed_source(src)
        assert result.status == "skipped_no_text"
        # No sidecar is written for numeric-only sources - a future
        # column-set change might add text columns and we want to embed
        # them on the next sweep
        assert sc.read_sidecar(src) is None

    def test_handles_missing_epoch_column(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        src = root / "news/no_epoch.parquet"
        src.parent.mkdir(parents=True)
        pq.write_table(
            pa.table({"title": ["a", "b"]}),
            src,
        )
        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        result = sweeper.embed_source(src)
        assert result.status == "embedded"
        frame = sc.read_sidecar(src)
        # Epochs default to zero when the source lacks the column
        assert frame.epochs.tolist() == [0, 0]

    def test_corrupt_source_lands_in_failures(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        src = root / "news/bad.parquet"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"not valid parquet")
        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        result = sweeper.embed_source(src)
        assert result.status == "failed"
        assert result.error_class != ""


# ── sweep_once: full-pass orchestration ──────────────────────────────

class TestSweepOnce:
    def test_full_sweep_telemetry(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        _write_news_parquet(root / "news/a.parquet", n=3)
        _write_news_parquet(root / "news/b.parquet", n=2)
        _write_empty(root / "news/empty.parquet")
        _write_numeric_only(root / "metrics/c.parquet")
        # Excluded
        _write_news_parquet(root / "IMMUTABLE/picks.parquet", n=1)
        _write_news_parquet(root / "logs/system/x.parquet", n=1)

        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        report = sweeper.sweep_once()

        assert report.sources_seen == 4  # IMMUTABLE + logs excluded
        assert report.sources_embedded == 2
        assert report.sources_skipped_empty == 1
        assert report.sources_skipped_no_text == 1
        assert report.sources_failed == 0
        assert report.rows_embedded == 5
        assert report.elapsed_ms >= 0
        assert len(report.per_source) == 4

    def test_second_sweep_is_idempotent(self, tmp_path: Path, stub_embedder):
        root = tmp_path.resolve()
        _write_news_parquet(root / "news/a.parquet", n=2)
        _write_empty(root / "news/empty.parquet")
        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        first = sweeper.sweep_once()
        second = sweeper.sweep_once()
        assert first.sources_embedded == 1
        assert second.sources_embedded == 0
        # Both the embedded source AND the empty one are now fresh
        assert second.sources_skipped_fresh == 2
        assert second.rows_embedded == 0

    def test_failed_source_does_not_stop_sweep(
        self, tmp_path: Path, stub_embedder
    ):
        root = tmp_path.resolve()
        # Mix one bad source with two good ones; the good ones must
        # still get embedded and the bad one lands in failures.
        good1 = root / "news/good1.parquet"
        good2 = root / "news/good2.parquet"
        bad = root / "news/bad.parquet"
        _write_news_parquet(good1, n=1)
        _write_news_parquet(good2, n=1)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not parquet")

        sweeper = sw.EmbeddingSweeper(root, embedder=stub_embedder)
        report = sweeper.sweep_once()

        assert report.sources_seen == 3
        assert report.sources_embedded == 2
        assert report.sources_failed == 1
        assert report.rows_embedded == 2
        assert len(report.failures) == 1
        assert report.failures[0].source == bad
        # The good sources got their sidecars
        assert sc.read_sidecar(good1) is not None
        assert sc.read_sidecar(good2) is not None


# ── Production embedder integration ──────────────────────────────────

class TestProductionEmbedderIntegration:
    """Smoke-checks the sweeper against the real all-MiniLM-L6-v2 model.

    Just one source with two rows, to keep the test fast - the embedder
    test pack already validates the model thoroughly. Here we're proving
    the sweeper + sidecar + embedder compose end-to-end.
    """

    def test_real_embedder_writes_384_dim_sidecar(self, tmp_path: Path):
        from analyzers.embedder import get_embedder
        root = tmp_path.resolve()
        src = root / "news/real.parquet"
        _write_news_parquet(src, n=2)
        sweeper = sw.EmbeddingSweeper(root, embedder=get_embedder())
        report = sweeper.sweep_once()
        assert report.sources_embedded == 1
        assert report.rows_embedded == 2
        frame = sc.read_sidecar(src)
        assert frame.dim == 384
        assert frame.n_rows == 2
        # Vectors are L2-normalized (the embedder guarantees it)
        norms = np.linalg.norm(frame.vectors, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-4)
