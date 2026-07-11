"""
Comprehensive tests for ``scheduled_input_engine.credentials.CredentialVault``.

Wave 4 of the production-readiness review (2026-04-16) called out zero
unit-test coverage for this module beyond the permission-enforcement tests
shipped in Wave 2.  This file covers every public method end-to-end:

  - input validation (``_validate_credential_input``)
  - encrypt/store + decrypt/retrieve round-trip
  - per-script multi-credential decrypt (``decrypt_for_script``)
  - upsert semantics on duplicate ``(script_id, key_name)``
  - delete (single key + bulk by script)
  - list_keys, has_credentials
  - migrate_staging (script_id=0 → real script_id)
  - tamper detection (modified ciphertext, wrong key)
  - immutability of decrypted output (``MappingProxyType``)
  - thread safety of ``_get_fernet``
"""
from __future__ import annotations

import sqlite3
import threading
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def vault(tmp_path):
    """Fresh per-test vault writing to a temp DB and key dir."""
    from scheduled_input_engine.credentials import CredentialVault
    db = tmp_path / "creds.sqlite"
    key_dir = tmp_path / "keys"
    return CredentialVault(db, key_dir=str(key_dir))


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestValidateCredentialInput:
    @pytest.fixture
    def validate(self):
        from scheduled_input_engine.credentials import CredentialVault
        return CredentialVault._validate_credential_input

    @pytest.mark.parametrize("bad", ["", "   ", "\t\n", None])
    def test_rejects_empty(self, validate, bad):
        with pytest.raises(ValueError, match="non-empty"):
            validate("api_key", bad)
        with pytest.raises(ValueError, match="non-empty"):
            validate(bad, "value")

    def test_rejects_non_ascii(self, validate):
        with pytest.raises(ValueError, match="non-ASCII"):
            validate("api_key", "v\u00e9lue")
        with pytest.raises(ValueError, match="non-ASCII"):
            validate("k\u00e9y", "value")

    @pytest.mark.parametrize("bad", ["with space", "tab\tin", "new\nline", "carriage\rreturn"])
    def test_rejects_internal_whitespace(self, validate, bad):
        with pytest.raises(ValueError, match="whitespace"):
            validate("api_key", bad)

    @pytest.mark.parametrize("bad", ["abc%20def", "%0a", "FF%aB"])
    def test_rejects_percent_encoding(self, validate, bad):
        with pytest.raises(ValueError, match="percent-encoded"):
            validate("api_key", bad)

    @pytest.mark.parametrize("bad_char", "`$\\;|&<>(){}!")
    def test_rejects_shell_metachars(self, validate, bad_char):
        with pytest.raises(ValueError, match="disallowed"):
            validate("api_key", f"value{bad_char}suffix")

    def test_rejects_null_byte(self, validate):
        with pytest.raises(ValueError, match="disallowed"):
            validate("api_key", "before\x00after")

    @pytest.mark.parametrize("ok", [
        "api_key", "API_KEY", "k1", "key.with.dots",
        "key-with-dashes", "alphanum123",
    ])
    def test_accepts_safe_keys(self, validate, ok):
        validate(ok, "value")

    def test_accepts_safe_value(self, validate):
        validate("api_key", "sk-proj-abc123XYZ_-.")


# ---------------------------------------------------------------------------
# Store / retrieve round-trip
# ---------------------------------------------------------------------------


