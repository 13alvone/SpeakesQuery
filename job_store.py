"""
Job Store
─────────
Manages query result snapshots ("jobs") for the loadjob command.

Two tiers of storage:
  - **Auto jobs**: The last N query results are kept automatically in a
    rotating ring buffer. These are transient and unnamed.
  - **Saved jobs**: User-initiated snapshots with an optional custom name
    and a configurable TTL (1–365 days).

Data is stored as Parquet files in ``jobs/``.  A single JSON index
(``jobs/_index.json``) holds metadata for all jobs.
"""

import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from functionality.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
JOBS_DIR = _PROJECT_ROOT / "jobs"
INDEX_FILE = JOBS_DIR / "_index.json"

# How many auto-saved results to keep in the ring buffer
AUTO_RING_SIZE = 10


class JobStore:
    """Thread-safe manager for query result snapshots."""

    def __init__(self):
        self._dir = JOBS_DIR
        self._lock = threading.Lock()
        self._index: List[Dict] = []

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the jobs directory and load (or create) the index."""
        self._dir.mkdir(parents=True, exist_ok=True)
        if INDEX_FILE.exists():
            try:
                with open(INDEX_FILE, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
                logger.info("[i] JobStore: loaded %d job(s) from index.", len(self._index))
            except (json.JSONDecodeError, IOError) as exc:
                logger.warning("[!] JobStore: corrupt index, starting fresh: %s", exc)
                self._index = []
        else:
            self._index = []
            self._flush_index()
        # Clean up expired jobs on startup
        self.cleanup_expired()

    # ------------------------------------------------------------------
    # Auto-save (ring buffer of last N results)
    # ------------------------------------------------------------------

    def save_auto(self, df: pd.DataFrame, query: str) -> str:
        """
        Save a query result as an auto-job.  Rotates the ring buffer so
        only the most recent ``AUTO_RING_SIZE`` auto-jobs are kept.

        Returns:
            The generated ``job_id``.
        """
        job_id = self._generate_job_id()
        meta = {
            "job_id": job_id,
            "name": None,
            "query": query,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": None,  # auto-jobs expire via rotation, not TTL
            "row_count": len(df),
            "col_count": len(df.columns),
            "auto": True,
            "ttl_days": None,
            "lookup_copy": None,
        }

        with self._lock:
            self._write_data(job_id, df)
            self._index.append(meta)
            self._rotate_auto_jobs()
            self._flush_index()

        logger.info("[i] JobStore: auto-saved job '%s' (%d rows).", job_id, len(df))
        return job_id

    # ------------------------------------------------------------------
    # User-initiated save
    # ------------------------------------------------------------------

    def save_job(
        self,
        job_id: str,
        *,
        name: Optional[str] = None,
        ttl_days: int = 10,
        save_to_lookups: bool = False,
    ) -> Dict:
        """
        Promote an existing auto-job to a saved job, or re-save with
        updated metadata.

        Args:
            job_id:          The job to save (must already exist).
            name:            Optional custom display name.
            ttl_days:        Retention period in days (1–365).
            save_to_lookups: If True, also write a CSV copy to lookups/.

        Returns:
            The updated job metadata dict.
        """
        ttl_days = max(1, min(365, ttl_days))

        with self._lock:
            meta = self._find_meta(job_id)
            if meta is None:
                raise FileNotFoundError(f"Job '{job_id}' not found.")

            now = datetime.utcnow()
            meta["auto"] = False
            meta["name"] = name or meta.get("name")
            meta["ttl_days"] = ttl_days
            meta["expires_at"] = (now + timedelta(days=ttl_days)).isoformat()

            if save_to_lookups:
                lookup_name = self._lookup_filename(meta)
                df = self._read_data(job_id)
                lookups_dir = _PROJECT_ROOT / "lookups"
                lookups_dir.mkdir(parents=True, exist_ok=True)
                csv_path = lookups_dir / lookup_name
                df.to_csv(csv_path, index=False)
                meta["lookup_copy"] = lookup_name
                logger.info("[i] JobStore: saved lookup copy '%s'.", lookup_name)

            self._flush_index()

        logger.info("[i] JobStore: saved job '%s' (TTL %dd).", job_id, ttl_days)
        return dict(meta)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def load(self, job_id: str) -> pd.DataFrame:
        """
        Load a job's DataFrame by its ``job_id``.

        Raises:
            FileNotFoundError: If the job does not exist or has expired.
        """
        with self._lock:
            meta = self._find_meta(job_id)

            # Also try matching by custom name
            if meta is None:
                meta = self._find_meta_by_name(job_id)

            if meta is None:
                raise FileNotFoundError(f"Job '{job_id}' not found.")

            # Check TTL
            if meta.get("expires_at"):
                expires = datetime.fromisoformat(meta["expires_at"])
                if datetime.utcnow() > expires:
                    raise FileNotFoundError(
                        f"Job '{job_id}' expired on {meta['expires_at']}."
                    )

            return self._read_data(meta["job_id"])

    def list_jobs(self) -> List[Dict]:
        """Return metadata for all non-expired jobs, newest first."""
        with self._lock:
            now = datetime.utcnow()
            result = []
            for meta in self._index:
                if meta.get("expires_at"):
                    try:
                        if datetime.utcnow() > datetime.fromisoformat(meta["expires_at"]):
                            continue
                    except ValueError:
                        pass
                result.append(dict(meta))
            result.sort(key=lambda m: m.get("created_at", ""), reverse=True)
            return result

    def get_job_meta(self, job_id: str) -> Optional[Dict]:
        """Return metadata for a single job, or None."""
        with self._lock:
            meta = self._find_meta(job_id)
            if meta is None:
                meta = self._find_meta_by_name(job_id)
            return dict(meta) if meta else None

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete_job(self, job_id: str):
        """Delete a job's data and metadata."""
        with self._lock:
            meta = self._find_meta(job_id)
            if meta is None:
                raise FileNotFoundError(f"Job '{job_id}' not found.")
            self._delete_data(job_id)
            self._index = [m for m in self._index if m["job_id"] != job_id]
            self._flush_index()
        logger.info("[i] JobStore: deleted job '%s'.", job_id)

    def cleanup_expired(self):
        """Remove all expired jobs from disk and index."""
        now = datetime.utcnow()
        to_delete = []
        with self._lock:
            for meta in self._index:
                if meta.get("expires_at"):
                    try:
                        if now > datetime.fromisoformat(meta["expires_at"]):
                            to_delete.append(meta["job_id"])
                    except ValueError:
                        pass
            for jid in to_delete:
                self._delete_data(jid)
            if to_delete:
                self._index = [
                    m for m in self._index if m["job_id"] not in set(to_delete)
                ]
                self._flush_index()
                logger.info("[i] JobStore: cleaned up %d expired job(s).", len(to_delete))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_job_id() -> str:
        """Generate a job ID in the format ``<epoch>.<uuid>``."""
        epoch = f"{time.time():.6f}"
        uid = str(uuid.uuid4())
        return f"{epoch}_{uid}"

    def _find_meta(self, job_id: str) -> Optional[Dict]:
        """Find a job by exact job_id.  Returns the dict ref (mutable)."""
        for meta in self._index:
            if meta["job_id"] == job_id:
                return meta
        return None

    def _find_meta_by_name(self, name: str) -> Optional[Dict]:
        """Find a job by custom name.  Returns the dict ref (mutable)."""
        for meta in self._index:
            if meta.get("name") and meta["name"] == name:
                return meta
        return None

    def _write_data(self, job_id: str, df: pd.DataFrame):
        """Write a DataFrame to disk (Parquet if available, else pickle)."""
        try:
            path = self._dir / f"{job_id}.parquet"
            # Coerce mixed-type object columns to string so PyArrow can
            # serialise them without "Expected bytes, got a 'float' object".
            clean = df.copy()
            for col in clean.columns:
                if clean[col].dtype == object:
                    clean[col] = clean[col].astype(str)
            clean.to_parquet(path, index=False)
        except ImportError:
            path = self._dir / f"{job_id}.pkl"
            df.to_pickle(path)

    def _read_data(self, job_id: str) -> pd.DataFrame:
        """Read a DataFrame from disk (tries Parquet first, then pickle)."""
        parquet_path = self._dir / f"{job_id}.parquet"
        pickle_path = self._dir / f"{job_id}.pkl"
        if parquet_path.exists():
            return pd.read_parquet(parquet_path)
        elif pickle_path.exists():
            return pd.read_pickle(pickle_path)
        else:
            raise FileNotFoundError(f"Data file for job '{job_id}' not found.")

    def _delete_data(self, job_id: str):
        """Delete the data file for a job (both Parquet and pickle variants)."""
        for ext in (".parquet", ".pkl"):
            path = self._dir / f"{job_id}{ext}"
            if path.exists():
                path.unlink()

    def _rotate_auto_jobs(self):
        """Keep only the most recent AUTO_RING_SIZE auto-jobs."""
        auto_jobs = [m for m in self._index if m.get("auto")]
        if len(auto_jobs) <= AUTO_RING_SIZE:
            return
        # Sort oldest first
        auto_jobs.sort(key=lambda m: m.get("created_at", ""))
        to_remove = auto_jobs[: len(auto_jobs) - AUTO_RING_SIZE]
        remove_ids = {m["job_id"] for m in to_remove}
        for jid in remove_ids:
            self._delete_data(jid)
        self._index = [m for m in self._index if m["job_id"] not in remove_ids]

    def _flush_index(self):
        """Write the in-memory index to disk."""
        try:
            text = json.dumps(self._index, indent=2)
            write_text_atomic(INDEX_FILE, text)
        except IOError as exc:
            logger.error("[x] JobStore: failed to write index: %s", exc)

    @staticmethod
    def _lookup_filename(meta: Dict) -> str:
        """Generate a CSV filename for the lookups copy."""
        name = meta.get("name") or meta["job_id"]
        # Sanitise for filesystem
        safe = "".join(c if c.isalnum() or c in "_-." else "_" for c in name)
        if not safe.endswith(".csv"):
            safe += ".csv"
        return safe


# ── Module-level default singleton ────────────────────────────────────
# Used by GeneralHandler.load_job() and any other module that needs
# direct access without going through CmdExecutionBackend.
_default_store: Optional[JobStore] = None
_default_lock = threading.Lock()


def get_default_job_store() -> JobStore:
    """Return (and lazily initialise) the module-level JobStore singleton."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = JobStore()
                _default_store.initialize()
    return _default_store
