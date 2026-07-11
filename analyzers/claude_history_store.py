"""
Claude API History Store
────────────────────────
Dedicated SQLite database that captures every Claude API call - metadata,
full request payload, and full response payload - so the user can audit
exactly what they paid for.

Design
  * Lives OUTSIDE ``indexes/`` (at ``<project_root>/claude_api_history.sqlite``)
    so it is NOT subject to the indexes/logs auto-cleanup budgets. Payloads
    are expensive to re-create (they cost real money); retention is the
    user's choice, not the scheduler's.
  * Payloads are gzip-compressed JSON blobs - typical Claude requests and
    responses are a few KB; compression keeps the DB tractable even at
    millions of rows.
  * Complement to the lightweight ``indexes/logs/claude_api/`` Parquet
    stream emitted by ``functionality.log_writer`` - that one is for SPQL
    cost alerting; this one is for forensic audit.

The store is strictly append-only from the app's point of view. The user
can manually ``VACUUM``, export, and truncate via the REST API / CLI when
the DB grows uncomfortable.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = _PROJECT_ROOT / "claude_api_history.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_api_calls (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id            TEXT    NOT NULL UNIQUE,
    triggered_at_epoch    INTEGER NOT NULL,
    triggered_at          TEXT    NOT NULL,
    source                TEXT    NOT NULL,
    group_name            TEXT,
    model                 TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd              REAL,
    latency_ms            INTEGER,
    attempt_num           INTEGER,
    retried               INTEGER DEFAULT 0,
    stop_reason           TEXT,
    error_class           TEXT,
    error_message         TEXT,
    request_body_gz       BLOB,
    response_body_gz      BLOB,
    extra_metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_claude_api_triggered_at
    ON claude_api_calls (triggered_at_epoch);

CREATE INDEX IF NOT EXISTS idx_claude_api_source
    ON claude_api_calls (source);

CREATE INDEX IF NOT EXISTS idx_claude_api_group_name
    ON claude_api_calls (group_name);

CREATE INDEX IF NOT EXISTS idx_claude_api_status
    ON claude_api_calls (status);
"""


def _gz(obj: Any) -> bytes | None:
    """Gzip-compress a JSON-serialisable object; return None for None."""
    if obj is None:
        return None
    try:
        text = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception as exc:
        logger.warning("[!] claude_history: could not serialise payload: %s", exc)
        return None
    return gzip.compress(text.encode("utf-8"))


def _gunzip(blob: bytes | None) -> Any:
    if blob is None:
        return None
    try:
        return json.loads(gzip.decompress(blob).decode("utf-8"))
    except Exception as exc:
        logger.warning("[!] claude_history: could not decode payload: %s", exc)
        return None