class TestStoreRetrieve:
    def test_round_trip(self, vault):
        vault.store(42, "api_key", "sk-secret-value")
        assert vault.retrieve(42, "api_key") == "sk-secret-value"

    def test_value_encrypted_at_rest(self, vault):
        vault.store(42, "api_key", "sk-secret-value")
        with sqlite3.connect(vault._db_path) as conn:
            row = conn.execute(
                "SELECT encrypted_value FROM credentials WHERE script_id=? AND key_name=?",
                (42, "api_key"),
            ).fetchone()
        assert row is not None
        # Plaintext must not appear anywhere in the encrypted blob
        assert b"sk-secret-value" not in row[0]

    def test_upsert_overwrites_value(self, vault):
        vault.store(42, "api_key", "first")
        vault.store(42, "api_key", "second")
        assert vault.retrieve(42, "api_key") == "second"

    def test_upsert_does_not_duplicate_row(self, vault):
        vault.store(42, "api_key", "v1")
        vault.store(42, "api_key", "v2")
        with sqlite3.connect(vault._db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM credentials WHERE script_id=? AND key_name=?",
                (42, "api_key"),
            ).fetchone()[0]
        assert count == 1

    def test_different_scripts_independent(self, vault):
        vault.store(1, "shared_key", "alpha")
        vault.store(2, "shared_key", "beta")
        assert vault.retrieve(1, "shared_key") == "alpha"
        assert vault.retrieve(2, "shared_key") == "beta"

    def test_retrieve_missing_raises_keyerror(self, vault):
        with pytest.raises(KeyError, match="No credential"):
            vault.retrieve(999, "missing_key")

    def test_store_validates_input(self, vault):
        with pytest.raises(ValueError):
            vault.store(1, "key with space", "value")
        with pytest.raises(ValueError):
            vault.store(1, "ok_key", "")

    def test_unicode_value_after_strip(self, vault):
        # Keys are stored after .strip() - leading/trailing whitespace
        # should not survive.  Value is not stripped at store time.
        vault.store(1, "  spaced_key  ", "ok-value")
        # Validation strips for the check, then store() stores key_name.strip()
        assert vault.retrieve(1, "spaced_key") == "ok-value"


# ---------------------------------------------------------------------------
# decrypt_for_script: bulk decryption with MappingProxyType
# ---------------------------------------------------------------------------


