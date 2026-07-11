"""
Notebook Cell Cache Store - Phase 3 / Bet 4 slice 3
────────────────────────────────────────────────────
Reactive content-hash cache for notebook cell outputs. The mechanism
that delivers the headline economics promise from ROADMAP Bet 4.2:

    "iterating on a brief becomes free until the moment you choose to
     spend"

Combined with the slice-3 LLM call cache from Phase 2, prompt iteration
becomes pay-once. Edit cell 5's prompt → cells 1-4 stay cached → only
cell 5 onwards re-runs. Edit cell 1's input → cells 2+ invalidate
(via content-hash propagation through the DAG).

How the DAG hashing works
─────────────────────────

Each cell's cache key is its ``content_hash``:

    content_hash = SHA-256(cell.type + cell.source + prior_output_hashes)

where ``prior_output_hashes`` are the SHA-256 hashes of the previously-
executed cells' OUTPUTS. So:

    * Editing cell 5 → cell 5's content_hash changes → cell 5 cache miss.
      Cells 1-4 untouched (their hashes don't depend on cell 5).
    * Editing cell 1 → cell 1's content_hash changes → cell 1 cache miss
      → cell 1's NEW output_hash → cell 2's content_hash changes → cell 2
      cache miss → cascading invalidation of cells 2+.
    * Editing cell 1 in a way that produces an IDENTICAL output (e.g. a
      whitespace-only change in source that yields the same DataFrame)
      → cell 1's output_hash unchanged → cells 2+ stay cached.

Storage
───────

* **Metadata**: SQLite at ``<project_root>/notebook_cache.sqlite``
  (one row per cached entry; LRU eviction queries sort by
  ``last_accessed_at``).
* **Payloads**: pickle files under ``<project_root>/notebook_cache/``.
  Each entry is one ``<content_hash>.pkl`` containing the cell's
  ``namespace_delta``, ``output``, ``output_repr``, ``stdout``,
  ``stderr``, and ``exposed_names``.

Both regenerable - losing the cache only loses iteration economy, not
data. Cache is gitignored, NOT in default backups, but IS bind-mounted
in Docker so container rebuilds preserve it.

Determinism assumption
──────────────────────

Cells are assumed deterministic given their inputs. A cell that
calls ``random.random()`` or ``datetime.now()`` will cache its first
result and return it on every subsequent run. Operators who want
fresh results pass ``use_cache=False`` per execute call.

Pickle is used for arbitrary Python objects (DataFrames, custom
classes, etc.). Admin-tool threat model: notebooks are operator-
authored on a trusted-local machine; pickle's "unsafe with
untrusted data" warning doesn't apply.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle  # nosec B403 - admin-tool cache; payloads are operator-authored
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml  # noqa: F401 - imported for future param-cell hash variant

from functionality.atomic_write import write_bytes_atomic

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).parent.resolve()
DEFAULT_DB_PATH = _PROJECT_ROOT / "notebook_cache.sqlite"
DEFAULT_PAYLOAD_DIR = _PROJECT_ROOT / "notebook_cache"


# Cache-payload pickle protocol. Protocol 5 (Python 3.8+) supports
# out-of-band buffers - useful for large numpy arrays. We're on 3.12;
# fixed protocol means determinism across writes.
_PICKLE_PROTOCOL = 5


# Schema version - additive-only contract. Slice 3 ships v1.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notebook_cell_cache (
    content_hash       TEXT    PRIMARY KEY,
    output_hash        TEXT    NOT NULL,
    notebook_id        TEXT    NOT NULL,
    cell_id            TEXT    NOT NULL,
    cell_type          TEXT    NOT NULL,
    payload_path       TEXT    NOT NULL,
    payload_size_bytes INTEGER NOT NULL,
    runtime_ms         INTEGER NOT NULL,
    executed_at        TEXT    NOT NULL,
    last_accessed_at   TEXT    NOT NULL,
    hit_count          INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL
);

-- LRU eviction sweeps by last_accessed_at ASC.
CREATE INDEX IF NOT EXISTS idx_cache_last_accessed
    ON notebook_cell_cache (last_accessed_at);

-- Per-notebook lookups (e.g. "invalidate everything for notebook X")
CREATE INDEX IF NOT EXISTS idx_cache_notebook_cell
    ON notebook_cell_cache (notebook_id, cell_id);
"""