class ClaudeHistoryStore:
    """Append-only SQLite log of every Claude API call."""

    _instance: "ClaudeHistoryStore | None" = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "ClaudeHistoryStore":
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ClaudeHistoryStore()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create the sqlite schema, or fail loudly with an actionable msg.

        The common production failure here is that ``_db_path`` points at
        a DIRECTORY instead of a file - which happens when Docker
        bind-mounts a non-existent host path (Docker auto-creates it as a
        directory). That would make every subsequent insert fail silently
        inside the ``_record_attempt`` try/except in claude_client.py, and
        the user sees a persistently empty Claude History in the UI even
        while the Parquet log shows rows. Caught 2026-04-21 when the user
        reported "not capturing anything"; root cause was
        ``claude_api_history.sqlite`` missing from docker-compose.yml's
        volumes list.
        """
        import os
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Bind-mount-as-directory detection: if the path exists AND is a
        # directory, no amount of insert-retrying will help. Raise so the
        # operator sees the problem at startup instead of discovering it
        # later via an empty history page.
        if self._db_path.exists() and self._db_path.is_dir():
            raise RuntimeError(
                f"claude_api_history path {self._db_path!s} is a DIRECTORY, "
                "not a file. This usually means the Docker host was "
                "missing the file when the container started and Docker "
                "auto-created a directory to satisfy the bind mount. Fix: "
                "stop the container, remove the directory on the host "
                "(`rm -rf " + str(self._db_path) + "`), touch it as an "
                "empty file (`touch " + str(self._db_path) + "`), and "
                "restart. See install.sh's touch list + docker-compose "
                "volumes - both must include this file."
            )

        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _retain_payloads(self) -> bool:
        try:
            from global_settings import get_settings
            return bool(get_settings().get("claude_history_retain_payloads"))
        except Exception:
            return True

    # ── Public API ────────────────────────────────────────────────

    def record_call(
        self,
        *,
        source: str,
        model: str,
        status: str,
        request_body: Any = None,
        response_body: Any = None,
        group_name: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        attempt_num: int | None = None,
        retried: bool = False,
        stop_reason: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        extra: dict | None = None,
        request_id: str | None = None,
    ) -> str:
        """Insert one call record. Returns the assigned ``request_id``.

        When ``claude_history_retain_payloads=False`` the request + response
        bodies are omitted but all other metadata is still stored - useful
        when the user wants cost tracking without storing prompts.
        """
        rid = request_id or str(uuid.uuid4())
        now_epoch = int(time.time())
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_epoch))

        keep_payloads = self._retain_payloads()
        req_gz = _gz(request_body) if keep_payloads else None
        resp_gz = _gz(response_body) if keep_payloads else None

        extra_json = None
        if extra:
            try:
                extra_json = json.dumps(extra, default=str)
            except Exception:
                extra_json = None

        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO claude_api_calls
                    (request_id, triggered_at_epoch, triggered_at, source,
                     group_name, model, status, input_tokens, output_tokens,
                     cache_read_tokens, cache_creation_tokens, cost_usd,
                     latency_ms, attempt_num, retried, stop_reason,
                     error_class, error_message, request_body_gz,
                     response_body_gz, extra_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, now_epoch, now_iso, source, group_name, model, status,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, latency_ms, attempt_num,
                    1 if retried else 0, stop_reason, error_class,
                    error_message, req_gz, resp_gz, extra_json,
                ),
            )
            conn.commit()

        return rid

    def get_call(self, request_id: str) -> dict | None:
        """Fetch a single call by request_id, decoding the payloads."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM claude_api_calls WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["request_body"] = _gunzip(data.pop("request_body_gz"))
        data["response_body"] = _gunzip(data.pop("response_body_gz"))
        if data.get("extra_metadata_json"):
            try:
                data["extra_metadata"] = json.loads(data["extra_metadata_json"])
            except Exception:
                data["extra_metadata"] = None
        data.pop("extra_metadata_json", None)
        return data

    def list_calls(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        since_epoch: int | None = None,
        until_epoch: int | None = None,
        source: str | None = None,
        group_name: str | None = None,
        status: str | None = None,
        include_payloads: bool = False,
    ) -> list[dict]:
        """Return a page of call records, newest first.

        Bodies are decoded only when ``include_payloads=True`` - listing
        hundreds of rows with full prompts is expensive, and the UI only
        needs payload preview on the detail view.
        """
        where: list[str] = []
        params: list[Any] = []
        if since_epoch is not None:
            where.append("triggered_at_epoch >= ?")
            params.append(int(since_epoch))
        if until_epoch is not None:
            where.append("triggered_at_epoch <= ?")
            params.append(int(until_epoch))
        if source:
            where.append("source = ?")
            params.append(source)
        if group_name:
            where.append("group_name = ?")
            params.append(group_name)
        if status:
            where.append("status = ?")
            params.append(status)

        cols = (
            "id, request_id, triggered_at, triggered_at_epoch, source, "
            "group_name, model, status, input_tokens, output_tokens, "
            "cache_read_tokens, cache_creation_tokens, cost_usd, latency_ms, "
            "attempt_num, retried, stop_reason, error_class, error_message, "
            "extra_metadata_json"
        )
        if include_payloads:
            cols += ", request_body_gz, response_body_gz"

        sql = f"SELECT {cols} FROM claude_api_calls"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY triggered_at_epoch DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        result: list[dict] = []
        for r in rows:
            d = dict(r)
            if include_payloads:
                d["request_body"] = _gunzip(d.pop("request_body_gz", None))
                d["response_body"] = _gunzip(d.pop("response_body_gz", None))
            if d.get("extra_metadata_json"):
                try:
                    d["extra_metadata"] = json.loads(d["extra_metadata_json"])
                except Exception:
                    d["extra_metadata"] = None
            d.pop("extra_metadata_json", None)
            result.append(d)
        return result

    def stats(
        self,
        *,
        since_epoch: int | None = None,
        group_name: str | None = None,
    ) -> dict:
        """Return aggregate counts / tokens / cost across the selected range."""
        where: list[str] = []
        params: list[Any] = []
        if since_epoch is not None:
            where.append("triggered_at_epoch >= ?")
            params.append(int(since_epoch))
        if group_name:
            where.append("group_name = ?")
            params.append(group_name)

        sql = (
            "SELECT COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            "SUM(cost_usd) AS cost_usd, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count "
            "FROM claude_api_calls"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)

        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return {}
        cols = [
            "calls", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cost_usd", "success_count", "error_count",
        ]
        return {c: (row[i] if row[i] is not None else 0) for i, c in enumerate(cols)}

    def db_size_bytes(self) -> int:
        """Return the raw size of the SQLite file on disk."""
        try:
            return self._db_path.stat().st_size
        except OSError:
            return 0

    def delete_older_than(self, cutoff_epoch: int) -> int:
        """Delete calls older than *cutoff_epoch*. Returns rows removed."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM claude_api_calls WHERE triggered_at_epoch < ?",
                (int(cutoff_epoch),),
            )
            conn.commit()
            return cur.rowcount

    def vacuum(self) -> None:
        """Run ``VACUUM`` to reclaim disk space after a prune."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("VACUUM")


# Module-level convenience
def record_call(**kwargs) -> str:
    """Shortcut for ``ClaudeHistoryStore.get_instance().record_call(**kwargs)``."""
    return ClaudeHistoryStore.get_instance().record_call(**kwargs)
