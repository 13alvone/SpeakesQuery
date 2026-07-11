"""H-CE-4 regression: execution_history composite primary key + legacy migration.

Pins the 2026-04-22 production-review fix: the original schema declared
``saved_search_filename TEXT PRIMARY KEY``, which silently overwrote rows
when two saved searches had the same filename in different folders.

After the fix the PK is ``(saved_search_filename, execution_start_time)``
- composite - and a migration path rebuilds the table when a legacy
single-column PK is detected on startup.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def tmp_history_db(tmp_path, monkeypatch):
    """Point QueryEngine.HISTORY_DB at a temp file for isolation."""
    from query_engine import QueryEngine
    db = tmp_path / "saved_search_history.db"
    monkeypatch.setattr(QueryEngine, "HISTORY_DB", str(db))
    return db


def _pragma_pk_cols(db_path: Path) -> list[str]:
    """Return the PK column names for execution_history, in PK order."""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("PRAGMA table_info(execution_history)").fetchall()
    # Filter rows with pk > 0 and sort by pk position.
    pk = [(r[5], r[1]) for r in rows if r[5] > 0]
    pk.sort(key=lambda p: p[0])
    return [name for _, name in pk]


# ----------------------------------------------------------------------
# Fresh install: composite PK is created from scratch.
# ----------------------------------------------------------------------

class TestFreshSchemaHasCompositePk:

    def test_initial_schema_uses_composite_primary_key(self, tmp_history_db):
        from query_engine.QueryEngine import initialize_history_db
        asyncio.run(initialize_history_db())

        pk_cols = _pragma_pk_cols(tmp_history_db)
        assert pk_cols == ["saved_search_filename", "execution_start_time"], (
            f"Fresh schema should declare composite PK; got {pk_cols}"
        )

    def test_repeat_runs_same_filename_different_start_time_coexist(
        self, tmp_history_db,
    ):
        """Two inserts with the same filename but different start_times both survive."""
        from query_engine.QueryEngine import initialize_history_db
        asyncio.run(initialize_history_db())

        with sqlite3.connect(str(tmp_history_db)) as conn:
            conn.execute(
                "INSERT INTO execution_history "
                "(saved_search_filename, runtime, execution_start_time, "
                "execution_end_time, query_name, saved_search_path, "
                "original_result_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("daily.csv", 1.2, 100.0, 101.2, "daily", "alpha/daily.csv", 10),
            )
            conn.execute(
                "INSERT INTO execution_history "
                "(saved_search_filename, runtime, execution_start_time, "
                "execution_end_time, query_name, saved_search_path, "
                "original_result_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("daily.csv", 1.5, 200.0, 201.5, "daily", "alpha/daily.csv", 12),
            )
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) FROM execution_history "
                "WHERE saved_search_filename = 'daily.csv'"
            ).fetchone()[0]
        assert n == 2, (
            f"Composite PK should allow both runs; got {n} row(s). "
            "Regression indicates the PK collapsed back to filename-only."
        )

    def test_two_folders_same_filename_at_same_time_raises(self, tmp_history_db):
        """Same filename AND same start_time: PK collision is expected and loud."""
        from query_engine.QueryEngine import initialize_history_db
        asyncio.run(initialize_history_db())

        with sqlite3.connect(str(tmp_history_db)) as conn:
            conn.execute(
                "INSERT INTO execution_history "
                "(saved_search_filename, runtime, execution_start_time, "
                "execution_end_time, query_name, saved_search_path, "
                "original_result_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("daily.csv", 1.0, 500.0, 501.0, "a-daily", "alpha/daily.csv", 5),
            )
            conn.commit()
            # Same filename + same start_time = composite-PK collision.
            # That's acceptable: two searches executing at the exact same
            # epoch second is nearly impossible in practice, and the
            # explicit IntegrityError is better than a silent overwrite.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO execution_history "
                    "(saved_search_filename, runtime, execution_start_time, "
                    "execution_end_time, query_name, saved_search_path, "
                    "original_result_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("daily.csv", 1.0, 500.0, 501.0, "b-daily", "beta/daily.csv", 7),
                )


# ----------------------------------------------------------------------
# Legacy-schema migration: existing DBs are rebuilt in place.
# ----------------------------------------------------------------------

class TestLegacySchemaMigration:

    def _seed_legacy(self, db_path: Path, rows: list[tuple]):
        """Create the old single-column-PK table and seed some rows."""
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute('''
                CREATE TABLE execution_history (
                    saved_search_filename TEXT PRIMARY KEY,
                    runtime REAL,
                    execution_start_time REAL,
                    execution_end_time REAL,
                    query_name TEXT,
                    saved_search_path TEXT,
                    original_result_count INTEGER
                )
            ''')
            conn.executemany(
                "INSERT INTO execution_history "
                "(saved_search_filename, runtime, execution_start_time, "
                "execution_end_time, query_name, saved_search_path, "
                "original_result_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()

    def test_legacy_schema_detected_and_rebuilt(self, tmp_history_db):
        """An existing single-column-PK table is migrated to composite without data loss."""
        from query_engine.QueryEngine import initialize_history_db

        self._seed_legacy(tmp_history_db, [
            ("alpha.csv", 1.0, 100.0, 101.0, "alpha",
             "alpha/daily.csv", 3),
            ("beta.csv", 2.0, 200.0, 202.0, "beta",
             "beta/daily.csv", 5),
        ])

        # Sanity: before migration, PK is single-column.
        assert _pragma_pk_cols(tmp_history_db) == ["saved_search_filename"]

        asyncio.run(initialize_history_db())

        # After migration, PK is composite.
        assert _pragma_pk_cols(tmp_history_db) == [
            "saved_search_filename", "execution_start_time",
        ]
        # Seeded rows are preserved.
        with sqlite3.connect(str(tmp_history_db)) as conn:
            rows = conn.execute(
                "SELECT saved_search_filename, runtime, "
                "execution_start_time, query_name FROM execution_history "
                "ORDER BY saved_search_filename"
            ).fetchall()
        assert rows == [
            ("alpha.csv", 1.0, 100.0, "alpha"),
            ("beta.csv", 2.0, 200.0, "beta"),
        ]

    def test_migration_is_idempotent(self, tmp_history_db):
        """Running initialize_history_db twice must not re-migrate or lose rows."""
        from query_engine.QueryEngine import initialize_history_db

        self._seed_legacy(tmp_history_db, [
            ("x.csv", 0.5, 50.0, 50.5, "x", "dir/x.csv", 1),
        ])
        asyncio.run(initialize_history_db())  # first migration
        asyncio.run(initialize_history_db())  # should be a no-op

        with sqlite3.connect(str(tmp_history_db)) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM execution_history"
            ).fetchone()[0]
        assert n == 1

    def test_legacy_row_with_null_start_time_survives_migration(self, tmp_history_db):
        """NULL execution_start_time must be backfilled to a distinct value during migration.

        Composite PK with NULL start_time would collapse multiple rows into
        one since SQLite treats NULL as distinct-but-not-unique in a PK.
        Migration uses ``COALESCE(execution_start_time, rowid * -1.0)`` so
        rows stay distinct.
        """
        from query_engine.QueryEngine import initialize_history_db

        # Insert via raw SQL to allow NULL.
        with sqlite3.connect(str(tmp_history_db)) as conn:
            conn.execute('''
                CREATE TABLE execution_history (
                    saved_search_filename TEXT PRIMARY KEY,
                    runtime REAL,
                    execution_start_time REAL,
                    execution_end_time REAL,
                    query_name TEXT,
                    saved_search_path TEXT,
                    original_result_count INTEGER
                )
            ''')
            conn.execute(
                "INSERT INTO execution_history (saved_search_filename) "
                "VALUES ('ancient_null.csv')"
            )
            conn.commit()

        asyncio.run(initialize_history_db())

        with sqlite3.connect(str(tmp_history_db)) as conn:
            row = conn.execute(
                "SELECT saved_search_filename, execution_start_time "
                "FROM execution_history WHERE saved_search_filename = "
                "'ancient_null.csv'"
            ).fetchone()
        assert row is not None
        assert row[0] == "ancient_null.csv"
        # The start_time was NULL originally; migration assigned a
        # deterministic distinct value via COALESCE(..., rowid * -1.0).
        assert row[1] is not None, (
            "Migration must backfill NULL execution_start_time."
        )
        assert row[1] < 0, (
            f"Backfill should use negative rowid; got {row[1]}"
        )
