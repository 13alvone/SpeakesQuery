"""
Tests for functionality/embedding_sidecar.py - slice 2 of Phase 1.

Covers:
  * Sidecar path derivation (basic + idempotent + classifier)
  * Write/read round-trip with exact-equality vector check
  * Parquet key-value metadata round-trip (model_name, dim, created_epoch)
  * Empty input shapes (shapeless + pre-shaped (0, dim))
  * Schema validation (length mismatches, ndim, dim disagreement)
  * Drift detection (missing sidecar, source mtime newer, model mismatch,
    dim mismatch)
  * Atomic write - temp file is cleaned up on success and on simulated
    failure mid-write; existing sidecar survives the failed write.

The vectors here are random (not real embeddings); we're testing the
storage layer, not the model. Tests run in tmp_path so they don't
collide with project state.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from functionality import embedding_sidecar as sc


# ── Helpers ──────────────────────────────────────────────────────────

def _write_dummy_source(path: Path, n: int = 3) -> None:
    """Write a minimal source parquet so the sidecar has a sibling."""
    tbl = pa.table(
        {"_epoch": pa.array(list(range(100, 100 + n)), type=pa.int64())}
    )
    pq.write_table(tbl, path)


def _normalized(rows: int, dim: int = 384, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = rng.standard_normal((rows, dim)).astype(np.float32)
    arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
    return arr


# ── Path derivation ──────────────────────────────────────────────────

class TestPathDerivation:
    def test_basic_derivation(self, tmp_path: Path):
        src = tmp_path / "data.parquet"
        sidecar = sc.sidecar_path_for(src)
        assert sidecar.name == "data.embeddings.parquet"
        assert sidecar.parent == tmp_path

    def test_idempotent_on_sidecar_path(self, tmp_path: Path):
        src = tmp_path / "data.parquet"
        sidecar = sc.sidecar_path_for(src)
        assert sc.sidecar_path_for(sidecar) == sidecar
        # Doubly-applied is still stable
        assert sc.sidecar_path_for(sc.sidecar_path_for(sidecar)) == sidecar

    def test_is_sidecar_path_classifier(self, tmp_path: Path):
        assert not sc.is_sidecar_path(tmp_path / "data.parquet")
        assert sc.is_sidecar_path(tmp_path / "data.embeddings.parquet")


# ── Write / read round-trip ──────────────────────────────────────────

class TestWriteRead:
    def test_roundtrip_exact(self, tmp_path: Path):
        src = tmp_path / "data.parquet"
        _write_dummy_source(src, n=4)
        embeds = _normalized(4, dim=384, seed=42)

        sc.write_sidecar(
            src,
            row_ids=[0, 1, 2, 3],
            epochs=[100, 101, 102, 103],
            embeddings=embeds,
            model_name="test/dummy",
        )
        frame = sc.read_sidecar(src)

        assert frame is not None
        assert frame.n_rows == 4
        assert frame.dim == 384
        assert frame.model_name == "test/dummy"
        assert frame.row_ids.tolist() == [0, 1, 2, 3]
        assert frame.epochs.tolist() == [100, 101, 102, 103]
        # Vector equality is exact; round-trip through parquet preserves
        # float32 bit-for-bit.
        assert np.array_equal(frame.vectors, embeds)
        assert frame.vectors.dtype == np.float32
        assert frame.vectors.flags["C_CONTIGUOUS"]

    def test_metadata_preserved(self, tmp_path: Path):
        src = tmp_path / "x.parquet"
        _write_dummy_source(src, n=2)
        sc.write_sidecar(
            src,
            row_ids=[0, 1],
            epochs=[10, 20],
            embeddings=_normalized(2, dim=384),
            model_name="my/model",
            created_epoch=1700000000,
        )
        frame = sc.read_sidecar(src)
        assert frame.model_name == "my/model"
        assert frame.dim == 384
        assert frame.created_epoch == 1700000000

    def test_read_returns_none_when_missing(self, tmp_path: Path):
        src = tmp_path / "no_sidecar.parquet"
        _write_dummy_source(src)
        assert sc.read_sidecar(src) is None

    def test_overwrite_replaces_existing(self, tmp_path: Path):
        src = tmp_path / "y.parquet"
        _write_dummy_source(src, n=1)
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=_normalized(1, dim=384, seed=1),
            model_name="m1",
        )
        first = sc.read_sidecar(src)

        new_embeds = _normalized(2, dim=384, seed=2)
        sc.write_sidecar(
            src, row_ids=[0, 1], epochs=[1, 2],
            embeddings=new_embeds,
            model_name="m2",
        )
        second = sc.read_sidecar(src)

        assert first.n_rows == 1
        assert second.n_rows == 2
        assert second.model_name == "m2"
        assert np.array_equal(second.vectors, new_embeds)

    def test_empty_shapeless_input_with_explicit_dim(self, tmp_path: Path):
        src = tmp_path / "empty1.parquet"
        _write_dummy_source(src, n=0)
        sc.write_sidecar(
            src, row_ids=[], epochs=[],
            embeddings=np.zeros((0,), dtype=np.float32),
            model_name="m", model_dim=384,
        )
        frame = sc.read_sidecar(src)
        assert frame.n_rows == 0
        assert frame.dim == 384
        assert frame.vectors.shape == (0, 384)

    def test_empty_shaped_input_infers_dim(self, tmp_path: Path):
        src = tmp_path / "empty2.parquet"
        _write_dummy_source(src, n=0)
        sc.write_sidecar(
            src, row_ids=[], epochs=[],
            embeddings=np.zeros((0, 768), dtype=np.float32),
            model_name="m",
        )
        frame = sc.read_sidecar(src)
        assert frame.n_rows == 0
        assert frame.dim == 768


# ── Schema validation ────────────────────────────────────────────────

class TestSchemaValidation:
    def test_row_id_epoch_length_mismatch_raises(self, tmp_path: Path):
        src = tmp_path / "v.parquet"
        _write_dummy_source(src, n=2)
        with pytest.raises(ValueError, match="length"):
            sc.write_sidecar(
                src, row_ids=[0, 1], epochs=[1, 2, 3],
                embeddings=_normalized(2, 384), model_name="m",
            )

    def test_embedding_row_count_mismatch_raises(self, tmp_path: Path):
        src = tmp_path / "v2.parquet"
        _write_dummy_source(src, n=2)
        with pytest.raises(ValueError, match="rows"):
            sc.write_sidecar(
                src, row_ids=[0, 1], epochs=[1, 2],
                embeddings=_normalized(3, 384), model_name="m",
            )

    def test_embeddings_must_be_2d_when_nonempty(self, tmp_path: Path):
        src = tmp_path / "v3.parquet"
        _write_dummy_source(src, n=2)
        with pytest.raises(ValueError, match="2-D"):
            sc.write_sidecar(
                src, row_ids=[0, 1], epochs=[1, 2],
                embeddings=np.array([1.0, 2.0], dtype=np.float32),  # 1-D
                model_name="m",
            )

    def test_dim_required_for_shapeless_empty(self, tmp_path: Path):
        src = tmp_path / "v4.parquet"
        _write_dummy_source(src, n=0)
        with pytest.raises(ValueError, match="model_dim is required"):
            sc.write_sidecar(
                src, row_ids=[], epochs=[],
                embeddings=np.zeros((0,), dtype=np.float32),
                model_name="m",
            )

    def test_explicit_model_dim_must_match_shape(self, tmp_path: Path):
        src = tmp_path / "v5.parquet"
        _write_dummy_source(src, n=2)
        with pytest.raises(ValueError, match="disagrees"):
            sc.write_sidecar(
                src, row_ids=[0, 1], epochs=[1, 2],
                embeddings=_normalized(2, 384),
                model_name="m", model_dim=256,
            )


# ── Drift detection ──────────────────────────────────────────────────

class TestStaleness:
    def test_missing_sidecar_is_stale(self, tmp_path: Path):
        src = tmp_path / "s.parquet"
        _write_dummy_source(src)
        assert sc.is_stale(src) is True

    def test_fresh_sidecar_not_stale(self, tmp_path: Path):
        src = tmp_path / "s2.parquet"
        _write_dummy_source(src, n=2)
        sc.write_sidecar(
            src, row_ids=[0, 1], epochs=[1, 2],
            embeddings=_normalized(2, 384), model_name="m",
        )
        assert sc.is_stale(src) is False

    def test_source_touched_after_write_is_stale(self, tmp_path: Path):
        src = tmp_path / "s3.parquet"
        _write_dummy_source(src, n=1)
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=_normalized(1, 384), model_name="m",
        )
        assert sc.is_stale(src) is False
        # Force the source's mtime to bump above the sidecar's. We can't
        # rely on a sleep here without slowing the test pack; use os.utime
        # to set the source timestamp 5 seconds in the future.
        sidecar = sc.sidecar_path_for(src)
        future = sidecar.stat().st_mtime + 5
        import os as _os
        _os.utime(src, (future, future))
        assert sc.is_stale(src) is True

    def test_missing_source_keeps_existing_sidecar(self, tmp_path: Path):
        src = tmp_path / "s4.parquet"
        _write_dummy_source(src, n=1)
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=_normalized(1, 384), model_name="m",
        )
        src.unlink()  # source vanishes (transient I/O glitch case)
        # Conservative: don't trigger a re-embed against nothing.
        assert sc.is_stale(src) is False

    def test_stale_on_model_name_mismatch(self, tmp_path: Path):
        src = tmp_path / "s5.parquet"
        _write_dummy_source(src, n=1)
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=_normalized(1, 384), model_name="old/model",
        )
        assert sc.is_stale(src, expected_model_name="new/model") is True
        assert sc.is_stale(src, expected_model_name="old/model") is False

    def test_stale_on_dim_mismatch(self, tmp_path: Path):
        src = tmp_path / "s6.parquet"
        _write_dummy_source(src, n=1)
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=_normalized(1, 384), model_name="m",
        )
        assert sc.is_stale(src, expected_dim=768) is True
        assert sc.is_stale(src, expected_dim=384) is False


# ── Atomic write semantics ───────────────────────────────────────────

class TestAtomicWrite:
    def test_no_tmp_file_on_success(self, tmp_path: Path):
        src = tmp_path / "a.parquet"
        _write_dummy_source(src, n=2)
        target = sc.write_sidecar(
            src, row_ids=[0, 1], epochs=[1, 2],
            embeddings=_normalized(2, 384), model_name="m",
        )
        # No leftover .tmp sibling
        leftovers = list(tmp_path.glob(".*tmp*"))
        assert leftovers == [], (
            f"Atomic write left temp files behind: {leftovers}"
        )
        assert target.exists()

    def test_existing_sidecar_survives_failed_rewrite(
        self, tmp_path: Path, monkeypatch
    ):
        """Simulate a write that crashes after the temp is created but
        before ``os.replace`` succeeds. The pre-existing sidecar must
        remain readable + unchanged, the tmp file must be cleaned up,
        and the user-visible sidecar bytes must be bit-identical to the
        original.
        """
        src = tmp_path / "b.parquet"
        _write_dummy_source(src, n=1)
        sc.write_sidecar(
            src, row_ids=[0], epochs=[1],
            embeddings=_normalized(1, 384, seed=1), model_name="orig",
        )
        sidecar = sc.sidecar_path_for(src)
        original_bytes = sidecar.read_bytes()

        # Patch os.replace to raise mid-write, simulating a system crash
        # after the tmp file landed but before atomic rename.
        import os as _os
        real_replace = _os.replace

        def boom(*args, **kwargs):
            raise OSError("simulated mid-write crash")

        monkeypatch.setattr(_os, "replace", boom)
        with pytest.raises(OSError, match="simulated"):
            sc.write_sidecar(
                src, row_ids=[0], epochs=[1],
                embeddings=_normalized(1, 384, seed=2), model_name="new",
            )
        # Restore
        monkeypatch.setattr(_os, "replace", real_replace)

        # Sidecar bytes are unchanged
        assert sidecar.read_bytes() == original_bytes
        # Read still returns the original frame
        frame = sc.read_sidecar(src)
        assert frame.model_name == "orig"
        # No leftover .tmp sibling
        tmp_leftovers = list(tmp_path.glob(".*.tmp"))
        assert tmp_leftovers == [], (
            f"Failed write left temp files behind: {tmp_leftovers}"
        )


# ── Read robustness ──────────────────────────────────────────────────

class TestReadRobustness:
    def test_corrupt_sidecar_raises_sidecar_error(self, tmp_path: Path):
        src = tmp_path / "c.parquet"
        _write_dummy_source(src)
        sidecar = sc.sidecar_path_for(src)
        # Write garbage that is NOT a valid parquet
        sidecar.write_bytes(b"this is not parquet")
        with pytest.raises(sc.SidecarError):
            sc.read_sidecar(src)

    def test_metadata_dim_disagrees_with_schema_dim_raises(self, tmp_path: Path):
        """If the parquet's key-value metadata claims dim=N but the
        schema's FixedSizeList says dim=M, that's a corruption signal -
        refuse to silently believe one over the other.
        """
        src = tmp_path / "d.parquet"
        _write_dummy_source(src, n=1)
        # Build a sidecar by hand with metadata-vs-schema disagreement
        schema = pa.schema([
            ("_row_id", pa.int64()),
            ("_epoch", pa.int64()),
            ("embedding", pa.list_(pa.float32(), 384)),
        ]).with_metadata({b"model_name": b"m", b"dim": b"768",
                          b"created_epoch": b"0"})
        embed_arr = pa.FixedSizeListArray.from_arrays(
            pa.array([0.0] * 384, type=pa.float32()), 384,
        )
        tbl = pa.table({
            "_row_id": pa.array([0], type=pa.int64()),
            "_epoch": pa.array([1], type=pa.int64()),
            "embedding": embed_arr,
        }, schema=schema)
        sidecar = sc.sidecar_path_for(src)
        pq.write_table(tbl, sidecar)

        with pytest.raises(sc.SidecarSchemaError, match="disagrees"):
            sc.read_sidecar(src)
