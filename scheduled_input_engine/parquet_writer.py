"""
Parquet Writer
──────────────
Atomic writes and periodic compaction for the indexes/ directory.

Design follows the Iceberg / Delta Lake append-then-compact pattern:
  1. Each ingestion writes immediately to a new file (no buffering, no data loss).
  2. Writes are atomic: .tmp → os.replace() (atomic on POSIX and Windows;
     unlike os.rename, os.replace overwrites an existing target on Windows
     instead of raising FileExistsError).
  3. A periodic compaction pass merges small files into target-sized ones.
  4. Readers never see partial files; concurrent reads are safe (POSIX unlink
     semantics keep the inode alive until all open handles close).
"""

import logging
import os
import time
import uuid
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Sentinel extension for in-progress compaction
_COMPACTING_LOCK = ".compacting"


class ParquetWriter:
    """Atomic Parquet output with single-pass compaction."""

    def __init__(self, indexes_dir: str | Path, target_file_mb: int = 128):
        self._indexes_dir = Path(indexes_dir).resolve()
        self._target_bytes = target_file_mb * 1024 * 1024
        self._indexes_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Atomic write
    # ------------------------------------------------------------------

    def write_atomic(
        self,
        df: pd.DataFrame,
        subdirectory: str,
        filename: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Write *df* as gzip-compressed Parquet via atomic rename.

        Parameters
        ----------
        df : DataFrame
            Data to persist.
        subdirectory : str
            Target subdirectory under indexes/ (created if absent).
        filename : str, optional
            Final filename.  Defaults to ``<epoch>_<uuid>.system4.system4.parquet``.
        overwrite : bool
            If *False* and file exists, append a timestamp to avoid collision.

        Returns
        -------
        Path
            The resolved path of the written file.
        """
        target_dir = self._resolve_subdir(subdirectory)
        target_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            # L-CE-12 (2026-04-22): the doubled ``.system4.system4.parquet``
            # suffix is load-bearing - it's the canonical signature that
            # distinguishes SpeakesQuery-managed Parquet from user-dropped
            # Parquet in the same directory. 27+ references across the
            # repo depend on it (CodeExecutor's AST enforcement at
            # executor.py:270, glob patterns in cleanup/compaction,
            # GENERATE_RESULTS contract in 90+ library scripts, live
            # parquet files on every deployed install). Renaming is a
            # breaking data-layout change; keep the convention.
            filename = f"{time.time()}_{uuid.uuid4()}.system4.system4.parquet"

        final_path = (target_dir / filename).resolve()
        if not final_path.is_relative_to(target_dir):
            raise ValueError("Path traversal detected in output filename.")

        if final_path.exists() and not overwrite:
            stem = final_path.stem
            suffix = final_path.suffix
            final_path = target_dir / f"{stem}_{int(time.time())}{suffix}"

        # Write to a hidden temp file, then atomic replace. os.replace is
        # atomic on POSIX (like os.rename) AND overwrites a pre-existing
        # target on Windows; os.rename raises FileExistsError on Windows when
        # the target exists, which breaks overwrite=True. Indexes are stored
        # under directory volumes (not per-file bind mounts), so the EBUSY
        # fallback in functionality/atomic_write.py is not needed here.
        tmp_path = final_path.with_name(f".{final_path.name}.tmp")
        try:
            df.to_parquet(tmp_path, index=False, compression="gzip")
            os.replace(tmp_path, final_path)
            logger.info(
                "[i] Wrote %d rows (%s) to %s",
                len(df),
                _human_size(final_path.stat().st_size),
                final_path,
            )
        except BaseException:
            # Clean up partial temp file on any failure
            tmp_path.unlink(missing_ok=True)
            raise

        return final_path

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def compact_subdirectory(self, subdirectory: str) -> int:
        """Merge small parquet files in *subdirectory* into target-sized files.

        Files already at or above 80% of the target size are left alone.
        Returns the number of files removed (replaced by merged files).
        """
        target_dir = self._resolve_subdir(subdirectory)
        if not target_dir.exists():
            return 0

        lock_file = target_dir / _COMPACTING_LOCK
        if lock_file.exists():
            logger.warning(
                "[!] Compaction already in progress for %s, skipping", subdirectory
            )
            return 0

        try:
            lock_file.touch()
            return self._compact_dir(target_dir)
        finally:
            lock_file.unlink(missing_ok=True)

    def compact_all(self) -> int:
        """Compact every subdirectory under indexes/.  Returns total files removed."""
        if not self._indexes_dir.exists():
            return 0

        total_removed = 0
        for subdir in self._indexes_dir.iterdir():
            if subdir.is_dir():
                try:
                    removed = self.compact_subdirectory(subdir.name)
                    total_removed += removed
                except Exception as exc:
                    logger.error(
                        "[x] Compaction failed for %s: %s", subdir.name, exc
                    )
        if total_removed:
            logger.info("[i] Compaction complete: %d files merged away", total_removed)
        return total_removed

    def _compact_dir(self, target_dir: Path) -> int:
        """Inner compaction for a single directory.  Caller holds the lock."""
        threshold = int(self._target_bytes * 0.8)

        # Collect candidate files (small parquet files, sorted oldest-first)
        candidates: list[tuple[Path, int]] = []
        for f in sorted(target_dir.glob("*.system4.system4.parquet")):
            if f.name.startswith("."):
                continue
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            if sz < threshold:
                candidates.append((f, sz))

        if len(candidates) < 2:
            return 0

        # Greedily group consecutive small files up to target size
        groups: list[list[tuple[Path, int]]] = []
        current_group: list[tuple[Path, int]] = []
        current_size = 0

        for path, sz in candidates:
            if current_size + sz > self._target_bytes and current_group:
                if len(current_group) >= 2:
                    groups.append(current_group)
                current_group = [(path, sz)]
                current_size = sz
            else:
                current_group.append((path, sz))
                current_size += sz

        if len(current_group) >= 2:
            groups.append(current_group)

        removed = 0
        for group in groups:
            try:
                removed += self._merge_group(target_dir, group)
            except Exception as exc:
                paths = [str(p) for p, _ in group]
                logger.error("[x] Failed to merge group %s: %s", paths, exc)

        return removed

    def _merge_group(self, target_dir: Path, group: list[tuple[Path, int]]) -> int:
        """Concatenate files in *group*, write a new merged file, delete originals."""
        dfs: list[pd.DataFrame] = []
        for path, _ in group:
            try:
                dfs.append(pd.read_parquet(path))
            except Exception as exc:
                logger.warning("[!] Skipping unreadable file %s: %s", path, exc)

        if len(dfs) < 2:
            return 0

        merged = pd.concat(dfs, ignore_index=True)
        merged_name = f"{time.time()}_{uuid.uuid4()}.system4.system4.parquet"
        merged_path = target_dir / merged_name
        tmp_path = merged_path.with_name(f".{merged_path.name}.tmp")

        try:
            merged.to_parquet(tmp_path, index=False, compression="gzip")
            # os.replace for parity with write_atomic above; also works on
            # Windows when the (rare) merged-name collision occurs.
            os.replace(tmp_path, merged_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        # Delete originals only after the merged file is safely in place
        deleted = 0
        for path, _ in group:
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                logger.warning("[!] Could not delete compacted file %s: %s", path, exc)

        logger.info(
            "[i] Compacted %d files (%s) → %s (%s)",
            deleted,
            _human_size(sum(sz for _, sz in group)),
            merged_path.name,
            _human_size(merged_path.stat().st_size),
        )
        return deleted

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_subdir(self, subdirectory: str) -> Path:
        """Resolve and validate a subdirectory under indexes/."""
        if not subdirectory:
            return self._indexes_dir

        resolved = (self._indexes_dir / subdirectory).resolve()
        if not resolved.is_relative_to(self._indexes_dir):
            raise ValueError("Path traversal detected in subdirectory.")
        return resolved


def _human_size(nbytes: int) -> str:
    """Format byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"
