"""Regression tests for scheduled_input_engine/store.py migrations.

Pins the 2026-04-21 H-SV-1 fix: any row with NULL or empty ``trust_level``
in scheduled_inputs.db is backfilled to 'sandboxed' on the next engine
startup. Companion engine-layer log is covered separately.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _fresh_store(tmp_path: Path, monkeypatch):
    """Return a ScheduledInputStore pointed at a temp inputs DB + history DB."""
    from scheduled_input_engine import store as store_mod

    inputs_db = tmp_path / "scheduled_inputs.db"
    history_db = tmp_path / "scheduled_inputs_history.db"
    monkeypatch.setattr(store_mod, "SCHEDULED_INPUTS_DB", inputs_db)
    monkeypatch.setattr(store_mod, "HISTORY_DB", history_db)

    # Re-import-safe: construct after monkeypatch so __init__ picks up the
    # swapped paths.
    store = store_mod.ScheduledInputStore()
    return store, inputs_db


# ----------------------------------------------------------------------
# H-SV-1: NULL trust_level must be backfilled on initialize
# ----------------------------------------------------------------------

class TestTrustLevelBackfill:

    def test_null_trust_level_backfilled_to_sandboxed(self, tmp_path, monkeypatch, caplog):
        """A legacy row with NULL trust_level must be backfilled on init.

        Note on setup: SQLite's ``ALTER TABLE ... ADD COLUMN ... DEFAULT 'x'``
        populates existing rows with the default, so simply simulating a
        pre-migration schema does NOT produce a NULL row. Instead we create
        the full current schema, insert a row, then force the column to NULL
        with an explicit UPDATE - mirroring a manual DB edit or a bad
        upstream migration that left a row with NULL trust_level.
        """
        store, inputs_db = _fresh_store(tmp_path, monkeypatch)

        # Bring the DB to the current schema with the migration path first.
        store.initialize_databases()

        # Insert a row, then null out its trust_level to simulate corruption.
        with sqlite3.connect(str(inputs_db)) as conn:
            conn.execute(
                "INSERT INTO scheduled_inputs (title, code, cron_schedule) "
                "VALUES ('legacy_task', 'df = 1', '* * * * *')"
            )
            conn.execute(
                "UPDATE scheduled_inputs SET trust_level = NULL WHERE title = 'legacy_task'"
            )
            conn.commit()

        # Now re-run initialize_databases: the ADD COLUMN step is skipped
        # (column exists) but the backfill UPDATE should fire and emit a
        # warning because rowcount > 0.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="scheduled_input_engine.store"):
            store.initialize_databases()

        # Assert the row now has trust_level='sandboxed'.
        with sqlite3.connect(str(inputs_db)) as conn:
            row = conn.execute(
                "SELECT trust_level FROM scheduled_inputs WHERE title = 'legacy_task'"
            ).fetchone()
        assert row is not None
        assert row[0] == "sandboxed", (
            f"Expected backfilled trust_level='sandboxed', got {row[0]!r}"
        )

        # The migration must log a warning when it touched any row.
        assert any(
            "Backfilled" in rec.getMessage() and "H-SV-1" in rec.getMessage()
            for rec in caplog.records
        ), (
            f"Expected the H-SV-1 backfill warning. records={[r.getMessage() for r in caplog.records]}"
        )

    def test_empty_string_trust_level_backfilled(self, tmp_path, monkeypatch):
        """An empty-string trust_level (not NULL) must also be backfilled."""
        store, inputs_db = _fresh_store(tmp_path, monkeypatch)
        store.initialize_databases()

        with sqlite3.connect(str(inputs_db)) as conn:
            conn.execute(
                "INSERT INTO scheduled_inputs "
                "(title, code, cron_schedule, trust_level) "
                "VALUES ('blank_task', 'df = 1', '* * * * *', '')"
            )
            conn.commit()

        # Re-run initialize_databases → backfill should fire for the blank row.
        store.initialize_databases()

        with sqlite3.connect(str(inputs_db)) as conn:
            row = conn.execute(
                "SELECT trust_level FROM scheduled_inputs WHERE title = 'blank_task'"
            ).fetchone()
        assert row[0] == "sandboxed"

    def test_existing_non_null_values_preserved(self, tmp_path, monkeypatch):
        """Backfill must NOT overwrite existing 'unrestricted' or 'sandboxed' rows."""
        store, inputs_db = _fresh_store(tmp_path, monkeypatch)
        store.initialize_databases()

        with sqlite3.connect(str(inputs_db)) as conn:
            conn.execute(
                "INSERT INTO scheduled_inputs "
                "(title, code, cron_schedule, trust_level) "
                "VALUES ('pro_task', 'df = 1', '* * * * *', 'unrestricted')"
            )
            conn.execute(
                "INSERT INTO scheduled_inputs "
                "(title, code, cron_schedule, trust_level) "
                "VALUES ('sbx_task', 'df = 1', '* * * * *', 'sandboxed')"
            )
            conn.commit()

        store.initialize_databases()  # re-run → idempotent

        with sqlite3.connect(str(inputs_db)) as conn:
            rows = dict(conn.execute(
                "SELECT title, trust_level FROM scheduled_inputs "
                "WHERE title IN ('pro_task', 'sbx_task')"
            ).fetchall())
        assert rows["pro_task"] == "unrestricted", "Must not overwrite pro row"
        assert rows["sbx_task"] == "sandboxed", "Idempotent on already-sandboxed"

    def test_idempotent_no_extra_warnings(self, tmp_path, monkeypatch, caplog):
        """Re-running initialize with no NULL rows must not emit the backfill warning."""
        store, inputs_db = _fresh_store(tmp_path, monkeypatch)
        store.initialize_databases()  # fresh create, no NULL rows

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="scheduled_input_engine.store"):
            store.initialize_databases()

        backfill_warnings = [
            r for r in caplog.records
            if "Backfilled" in r.getMessage() and "H-SV-1" in r.getMessage()
        ]
        assert backfill_warnings == [], (
            f"Idempotent re-run should emit no backfill warning; got {backfill_warnings}"
        )


# ----------------------------------------------------------------------
# H-SV-1: engine-level warning when the task record is missing trust_level
# ----------------------------------------------------------------------

class TestEngineTrustLevelFallbackLog:

    def test_missing_trust_level_logs_warning(self, caplog):
        """_run_task must emit a '[!] ... no explicit trust_level' log for legacy rows."""
        # We don't want to actually execute a script - just hit the early log
        # path and let downstream fail quickly. Use a minimal task dict with
        # no 'trust_level' key and a code body that will raise before any I/O.
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()
        try:
            task = {
                "id": 999999,
                "title": "legacy_missing_trust_level",
                # NO trust_level key - this is the regression surface.
                "code": "raise RuntimeError('test short-circuit')",
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_sv1",
            }
            with caplog.at_level(logging.WARNING, logger="scheduled_input_engine.engine"):
                # _run_task swallows RuntimeError and records failure; returns
                # normally. We only care about the log line.
                engine._run_task(task)

            assert any(
                "no explicit trust_level" in rec.getMessage()
                and str(task["id"]) in rec.getMessage()
                for rec in caplog.records
            ), (
                "Expected the H-SV-1 fallback warning. records=\n"
                + "\n".join(r.getMessage() for r in caplog.records)
            )
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

    def test_explicit_trust_level_does_not_log_warning(self, caplog):
        """A task with an explicit trust_level must NOT emit the fallback warning."""
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()
        try:
            task = {
                "id": 999998,
                "title": "explicit_trust_level_task",
                "trust_level": "sandboxed",
                "code": "raise RuntimeError('test short-circuit')",
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_sv1",
            }
            with caplog.at_level(logging.WARNING, logger="scheduled_input_engine.engine"):
                engine._run_task(task)

            fallback_warnings = [
                r for r in caplog.records
                if "no explicit trust_level" in r.getMessage()
            ]
            assert fallback_warnings == [], (
                f"Expected NO fallback warning for explicit trust_level; "
                f"got {[r.getMessage() for r in fallback_warnings]}"
            )
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass
