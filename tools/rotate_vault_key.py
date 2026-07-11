"""Fernet master-key rotation for the SpeakesQuery credential vault.

M-SV-5 (2026-04-22): documents the rotation procedure and ships an
operator utility that can re-encrypt every row of ``credentials.sqlite``
under a newly-generated Fernet key. See ``docs/lang/13_backup_recovery.md``
→ "Credential vault master-key rotation" for the full procedure context.

Scope (deliberately narrow)
---------------------------
- Reads one row at a time from the existing ``credentials`` table.
- Decrypts with the OLD key, re-encrypts with the NEW key.
- Writes the re-encrypted rows to a sibling database at
  ``<db>.rotated.sqlite`` so the original is untouched until the
  operator explicitly swaps files.
- Writes the generated NEW key to the operator-supplied path.

**Out of scope (do not add here without design review):**

* Hot rotation (the vault caches the Fernet instance per process; a
  restart is required - this tool will NOT ping the running server).
* Automatic file swap (the doc procedure has the operator rename
  ``<db>.rotated.sqlite`` → ``credentials.sqlite`` manually so a bad
  rotation can't clobber the source of truth).
* Multi-key support (every row uses a single master key; multi-key
  layouts would be a schema change).

Usage
-----
.. code-block:: bash

    python -m tools.rotate_vault_key \\
        --old-key ~/.speakes-query/master.key \\
        --new-key ~/.speakes-query/master.new.key \\
        --db     credentials.sqlite \\
        --dry-run

The ``--dry-run`` flag reads + decrypts each row but does NOT write the
new DB or generate the new key file. Use it first to confirm every row
decrypts cleanly under the OLD key.

Exit codes
----------
* ``0`` - success (or dry-run pass).
* ``1`` - one or more rows failed to decrypt under the OLD key; no DB
  was written.
* ``2`` - argument / filesystem error.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("tools.rotate_vault_key")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rotate_vault_key",
        description=(
            "Re-encrypt every row of credentials.sqlite under a new Fernet "
            "master key. See docs/lang/13_backup_recovery.md → 'Credential "
            "vault master-key rotation' for the full procedure."
        ),
    )
    p.add_argument(
        "--old-key", required=True,
        help="Path to the current master.key file.",
    )
    p.add_argument(
        "--new-key", required=True,
        help=(
            "Path to write the newly-generated master.key. Must NOT exist "
            "yet (the tool refuses to overwrite an existing file)."
        ),
    )
    p.add_argument(
        "--db", required=True,
        help=(
            "Path to the credentials SQLite database (typically "
            "credentials.sqlite in the project root)."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Decrypt every row with the OLD key but do not write the new "
            "DB or the new key file. Use this to confirm all rows are "
            "readable before the real rotation."
        ),
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Emit one log line per row processed.",
    )
    return p


def rotate(
    old_key_path: Path,
    new_key_path: Path,
    db_path: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Core rotation routine. Returns an exit code suitable for ``sys.exit()``."""
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        logger.error(
            "[x] cryptography.Fernet is not installed. "
            "pip install cryptography"
        )
        return 2

    if not old_key_path.is_file():
        logger.error("[x] old-key not found: %s", old_key_path)
        return 2
    if not db_path.is_file():
        logger.error("[x] db not found: %s", db_path)
        return 2
    if new_key_path.exists() and not dry_run:
        logger.error(
            "[x] new-key path already exists: %s. "
            "Refusing to overwrite - move it aside or pick a different path.",
            new_key_path,
        )
        return 2

    old_key = old_key_path.read_bytes().strip()
    try:
        old_fernet = Fernet(old_key)
    except Exception as exc:
        logger.error("[x] could not initialise Fernet with old-key: %s", exc)
        return 2

    new_key = Fernet.generate_key()
    new_fernet = Fernet(new_key)

    # Read every row with the old key. Any decrypt failure aborts the
    # rotation - we don't want to produce a new DB that silently drops
    # rows. The operator can investigate the failed row and re-run.
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, script_id, key_name, encrypted_value FROM credentials"
        ).fetchall()

    re_encrypted: list[tuple[int, int, str, bytes]] = []
    failures: list[tuple[int, int, str, str]] = []
    for r in rows:
        try:
            plaintext = old_fernet.decrypt(r["encrypted_value"])
        except InvalidToken as exc:
            failures.append((r["id"], r["script_id"], r["key_name"], str(exc)))
            continue
        new_blob = new_fernet.encrypt(plaintext)
        re_encrypted.append(
            (r["id"], r["script_id"], r["key_name"], new_blob),
        )
        if verbose:
            logger.info(
                "[i] re-encrypted id=%d script=%d key=%s",
                r["id"], r["script_id"], r["key_name"],
            )

    if failures:
        logger.error(
            "[x] %d row(s) failed to decrypt under the old key. "
            "No new DB written. Failures:",
            len(failures),
        )
        for row_id, script_id, key_name, msg in failures:
            logger.error(
                "    id=%d script=%d key=%s error=%s",
                row_id, script_id, key_name, msg,
            )
        return 1

    logger.info(
        "[i] Successfully re-encrypted %d row(s) under the new key.",
        len(re_encrypted),
    )

    if dry_run:
        logger.info(
            "[i] Dry-run complete. No files written. Run without "
            "--dry-run to produce %s and %s.",
            db_path.with_suffix(db_path.suffix + ".rotated.sqlite"),
            new_key_path,
        )
        return 0

    # Write the new DB to a sibling file so the original is only touched
    # by the operator's explicit rename step (per the documented
    # procedure).
    rotated_db = db_path.with_suffix(db_path.suffix + ".rotated.sqlite")
    if rotated_db.exists():
        logger.error(
            "[x] rotated DB already exists: %s. "
            "Remove it manually before re-running.",
            rotated_db,
        )
        return 2

    # Copy the schema from the source DB so columns / indexes match.
    # Filter out SQLite-managed internal tables (``sqlite_sequence`` is
    # auto-created by AUTOINCREMENT columns and cannot be CREATEd
    # directly - the name is reserved).
    with sqlite3.connect(str(db_path)) as src, \
            sqlite3.connect(str(rotated_db)) as dst:
        for stmt, in src.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            dst.execute(stmt)
        dst.executemany(
            "INSERT INTO credentials "
            "(id, script_id, key_name, encrypted_value) "
            "VALUES (?, ?, ?, ?)",
            re_encrypted,
        )
        dst.commit()

    new_key_path.parent.mkdir(parents=True, exist_ok=True)
    new_key_path.write_bytes(new_key)
    # 0600 perms - credential-key files should not be world-readable.
    try:
        new_key_path.chmod(0o600)
    except OSError:
        # Windows / filesystems that don't support POSIX perms: warn but
        # proceed. The operator's doc procedure calls out the ACL check
        # separately.
        logger.warning(
            "[!] Could not set 0600 on %s - verify access control manually.",
            new_key_path,
        )

    logger.info(
        "[i] New DB written to %s. New master key written to %s.",
        rotated_db, new_key_path,
    )
    logger.info(
        "[i] Next steps (per docs/lang/13_backup_recovery.md):\n"
        "    1. cp %s %s.backup-$(date +%%F)\n"
        "    2. mv %s %s\n"
        "    3. mv %s %s\n"
        "    4. Restart SpeakesQuery and verify one ingestion run.",
        db_path, db_path,
        old_key_path, old_key_path.with_suffix(old_key_path.suffix + ".pre-rotate"),
        new_key_path, old_key_path,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return rotate(
        old_key_path=Path(args.old_key).expanduser().resolve(),
        new_key_path=Path(args.new_key).expanduser().resolve(),
        db_path=Path(args.db).expanduser().resolve(),
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
