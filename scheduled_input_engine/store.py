"""
Scheduled Input Store
─────────────────────
All SQLite CRUD operations, database initialisation, and input validation.
Pure sync sqlite3 - no async needed since scheduler jobs run in a thread pool.
"""

import logging
import re
import sqlite3
import time
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

from functionality.cron_compat import linux_dow_to_apscheduler

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
SCHEDULED_INPUTS_DB = _PROJECT_ROOT / "scheduled_inputs.db"
HISTORY_DB = _PROJECT_ROOT / "scheduled_inputs_history.db"

# Validation patterns (carried over from ScheduledInputBackend)
_VALID_NAME = re.compile(r"^[a-zA-Z0-9 _.\-&()',#+]+$")
_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*]')


class ScheduledInputStore:
    """Manages all database access for scheduled inputs and execution history."""

    def __init__(self):
        self._inputs_db = str(SCHEDULED_INPUTS_DB)
        self._history_db = str(HISTORY_DB)

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------

    def initialize_databases(self):
        """Create tables if they don't exist. Safe to call multiple times."""
        self._init_inputs_db()
        self._init_history_db()

    def _init_inputs_db(self):
        with sqlite3.connect(self._inputs_db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_inputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT UNIQUE,
                    description TEXT,
                    code TEXT,
                    cron_schedule TEXT,
                    overwrite BOOLEAN,
                    subdirectory TEXT,
                    api_url TEXT,
                    created_at REAL,
                    disabled BOOLEAN DEFAULT 0
                )
                """
            )
            # Migrate: add api_url if missing (backward compat)
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(scheduled_inputs)")
            }
            if "api_url" not in cols:
                conn.execute("ALTER TABLE scheduled_inputs ADD COLUMN api_url TEXT")

            # Migrate: add trust_level if missing (backward compat)
            # All pre-existing rows default to 'sandboxed' - preserves current behaviour.
            if "trust_level" not in cols:
                conn.execute(
                    "ALTER TABLE scheduled_inputs "
                    "ADD COLUMN trust_level TEXT DEFAULT 'sandboxed'"
                )

            # Migrate: add timeout_seconds if missing (added 2026-04-23).
            # NULL means "use the global default_script_timeout_seconds".
            # Per-task override exists so scripts that legitimately need
            # longer wall time (e.g. options_unusual_activity_pro with
            # 10 tickers × Yahoo pacing + Black-Scholes greeks) don't
            # force the operator to raise the global default for every
            # other short-run script. Auto-populated on library deploy
            # from the script JSON's ``suggested_timeout_seconds`` hint.
            if "timeout_seconds" not in cols:
                conn.execute(
                    "ALTER TABLE scheduled_inputs "
                    "ADD COLUMN timeout_seconds INTEGER"
                )

            # H-SV-1 (2026-04-21 production review): backfill any row whose
            # ``trust_level`` is NULL or empty to 'sandboxed' so that
            # ``task.get('trust_level') or 'sandboxed'`` at the engine layer
            # no longer silently masks DB-level corruption. SQLite does not
            # support ``ALTER TABLE ... ALTER COLUMN ... NOT NULL`` without
            # a full table rebuild, so we enforce the invariant at write +
            # read time (see engine._run_task) rather than the schema.
            backfilled = conn.execute(
                "UPDATE scheduled_inputs "
                "SET trust_level = 'sandboxed' "
                "WHERE trust_level IS NULL OR trust_level = ''"
            ).rowcount
            if backfilled:
                logger.warning(
                    "[!] Backfilled %d scheduled_inputs row(s) with NULL/empty "
                    "trust_level to 'sandboxed' (H-SV-1 migration).",
                    backfilled,
                )

            # Repo tables
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS input_repos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    git_url TEXT,
                    path TEXT,
                    active INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repo_scripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id INTEGER REFERENCES input_repos(id),
                    script_name TEXT,
                    cron_schedule TEXT,
                    output_subdir TEXT,
                    overwrite INTEGER DEFAULT 0
                )
                """
            )
            conn.commit()
        logger.info("[i] Scheduled inputs database initialised")

    def _init_history_db(self):
        with sqlite3.connect(self._history_db) as conn:
            # Check if the table already exists
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(execution_history)")
            }

            if not existing:
                # Fresh table with new column names
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS execution_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT,
                        title TEXT,
                        runtime REAL,
                        start_time REAL,
                        end_time REAL,
                        status TEXT DEFAULT 'success',
                        error_message TEXT,
                        attempt INTEGER DEFAULT 0
                    )
                    """
                )
            else:
                # Migrate older schema: add new columns if missing
                for col, typedef in [
                    ("status", "TEXT DEFAULT 'success'"),
                    ("error_message", "TEXT"),
                    ("attempt", "INTEGER DEFAULT 0"),
                ]:
                    if col not in existing:
                        conn.execute(
                            f"ALTER TABLE execution_history ADD COLUMN {col} {typedef}"
                        )

            # Detect which column names the table uses for start/end times
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(execution_history)")
            }
            if "execution_start_time" in cols and "start_time" not in cols:
                self._start_col = "execution_start_time"
                self._end_col = "execution_end_time"
            else:
                self._start_col = "start_time"
                self._end_col = "end_time"

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deletion_history (
                    deleted_file TEXT,
                    deletion_time REAL,
                    reason TEXT
                )
                """
            )
            conn.commit()
        logger.info("[i] History database initialised")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_cron(cron_string):
        """Validate a cron expression. Raises ValueError on failure.

        Translates the Linux day-of-week numbering (0=Sun) to APScheduler
        (0=Mon) before parsing - both for correctness and so the validation
        path mirrors what the actual scheduler will see.
        """
        try:
            CronTrigger.from_crontab(linux_dow_to_apscheduler(cron_string))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid cron schedule: {exc}") from exc

    @staticmethod
    def validate_inputs(title, description=None, cron_schedule=None, subdirectory=None, trust_level=None):
        """Validate user-supplied fields. Raises ValueError on bad input."""
        if not title or not _VALID_NAME.match(title) or _INVALID_PATH_CHARS.search(title):
            raise ValueError("Invalid title: only letters, numbers, spaces, and common punctuation (_.&()',#+) allowed.")
        if description and re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', description):
            raise ValueError("Description contains invalid characters.")
        if cron_schedule:
            ScheduledInputStore.validate_cron(cron_schedule)
        if subdirectory:
            ScheduledInputStore.validate_subdirectory(subdirectory)
        if trust_level is not None:
            ScheduledInputStore.validate_trust_level(trust_level)

    @staticmethod
    def validate_trust_level(value):
        """Validate the trust_level field. Raises ValueError if not a recognised tier."""
        if value is None:
            return "sandboxed"
        v = str(value).strip().lower()
        if v not in ("sandboxed", "unrestricted"):
            raise ValueError(
                f"Invalid trust_level '{value}'. Must be 'sandboxed' or 'unrestricted'."
            )
        return v

    @staticmethod
    def validate_subdirectory(subdirectory):
        """Validate a subdirectory path. Allows nested paths with '/' separators.

        Raises ValueError on traversal attempts, invalid characters, or excessive depth.
        """
        # Normalize backslashes to forward slashes
        normed = subdirectory.replace("\\", "/")

        # Strip leading/trailing slashes
        normed = normed.strip("/")

        if not normed:
            raise ValueError("Subdirectory cannot be empty after normalization.")

        segments = normed.split("/")

        # Block traversal: any segment that is '..' or '.' or encoded variants
        for seg in segments:
            if seg in ("..", ".", "~"):
                raise ValueError(
                    "Directory traversal is forbidden: '..' and '.' segments are not allowed."
                )
            # Also catch URL-encoded and other obfuscation of '..'
            if ".." in seg:
                raise ValueError(
                    "Directory traversal is forbidden: segments containing '..' are not allowed."
                )
            if not seg or not _VALID_NAME.match(seg):
                raise ValueError(
                    f"Invalid subdirectory segment '{seg}': only letters, numbers, spaces, "
                    "underscores, hyphens, and periods allowed."
                )

        # Enforce depth limit from global settings
        max_depth = 5  # default
        try:
            from global_settings import get_settings
            max_depth = get_settings().get("max_subdirectory_depth")
        except Exception:
            pass

        if len(segments) > max_depth:
            raise ValueError(
                f"Subdirectory depth {len(segments)} exceeds the maximum allowed depth of {max_depth}."
            )

    # ------------------------------------------------------------------
    # CRUD - Scheduled Inputs
    # ------------------------------------------------------------------

    def add_scheduled_input(
        self, title, code, cron_schedule, overwrite="false",
        description="", subdirectory="", api_url=None, trust_level="sandboxed",
        timeout_seconds=None,
    ):
        """Insert a new scheduled input. Returns the created row as a dict.

        ``timeout_seconds`` is the per-task wall-clock cap (None falls
        back to the global ``default_script_timeout_seconds`` setting).
        Library scripts with a ``suggested_timeout_seconds`` JSON hint
        populate this on deploy.
        """
        self.validate_inputs(title, description, cron_schedule, subdirectory, trust_level)
        overwrite_bool = str(overwrite).lower() == "true" if isinstance(overwrite, str) else bool(overwrite)
        trust_level = self.validate_trust_level(trust_level)
        timeout_seconds = self._validate_timeout(timeout_seconds)

        try:
            with sqlite3.connect(self._inputs_db) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    INSERT INTO scheduled_inputs
                        (title, description, code, cron_schedule, overwrite, subdirectory, api_url, trust_level, timeout_seconds, created_at, disabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (title, description, code, cron_schedule, overwrite_bool, subdirectory, api_url, trust_level, timeout_seconds, time.time()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM scheduled_inputs WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
                result = dict(row)
                _emit_scheduled_input_event(result["id"], "create", None, result)
                return result
        except sqlite3.IntegrityError:
            raise ValueError("A scheduled input with that title already exists.")

    @staticmethod
    def _validate_timeout(timeout_seconds):
        """Coerce + bound the per-task timeout. None is always valid
        (means "fall back to global"). Otherwise must be an int in
        [10, 3600]. Returns the coerced int or None."""
        if timeout_seconds is None or timeout_seconds == "":
            return None
        try:
            value = int(timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError(
                f"timeout_seconds must be an integer, got {timeout_seconds!r}"
            )
        if value < 10 or value > 3600:
            raise ValueError(
                f"timeout_seconds must be between 10 and 3600; got {value}"
            )
        return value

    def update_scheduled_input(self, task_id, **kwargs):
        """Update fields on an existing scheduled input. Returns updated row."""
        allowed = {"title", "description", "code", "cron_schedule", "overwrite", "subdirectory", "api_url", "trust_level", "timeout_seconds", "disabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_scheduled_input(task_id)

        # Validate mutable fields
        if "title" in updates:
            self.validate_inputs(updates["title"])
        if "cron_schedule" in updates:
            self.validate_cron(updates["cron_schedule"])
        if "subdirectory" in updates:
            self.validate_inputs("placeholder", subdirectory=updates["subdirectory"])
        if "trust_level" in updates:
            updates["trust_level"] = self.validate_trust_level(updates["trust_level"])
        if "timeout_seconds" in updates:
            updates["timeout_seconds"] = self._validate_timeout(updates["timeout_seconds"])
        if "overwrite" in updates:
            v = updates["overwrite"]
            updates["overwrite"] = str(v).lower() == "true" if isinstance(v, str) else bool(v)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]

        with sqlite3.connect(self._inputs_db) as conn:
            conn.row_factory = sqlite3.Row
            before_row = conn.execute(
                "SELECT * FROM scheduled_inputs WHERE id = ?", (task_id,)
            ).fetchone()
            before = dict(before_row) if before_row else None
            conn.execute(
                f"UPDATE scheduled_inputs SET {set_clause} WHERE id = ?", values
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM scheduled_inputs WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Scheduled input {task_id} not found.")
            after = dict(row)
        _emit_scheduled_input_event(task_id, "update", before, after)
        return after

    def delete_scheduled_input(self, task_id):
        """Delete a scheduled input by ID."""
        with sqlite3.connect(self._inputs_db) as conn:
            conn.row_factory = sqlite3.Row
            before_row = conn.execute(
                "SELECT * FROM scheduled_inputs WHERE id = ?", (task_id,)
            ).fetchone()
            conn.execute("DELETE FROM scheduled_inputs WHERE id = ?", (task_id,))
            conn.commit()
        _emit_scheduled_input_event(
            task_id, "delete",
            dict(before_row) if before_row else None,
            None,
        )

    def get_scheduled_input(self, task_id):
        """Fetch a single scheduled input by ID."""
        with sqlite3.connect(self._inputs_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM scheduled_inputs WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Scheduled input {task_id} not found.")
            return dict(row)

    def list_scheduled_inputs(self, enabled_only=False):
        """Return all scheduled inputs as a list of dicts."""
        with sqlite3.connect(self._inputs_db) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM scheduled_inputs"
            if enabled_only:
                query += " WHERE disabled = 0"
            query += " ORDER BY id"
            return [dict(row) for row in conn.execute(query)]

    # ------------------------------------------------------------------
    # CRUD - Repo Scripts
    # ------------------------------------------------------------------

    def list_repo_scripts(self, active_only=True):
        """Return repo scripts joined with their repo info."""
        with sqlite3.connect(self._inputs_db) as conn:
            conn.row_factory = sqlite3.Row
            query = (
                "SELECT rs.id, ir.path AS repo_path, rs.script_name, rs.cron_schedule, "
                "rs.output_subdir, rs.overwrite "
                "FROM repo_scripts rs JOIN input_repos ir ON rs.repo_id = ir.id"
            )
            if active_only:
                query += " WHERE ir.active = 1"
            return [dict(row) for row in conn.execute(query)]

    # ------------------------------------------------------------------
    # Execution history
    # ------------------------------------------------------------------

    def record_execution(self, task_id, title, runtime, status="success", error_message=None, attempt=0):
        """Write a row to execution_history."""
        now = time.time()
        sc, ec = self._start_col, self._end_col
        with sqlite3.connect(self._history_db) as conn:
            conn.execute(
                f"""
                INSERT INTO execution_history
                    (task_id, title, runtime, {sc}, {ec}, status, error_message, attempt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(task_id), title, runtime, now - runtime, now, status, error_message, attempt),
            )
            conn.commit()

    def record_deletion(self, deleted_file, reason):
        """Write a row to deletion_history."""
        with sqlite3.connect(self._history_db) as conn:
            conn.execute(
                "INSERT INTO deletion_history (deleted_file, deletion_time, reason) VALUES (?, ?, ?)",
                (str(deleted_file), time.time(), reason),
            )
            conn.commit()

    def get_execution_history(self, task_id=None, limit=50):
        """Fetch recent execution history, optionally filtered by task_id."""
        sc = self._start_col
        with sqlite3.connect(self._history_db) as conn:
            conn.row_factory = sqlite3.Row
            if task_id is not None:
                rows = conn.execute(
                    f"SELECT * FROM execution_history WHERE task_id = ? ORDER BY {sc} DESC LIMIT ?",
                    (str(task_id), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM execution_history ORDER BY {sc} DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_last_run(self, task_id, *, status=None):
        """Return the most recent execution_history row for *task_id*, or None.

        When ``status`` is provided (e.g. ``"success"``), filter to that
        status only - useful for the UI's "Last Run" column which
        typically wants the last SUCCESSFUL run, not the last attempt.

        Ordering: ``start_time DESC, rowid DESC``. The secondary
        ``rowid DESC`` tiebreaker matters when two runs fire within the
        same sub-microsecond window and share a ``start_time`` float;
        SQLite's implicit ``rowid`` is monotonic under INSERT so the
        newer row always wins. (Older installs' schema omits an
        explicit ``id`` column so we lean on ``rowid`` - every ordinary
        SQLite table has one.)
        """
        sc = self._start_col
        with sqlite3.connect(self._history_db) as conn:
            conn.row_factory = sqlite3.Row
            if status is None:
                row = conn.execute(
                    f"SELECT rowid AS _rowid, * FROM execution_history "
                    f"WHERE task_id = ? "
                    f"ORDER BY {sc} DESC, rowid DESC LIMIT 1",
                    (str(task_id),),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT rowid AS _rowid, * FROM execution_history "
                    f"WHERE task_id = ? AND status = ? "
                    f"ORDER BY {sc} DESC, rowid DESC LIMIT 1",
                    (str(task_id), status),
                ).fetchone()
            return dict(row) if row else None


def _emit_scheduled_input_event(
    task_id, action: str,
    old_value: dict | None, new_value: dict | None,
) -> None:
    """Record a scheduled-input CRUD event to the config log stream.

    Strips the ``code`` field from both old + new values so huge Python
    source blobs don't bloat Parquet log files - the user wants an audit
    trail of WHICH job changed and WHEN, not a full code diff in the log.
    The full code is still retrievable via the scheduled_inputs DB + YAML.
    """
    def _trim(rec):
        if not rec:
            return rec
        trimmed = dict(rec)
        if "code" in trimmed and trimmed["code"]:
            code_len = len(trimmed["code"])
            trimmed["code"] = f"<omitted:{code_len} chars>"
        return trimmed
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=str(task_id),
            action=action,
            subject_type="scheduled_input",
            old_value=_trim(old_value),
            new_value=_trim(new_value),
            actor="api",
            source="scheduled_input_store",
        )
    except Exception:
        pass