class TestDecryptForScript:
    def test_returns_all_credentials_for_script(self, vault):
        vault.store(7, "api_key", "alpha")
        vault.store(7, "api_secret", "beta")
        vault.store(7, "endpoint", "https-not-checked-here")
        result = vault.decrypt_for_script(7)
        assert dict(result) == {
            "api_key": "alpha",
            "api_secret": "beta",
            "endpoint": "https-not-checked-here",
        }

    def test_returns_none_for_unknown_script(self, vault):
        """M-SV-4 (2026-04-22): unknown script → None (was empty MappingProxyType)."""
        result = vault.decrypt_for_script(999)
        assert result is None, (
            "Zero-row case must return None so callers can distinguish "
            "'no creds configured' from 'creds exist but failed to decrypt'."
        )

    def test_returns_immutable_mapping(self, vault):
        vault.store(7, "api_key", "secret")
        result = vault.decrypt_for_script(7)
        assert isinstance(result, MappingProxyType)
        with pytest.raises(TypeError):
            result["new_key"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            del result["api_key"]  # type: ignore[attr-defined]

    def test_does_not_leak_other_scripts(self, vault):
        vault.store(1, "a", "alpha")
        vault.store(2, "b", "beta")
        result = vault.decrypt_for_script(1)
        assert "b" not in result

    def test_corrupted_row_skipped_with_log(self, vault, caplog):
        vault.store(7, "good", "v1")
        vault.store(7, "bad", "v2")
        # Corrupt the "bad" row
        with sqlite3.connect(vault._db_path) as conn:
            conn.execute(
                "UPDATE credentials SET encrypted_value=? WHERE script_id=? AND key_name=?",
                (b"not-valid-fernet-token", 7, "bad"),
            )
            conn.commit()
        with caplog.at_level("ERROR"):
            result = vault.decrypt_for_script(7)
        assert "good" in result and result["good"] == "v1"
        assert "bad" not in result
        assert any("Failed to decrypt" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# delete + list_keys + has_credentials
# ---------------------------------------------------------------------------


class TestDeleteAndListing:
    def test_delete_single_key(self, vault):
        vault.store(1, "a", "alpha")
        vault.store(1, "b", "beta")
        deleted = vault.delete(1, "a")
        assert deleted == 1
        assert vault.list_keys(1) == ["b"]

    def test_delete_all_for_script(self, vault):
        vault.store(1, "a", "alpha")
        vault.store(1, "b", "beta")
        vault.store(2, "c", "gamma")
        deleted = vault.delete(1)
        assert deleted == 2
        assert vault.list_keys(1) == []
        assert vault.list_keys(2) == ["c"]

    def test_delete_missing_returns_zero(self, vault):
        assert vault.delete(999) == 0
        assert vault.delete(999, "missing") == 0

    def test_list_keys_sorted(self, vault):
        vault.store(1, "zebra", "z")
        vault.store(1, "apple", "a")
        vault.store(1, "mango", "m")
        assert vault.list_keys(1) == ["apple", "mango", "zebra"]

    def test_list_keys_never_returns_values(self, vault):
        vault.store(1, "secret_key", "very-secret-value")
        keys = vault.list_keys(1)
        assert keys == ["secret_key"]
        # Sanity: the value string should not appear in the list output
        assert "very-secret-value" not in str(keys)

    def test_has_credentials_true_when_present(self, vault):
        vault.store(1, "k", "v")
        assert vault.has_credentials(1) is True

    def test_has_credentials_false_when_absent(self, vault):
        assert vault.has_credentials(999) is False

    def test_has_credentials_false_after_delete_all(self, vault):
        vault.store(1, "k", "v")
        vault.delete(1)
        assert vault.has_credentials(1) is False


# ---------------------------------------------------------------------------
# migrate_staging
# ---------------------------------------------------------------------------


class TestMigrateStaging:
    def test_migrates_staging_to_real_script(self, vault):
        # Staging uses script_id=0
        vault.store(0, "api_key", "alpha")
        vault.store(0, "api_secret", "beta")
        moved = vault.migrate_staging(target_script_id=42)
        assert moved == 2
        # Source removed
        assert vault.list_keys(0) == []
        # Destination populated
        assert vault.retrieve(42, "api_key") == "alpha"
        assert vault.retrieve(42, "api_secret") == "beta"

    def test_returns_zero_when_no_staging(self, vault):
        assert vault.migrate_staging(target_script_id=42) == 0

    def test_overwrites_existing_destination_key(self, vault):
        vault.store(42, "api_key", "old")
        vault.store(0, "api_key", "new")
        vault.migrate_staging(target_script_id=42)
        assert vault.retrieve(42, "api_key") == "new"

    def test_does_not_disturb_other_scripts(self, vault):
        vault.store(0, "k_staging", "s")
        vault.store(99, "k_other", "o")
        vault.migrate_staging(target_script_id=42)
        assert vault.retrieve(99, "k_other") == "o"
        assert vault.retrieve(42, "k_staging") == "s"


# ---------------------------------------------------------------------------
# Tamper detection + key rotation
# ---------------------------------------------------------------------------


class TestTamperAndKeyRotation:
    def test_modified_ciphertext_raises_runtime_error(self, vault):
        vault.store(1, "k", "v")
        with sqlite3.connect(vault._db_path) as conn:
            conn.execute(
                "UPDATE credentials SET encrypted_value=? WHERE script_id=?",
                (b"tampered-ciphertext", 1),
            )
            conn.commit()
        with pytest.raises(RuntimeError, match="Failed to decrypt"):
            vault.retrieve(1, "k")

    def test_wrong_key_after_rotation_raises(self, vault, tmp_path):
        from scheduled_input_engine.credentials import CredentialVault
        vault.store(1, "k", "v")
        # Replace the master key with a fresh, unrelated key
        vault._key_file.write_bytes(Fernet.generate_key() + b"\n")
        # Stat must remain 0600 (Wave 2 fix would otherwise raise)
        import os
        import stat as _stat
        os.chmod(vault._key_file, _stat.S_IRUSR | _stat.S_IWUSR)
        # Reset the cached fernet so next call re-reads the new key
        vault._fernet = None
        with pytest.raises(RuntimeError, match="Failed to decrypt"):
            vault.retrieve(1, "k")


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_get_fernet_returns_same_instance(self, vault):
        results: list = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(vault._get_fernet())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Double-checked locking should give every thread the same Fernet
        assert all(f is results[0] for f in results)


# ---------------------------------------------------------------------------
# Global credential vault (added 2026-04-23 - one-to-many reuse)
# ---------------------------------------------------------------------------
#
# These tests pin the contract that:
#   * Globals are keyed by name only - no script_id.
#   * A script that declares FRED_API_KEY in requires_credentials picks it
#     up from the global vault automatically, even if the script's own
#     (script_id, key_name) row never existed.
#   * Per-task credentials OVERRIDE globals when both are set (rotation /
#     A-B testing / isolated deployments).
#   * list_keys(script_id) merges both layers by default so the UI pill
#     reflects reality.
#   * Deleting a global leaves per-task overrides intact.


class TestGlobalVault:
    def test_store_and_retrieve_global(self, vault):
        vault.store_global("FRED_API_KEY", "fred-global-abc123")
        assert vault.retrieve_global("FRED_API_KEY") == "fred-global-abc123"
        assert vault.has_global("FRED_API_KEY") is True
        assert vault.has_global("NOT_SET") is False

    def test_list_global_keys_returns_sorted_names(self, vault):
        vault.store_global("ZETA_KEY", "zz")
        vault.store_global("ALPHA_KEY", "aa")
        vault.store_global("MU_KEY", "mm")
        assert vault.list_global_keys() == ["ALPHA_KEY", "MU_KEY", "ZETA_KEY"]

    def test_store_global_is_upsert(self, vault):
        vault.store_global("ROTATE_KEY", "first-version")
        vault.store_global("ROTATE_KEY", "second-version")
        assert vault.retrieve_global("ROTATE_KEY") == "second-version"
        assert vault.list_global_keys() == ["ROTATE_KEY"]

    def test_retrieve_missing_global_raises(self, vault):
        with pytest.raises(KeyError, match="NOT_SET"):
            vault.retrieve_global("NOT_SET")

    def test_global_injects_into_decrypt_for_script(self, vault):
        """A global credential resolves for any script_id without a
        per-task row of its own - the whole point of the feature."""
        vault.store_global("FRED_API_KEY", "global-fred")
        # Script 42 has NEVER stored any credentials, yet it resolves
        # the global value through decrypt_for_script.
        creds = vault.decrypt_for_script(42)
        assert creds is not None
        assert creds["FRED_API_KEY"] == "global-fred"

    def test_per_task_overrides_global(self, vault):
        vault.store_global("FRED_API_KEY", "shared-global")
        vault.store(7, "FRED_API_KEY", "script-specific")
        creds = vault.decrypt_for_script(7)
        assert creds["FRED_API_KEY"] == "script-specific"
        # A different script without its own override still sees the global
        other = vault.decrypt_for_script(99)
        assert other["FRED_API_KEY"] == "shared-global"

    def test_decrypt_returns_none_when_both_empty(self, vault):
        """Preserve the M-SV-4 'None = truly empty' sentinel - globals
        mustn't magic that into an empty mapping."""
        assert vault.decrypt_for_script(999) is None

    def test_list_keys_merges_globals_by_default(self, vault):
        vault.store_global("FRED_API_KEY", "g")
        vault.store(5, "TASK_ONLY_KEY", "t")
        # Merged view (default) - both should show up
        merged = vault.list_keys(5)
        assert set(merged) == {"FRED_API_KEY", "TASK_ONLY_KEY"}
        # Per-task-only view - globals hidden, only the task-specific row
        task_only = vault.list_keys(5, include_global=False)
        assert task_only == ["TASK_ONLY_KEY"]

    def test_delete_global_preserves_per_task_override(self, vault):
        vault.store_global("FRED_API_KEY", "global-value")
        vault.store(3, "FRED_API_KEY", "task-value")
        assert vault.delete_global("FRED_API_KEY") == 1
        # Per-task row still works
        assert vault.decrypt_for_script(3)["FRED_API_KEY"] == "task-value"
        # A script without a per-task override no longer sees the key
        assert vault.decrypt_for_script(88) is None

    def test_delete_missing_global_returns_zero(self, vault):
        assert vault.delete_global("NEVER_SET") == 0

    def test_global_store_validates_input(self, vault):
        # Reuses the same validator as per-task store, so whitespace /
        # shell metacharacters / etc. are rejected consistently.
        with pytest.raises(ValueError):
            vault.store_global("BAD KEY", "value")  # space in key
        with pytest.raises(ValueError):
            vault.store_global("GOOD_KEY", "bad value")  # space in value
        with pytest.raises(ValueError):
            vault.store_global("GOOD_KEY", "")  # empty value

    def test_global_decrypt_survives_corrupt_row(self, vault, tmp_path):
        """If one global's ciphertext is corrupted, the others still
        decrypt (and the corrupt one is dropped with a log warning - per
        the same 'tamper skip' contract as per-task rows)."""
        vault.store_global("GOOD_KEY", "ok")
        vault.store_global("BAD_KEY", "will-be-corrupted")
        # Corrupt only the BAD_KEY row
        with sqlite3.connect(vault._db_path) as conn:
            conn.execute(
                "UPDATE credentials_global SET encrypted_value = ? WHERE key_name = ?",
                (b"not-a-valid-fernet-token", "BAD_KEY"),
            )
        creds = vault.decrypt_for_script(100)
        assert creds is not None
        assert creds["GOOD_KEY"] == "ok"
        assert "BAD_KEY" not in creds
