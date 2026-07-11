"""
LLM Call History Store - Phase 2 / Bet 3 slice 3
────────────────────────────────────────────────
Provider-agnostic SQLite store for every LLM call dispatched through
:mod:`analyzers.llm_router`. Two purposes wrapped into one table:

1. **History capture** (always-on) - every call lands here for forensic
   audit. Provider-uniform shape: ``model_id``, ``provider``,
   ``prompt`` / ``system`` / ``response_text``, ``cost_usd``,
   ``input_tokens`` / ``output_tokens``, latency, error class.
2. **Cache** (opt-in via the router's ``use_cache`` parameter) -
   content-hash keyed lookup. The hash includes ``model_id +
   model_name + provider + max_tokens + system + prompt`` so a registry
   edit that swaps the underlying model invalidates the cache
   automatically.

Design parallel to :mod:`analyzers.claude_history_store`:

* Lives OUTSIDE ``indexes/`` (at ``<project_root>/llm_call_history.sqlite``)
  so cleanup-budget eviction never touches it. Payloads cost real money
  to recreate; retention is the user's choice.
* Payloads are gzip-compressed UTF-8 - typical LLM prompts and responses
  are a few KB; compression keeps the DB tractable at millions of rows.
* Append-only from the app's point of view. The user can manually
  ``VACUUM``, export, or truncate via a future REST/CLI surface.

Coexists with :mod:`analyzers.claude_history_store`:

* ``claude_api_history.sqlite`` captures Anthropic SDK-detail (full
  Anthropic-format request/response objects).
* ``llm_call_history.sqlite`` (this module) captures the
  application-level uniform view - what prompt was sent, what came
  back, what did it cost - across every provider including Anthropic.

This is not a migration - both tables live side by side at different
abstraction layers.

Cache behavior
--------------
* Only successful calls are cache-eligible. Errored rows still land
  in the table for audit but :meth:`get_cached_response` skips them.
* The cache is content-keyed; identical inputs deterministically produce
  identical hash and therefore identical cache lookups.
* Default TTL is unlimited - same prompt → same model → same response
  forever, until the operator manually wipes or the registry record
  changes (which changes ``model_name`` and therefore the hash).
* Caller can override with ``max_age_seconds`` for use cases where
  freshness matters (e.g. retrying a fact-check that depended on
  current-day data).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_DB_PATH = _PROJECT_ROOT / "llm_call_history.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_call_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT    NOT NULL UNIQUE,
    triggered_at_epoch  INTEGER NOT NULL,
    triggered_at        TEXT    NOT NULL,
    content_hash        TEXT    NOT NULL,
    model_id            TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    model_name          TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    status              TEXT    NOT NULL,            -- "success" | "error"
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cost_usd            REAL,
    latency_ms          INTEGER,
    max_tokens          INTEGER,
    prompt_gz           BLOB,
    system_gz           BLOB,
    response_text_gz    BLOB,
    raw_response_gz     BLOB,
    error_class         TEXT,
    error_message       TEXT
);

-- Cache lookup: hot path is `WHERE content_hash = ? AND status = 'success'
-- ORDER BY triggered_at_epoch DESC LIMIT 1`. Composite index serves it.
CREATE INDEX IF NOT EXISTS idx_llm_history_content_hash
    ON llm_call_history (content_hash, status, triggered_at_epoch DESC);

CREATE INDEX IF NOT EXISTS idx_llm_history_model_id
    ON llm_call_history (model_id, triggered_at_epoch DESC);

CREATE INDEX IF NOT EXISTS idx_llm_history_provider
    ON llm_call_history (provider, triggered_at_epoch DESC);

CREATE INDEX IF NOT EXISTS idx_llm_history_status
    ON llm_call_history (status);
"""


# ── Helpers ──────────────────────────────────────────────────────────

def _gz_text(s: Optional[str]) -> Optional[bytes]:
    """Gzip-compress a UTF-8 string; return None for None."""
    if s is None:
        return None
    return gzip.compress(s.encode("utf-8"))


