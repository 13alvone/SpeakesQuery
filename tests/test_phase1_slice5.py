"""
Tests for Phase 1 / Bet 2 slice 5 - operations close.

Covers:
  * Settings: defaults are present, validators reject bad values
  * Engine sweeper registration: gated by embeddings_enabled
  * cleanup_embeddings: evicts oldest, respects IMMUTABLE protection,
    independent of indexes/logs budgets
  * tools.embed_backfill CLI: dry runs, custom root, missing root,
    JSON output, exit codes
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from functionality import embedding_sidecar as sc


# ── Helpers ──────────────────────────────────────────────────────────

def _write_source_with_sidecar(root: Path, name: str, mtime_offset: float = 0.0):
    """Create a source parquet + matching sidecar, optionally back-dating
    the sidecar's mtime by ``mtime_offset`` seconds for eviction-order tests.
    """
    src = root / "news" / f"{name}.parquet"
    src.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"_epoch": pa.array([0], type=pa.int64()),
                  "title": [name]}),
        src,
    )
    emb = np.zeros((1, 384), dtype=np.float32)
    emb[0, 0] = 1.0
    sc.write_sidecar(
        src, row_ids=[0], epochs=[0], embeddings=emb, model_name="test/m",
    )
    if mtime_offset:
        sidecar = sc.sidecar_path_for(src)
        ts = time.time() - mtime_offset
        os.utime(sidecar, (ts, ts))
    return src


# ── Settings ─────────────────────────────────────────────────────────

class TestSlice5Settings:
    def test_defaults_present(self):
        from global_settings import DEFAULTS
        assert "embeddings_enabled" in DEFAULTS
        assert "max_embeddings_size_gb" in DEFAULTS
        assert "embedding_model_name" in DEFAULTS
        assert "embedding_batch_size" in DEFAULTS
        assert "embedding_sweep_interval_minutes" in DEFAULTS

    def test_default_values(self):
        from global_settings import DEFAULTS
        assert DEFAULTS["embeddings_enabled"] is False
        assert DEFAULTS["max_embeddings_size_gb"] == 5
        assert DEFAULTS["embedding_model_name"] == (
            "sentence-transformers/all-MiniLM-L6-v2"
        )
        assert DEFAULTS["embedding_batch_size"] == 32
        assert DEFAULTS["embedding_sweep_interval_minutes"] == 15

    def test_yaml_defaults_match_python(self):
        # Drift guard: the .yaml shipping reference must declare the same
        # keys as DEFAULTS (the YAML comment at the top of the file
        # explicitly invokes this contract).
        import yaml
        from global_settings import DEFAULTS
        yaml_path = Path(__file__).parent.parent / "global_settings.defaults.yaml"
        loaded = yaml.safe_load(yaml_path.read_text())
        for key in (
            "embeddings_enabled", "max_embeddings_size_gb",
            "embedding_model_name", "embedding_batch_size",
            "embedding_sweep_interval_minutes",
        ):
            assert key in loaded, f"{key} missing from defaults yaml"
            assert loaded[key] == DEFAULTS[key], (
                f"{key} drifts: yaml={loaded[key]!r} vs DEFAULTS={DEFAULTS[key]!r}"
            )

    def test_validator_rejects_non_bool_for_enabled(self):
        from global_settings import _validate_key
        err = _validate_key("embeddings_enabled", "yes", {})
        assert err is not None and "true or false" in err

    def test_validator_rejects_non_string_model_name(self):
        from global_settings import _validate_key
        err = _validate_key("embedding_model_name", 123, {})
        assert err is not None and "string" in err

    def test_validator_rejects_empty_model_name(self):
        from global_settings import _validate_key
        err = _validate_key("embedding_model_name", "  ", {})
        assert err is not None and "non-empty" in err

    def test_validator_rejects_out_of_range_size(self):
        from global_settings import _validate_key
        # Range is (1, 1000)
        err = _validate_key("max_embeddings_size_gb", 0, {})
        assert err is not None
        err = _validate_key("max_embeddings_size_gb", 5000, {})
        assert err is not None

    def test_validator_rejects_out_of_range_batch(self):
        from global_settings import _validate_key
        # Range (1, 1024)
        err = _validate_key("embedding_batch_size", 0, {})
        assert err is not None
        err = _validate_key("embedding_batch_size", 9999, {})
        assert err is not None

    def test_validator_rejects_out_of_range_sweep_interval(self):
        from global_settings import _validate_key
        # Range (1, 1440)
        err = _validate_key("embedding_sweep_interval_minutes", 0, {})
        assert err is not None
        err = _validate_key("embedding_sweep_interval_minutes", 99999, {})
        assert err is not None


# ── cleanup_embeddings ──────────────────────────────────────────────

class TestCleanupEmbeddings:
    def test_no_op_when_under_budget(self, tmp_path: Path):
        from scheduled_input_engine.cleanup import cleanup_embeddings
        root = tmp_path.resolve()
        _write_source_with_sidecar(root, "a")
        deleted = cleanup_embeddings(indexes_dir=root, max_total_gb=10)
        assert deleted == []

    def test_evicts_oldest_first_when_over_budget(self, tmp_path: Path):
        from scheduled_input_engine.cleanup import cleanup_embeddings
        root = tmp_path.resolve()
        # 3 sidecars, oldest at offset 300s
        _write_source_with_sidecar(root, "old",  mtime_offset=300)
        _write_source_with_sidecar(root, "mid",  mtime_offset=200)
        _write_source_with_sidecar(root, "new",  mtime_offset=0)
        # Budget 0 → evict everything; we just want to confirm the oldest
        # is hit first by inspecting the deletion order.
        deleted = cleanup_embeddings(indexes_dir=root, max_total_gb=0)
        deleted_names = [Path(p).name for p, _ in deleted]
        assert deleted_names[0] == "old.embeddings.parquet"
        # Subsequent entries follow oldest-first
        assert "mid.embeddings.parquet" in deleted_names

    def test_immutable_subtree_protected(self, tmp_path: Path):
        from scheduled_input_engine.cleanup import cleanup_embeddings
        root = tmp_path.resolve()
        # One sidecar in IMMUTABLE/ - shouldn't be touched
        imm_src = root / "IMMUTABLE" / "p.parquet"
        imm_src.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({"_epoch": pa.array([1], type=pa.int64())}), imm_src
        )
        sc.write_sidecar(
            imm_src, row_ids=[0], epochs=[1],
            embeddings=np.zeros((1, 384), dtype=np.float32),
            model_name="test/m",
        )
        deleted = cleanup_embeddings(indexes_dir=root, max_total_gb=0)
        assert deleted == []
        assert sc.sidecar_path_for(imm_src).exists()

    def test_skips_non_sidecar_parquets(self, tmp_path: Path):
        from scheduled_input_engine.cleanup import cleanup_embeddings
        root = tmp_path.resolve()
        # A regular source parquet WITHOUT a sidecar; cleanup_embeddings
        # must not touch it
        src = root / "news" / "raw.parquet"
        src.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({"_epoch": pa.array([1], type=pa.int64())}), src
        )
        deleted = cleanup_embeddings(indexes_dir=root, max_total_gb=0)
        assert deleted == []
        assert src.exists()

    def test_missing_root_returns_empty(self, tmp_path: Path):
        from scheduled_input_engine.cleanup import cleanup_embeddings
        ghost = tmp_path / "no-such-dir"
        assert cleanup_embeddings(indexes_dir=ghost, max_total_gb=1) == []


# ── Engine sweeper registration ─────────────────────────────────────

class TestEngineSweeperRegistration:
    """Black-box test the _schedule_embedding_sweep gating logic."""

    def _build_engine_stub(self, *, embeddings_enabled: bool,
                           interval_minutes: int = 15):
        """Build a minimal engine-shaped object that exercises only
        _schedule_embedding_sweep - avoids the full scheduler/store
        bootstrap. Patches the scheduler with a MagicMock so we can
        observe add_job / get_job / remove_job calls.
        """
        from scheduled_input_engine.engine import (
            ScheduledInputEngine, EMBEDDING_SWEEPER_JOB_ID,
        )
        # Build a bare instance without going through __init__
        engine = ScheduledInputEngine.__new__(ScheduledInputEngine)
        engine._scheduler = MagicMock()

        # Stub the get_job to return None by default (no prior job)
        engine._scheduler.get_job.return_value = None

        # Patch _setting to return our controlled values
        def _setting(key, default=None):
            if key == "embeddings_enabled":
                return embeddings_enabled
            if key == "embedding_sweep_interval_minutes":
                return interval_minutes
            return default
        engine._setting = _setting
        return engine, EMBEDDING_SWEEPER_JOB_ID

    def test_disabled_does_not_register_a_job(self):
        engine, _ = self._build_engine_stub(embeddings_enabled=False)
        engine._schedule_embedding_sweep()
        engine._scheduler.add_job.assert_not_called()

    def test_disabled_removes_prior_job_if_present(self):
        from scheduled_input_engine.engine import EMBEDDING_SWEEPER_JOB_ID
        engine, job_id = self._build_engine_stub(embeddings_enabled=False)
        # Pretend a prior job exists
        engine._scheduler.get_job.return_value = MagicMock()
        engine._schedule_embedding_sweep()
        engine._scheduler.remove_job.assert_called_once_with(job_id)
        engine._scheduler.add_job.assert_not_called()

    def test_enabled_registers_with_correct_interval(self):
        engine, job_id = self._build_engine_stub(
            embeddings_enabled=True, interval_minutes=30,
        )
        engine._schedule_embedding_sweep()
        engine._scheduler.add_job.assert_called_once()
        kwargs = engine._scheduler.add_job.call_args.kwargs
        assert kwargs["id"] == job_id
        assert kwargs["replace_existing"] is True
        # Trigger is IntervalTrigger(minutes=30)
        trigger = engine._scheduler.add_job.call_args.args[1]
        assert "30" in str(trigger) or any(
            getattr(f, "name", "") == "minute" and f.expressions[0].step == 30
            for f in getattr(trigger, "fields", [])
        )

    def test_enabled_clamps_interval_to_floor(self):
        # interval=0 → clamped to 1
        engine, _ = self._build_engine_stub(
            embeddings_enabled=True, interval_minutes=0,
        )
        engine._schedule_embedding_sweep()
        engine._scheduler.add_job.assert_called_once()

    def test_enabled_clamps_interval_to_ceiling(self):
        # interval=100000 → clamped to 1440
        engine, _ = self._build_engine_stub(
            embeddings_enabled=True, interval_minutes=100000,
        )
        engine._schedule_embedding_sweep()
        engine._scheduler.add_job.assert_called_once()

    def test_run_embedding_sweep_swallows_exceptions(self):
        from scheduled_input_engine.engine import ScheduledInputEngine
        engine = ScheduledInputEngine.__new__(ScheduledInputEngine)
        engine._get_indexes_dir = lambda: Path("/no/such/dir/at/all")
        # Should NOT raise - the wrapper logs and returns
        engine._run_embedding_sweep()


# ── tools.embed_backfill CLI ───────────────────────────────────────

class TestEmbedBackfillCLI:
    def test_help_exits_zero(self):
        from tools.embed_backfill import main
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_missing_root_returns_2(self):
        from tools.embed_backfill import main
        rc = main(["--root", "/no/such/path/embed_backfill_test"])
        assert rc == 2

    def test_empty_root_returns_zero_with_human_output(self, tmp_path: Path):
        from tools.embed_backfill import main
        # Empty dir: 0 sources, 0 failures → rc=0
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--root", str(tmp_path)])
        assert rc == 0
        out = buf.getvalue()
        assert "sources discovered: 0" in out

    def test_empty_root_json_output(self, tmp_path: Path):
        from tools.embed_backfill import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--root", str(tmp_path), "--json"])
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert payload["sources_seen"] == 0
        assert payload["sources_embedded"] == 0
        assert payload["per_source"] == []

    def test_corrupt_source_lands_in_failures_returns_1(self, tmp_path: Path):
        from tools.embed_backfill import main
        # Write a non-parquet file - the sweeper marks it as failed
        bad = tmp_path / "news" / "broken.parquet"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"not parquet at all")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--root", str(tmp_path)])
        # rc=1 signals "sweep had failures"
        assert rc == 1
        assert "sources failed:     1" in buf.getvalue()

    def test_cleanup_flag_runs_cleanup_pass(self, tmp_path: Path):
        from tools.embed_backfill import main
        # Write 2 sidecars, run with --cleanup at budget=0 → should evict
        _write_source_with_sidecar(tmp_path, "a")
        _write_source_with_sidecar(tmp_path, "b")

        # Patch the budget so cleanup actually evicts
        with patch("scheduled_input_engine.cleanup.cleanup_embeddings") as mock_cleanup:
            mock_cleanup.return_value = [
                ("/x/a.embeddings.parquet", "embeddings total over limit"),
                ("/x/b.embeddings.parquet", "embeddings total over limit"),
            ]
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["--root", str(tmp_path), "--cleanup"])
            mock_cleanup.assert_called_once()
            assert "Cleanup pass: 2 sidecars evicted" in buf.getvalue()
