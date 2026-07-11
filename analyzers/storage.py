"""
Analyzer Storage
────────────────
SQLite-backed persistence for analysis results, daily budget tracking,
and batch request state.

Uses plain ``sqlite3`` (sync) because the analyzer pipeline is synchronous.
Thread-safe via SQLite's built-in serialization + short-lived connections.

All write operations are wrapped in try/except so that storage failures
never block the analysis pipeline.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    """M-AN-10 (2026-04-22): UTC-aware ISO timestamp for every DB write."""
    return datetime.now(timezone.utc).isoformat()
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("analyzers.storage")

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyzer_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    search_name     TEXT    NOT NULL,
    execution_time  TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'skipped',
    alert_priority  TEXT    DEFAULT 'LOW',
    summary         TEXT    DEFAULT '',
    model_used      TEXT    DEFAULT '',
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_cents      REAL    DEFAULT 0.0,
    filter_passed   INTEGER DEFAULT 1,
    filter_answer   TEXT    DEFAULT '',
    skip_reason     TEXT    DEFAULT '',
    error_message   TEXT    DEFAULT '',
    batch_id        TEXT    DEFAULT '',
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ar_search_name ON analyzer_results(search_name);
CREATE INDEX IF NOT EXISTS idx_ar_created_at  ON analyzer_results(created_at);

CREATE TABLE IF NOT EXISTS analyzer_budget (
    date                TEXT PRIMARY KEY,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_calls         INTEGER DEFAULT 0,
    total_cost_cents    REAL    DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS batch_requests (
    custom_id           TEXT PRIMARY KEY,
    batch_id            TEXT    NOT NULL,
    search_name         TEXT    NOT NULL,
    status              TEXT    DEFAULT 'submitted',
    model               TEXT    DEFAULT '',
    system_prompt       TEXT    DEFAULT '',
    user_content        TEXT    DEFAULT '',
    search_metadata_json TEXT   DEFAULT '{}',
    result_parquet_path TEXT    DEFAULT '',
    filter_enabled      INTEGER DEFAULT 0,
    filter_question     TEXT    DEFAULT '',
    created_at          TEXT    NOT NULL,
    completed_at        TEXT    DEFAULT '',
    result_json         TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_br_batch_id ON batch_requests(batch_id);
CREATE INDEX IF NOT EXISTS idx_br_status   ON batch_requests(status);

-- M-AN-9 (2026-04-22): resume checkpoint for partial batch iteration.
-- The poller records ``last_processed_index`` after each result row so
-- a mid-iteration failure doesn't re-handle rows that already landed.
CREATE TABLE IF NOT EXISTS batch_progress (
    batch_id                TEXT PRIMARY KEY,
    last_processed_index    INTEGER DEFAULT 0,
    updated_at              TEXT    DEFAULT ''
);
"""