def _gunzip_text(blob: Optional[bytes]) -> Optional[str]:
    if blob is None:
        return None
    try:
        return gzip.decompress(blob).decode("utf-8")
    except Exception as exc:
        logger.warning("[!] llm_history: could not decode text payload: %s", exc)
        return None


def _gz_json(obj: Any) -> Optional[bytes]:
    if obj is None:
        return None
    try:
        text = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception as exc:
        logger.warning("[!] llm_history: could not serialise payload: %s", exc)
        return None
    return gzip.compress(text.encode("utf-8"))


def _gunzip_json(blob: Optional[bytes]) -> Any:
    if blob is None:
        return None
    try:
        return json.loads(gzip.decompress(blob).decode("utf-8"))
    except Exception as exc:
        logger.warning("[!] llm_history: could not decode JSON payload: %s", exc)
        return None


def compute_content_hash(
    *,
    model_id: str,
    model_name: str,
    provider: str,
    prompt: str,
    system: Optional[str],
    max_tokens: int,
) -> str:
    """Stable sha256 of the cache-relevant inputs.

    Including ``model_name`` (in addition to ``model_id``) means a
    registry edit that swaps the underlying model - e.g. updating
    ``default_models/claude-sonnet-4-6.yaml`` to point at a successor -
    invalidates the cache automatically. The hash differs; old rows
    become orphaned audit history but unreachable from the cache path.

    Field separator is ``\\x00`` (NUL) which cannot appear in the
    delimited fields, so concatenation is unambiguous.
    """
    h = hashlib.sha256()
    h.update(f"{model_id}\x00".encode("utf-8"))
    h.update(f"{model_name}\x00".encode("utf-8"))
    h.update(f"{provider}\x00".encode("utf-8"))
    h.update(f"{int(max_tokens)}\x00".encode("utf-8"))
    h.update(f"{system or ''}\x00".encode("utf-8"))
    h.update(prompt.encode("utf-8"))
    return h.hexdigest()


# ── Store ────────────────────────────────────────────────────────────