# ── Public dataclasses ─────────────────────────────────────────────

@dataclass
class CachedEntry:
    """Hydrated cache entry - payload + metadata.

    Slice-5 (2026-05-09) added three forward-declared fields for
    rich-rendering payloads (per ``feedback_dual_audience_ai_and_human``):

    * ``output_preview`` - structured DataFrame preview for spql/pipe
    * ``output_html`` - rendered markdown HTML for markdown cells
    * ``param_spec`` - parsed YAML param spec dict for param cells

    All three default to ``None`` / ``""`` so cache entries written
    before slice 5 remain valid (the load path tolerates missing keys).
    """
    content_hash: str
    output_hash: str
    notebook_id: str
    cell_id: str
    cell_type: str
    namespace_delta: dict
    output: Any
    output_repr: str
    stdout: str
    stderr: str
    exposed_names: list[str]
    runtime_ms: int
    executed_at: str
    last_accessed_at: str
    hit_count: int
    payload_size_bytes: int
    # Slice-5 forward-additive fields (load tolerates absence).
    output_preview: Optional[dict] = None
    output_html: str = ""
    param_spec: Optional[dict] = None


# ── Hash helpers ───────────────────────────────────────────────────

def compute_content_hash(
    cell: dict, prior_output_hashes: list[str],
) -> str:
    """Cache-key hash for a cell.

    Composed from the cell's ``type`` + ``source`` + the ordered list
    of prior cells' output hashes. Editing the cell or any upstream
    cell's output produces a different hash → cache miss. SHA-256 hex
    digest, 64 chars.
    """
    h = hashlib.sha256()
    h.update(b"type:")
    h.update((cell.get("type") or "").encode("utf-8"))
    h.update(b"\x00source:")
    h.update((cell.get("source") or "").encode("utf-8"))
    h.update(b"\x00prior:")
    for prior in prior_output_hashes:
        h.update(prior.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def compute_output_hash(payload: dict) -> str:
    """Forward-propagation hash for a cell's output.

    Hashes the canonical pickle of the cache payload (output +
    namespace_delta). Two cells producing byte-equivalent payloads
    produce equal output_hashes - preserves downstream cache validity
    when an upstream cell re-runs but yields the same result.
    """
    pickled = pickle.dumps(payload, protocol=_PICKLE_PROTOCOL)
    return hashlib.sha256(pickled).hexdigest()


# ── Internal helpers ───────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


# ── Store ──────────────────────────────────────────────────────────

class NotebookCacheStore:
    """SQLite-indexed filesystem cache for notebook cell outputs.

    Concurrency: SQLite WAL mode + one connection per call. The
    payload-write path uses :func:`functionality.atomic_write.write_bytes_atomic`
    so a crash mid-write never leaves a partial pickle.

    Eviction: LRU at the budget boundary. ``evict_to_budget(bytes)``
    deletes entries by ``last_accessed_at ASC`` until the cumulative
    payload size is at or below the budget. Active entries (recently
    accessed) naturally stay hot.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        payload_dir: Optional[Path | str] = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._payload_dir = (
            Path(payload_dir) if payload_dir else DEFAULT_PAYLOAD_DIR
        )
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema + connection management
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        os.makedirs(self._payload_dir, exist_ok=True)
        # Touch parent dir for the DB file
        os.makedirs(self._db_path.parent, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=30.0)
        try:
            conn.executescript(_SCHEMA)
            # WAL mode for concurrent reader/writer safety. Notebook
            # execution is mostly single-threaded but the SPA may
            # query the cache while the engine writes - WAL avoids
            # SQLITE_BUSY in that case.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, content_hash: str) -> Optional[CachedEntry]:
        """Look up a cached entry by content_hash. Returns ``None`` on
        miss. Updates ``last_accessed_at`` + ``hit_count`` on hit.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM notebook_cell_cache "
                    "WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if row is None:
                    return None
                payload_path = self._payload_dir / row["payload_path"]
                if not payload_path.is_file():
                    # Metadata says we have it but the payload's gone -
                    # row leaked. Clean up + miss.
                    logger.warning(
                        "[!] notebook_cache: payload missing for %s; "
                        "evicting metadata row.", content_hash[:12],
                    )
                    conn.execute(
                        "DELETE FROM notebook_cell_cache "
                        "WHERE content_hash = ?",
                        (content_hash,),
                    )
                    conn.commit()
                    return None
                try:
                    with open(payload_path, "rb") as f:
                        payload = pickle.load(f)  # nosec B301 - admin-tool
                except Exception as exc:
                    logger.warning(
                        "[!] notebook_cache: failed to unpickle %s: %s",
                        payload_path.name, exc,
                    )
                    return None
                # Update hit telemetry
                now = _now_iso()
                conn.execute(
                    "UPDATE notebook_cell_cache "
                    "SET last_accessed_at = ?, hit_count = hit_count + 1 "
                    "WHERE content_hash = ?",
                    (now, content_hash),
                )
                conn.commit()
                return CachedEntry(
                    content_hash=row["content_hash"],
                    output_hash=row["output_hash"],
                    notebook_id=row["notebook_id"],
                    cell_id=row["cell_id"],
                    cell_type=row["cell_type"],
                    namespace_delta=payload.get("namespace_delta", {}),
                    output=payload.get("output"),
                    output_repr=payload.get("output_repr", ""),
                    stdout=payload.get("stdout", ""),
                    stderr=payload.get("stderr", ""),
                    exposed_names=list(payload.get("exposed_names", [])),
                    runtime_ms=int(row["runtime_ms"]),
                    executed_at=row["executed_at"],
                    last_accessed_at=now,
                    hit_count=int(row["hit_count"]) + 1,
                    payload_size_bytes=int(row["payload_size_bytes"]),
                    # Slice-5: tolerate absent keys for pre-slice-5
                    # cache entries; new entries surface the full set.
                    output_preview=payload.get("output_preview"),
                    output_html=payload.get("output_html", "") or "",
                    param_spec=payload.get("param_spec"),
                )
            finally:
                conn.close()

    def put(
        self,
        *,
        content_hash: str,
        output_hash: str,
        notebook_id: str,
        cell_id: str,
        cell_type: str,
        payload: dict,
        runtime_ms: int,
        executed_at: str,
    ) -> int:
        """Serialise ``payload`` to disk + insert/replace metadata.

        Returns the on-disk size of the payload. Atomic write via
        ``write_bytes_atomic`` so a crash mid-write never leaves a
        half-pickle. ``content_hash`` is the primary key - re-puts
        for the same key replace the prior entry.
        """
        pickled = pickle.dumps(payload, protocol=_PICKLE_PROTOCOL)
        size = len(pickled)
        # Use the content_hash as the filename - collision-free, dedup-able.
        payload_path = self._payload_dir / f"{content_hash}.pkl"
        write_bytes_atomic(payload_path, pickled)

        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO notebook_cell_cache ("
                    "content_hash, output_hash, notebook_id, cell_id, "
                    "cell_type, payload_path, payload_size_bytes, "
                    "runtime_ms, executed_at, last_accessed_at, "
                    "hit_count, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "COALESCE((SELECT hit_count FROM notebook_cell_cache "
                    "WHERE content_hash = ?), 0), ?)",
                    (
                        content_hash, output_hash, notebook_id, cell_id,
                        cell_type, payload_path.name, size,
                        runtime_ms, executed_at, now,
                        content_hash, now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return size

    def invalidate(self, content_hash: str) -> bool:
        """Remove a cache entry by content_hash. Returns True if a row
        was deleted (and the payload file removed).
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload_path FROM notebook_cell_cache "
                    "WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if row is None:
                    return False
                conn.execute(
                    "DELETE FROM notebook_cell_cache "
                    "WHERE content_hash = ?",
                    (content_hash,),
                )
                conn.commit()
                payload_path = self._payload_dir / row["payload_path"]
                try:
                    if payload_path.is_file():
                        payload_path.unlink()
                except OSError as exc:
                    logger.warning(
                        "[!] notebook_cache: could not unlink %s: %s",
                        payload_path, exc,
                    )
                return True
            finally:
                conn.close()

    def invalidate_notebook(self, notebook_id: str) -> int:
        """Drop every cache entry for a notebook. Returns count
        deleted. Used when a notebook is renamed / hard-deleted.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT content_hash, payload_path "
                    "FROM notebook_cell_cache WHERE notebook_id = ?",
                    (notebook_id,),
                ).fetchall()
                if not rows:
                    return 0
                for row in rows:
                    payload_path = self._payload_dir / row["payload_path"]
                    try:
                        if payload_path.is_file():
                            payload_path.unlink()
                    except OSError as exc:
                        logger.warning(
                            "[!] notebook_cache: could not unlink %s: %s",
                            payload_path, exc,
                        )
                conn.execute(
                    "DELETE FROM notebook_cell_cache "
                    "WHERE notebook_id = ?",
                    (notebook_id,),
                )
                conn.commit()
                return len(rows)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Stats + eviction
    # ------------------------------------------------------------------

    def total_size_bytes(self) -> int:
        """Sum of ``payload_size_bytes`` across all cache rows."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(payload_size_bytes), 0) AS total "
                    "FROM notebook_cell_cache"
                ).fetchone()
                return int(row["total"])
            finally:
                conn.close()

    def count(self) -> int:
        """Number of cache entries."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM notebook_cell_cache"
                ).fetchone()
                return int(row["n"])
            finally:
                conn.close()

    def stats(self) -> dict:
        """Aggregate stats for the Settings page / audit logs."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT "
                    "  COUNT(*) AS entries, "
                    "  COALESCE(SUM(payload_size_bytes), 0) AS bytes, "
                    "  COALESCE(SUM(hit_count), 0) AS total_hits, "
                    "  MIN(created_at) AS oldest, "
                    "  MAX(last_accessed_at) AS most_recent "
                    "FROM notebook_cell_cache"
                ).fetchone()
                return {
                    "entries": int(row["entries"]),
                    "size_bytes": int(row["bytes"]),
                    "size_gb": round(int(row["bytes"]) / (1024 ** 3), 4),
                    "total_hits": int(row["total_hits"]),
                    "oldest_created_at": row["oldest"],
                    "most_recent_access_at": row["most_recent"],
                }
            finally:
                conn.close()

    def evict_to_budget(self, budget_bytes: int) -> int:
        """LRU-evict entries until total size <= ``budget_bytes``.

        Returns total bytes freed. ``budget_bytes <= 0`` evicts
        everything (full clear).
        """
        freed = 0
        with self._lock:
            conn = self._connect()
            try:
                while True:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(payload_size_bytes), 0) AS total "
                        "FROM notebook_cell_cache"
                    ).fetchone()
                    total = int(row["total"])
                    if budget_bytes > 0 and total <= budget_bytes:
                        break
                    # Pick LRU candidate
                    victim = conn.execute(
                        "SELECT content_hash, payload_path, payload_size_bytes "
                        "FROM notebook_cell_cache "
                        "ORDER BY last_accessed_at ASC "
                        "LIMIT 1"
                    ).fetchone()
                    if victim is None:
                        break  # Cache empty; nothing to evict
                    conn.execute(
                        "DELETE FROM notebook_cell_cache "
                        "WHERE content_hash = ?",
                        (victim["content_hash"],),
                    )
                    payload_path = self._payload_dir / victim["payload_path"]
                    try:
                        if payload_path.is_file():
                            payload_path.unlink()
                    except OSError as exc:
                        logger.warning(
                            "[!] notebook_cache: could not unlink %s: %s",
                            payload_path, exc,
                        )
                    freed += int(victim["payload_size_bytes"])
                conn.commit()
            finally:
                conn.close()
        if freed > 0:
            logger.info(
                "[i] notebook_cache: evicted %d bytes "
                "(LRU, budget=%d bytes)", freed, budget_bytes,
            )
        return freed

    def clear(self) -> int:
        """Drop EVERY cache entry. Returns total bytes freed."""
        return self.evict_to_budget(budget_bytes=-1)


# ── Singleton ──────────────────────────────────────────────────────

_instance: Optional[NotebookCacheStore] = None
_instance_lock = threading.Lock()


def get_store() -> NotebookCacheStore:
    """Return the process-wide NotebookCacheStore singleton."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = NotebookCacheStore()
        return _instance


def reset_for_tests() -> None:
    """Clear the cached singleton. Tests should call this before
    monkeypatching DEFAULT_DB_PATH / DEFAULT_PAYLOAD_DIR.
    """
    global _instance
    with _instance_lock:
        _instance = None


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_PAYLOAD_DIR",
    "CachedEntry",
    "NotebookCacheStore",
    "compute_content_hash",
    "compute_output_hash",
    "get_store",
    "reset_for_tests",
]
