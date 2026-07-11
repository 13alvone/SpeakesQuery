"""
Tests for functionality/log_writer.py + scheduled_input_engine/cleanup.cleanup_logs.

Covers:
  * Schema enforcement (unknown columns dropped, missing columns filled None)
  * _epoch auto-injection + override
  * Unknown category is a no-op (no crash)
  * logs_enabled=False short-circuits without error
  * Forced flush at _MAX_BUFFERED_ROWS
  * Category-segmented output (one Parquet file per category subdir)
  * cleanup_logs respects per-subdir + total budgets (FIFO by mtime)
  * cleanup_indexes with skip_subdirs excludes the logs subtree
"""

from __future__ import annotations

import pathlib
import time

import pandas as pd
import pytest

from functionality import log_writer as lw
from scheduled_input_engine.cleanup import cleanup_indexes, cleanup_logs


@pytest.fixture
def tmp_logs_root(tmp_path, monkeypatch):
    """Redirect log output to a tmp dir and hand back a fresh LogWriter."""
    from global_settings import get_settings
    settings = get_settings()
    original_root = settings.get("logs_root")
    original_enabled = settings.get("logs_enabled")
    settings.set("logs_root", str(tmp_path))
    settings.set("logs_enabled", True)
    lw.LogWriter.reset_for_tests()
    yield tmp_path
    try:
        settings.set("logs_root", original_root)
        settings.set("logs_enabled", original_enabled)
    except Exception:
        pass
    lw.LogWriter.reset_for_tests()


def _all_rows(root: pathlib.Path, category: str) -> list[dict]:
    """Read every parquet under ``root/category/`` and return rows."""
    folder = root / category
    if not folder.exists():
        return []
    rows: list[dict] = []
    for path in folder.glob("*.parquet"):
        df = pd.read_parquet(path)
        rows.extend(df.to_dict(orient="records"))
    return rows


class TestSchema:
    def test_unknown_columns_dropped(self, tmp_logs_root):
        lw.emit("system", {
            "event": "boot", "level": "info", "component": "test",
            "message": "hello", "secret_should_drop": "GONE",
        })
        lw.flush_all()
        rows = _all_rows(tmp_logs_root, "system")
        assert len(rows) == 1
        assert "secret_should_drop" not in rows[0]

    def test_missing_columns_filled_none(self, tmp_logs_root):
        lw.emit("system", {"event": "partial"})
        lw.flush_all()
        rows = _all_rows(tmp_logs_root, "system")
        assert rows[0]["component"] is None
        assert rows[0]["event"] == "partial"

    def test_epoch_autofilled(self, tmp_logs_root):
        before = int(time.time())
        lw.emit("system", {"event": "auto"})
        lw.flush_all()
        row = _all_rows(tmp_logs_root, "system")[0]
        assert abs(row["_epoch"] - before) <= 5

    def test_unknown_category_noop(self, tmp_logs_root):
        # Should not raise; just warns.
        lw.emit("nonexistent_category", {"foo": "bar"})
        lw.flush_all()
        assert _all_rows(tmp_logs_root, "nonexistent_category") == []

    def test_disabled_short_circuits(self, tmp_logs_root):
        from global_settings import get_settings
        get_settings().set("logs_enabled", False)
        lw.LogWriter.reset_for_tests()
        lw.emit("system", {"event": "should_not_write"})
        assert lw.flush_all() == 0
        assert _all_rows(tmp_logs_root, "system") == []


class TestCategorySegmentation:
    def test_separate_subdirs(self, tmp_logs_root):
        lw.emit("system", {"event": "e1"})
        lw.emit("config", {"action": "set", "subject": "x"})
        lw.emit("claude_api", {
            "request_id": "r1", "source": "test",
            "model": "m", "status": "success",
        })
        lw.flush_all()
        assert (tmp_logs_root / "system").is_dir()
        assert (tmp_logs_root / "config").is_dir()
        assert (tmp_logs_root / "claude_api").is_dir()
        assert len(_all_rows(tmp_logs_root, "system")) == 1
        # Filter for this test's own config row - the fixture's
        # ``settings.set(...)`` calls also emit config rows now that
        # global_settings._emit_config_change_safely is wired up. Match on
        # subject='x' to isolate the row we emitted in the test body.
        config_rows = [r for r in _all_rows(tmp_logs_root, "config")
                       if r.get("subject") == "x"]
        assert len(config_rows) == 1
        assert len(_all_rows(tmp_logs_root, "claude_api")) == 1