class AnalyzerStorage:
    """Thread-safe SQLite storage for analyzer results, budget, and batch state."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(_PROJECT_ROOT / "analyzer_results.sqlite")
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(_SCHEMA)
        except Exception as exc:
            logger.error("[x] Failed to initialize analyzer storage: %s", exc)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Analysis Results
    # ------------------------------------------------------------------

    def store_result(self, search_name: str, execution_time: str, analysis) -> None:
        """Persist an AnalysisResult. Never raises."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO analyzer_results (
                        search_name, execution_time, status, alert_priority,
                        summary, model_used, input_tokens, output_tokens,
                        cost_cents, filter_passed, filter_answer, skip_reason,
                        error_message, batch_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        search_name,
                        execution_time,
                        analysis.status,
                        analysis.alert_priority,
                        analysis.summary,
                        analysis.model_used,
                        analysis.input_tokens,
                        analysis.output_tokens,
                        analysis.cost_cents,
                        1 if analysis.filter_passed else 0,
                        analysis.filter_answer,
                        analysis.skip_reason,
                        analysis.error_message,
                        getattr(analysis, "batch_id", ""),
                        _utc_now_iso(),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.error("[x] Failed to store analysis result: %s", exc)

    def get_results(
        self,
        search_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent analysis results, optionally filtered by search name."""
        try:
            with self._connect() as conn:
                if search_name:
                    rows = conn.execute(
                        "SELECT * FROM analyzer_results WHERE search_name = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (search_name, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM analyzer_results "
                        "ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("[x] Failed to read analysis results: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Budget Tracking
    # ------------------------------------------------------------------

    def load_daily_budget(self, date_str: str) -> Dict[str, Any]:
        """Load cumulative budget for a given date. Returns zeroed dict if none."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM analyzer_budget WHERE date = ?", (date_str,)
                ).fetchone()
                if row:
                    return dict(row)
        except Exception as exc:
            logger.error("[x] Failed to load daily budget: %s", exc)
        return {
            "date": date_str,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_calls": 0,
            "total_cost_cents": 0.0,
        }

    def record_usage(
        self,
        date_str: str,
        input_tokens: int,
        output_tokens: int,
        cost_cents: float,
    ) -> None:
        """Atomically increment usage counters for the given date. Never raises."""
        try:
            with self._lock:
                with self._connect() as conn:
                    conn.execute(
                        """INSERT INTO analyzer_budget
                               (date, total_input_tokens, total_output_tokens,
                                total_calls, total_cost_cents)
                           VALUES (?, ?, ?, 1, ?)
                           ON CONFLICT(date) DO UPDATE SET
                               total_input_tokens  = total_input_tokens  + excluded.total_input_tokens,
                               total_output_tokens = total_output_tokens + excluded.total_output_tokens,
                               total_calls         = total_calls + 1,
                               total_cost_cents    = total_cost_cents + excluded.total_cost_cents""",
                        (date_str, input_tokens, output_tokens, cost_cents),
                    )
                    conn.commit()
        except Exception as exc:
            logger.error("[x] Failed to record usage: %s", exc)

    # ------------------------------------------------------------------
    # Batch Requests
    # ------------------------------------------------------------------

    def create_batch_request(
        self,
        custom_id: str,
        batch_id: str,
        search_name: str,
        model: str,
        system_prompt: str,
        user_content: str,
        search_metadata: dict,
        result_parquet_path: str,
        filter_enabled: bool = False,
        filter_question: str = "",
    ) -> None:
        """Store a pending batch request. Never raises."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO batch_requests (
                        custom_id, batch_id, search_name, status, model,
                        system_prompt, user_content, search_metadata_json,
                        result_parquet_path, filter_enabled, filter_question,
                        created_at
                    ) VALUES (?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        custom_id,
                        batch_id,
                        search_name,
                        model,
                        system_prompt,
                        user_content,
                        json.dumps(search_metadata or {}, default=str),
                        result_parquet_path,
                        1 if filter_enabled else 0,
                        filter_question,
                        _utc_now_iso(),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.error("[x] Failed to store batch request: %s", exc)

    def get_pending_batch_ids(self) -> List[str]:
        """Return distinct batch IDs that are still pending/submitted."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT batch_id FROM batch_requests "
                    "WHERE status = 'submitted'"
                ).fetchall()
                return [r["batch_id"] for r in rows]
        except Exception as exc:
            logger.error("[x] Failed to query pending batches: %s", exc)
            return []

    def get_requests_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        """Return all requests associated with a batch ID."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM batch_requests WHERE batch_id = ?",
                    (batch_id,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("[x] Failed to get batch requests: %s", exc)
            return []

    def get_request(self, custom_id: str) -> Optional[Dict[str, Any]]:
        """Return a single batch request by custom_id."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM batch_requests WHERE custom_id = ?",
                    (custom_id,),
                ).fetchone()
                return dict(row) if row else None
        except Exception as exc:
            logger.error("[x] Failed to get batch request: %s", exc)
            return None

    def mark_batch_completed(
        self,
        custom_id: str,
        status: str,
        result_json: str = "",
    ) -> None:
        """Update a batch request's status and result. Never raises."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """UPDATE batch_requests
                       SET status = ?, completed_at = ?, result_json = ?
                       WHERE custom_id = ?""",
                    (status, _utc_now_iso(), result_json, custom_id),
                )
                conn.commit()
        except Exception as exc:
            logger.error("[x] Failed to update batch request: %s", exc)

    # ------------------------------------------------------------------
    # Batch iteration checkpoint (M-AN-9)
    # ------------------------------------------------------------------

    def get_batch_progress(self, batch_id: str) -> int:
        """Return the last processed result index for *batch_id*, or 0.

        Used by :func:`analyzers.batch_poller._process_batch` to resume
        iteration after a mid-cycle failure without re-handling rows.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT last_processed_index FROM batch_progress "
                    "WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if row:
                    return int(row[0] or 0)
        except Exception as exc:
            logger.error("[x] Failed to read batch_progress: %s", exc)
        return 0

    def set_batch_progress(self, batch_id: str, last_processed_index: int) -> None:
        """Upsert the last processed result index for *batch_id*. Never raises."""
        try:
            with self._lock:
                with self._connect() as conn:
                    conn.execute(
                        """INSERT INTO batch_progress
                               (batch_id, last_processed_index, updated_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(batch_id) DO UPDATE SET
                               last_processed_index = excluded.last_processed_index,
                               updated_at = excluded.updated_at""",
                        (batch_id, int(last_processed_index), _utc_now_iso()),
                    )
                    conn.commit()
        except Exception as exc:
            logger.error("[x] Failed to update batch_progress: %s", exc)
