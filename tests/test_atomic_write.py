"""
Tests for ``functionality.atomic_write`` - the shared helper introduced in
Wave 3 of the production-readiness review (2026-04-16) so that YAML/JSON
stores no longer corrupt their persistence files on a crash mid-write.

Key invariants under test:
  - On success the destination contains the new payload exactly.
  - On any failure (raised inside the write, ``KeyboardInterrupt`` from a
    Ctrl-C, ``os.replace`` failing) the original destination is untouched
    and no ``.tmp.<rand>`` file is left behind.
  - The temp file is created in the same directory as the destination so
    the rename is atomic on the same filesystem.
  - Optional ``mode`` chmods the temp file *before* the rename so the
    final file lands with the correct permissions atomically.
  - Bytes variant works the same way for binary payloads.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from functionality.atomic_write import write_bytes_atomic, write_text_atomic


def _list_tmp_siblings(target: Path) -> list[Path]:
    """Return any ``.<name>.<rand>.tmp`` siblings still in the directory."""
    return [
        p for p in target.parent.iterdir()
        if p.name.startswith(f".{target.name}.") and p.suffix == ".tmp"
    ]


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestSuccessPath:
    def test_writes_text_when_target_missing(self, tmp_path):
        target = tmp_path / "fresh.yaml"
        write_text_atomic(target, "key: value\n")
        assert target.read_text() == "key: value\n"

    def test_overwrites_existing_target(self, tmp_path):
        target = tmp_path / "existing.yaml"
        target.write_text("old: 1\n")
        write_text_atomic(target, "new: 2\n")
        assert target.read_text() == "new: 2\n"

    def test_creates_missing_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "file.yaml"
        write_text_atomic(target, "x: y\n")
        assert target.read_text() == "x: y\n"

    def test_no_temp_files_left_after_success(self, tmp_path):
        target = tmp_path / "clean.yaml"
        write_text_atomic(target, "ok\n")
        assert _list_tmp_siblings(target) == []

    def test_mode_arg_applied(self, tmp_path):
        target = tmp_path / "perms.yaml"
        write_text_atomic(target, "secret\n", mode=0o600)
        actual_mode = target.stat().st_mode & 0o777
        assert actual_mode == 0o600

    def test_unicode_round_trip(self, tmp_path):
        target = tmp_path / "unicode.yaml"
        payload = "name: \u00e9clair\nemoji: \U0001F600\n"
        write_text_atomic(target, payload)
        assert target.read_text(encoding="utf-8") == payload

    def test_accepts_string_path(self, tmp_path):
        target = tmp_path / "str_path.yaml"
        write_text_atomic(str(target), "ok\n")
        assert target.read_text() == "ok\n"

    def test_temp_file_lives_in_same_directory(self, tmp_path):
        """Verify the temp file is created next to the target, not in /tmp.

        We prove this by patching ``os.replace`` to a no-op so the temp file
        persists, then checking it landed in the right directory.
        """
        target = tmp_path / "neighbor.yaml"
        with patch("functionality.atomic_write.os.replace"):
            write_text_atomic(target, "ok\n")
        leftovers = _list_tmp_siblings(target)
        # Exactly one temp file in the same directory
        assert len(leftovers) == 1
        # Cleanup
        leftovers[0].unlink()


# ---------------------------------------------------------------------------
# Failure modes - crash safety
# ---------------------------------------------------------------------------


class TestFailureSafety:
    def test_target_unchanged_on_replace_failure(self, tmp_path):
        target = tmp_path / "preserve.yaml"
        target.write_text("original\n")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError, match="disk full"):
                write_text_atomic(target, "would corrupt\n")
        # Original survives
        assert target.read_text() == "original\n"
        # Temp file cleaned up
        assert _list_tmp_siblings(target) == []

    def test_no_temp_leak_when_write_raises(self, tmp_path):
        target = tmp_path / "writefail.yaml"
        # fsync is the last thing to run inside the open() block; failing it
        # exercises the cleanup path while the file handle is still open.
        with patch(
            "functionality.atomic_write.os.fsync",
            side_effect=OSError("io error"),
        ):
            with pytest.raises(OSError, match="io error"):
                write_text_atomic(target, "never lands\n")
        assert not target.exists()
        assert _list_tmp_siblings(target) == []

    def test_keyboard_interrupt_cleans_temp(self, tmp_path):
        target = tmp_path / "ctrlc.yaml"
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=KeyboardInterrupt,
        ):
            with pytest.raises(KeyboardInterrupt):
                write_text_atomic(target, "aborted\n")
        assert not target.exists()
        assert _list_tmp_siblings(target) == []

    def test_chmod_failure_cleans_temp(self, tmp_path):
        target = tmp_path / "chmodfail.yaml"
        with patch(
            "functionality.atomic_write.os.chmod",
            side_effect=PermissionError("nope"),
        ):
            with pytest.raises(PermissionError):
                write_text_atomic(target, "x\n", mode=0o600)
        assert not target.exists()
        assert _list_tmp_siblings(target) == []


# ---------------------------------------------------------------------------
# Docker bind-mount fallback
# ---------------------------------------------------------------------------
#
# When global_settings.yaml (or any persistent YAML) is bind-mounted into a
# container as a single file, ``os.replace`` cannot replace the mount point
# and fails with ``OSError(errno=EBUSY)``. The helper falls back to an
# in-place truncate+write so saves still land. These tests lock in that
# behaviour across EBUSY, EXDEV, and EPERM; all other OSErrors must still
# re-raise so real disk failures stay visible.


class TestBindMountFallback:
    def _make_errno_side_effect(self, errno_value):
        def _side_effect(*args, **kwargs):
            exc = OSError("bind mount busy")
            exc.errno = errno_value
            raise exc
        return _side_effect

    def test_ebusy_falls_back_to_inplace_write(self, tmp_path):
        import errno as _errno

        target = tmp_path / "mounted.yaml"
        target.write_text("old\n")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=self._make_errno_side_effect(_errno.EBUSY),
        ):
            write_text_atomic(target, "new\n")
        assert target.read_text() == "new\n"
        assert _list_tmp_siblings(target) == []

    def test_exdev_falls_back_to_inplace_write(self, tmp_path):
        import errno as _errno

        target = tmp_path / "cross_device.yaml"
        target.write_text("old\n")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=self._make_errno_side_effect(_errno.EXDEV),
        ):
            write_text_atomic(target, "new\n")
        assert target.read_text() == "new\n"
        assert _list_tmp_siblings(target) == []

    def test_eperm_falls_back_to_inplace_write(self, tmp_path):
        import errno as _errno

        target = tmp_path / "eperm.yaml"
        target.write_text("old\n")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=self._make_errno_side_effect(_errno.EPERM),
        ):
            write_text_atomic(target, "new\n")
        assert target.read_text() == "new\n"
        assert _list_tmp_siblings(target) == []

    def test_other_oserror_still_raises(self, tmp_path):
        import errno as _errno

        target = tmp_path / "real_failure.yaml"
        target.write_text("preserved\n")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=self._make_errno_side_effect(_errno.ENOSPC),
        ):
            with pytest.raises(OSError):
                write_text_atomic(target, "would corrupt\n")
        assert target.read_text() == "preserved\n"
        assert _list_tmp_siblings(target) == []

    def test_bytes_variant_ebusy_falls_back(self, tmp_path):
        import errno as _errno

        target = tmp_path / "mounted.bin"
        target.write_bytes(b"old")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=self._make_errno_side_effect(_errno.EBUSY),
        ):
            write_bytes_atomic(target, b"new")
        assert target.read_bytes() == b"new"
        assert _list_tmp_siblings(target) == []


# ---------------------------------------------------------------------------
# Bytes variant
# ---------------------------------------------------------------------------


class TestBytesVariant:
    def test_writes_bytes(self, tmp_path):
        target = tmp_path / "blob.bin"
        payload = bytes(range(256))
        write_bytes_atomic(target, payload)
        assert target.read_bytes() == payload

    def test_target_unchanged_on_failure(self, tmp_path):
        target = tmp_path / "blob.bin"
        target.write_bytes(b"original")
        with patch(
            "functionality.atomic_write.os.replace",
            side_effect=OSError("nope"),
        ):
            with pytest.raises(OSError):
                write_bytes_atomic(target, b"would corrupt")
        assert target.read_bytes() == b"original"
        assert _list_tmp_siblings(target) == []

    def test_mode_arg_applied(self, tmp_path):
        target = tmp_path / "blob.bin"
        write_bytes_atomic(target, b"secret", mode=0o600)
        assert (target.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Integration smoke - real JSON round-trip
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_json_round_trip(self, tmp_path):
        target = tmp_path / "index.json"
        data = {"jobs": [{"id": "abc", "ts": 1234567890}], "count": 1}
        write_text_atomic(target, json.dumps(data, indent=2))
        loaded = json.loads(target.read_text())
        assert loaded == data