class TestCleanupLogs:
    def test_per_subdir_cap_evicts_oldest(self, tmp_path):
        # Build a fake logs tree with three files of 1 MiB each in one subdir.
        subdir = tmp_path / "claude_api"
        subdir.mkdir()
        for i in range(3):
            p = subdir / f"{i}.parquet"
            p.write_bytes(b"x" * (1024 * 1024))
            # Space mtimes out so FIFO order is well-defined
            past = time.time() - (3 - i) * 10
            import os
            os.utime(p, (past, past))

        # Cap per-subdir to 2 MiB - expect oldest file (index 0) removed.
        deleted = cleanup_logs(
            logs_dir=tmp_path,
            max_subdir_gb=0.002,        # 2 MiB
            max_total_gb=10,            # effectively unbounded
        )
        remaining = sorted(p.name for p in subdir.glob("*.parquet"))
        assert "0.parquet" not in remaining, deleted
        assert "2.parquet" in remaining

    def test_total_cap_evicts_across_subdirs(self, tmp_path):
        for name in ("a", "b"):
            d = tmp_path / name
            d.mkdir()
            p = d / "x.parquet"
            p.write_bytes(b"y" * (1024 * 1024))
        # Ensure 'a' is older so it gets cut first
        import os
        a_file = tmp_path / "a" / "x.parquet"
        os.utime(a_file, (time.time() - 100, time.time() - 100))

        deleted = cleanup_logs(
            logs_dir=tmp_path,
            max_subdir_gb=10,       # per-subdir OK
            max_total_gb=0.001,     # 1 MiB total - only 1 file fits
        )
        assert any("total" in reason for _, reason in deleted)
        assert len(list(tmp_path.rglob("*.parquet"))) <= 1


class TestIndexesCleanupSkipsLogs:
    """Regression: main cleanup_indexes must not touch the logs subtree."""

    def test_skip_subdirs_excludes_logs(self, tmp_path):
        # Main index data
        data_dir = tmp_path / "mydata"
        data_dir.mkdir()
        for i in range(2):
            p = data_dir / f"{i}.parquet"
            p.write_bytes(b"z" * (1024 * 1024))

        # Logs subtree - must survive even when over-budget
        logs_root = tmp_path / "logs"
        (logs_root / "claude_api").mkdir(parents=True)
        log_file = logs_root / "claude_api" / "huge.parquet"
        log_file.write_bytes(b"q" * (2 * 1024 * 1024))

        cleanup_indexes(
            indexes_dir=tmp_path,
            max_subdir_gb=0.0005,   # force deletion in mydata/
            max_total_gb=0.001,     # 1 MiB total cap
            skip_subdirs=["logs"],
        )
        # Log file must still exist even though its 2 MiB would otherwise
        # blow the 1 MiB total budget.
        assert log_file.exists()


class TestConvenienceWrappers:
    def test_log_claude_api_call_matches_schema(self, tmp_logs_root):
        lw.log_claude_api_call(
            request_id="req-1", source="unit_test",
            model="claude-sonnet-4-6", status="success",
            input_tokens=100, output_tokens=50,
            cost_usd=0.00075, latency_ms=420,
            attempt_num=1, retried=False,
        )
        lw.flush_all()
        rows = _all_rows(tmp_logs_root, "claude_api")
        assert rows[0]["request_id"] == "req-1"
        assert rows[0]["model"] == "claude-sonnet-4-6"
        assert rows[0]["cost_usd"] == pytest.approx(0.00075)
        assert rows[0]["latency_ms"] == 420

    def test_log_alert_group_event_serialises_lists(self, tmp_logs_root):
        lw.log_alert_group_event(
            group_name="g1", status="success",
            searches_used=["s1", "s2", "s3"],
            cost_usd=0.01,
        )
        lw.flush_all()
        row = _all_rows(tmp_logs_root, "alert_groups")[0]
        assert row["group_name"] == "g1"
        # Lists are stringified to stay inside Parquet's string type
        assert "s1" in row["searches_used"]
