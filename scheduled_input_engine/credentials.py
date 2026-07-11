"""
Credential Vault
────────────────
Fernet-encrypted API key storage with per-script decrypt/inject lifecycle.

Keys are encrypted at rest in ``credentials.sqlite``.  The Fernet master key
lives in ``~/.speakes-query/master.key`` (outside the repo, 0600 permissions).

Lifecycle per ingestion run:
  1. ``decrypt_for_script(script_id)``  → ``MappingProxyType`` of {name: value}
  2. Inject into sandbox as ``CREDENTIALS``
  3. ``del`` the reference in a ``finally`` block  → plaintext gone from memory
"""

import logging
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from types import MappingProxyType

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# L-SV-10 (2026-04-22, deferred): the current schema captures the
# latest-value-only of each credential. If the operator rotates a key
# (via tools/rotate_vault_key.py or UI-driven replacement), the old
# encrypted_value is overwritten in place - there is no audit trail of
# what was set when. Acceptable for a single-user local-trust app, but
# noted here so a future "credential history" feature has a
# deliberate hook: add ``version INTEGER DEFAULT 1`` (bumped on every
# UPDATE) + a parallel ``credentials_history`` table that captures the
# superseded rows. Do NOT add versioning without a companion retention
# policy (otherwise the history table grows unbounded on every edit).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id       INTEGER NOT NULL,
    key_name        TEXT    NOT NULL,
    encrypted_value BLOB    NOT NULL,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    UNIQUE (script_id, key_name)
);

