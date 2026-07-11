"""
Tests for Wave 2 production-readiness fixes (2026-04-16):

  - Item 7:  desktop_app.server._safe_error_message() - strip absolute
             paths from exception text before returning JSON to clients.
  - Item 8:  CredentialVault._verify_permissions() - auto-chmod 0600 on
             load, raise if chmod fails or has no effect.
  - Item 10: query_engine.Alert.build_email_message() - refuse oversized
             attachments instead of relying on the SMTP relay to reject
             them (which would lose the result silently).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Item 7 - _safe_error_message() in server.py
# ---------------------------------------------------------------------------


class TestSafeErrorMessage:
    def _helper(self):
        from desktop_app.server import _safe_error_message
        return _safe_error_message

    def test_redacts_project_root(self):
        from desktop_app.server import PROJECT_ROOT
        helper = self._helper()
        leaky = FileNotFoundError(f"{PROJECT_ROOT}/indexes/nope.parquet")
        msg = helper(leaky)
        assert PROJECT_ROOT not in msg
        assert "<project>" in msg

    def test_redacts_home_dir(self):
        helper = self._helper()
        home = os.path.expanduser("~")
        leaky = OSError(f"Permission denied: {home}/.ssh/id_rsa")
        msg = helper(leaky)
        assert home not in msg
        assert "~" in msg

    def test_collapses_multiline_to_first_line(self):
        helper = self._helper()
        exc = RuntimeError("boom\nTraceback (most recent call last):\n  File ...")
        msg = helper(exc)
        assert msg == "boom"

    def test_truncates_pathological_message(self):
        helper = self._helper()
        exc = ValueError("x" * 10_000)
        msg = helper(exc, max_len=200)
        assert len(msg) <= 200
        assert msg.endswith("\u2026")

    def test_passes_through_safe_message(self):
        helper = self._helper()
        exc = ValueError("query is required")
        msg = helper(exc)
        assert msg == "query is required"

    def test_falls_back_to_class_name_on_empty_message(self):
        helper = self._helper()
        exc = RuntimeError()
        msg = helper(exc)
        assert msg == "RuntimeError"


# ---------------------------------------------------------------------------
# Item 8 - CredentialVault permission enforcement
# ---------------------------------------------------------------------------


class TestCredentialVaultPermissions:
    @pytest.fixture
    def vault(self, tmp_path):
        from scheduled_input_engine.credentials import CredentialVault
        db = tmp_path / "creds.sqlite"
        key_dir = tmp_path / "keydir"
        v = CredentialVault(db, key_dir=str(key_dir))
        # Trigger key creation so the key file exists at 0600
        v._get_fernet()
        # Reset the cached fernet so the next _get_fernet() re-reads the file
        # and re-runs _verify_permissions() against the on-disk mode.
        v._fernet = None
        return v

    def test_correct_permissions_pass_through(self, vault):
        # Key was created at 0600 and not touched - should load cleanly.
        f = vault._get_fernet()
        assert f is not None

    def test_loose_permissions_auto_corrected(self, vault, caplog):
        # Loosen perms to simulate user accidentally chmod'ing 0644
        os.chmod(vault._key_file, 0o644)
        with caplog.at_level("WARNING"):
            f = vault._get_fernet()
        assert f is not None
        # Verify auto-correction took effect
        new_mode = vault._key_file.stat().st_mode & 0o777
        assert new_mode == 0o600
        assert any("Auto-correcting" in rec.message for rec in caplog.records)

    def test_chmod_failure_raises(self, vault):
        os.chmod(vault._key_file, 0o644)
        with patch("os.chmod", side_effect=PermissionError("denied")):
            with pytest.raises(RuntimeError, match="chmod failed"):
                vault._get_fernet()

    def test_chmod_no_op_raises(self, vault):
        os.chmod(vault._key_file, 0o644)
        # chmod returns success but does not actually change the mode
        # (e.g. exotic filesystem with ACLs that block chmod).
        with patch("os.chmod"):  # silently no-op
            with pytest.raises(RuntimeError, match="permissions are still"):
                vault._get_fernet()

    def test_missing_key_file_does_not_trigger_verify(self, tmp_path):
        # Sanity: _verify_permissions is only called when key exists; a fresh
        # vault with no key file just generates one.
        from scheduled_input_engine.credentials import CredentialVault
        db = tmp_path / "creds.sqlite"
        key_dir = tmp_path / "fresh_keydir"
        v = CredentialVault(db, key_dir=str(key_dir))
        f = v._get_fernet()
        assert f is not None
        new_mode = v._key_file.stat().st_mode & 0o777
        assert new_mode == 0o600


# ---------------------------------------------------------------------------
# Item 10 - Email attachment size cap
# ---------------------------------------------------------------------------


class TestEmailAttachmentCap:
    def test_under_cap_attaches_normally(self):
        from query_engine.Alert import build_email_message
        small = b"a,b,c\n1,2,3\n" * 100
        msg = build_email_message(
            subject="t", body="ok", to_addrs="x@example.com",
            from_addr="from@example.com",
            csv_bytes=small, csv_filename="results.csv",
        )
        # First payload is the body; subsequent payloads are attachments
        attachments = [p for p in msg.iter_attachments()]
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "results.csv"

    def test_over_cap_raises(self):
        from query_engine.Alert import build_email_message, MAX_ATTACHMENT_BYTES
        oversized = b"x" * (MAX_ATTACHMENT_BYTES + 1)
        with pytest.raises(ValueError, match="exceeds.*max allowed|max allowed is"):
            build_email_message(
                subject="t", body="ok", to_addrs="x@example.com",
                from_addr="from@example.com",
                csv_bytes=oversized, csv_filename="huge.csv",
            )

    def test_no_attachment_unaffected(self):
        from query_engine.Alert import build_email_message
        msg = build_email_message(
            subject="t", body="ok", to_addrs="x@example.com",
            from_addr="from@example.com",
        )
        attachments = list(msg.iter_attachments())
        assert attachments == []

    def test_at_cap_boundary_is_allowed(self):
        from query_engine.Alert import build_email_message, MAX_ATTACHMENT_BYTES
        exactly = b"y" * MAX_ATTACHMENT_BYTES
        msg = build_email_message(
            subject="t", body="ok", to_addrs="x@example.com",
            from_addr="from@example.com",
            csv_bytes=exactly, csv_filename="boundary.csv",
        )
        assert any(p.get_filename() == "boundary.csv"
                   for p in msg.iter_attachments())
