"""Regression tests for scheduled_input_engine/parquet_writer.py.

Pins the ``os.replace`` contract introduced in H-CE-5 (2026-04-21 production
review) - previously ``os.rename`` was used, which raises ``FileExistsError``
on Windows when the target exists and silently broke ``overwrite=True`` on
that platform. ``os.replace`` has identical POSIX atomicity semantics and is
correct on Windows.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from scheduled_input_engine.parquet_writer import ParquetWriter


# ----------------------------------------------------------------------
# Source-code invariant: no bare ``os.rename`` remains in the writer.
# ----------------------------------------------------------------------

def test_no_os_rename_in_parquet_writer():
    """Prevent regression: os.rename must not reappear in parquet_writer.py.

    os.rename raises FileExistsError on Windows when the target exists,
    breaking overwrite=True. Always use os.replace for atomic-overwrite.
    """
    src_path = Path(__file__).resolve().parent.parent / \
        "scheduled_input_engine" / "parquet_writer.py"
    text = src_path.read_text()

    # Match ``os.rename(`` as a function call but not the word inside a
    # comment or string that mentions the name for context.
    call_sites = re.findall(r"(?<![#'\"])\bos\.rename\s*\(", text)
    assert call_sites == [], (
        f"os.rename call(s) found in parquet_writer.py - use os.replace. "
        f"Matches: {call_sites}"
    )


# ----------------------------------------------------------------------
# Overwrite=True double-write (the specific Windows regression).
# ----------------------------------------------------------------------

def test_write_atomic_overwrite_twice(tmp_path: Path):
    """Writing the same filename twice with overwrite=True must succeed."""
    writer = ParquetWriter(tmp_path, target_file_mb=128)

    df1 = pd.DataFrame({"a": [1, 2, 3], "_epoch": [1000, 1001, 1002]})
    p1 = writer.write_atomic(
        df1,
        subdirectory="double_write",
        filename="fixed.system4.system4.parquet",
        overwrite=True,
    )

    df2 = pd.DataFrame({"a": [7, 8, 9, 10], "_epoch": [2000, 2001, 2002, 2003]})
    p2 = writer.write_atomic(
        df2,
        subdirectory="double_write",
        filename="fixed.system4.system4.parquet",
        overwrite=True,
    )

    # Same target path both times.
    assert p1 == p2
    assert p1.exists()

    # Second write overwrote the first (4 rows, not 3).
    readback = pd.read_parquet(p1)
    assert len(readback) == 4
    assert list(readback["a"]) == [7, 8, 9, 10]


def test_write_atomic_overwrite_false_produces_unique_path(tmp_path: Path):
    """overwrite=False appends a timestamp suffix instead of clobbering."""
    writer = ParquetWriter(tmp_path, target_file_mb=128)

    df = pd.DataFrame({"a": [1], "_epoch": [1000]})
    first = writer.write_atomic(
        df,
        subdirectory="no_clobber",
        filename="same.system4.system4.parquet",
        overwrite=False,
    )
    second = writer.write_atomic(
        df,
        subdirectory="no_clobber",
        filename="same.system4.system4.parquet",
        overwrite=False,
    )

    assert first != second
    assert first.exists() and second.exists()


# ----------------------------------------------------------------------
# Temp-file cleanup: successful writes must not leave ``.tmp`` siblings.
# ----------------------------------------------------------------------

def test_write_atomic_leaves_no_tmp_on_success(tmp_path: Path):
    writer = ParquetWriter(tmp_path, target_file_mb=128)
    df = pd.DataFrame({"a": [1, 2], "_epoch": [1000, 1001]})
    writer.write_atomic(
        df,
        subdirectory="cleanup",
        filename="ok.system4.system4.parquet",
        overwrite=True,
    )

    target_dir = tmp_path / "cleanup"
    tmp_siblings = list(target_dir.glob(".*.tmp"))
    assert tmp_siblings == [], (
        f"Temp files leaked: {tmp_siblings}"
    )


def test_write_atomic_cleans_tmp_on_write_failure(tmp_path: Path, monkeypatch):
    """If to_parquet raises, the .tmp file must be removed and exception propagated."""
    writer = ParquetWriter(tmp_path, target_file_mb=128)

    # Force DataFrame.to_parquet to raise mid-write. The .tmp was created by
    # pyarrow's opener before the raise, so cleanup must fire.
    original_to_parquet = pd.DataFrame.to_parquet

    def boom(self, path, *args, **kwargs):
        # Create the tmp file first so cleanup has something to remove.
        Path(path).write_bytes(b"partial")
        raise RuntimeError("simulated pyarrow failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)

    df = pd.DataFrame({"a": [1], "_epoch": [1000]})
    with pytest.raises(RuntimeError, match="simulated pyarrow failure"):
        writer.write_atomic(
            df,
            subdirectory="boom",
            filename="x.system4.system4.parquet",
            overwrite=True,
        )

    # Restore before asserting to avoid polluting later tests.
    monkeypatch.setattr(pd.DataFrame, "to_parquet", original_to_parquet)

    target_dir = tmp_path / "boom"
    tmp_siblings = list(target_dir.glob(".*.tmp"))
    final = list(target_dir.glob("*.system4.system4.parquet"))
    assert tmp_siblings == []
    assert final == []