class LLMHistoryStore:
    """SQLite-backed history + cache for LLM router calls.

    Thread-safe via per-instance write lock. Reads use SQLite's own
    concurrency (multiple readers OK; single writer serialised by the
    lock).
    """

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def record_call(
        self,
        *,
        request_id: str,
        content_hash: str,
        model_id: str,
        provider: str,
        model_name: str,
        source: str,
        status: str,
        prompt: str,
        system: Optional[str],
        response_text: Optional[str],
        raw_response: Any,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        max_tokens: int,
        error_class: str = "",
        error_message: str = "",
        retain_payloads: bool = True,
    ) -> str:
        """Insert one history row. Returns the ``request_id``."""
        rid = request_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        triggered_epoch = int(now.timestamp())
        triggered_iso = now.isoformat()

        if retain_payloads:
            prompt_gz = _gz_text(prompt)
            system_gz = _gz_text(system)
            response_text_gz = _gz_text(response_text)
            raw_response_gz = _gz_json(raw_response)
        else:
            prompt_gz = system_gz = response_text_gz = raw_response_gz = None

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO llm_call_history (
                        request_id, triggered_at_epoch, triggered_at,
                        content_hash, model_id, provider, model_name,
                        source, status,
                        input_tokens, output_tokens, cost_usd, latency_ms,
                        max_tokens,
                        prompt_gz, system_gz, response_text_gz, raw_response_gz,
                        error_class, error_message
                    ) VALUES (
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?,
                        ?, ?, ?, ?,
                        ?,
                        ?, ?, ?, ?,
                        ?, ?
                    )
                    """,
                    (
                        rid, triggered_epoch, triggered_iso,
                        content_hash, model_id, provider, model_name,
                        source, status,
                        input_tokens, output_tokens, cost_usd, latency_ms,
                        max_tokens,
                        prompt_gz, system_gz, response_text_gz, raw_response_gz,
                        error_class, error_message,
                    ),
                )
                conn.commit()
        return rid

    # ------------------------------------------------------------------
    # Cache lookup
    # ------------------------------------------------------------------

    def get_cached_response(
        self,
        content_hash: str,
        *,
        max_age_seconds: Optional[int] = None,
    ) -> Optional[dict]:
        """Return the most-recent SUCCESS row matching ``content_hash``.

        Errored rows are never cache hits - only ``status='success'``
        rows are eligible.

        Parameters
        ----------
        content_hash :
            Result of :func:`compute_content_hash` from the caller's
            inputs.
        max_age_seconds :
            If supplied, exclude rows older than this. ``None`` means
            unlimited (same prompt → same response forever).

        Returns
        -------
        dict | None
            Row contents (with payloads decoded) on hit; ``None`` on
            miss. The caller adapts the dict into an :class:`LLMResponse`.
        """
        sql = (
            "SELECT * FROM llm_call_history "
            "WHERE content_hash = ? AND status = 'success'"
        )
        params: list[Any] = [content_hash]
        if max_age_seconds is not None and max_age_seconds > 0:
            cutoff = int(time.time()) - int(max_age_seconds)
            sql += " AND triggered_at_epoch >= ?"
            params.append(cutoff)
        sql += " ORDER BY triggered_at_epoch DESC LIMIT 1"

        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get_call(self, request_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM llm_call_history WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_calls(
        self,
        *,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
        status: Optional[str] = None,
        since_epoch: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since_epoch is not None:
            clauses.append("triggered_at_epoch >= ?")
            params.append(int(since_epoch))

        # `clauses` is a list of hardcoded SQL fragment LITERALS
        # ("model_id = ?", "provider = ?", etc.); user-supplied values
        # land via the parameterised `params` list. No string
        # interpolation of user input. `noqa: S608` / `# nosec B608`
        # avoid the false-positive flag.
        base = "SELECT * FROM llm_call_history"
        tail = " ORDER BY triggered_at_epoch DESC LIMIT ?"
        if clauses:
            sql = base + " WHERE " + " AND ".join(clauses) + tail  # nosec B608
        else:
            sql = base + tail
        params.append(int(max(1, limit)))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(self) -> dict:
        """Aggregate counts + cost totals across the whole table."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
                    SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END) AS errors,
                    COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd,
                    COALESCE(SUM(input_tokens),  0) AS total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS total_output_tokens
                FROM llm_call_history
                """
            ).fetchone()
        if row is None:
            return {
                "total": 0, "successes": 0, "errors": 0,
                "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0,
            }
        return dict(row)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def delete_older_than(self, before_epoch: int) -> int:
        """Hard-delete rows triggered before ``before_epoch``. Returns count."""
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM llm_call_history WHERE triggered_at_epoch < ?",
                    (int(before_epoch),),
                )
                conn.commit()
                return cur.rowcount or 0

    def vacuum(self) -> None:
        """SQLite ``VACUUM`` - reclaims space after large deletions."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("VACUUM")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["prompt"] = _gunzip_text(d.pop("prompt_gz", None))
        d["system"] = _gunzip_text(d.pop("system_gz", None))
        d["response_text"] = _gunzip_text(d.pop("response_text_gz", None))
        d["raw_response"] = _gunzip_json(d.pop("raw_response_gz", None))
        return d


# ── Singleton ────────────────────────────────────────────────────────

_instance: Optional[LLMHistoryStore] = None
_instance_lock = threading.Lock()


def get_store(db_path: Optional[str | Path] = None) -> LLMHistoryStore:
    """Return the process-wide LLMHistoryStore singleton.

    Tests pass ``db_path`` to point at a tmp file; production callers
    omit it and the default (project-root path) wins.
    """
    global _instance
    with _instance_lock:
        if _instance is None or (
            db_path is not None and Path(db_path) != _instance._db_path
        ):
            _instance = LLMHistoryStore(db_path=db_path)
        return _instance


def reset_for_tests() -> None:
    """Clear the singleton. Tests call this in fixture teardown so a
    tmp_path-bound instance doesn't bleed into subsequent tests.
    """
    global _instance
    with _instance_lock:
        _instance = None


__all__ = [
    "DEFAULT_DB_PATH",
    "LLMHistoryStore",
    "compute_content_hash",
    "get_store",
    "reset_for_tests",
]