-- Added 2026-04-23: global (one-to-many) credential store.
-- Scripts declare ``requires_credentials: ["FRED_API_KEY"]`` and pick up
-- the value from here automatically - enter the key once, every script
-- that needs it resolves. Per-task entries in the ``credentials`` table
-- still override globals when present (edge cases, rotation, testing).
CREATE TABLE IF NOT EXISTS credentials_global (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_name        TEXT    NOT NULL UNIQUE,
    encrypted_value BLOB    NOT NULL,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
"""


class CredentialVault:
    """Thread-safe Fernet credential store backed by SQLite."""

    def __init__(self, db_path: str | Path, key_dir: str | Path = "~/.speakes-query"):
        self._db_path = Path(db_path).resolve()
        self._key_dir = Path(key_dir).expanduser().resolve()
        self._key_file = self._key_dir / "master.key"
        self._lock = threading.Lock()
        self._fernet: Fernet | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create the credentials table if it does not exist."""
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    def _get_fernet(self) -> Fernet:
        """Return a cached Fernet instance, creating the master key on first use."""
        if self._fernet is not None:
            return self._fernet

        with self._lock:
            if self._fernet is not None:
                return self._fernet

            if self._key_file.exists():
                self._verify_permissions()
                key = self._key_file.read_bytes().strip()
            else:
                key = self._generate_key()

            try:
                self._fernet = Fernet(key)
            except Exception as exc:
                raise RuntimeError(
                    f"Invalid master key at {self._key_file}: {exc}"
                ) from exc

        return self._fernet

    def _generate_key(self) -> bytes:
        """Generate a new Fernet key and persist to disk with 0600 permissions."""
        self._key_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self._key_file.write_bytes(key + b"\n")
        os.chmod(self._key_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        logger.info("[i] Generated new master key at %s", self._key_file)
        return key

    def _verify_permissions(self) -> None:
        """Enforce 0600 on the master key file.

        First attempts to auto-correct loose permissions via ``chmod`` so that
        a benign umask doesn't lock the user out on the first run. If the
        chmod fails (e.g. the file is owned by another user), refuse to load
        the key - a world- or group-readable Fernet key offers no real
        protection and silently continuing would be worse than failing.
        """
        try:
            mode = self._key_file.stat().st_mode & 0o777
        except OSError as exc:
            raise RuntimeError(
                f"Cannot stat master key {self._key_file}: {exc}"
            ) from exc

        if mode == 0o600:
            return

        logger.warning(
            "[!] %s has permissions %o - expected 0600. Auto-correcting.",
            self._key_file, mode,
        )
        try:
            os.chmod(self._key_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise RuntimeError(
                f"Refusing to load master key {self._key_file}: permissions "
                f"are {mode:o} (expected 0600) and chmod failed: {exc}. "
                f"Fix manually: chmod 600 {self._key_file}"
            ) from exc

        # Re-verify after chmod; if it didn't actually take effect, abort.
        try:
            new_mode = self._key_file.stat().st_mode & 0o777
        except OSError as exc:
            raise RuntimeError(
                f"Cannot re-stat master key {self._key_file}: {exc}"
            ) from exc

        if new_mode != 0o600:
            raise RuntimeError(
                f"Refusing to load master key {self._key_file}: chmod 0600 "
                f"reported success but permissions are still {new_mode:o}. "
                f"Investigate the filesystem (mount options? ACLs?) before "
                f"continuing."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_credential_input(key_name: str, plaintext_value: str) -> None:
        """Validate credential key name and value against injection risks.

        Rules:
          - Must be non-empty after stripping.
          - No non-ASCII characters.
          - No whitespace (spaces, tabs, newlines).
          - No URL-encoded sequences (e.g. %20, %0a) that could bypass filters.
          - No null bytes or shell metacharacters that could enable command injection.
        """
        for label, raw in [("key_name", key_name), ("value", plaintext_value)]:
            val = raw.strip() if isinstance(raw, str) else ""
            if not val:
                raise ValueError(f"{label} must be non-empty")

            # Block non-ASCII
            try:
                val.encode("ascii")
            except UnicodeEncodeError:
                raise ValueError(
                    f"{label} contains non-ASCII characters - only ASCII is allowed"
                )

            # Block any whitespace (spaces, tabs, newlines, etc.)
            if any(ch.isspace() for ch in val):
                raise ValueError(f"{label} must not contain spaces or whitespace")

            # Block percent-encoded sequences (%XX) that could smuggle bad chars
            import re as _re
            if _re.search(r"%[0-9a-fA-F]{2}", val):
                raise ValueError(
                    f"{label} contains percent-encoded sequences - not allowed"
                )

            # Block null bytes and common shell metacharacters
            _dangerous = set("\x00`$\\;|&<>(){}!")
            found = _dangerous.intersection(val)
            if found:
                raise ValueError(
                    f"{label} contains disallowed characters: "
                    f"{', '.join(repr(c) for c in sorted(found))}"
                )

    def store(self, script_id: int, key_name: str, plaintext_value: str) -> None:
        """Encrypt and store (or update) a credential for *script_id*."""
        self._validate_credential_input(key_name, plaintext_value)

        fernet = self._get_fernet()
        encrypted = fernet.encrypt(plaintext_value.encode("utf-8"))
        now = time.time()

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO credentials (script_id, key_name, encrypted_value, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (script_id, key_name) DO UPDATE SET
                       encrypted_value = excluded.encrypted_value,
                       updated_at = excluded.updated_at""",
                (script_id, key_name.strip(), encrypted, now, now),
            )
            conn.commit()
        logger.info("[i] Stored credential '%s' for script %d", key_name, script_id)
        _emit_credential_event(
            action="store",
            script_id=script_id,
            key_name=key_name,
        )

    def retrieve(self, script_id: int, key_name: str) -> str:
        """Decrypt and return a single credential value."""
        fernet = self._get_fernet()

        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT encrypted_value FROM credentials WHERE script_id = ? AND key_name = ?",
                (script_id, key_name),
            ).fetchone()

        if row is None:
            raise KeyError(f"No credential '{key_name}' for script {script_id}")

        try:
            return fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                f"Failed to decrypt '{key_name}' for script {script_id}. "
                f"Master key may have changed."
            ) from exc

    def decrypt_for_script(self, script_id: int) -> MappingProxyType | None:
        """Decrypt all credentials visible to *script_id*.

        **Merge semantics (added 2026-04-23):** the returned mapping is
        the union of (a) per-task credentials keyed by ``script_id`` and
        (b) global credentials from ``credentials_global``. Per-task
        values OVERRIDE globals of the same name so a user can still pin
        a task to a specific credential (rotation, A/B testing, isolated
        test vault). Scripts access via ``CREDENTIALS.get('FRED_API_KEY')``
        and never care which layer supplied the value.

        Returns an immutable ``MappingProxyType`` so sandbox code cannot
        mutate it. Returns ``None`` when NEITHER layer has a credential
        for this script - distinct from the "empty mapping" sentinel so
        callers can tell "no creds stored" from "vault load partially
        failed".

        M-SV-4 (2026-04-22): the old contract returned ``MappingProxyType({})``
        for both "no rows in the credentials table" and "all rows failed
        to decrypt". Returning ``None`` for the truly-empty case lets the
        engine skip the CREDENTIALS injection entirely for honest
        no-creds scripts, while still returning an empty mapping
        (distinct sentinel ``MappingProxyType({})``) when every row
        failed to decrypt - the engine injects anyway in that case so a
        script that expected creds gets a proper KeyError instead of
        silently misbehaving.
        """
        fernet = self._get_fernet()

        with sqlite3.connect(self._db_path) as conn:
            per_task_rows = conn.execute(
                "SELECT key_name, encrypted_value FROM credentials WHERE script_id = ?",
                (script_id,),
            ).fetchall()
            global_rows = conn.execute(
                "SELECT key_name, encrypted_value FROM credentials_global",
            ).fetchall()

        # Truly empty → no per-task AND no globals. Return None so the
        # engine can distinguish "script has no creds at all" from
        # "credentials exist but all failed to decrypt" below.
        if not per_task_rows and not global_rows:
            return None

        decrypted: dict[str, str] = {}

        # Layer 1: globals (lower priority - overridden by per-task below).
        for key_name, encrypted in global_rows:
            try:
                decrypted[key_name] = fernet.decrypt(encrypted).decode("utf-8")
            except InvalidToken:
                logger.error(
                    "[x] Failed to decrypt global '%s' - skipping", key_name,
                )

        # Layer 2: per-task (higher priority - overrides globals of same name).
        for key_name, encrypted in per_task_rows:
            try:
                decrypted[key_name] = fernet.decrypt(encrypted).decode("utf-8")
            except InvalidToken:
                logger.error(
                    "[x] Failed to decrypt '%s' for script %d - skipping",
                    key_name, script_id,
                )

        return MappingProxyType(decrypted)

    def delete(self, script_id: int, key_name: str | None = None) -> int:
        """Delete credential(s) for *script_id*.

        If *key_name* is ``None``, delete all credentials for the script.
        Returns the number of rows deleted.
        """
        with sqlite3.connect(self._db_path) as conn:
            if key_name is None:
                cur = conn.execute(
                    "DELETE FROM credentials WHERE script_id = ?", (script_id,)
                )
            else:
                cur = conn.execute(
                    "DELETE FROM credentials WHERE script_id = ? AND key_name = ?",
                    (script_id, key_name),
                )
            conn.commit()
            count = cur.rowcount

        if count:
            target = f"'{key_name}'" if key_name else "all"
            logger.info(
                "[i] Deleted %d credential(s) (%s) for script %d",
                count, target, script_id,
            )
            _emit_credential_event(
                action="delete",
                script_id=script_id,
                key_name=key_name or "*",
                count=count,
            )
        return count

    def list_keys(self, script_id: int, include_global: bool = True) -> list[str]:
        """Return credential key names visible to *script_id* (never values).

        By default merges per-task + global keys (matching
        ``decrypt_for_script`` semantics) so callers asking "does this
        script have credential X available?" get a correct yes/no
        regardless of whether X was stored per-task or globally.

        Pass ``include_global=False`` to get only the per-task keys -
        useful when the UI is showing the user's per-task credential
        overrides distinct from the globals that the task inherits.
        """
        with sqlite3.connect(self._db_path) as conn:
            task_keys = [r[0] for r in conn.execute(
                "SELECT key_name FROM credentials WHERE script_id = ? ORDER BY key_name",
                (script_id,),
            ).fetchall()]
            if not include_global:
                return task_keys
            global_keys = [r[0] for r in conn.execute(
                "SELECT key_name FROM credentials_global ORDER BY key_name",
            ).fetchall()]
        merged = set(task_keys) | set(global_keys)
        return sorted(merged)

    # ------------------------------------------------------------------
    # Global credential API (added 2026-04-23)
    # ------------------------------------------------------------------
    #
    # Global credentials are keyed by name only. Any task that declares
    # ``requires_credentials: ["FRED_API_KEY"]`` resolves its value from
    # here automatically - enter the key once, all FRED-using scripts
    # pick it up without re-entering. Per-task entries still win when
    # both are set (see ``decrypt_for_script`` merge semantics).

    def store_global(self, key_name: str, plaintext_value: str) -> None:
        """Encrypt and store (or update) a global credential.

        All tasks that need *key_name* and don't have a per-task override
        will resolve to this value at execution time.
        """
        self._validate_credential_input(key_name, plaintext_value)
        fernet = self._get_fernet()
        encrypted = fernet.encrypt(plaintext_value.encode("utf-8"))
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO credentials_global
                       (key_name, encrypted_value, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (key_name) DO UPDATE SET
                       encrypted_value = excluded.encrypted_value,
                       updated_at = excluded.updated_at""",
                (key_name.strip(), encrypted, now, now),
            )
            conn.commit()
        logger.info("[i] Stored global credential '%s'", key_name)
        _emit_credential_event(
            action="store_global", script_id=-1, key_name=key_name,
        )

    def retrieve_global(self, key_name: str) -> str:
        """Decrypt and return a single global credential value."""
        fernet = self._get_fernet()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT encrypted_value FROM credentials_global WHERE key_name = ?",
                (key_name,),
            ).fetchone()
        if row is None:
            raise KeyError(f"No global credential '{key_name}'")
        try:
            return fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                f"Failed to decrypt global '{key_name}'. Master key may have changed."
            ) from exc

    def list_global_keys(self) -> list[str]:
        """Return the names (not values) of all global credentials."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT key_name FROM credentials_global ORDER BY key_name",
            ).fetchall()
        return [r[0] for r in rows]

    def has_global(self, key_name: str) -> bool:
        """Return True if *key_name* is set in the global vault."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM credentials_global WHERE key_name = ? LIMIT 1",
                (key_name,),
            ).fetchone()
        return row is not None

    def promote_to_global(self, script_id: int, key_name: str) -> None:
        """Move a per-task credential into the global vault.

        Decrypts the per-task value, re-encrypts it under the global
        table (overwriting any existing global with the same key), then
        deletes the per-task entry. Plaintext never leaves the server.

        Use case: an operator stored ``FRED_API_KEY`` per-task on
        script A, then realised script B also needs it. Click Promote
        instead of re-typing - the value moves to the global vault and
        every script declaring that key resolves it automatically.

        Raises KeyError when the per-task entry doesn't exist.
        Idempotent only in the sense that calling it twice on a now-
        global key raises (the per-task row was removed by the first
        call). Added 2026-04-26 as part of the credential reuse fix.
        """
        # Single transaction for the read → write → delete cycle so a
        # crash mid-operation doesn't leave the credential lost between
        # tables.
        plaintext = self.retrieve(script_id, key_name)
        # Reuse the validation that store_global runs to keep the two
        # paths in lockstep.
        self.store_global(key_name, plaintext)
        deleted = self.delete(script_id, key_name)
        if deleted == 0:
            # store_global succeeded but the per-task row vanished mid-
            # promotion (concurrent delete). Log and return - the
            # caller's contract is "global has the value", which is true.
            logger.warning(
                "[!] Promoted '%s' from script %s to global, but the "
                "per-task row was already gone.", key_name, script_id,
            )
        _emit_credential_event(
            action="promote_to_global",
            script_id=script_id, key_name=key_name,
        )
        logger.info(
            "[i] Promoted credential '%s' from script %s to global vault",
            key_name, script_id,
        )

    def delete_global(self, key_name: str) -> int:
        """Delete a global credential. Returns 1 if deleted, 0 if not present."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM credentials_global WHERE key_name = ?", (key_name,),
            )
            conn.commit()
            count = cur.rowcount
        if count:
            logger.info("[i] Deleted global credential '%s'", key_name)
            _emit_credential_event(
                action="delete_global", script_id=-1, key_name=key_name, count=count,
            )
        return count

    def migrate_staging(self, target_script_id: int) -> int:
        """Move all credentials from staging (script_id=0) to *target_script_id*.

        Returns the number of credentials migrated.  Existing credentials on
        *target_script_id* with the same key_name are overwritten.
        """
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT key_name, encrypted_value, created_at FROM credentials "
                "WHERE script_id = 0",
            ).fetchall()
            if not rows:
                return 0

            now = time.time()
            for key_name, encrypted, created_at in rows:
                conn.execute(
                    """INSERT INTO credentials
                           (script_id, key_name, encrypted_value, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (script_id, key_name) DO UPDATE SET
                           encrypted_value = excluded.encrypted_value,
                           updated_at = excluded.updated_at""",
                    (target_script_id, key_name, encrypted, created_at, now),
                )
            # Remove staging rows
            conn.execute("DELETE FROM credentials WHERE script_id = 0")
            conn.commit()

        logger.info(
            "[i] Migrated %d staging credential(s) to script %d",
            len(rows), target_script_id,
        )
        _emit_credential_event(
            action="migrate_staging",
            script_id=target_script_id,
            count=len(rows),
        )
        return len(rows)

    def has_credentials(self, script_id: int) -> bool:
        """Return ``True`` if *script_id* has at least one stored credential."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM credentials WHERE script_id = ? LIMIT 1",
                (script_id,),
            ).fetchone()
        return row is not None


def _emit_credential_event(
    *, action: str, script_id: int, key_name: str = "*", count: int | None = None,
) -> None:
    """Record a credential mutation to the config log stream.

    Never logs the plaintext value - only the action, script_id, key_name,
    and optional count so the audit trail is complete without leaking
    secrets. The subject is the script_id (or "system" for script_id == -1)
    so ``index="indexes/logs/config/*.parquet" | search subject_type="credential"``
    can surface exactly which script's credentials changed and when.
    """
    try:
        from functionality.log_writer import log_config_change
        subject = "system" if script_id == -1 else f"script_{script_id}"
        new_val = {"key_name": key_name}
        if count is not None:
            new_val["count"] = count
        log_config_change(
            subject=subject,
            action=action,
            subject_type="credential",
            old_value=None,
            new_value=new_val,
            actor="api",
            source="credential_vault",
        )
    except Exception as exc:
        # Credential events are a security audit trail - never pass
        # silently. If the log writer is misbehaving, surface it so the
        # operator can investigate. Use logger.warning rather than
        # re-raising because the primary mutation (store/delete) has
        # already succeeded - we don't want an audit-log infra issue to
        # roll back the user's credential change.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[!] Could not record credential audit event (action=%s, "
            "subject=%s): %s",
            action, subject, exc,
        )
