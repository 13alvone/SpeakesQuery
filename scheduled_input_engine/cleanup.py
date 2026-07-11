"""
Disk Cleanup
────────────
Single-pass directory scanning with size-delta tracking.
No re-scanning after each deletion.

Configurable via GlobalSettings - reads limits at invocation time so changes
take effect without restarting the engine.  Skips subdirectories with a
``.compacting`` lock file (compaction in progress).
"""

import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

INDEXES_DIR = Path(__file__).parent.parent / "indexes"
LOGS_DIR = Path(__file__).parent.parent / "indexes" / "logs"
GIGABYTE = 1024 ** 3

# Fallback defaults (used only when GlobalSettings is not available)
DEFAULT_MAX_DIR_SIZE_GB = 5
DEFAULT_MAX_TOTAL_SIZE_GB = 100
DEFAULT_MAX_LOGS_TOTAL_SIZE_GB = 5
DEFAULT_MAX_LOGS_SUBDIR_SIZE_GB = 2

# Compaction lock sentinel - shared constant with parquet_writer.py
_COMPACTING_LOCK = ".compacting"


def cleanup_indexes(
    indexes_dir: str | Path | None = None,
    max_subdir_gb: int | None = None,
    max_total_gb: int | None = None,
    *,
    skip_subdirs: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """Enforce disk limits on the indexes directory.

    Parameters
    ----------
    indexes_dir : Path, optional
        Override the default indexes/ path.
    max_subdir_gb : int, optional
        Per-subdirectory cap in GB.  Defaults to ``DEFAULT_MAX_DIR_SIZE_GB``
        or the value from GlobalSettings if available.
    max_total_gb : int, optional
        Total indexes/ cap in GB.  Defaults to ``DEFAULT_MAX_TOTAL_SIZE_GB``
        or the value from GlobalSettings if available.
    skip_subdirs : iterable of str, optional
        Subdirectory names under ``indexes_dir`` to exclude from both the
        per-subdir and total-size accounting. Used to keep ``indexes/logs/``
        out of the main indexes budget - it has its own cap enforced by
        ``cleanup_logs()`` and a shared-parent size ledger here would be
        double-counted.

    Returns
    -------
    list of (deleted_path, reason) tuples for telemetry.
    """
    # Resolve settings - prefer explicit args, then GlobalSettings, then defaults
    if max_subdir_gb is None or max_total_gb is None:
        try:
            from global_settings import get_settings
            settings = get_settings()
            if max_subdir_gb is None:
                max_subdir_gb = settings.get("max_subdirectory_size_gb")
            if max_total_gb is None:
                max_total_gb = settings.get("max_total_size_gb")
        except Exception:
            pass
    if max_subdir_gb is None:
        max_subdir_gb = DEFAULT_MAX_DIR_SIZE_GB
    if max_total_gb is None:
        max_total_gb = DEFAULT_MAX_TOTAL_SIZE_GB

    max_dir_bytes = max_subdir_gb * GIGABYTE
    max_total_bytes = max_total_gb * GIGABYTE

    root = Path(indexes_dir) if indexes_dir else INDEXES_DIR
    if not root.exists():
        return []

    skip_set = {s for s in (skip_subdirs or ()) if s}
    deleted: list[tuple[str, str]] = []
    total_size = 0

    # Pass 1: Enforce per-subdirectory limits
    for subdir in root.iterdir():
        if not subdir.is_dir():
            continue

        if subdir.name in skip_set:
            # Excluded from this cleanup (e.g. logs/ has its own budget).
            continue

        # Skip if compaction is in progress
        if (subdir / _COMPACTING_LOCK).exists():
            logger.info("[i] Cleanup: skipping %s (compaction in progress)", subdir.name)
            # Still need to account for its size in the total
            for f in subdir.rglob("*"):
                if f.is_file():
                    try:
                        total_size += f.stat().st_size
                    except OSError:
                        pass
            continue

        # Scan once: collect all files with size and mtime
        files: list[tuple[Path, int, float]] = []
        dir_size = 0
        for f in subdir.rglob("*"):
            if f.is_file():
                try:
                    st = f.stat()
                    files.append((f, st.st_size, st.st_mtime))
                    dir_size += st.st_size
                except OSError:
                    continue

        # Sort by mtime ascending (oldest first) for FIFO deletion
        files.sort(key=lambda x: x[2])

        # Delete oldest files until under limit, tracking delta
        i = 0
        while dir_size > max_dir_bytes and i < len(files):
            path, sz, _ = files[i]
            try:
                path.unlink(missing_ok=True)
                dir_size -= sz
                deleted.append((
                    str(path),
                    f"subdir over {max_subdir_gb}GB limit",
                ))
                logger.info("[i] Cleanup: deleted %s (subdir limit)", path)
            except OSError as exc:
                logger.error("[x] Cleanup: could not delete %s: %s", path, exc)
            i += 1

        total_size += dir_size

    # Pass 2: Enforce total limit across all subdirectories
    if total_size > max_total_bytes:
        all_files: list[tuple[Path, int, float]] = []
        for f in root.rglob("*"):
            if f.is_file():
                # Respect skip_subdirs in the total-size pass as well.
                try:
                    rel = f.relative_to(root)
                except ValueError:
                    continue
                if rel.parts and rel.parts[0] in skip_set:
                    continue
                try:
                    st = f.stat()
                    all_files.append((f, st.st_size, st.st_mtime))
                except OSError:
                    continue
        all_files.sort(key=lambda x: x[2])

        for path, sz, _ in all_files:
            if total_size <= max_total_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total_size -= sz
                deleted.append((str(path), "total over limit"))
                logger.info("[i] Cleanup: deleted %s (total limit)", path)
            except OSError as exc:
                logger.error("[x] Cleanup: could not delete %s: %s", path, exc)

    if deleted:
        logger.info("[i] Cleanup complete: %d files removed", len(deleted))

    return deleted


def cleanup_logs(
    logs_dir: str | Path | None = None,
    max_subdir_gb: int | None = None,
    max_total_gb: int | None = None,
) -> list[tuple[str, str]]:
    """Enforce the logs-tree size budget.

    Mirrors :func:`cleanup_indexes` semantics (per-subdir + total, oldest-first
    by mtime) but operates on ``indexes/logs/`` with its own configurable
    ``max_logs_size_gb`` / ``max_logs_subdirectory_size_gb`` budgets. Kept as
    a separate function so the two directories' budgets are independent - an
    overfull claude_api/ log should never evict user-ingested Parquet data.
    """
    if max_subdir_gb is None or max_total_gb is None:
        try:
            from global_settings import get_settings
            settings = get_settings()
            if max_subdir_gb is None:
                max_subdir_gb = settings.get("max_logs_subdirectory_size_gb")
            if max_total_gb is None:
                max_total_gb = settings.get("max_logs_size_gb")
        except Exception:
            pass
    if max_subdir_gb is None:
        max_subdir_gb = DEFAULT_MAX_LOGS_SUBDIR_SIZE_GB
    if max_total_gb is None:
        max_total_gb = DEFAULT_MAX_LOGS_TOTAL_SIZE_GB

    max_dir_bytes = max_subdir_gb * GIGABYTE
    max_total_bytes = max_total_gb * GIGABYTE

    # Resolve the logs root (must be provided, settings-derived, or default).
    if logs_dir is not None:
        root = Path(logs_dir)
    else:
        try:
            from global_settings import get_settings
            root = get_settings().logs_dir()
        except Exception:
            root = LOGS_DIR

    if not root.exists():
        return []

    deleted: list[tuple[str, str]] = []
    total_size = 0

    # Pass 1: per-subdir enforcement
    for subdir in root.iterdir():
        if not subdir.is_dir():
            continue

        if (subdir / _COMPACTING_LOCK).exists():
            logger.info("[i] Logs cleanup: skipping %s (compacting)", subdir.name)
            for f in subdir.rglob("*"):
                if f.is_file():
                    try:
                        total_size += f.stat().st_size
                    except OSError:
                        pass
            continue

        files: list[tuple[Path, int, float]] = []
        dir_size = 0
        for f in subdir.rglob("*"):
            if f.is_file():
                try:
                    st = f.stat()
                    files.append((f, st.st_size, st.st_mtime))
                    dir_size += st.st_size
                except OSError:
                    continue

        files.sort(key=lambda x: x[2])

        i = 0
        while dir_size > max_dir_bytes and i < len(files):
            path, sz, _ = files[i]
            try:
                path.unlink(missing_ok=True)
                dir_size -= sz
                deleted.append((
                    str(path),
                    f"logs subdir over {max_subdir_gb}GB limit",
                ))
                logger.info("[i] Logs cleanup: deleted %s (subdir limit)", path)
            except OSError as exc:
                logger.error("[x] Logs cleanup: could not delete %s: %s", path, exc)
            i += 1

        total_size += dir_size

    # Pass 2: total enforcement across all log subdirs
    if total_size > max_total_bytes:
        all_files: list[tuple[Path, int, float]] = []
        for f in root.rglob("*"):
            if f.is_file():
                try:
                    st = f.stat()
                    all_files.append((f, st.st_size, st.st_mtime))
                except OSError:
                    continue
        all_files.sort(key=lambda x: x[2])

        for path, sz, _ in all_files:
            if total_size <= max_total_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total_size -= sz
                deleted.append((str(path), "logs total over limit"))
                logger.info("[i] Logs cleanup: deleted %s (total limit)", path)
            except OSError as exc:
                logger.error("[x] Logs cleanup: could not delete %s: %s", path, exc)

    if deleted:
        logger.info("[i] Logs cleanup complete: %d files removed", len(deleted))

    return deleted


# Default fallback for the embeddings budget (used only when GlobalSettings
# is not available - every production caller goes through the settings path).
DEFAULT_MAX_EMBEDDINGS_SIZE_GB = 5


def cleanup_embeddings(
    indexes_dir: str | Path | None = None,
    max_total_gb: int | None = None,
) -> list[tuple[str, str]]:
    """Enforce the embedding-sidecar budget (Phase 1 / Bet 2 slice 5).

    Walks ``indexes/`` for ``*.embeddings.parquet`` files (the sidecar
    convention from slice 2). Computes total size; if over budget, evicts
    oldest-first by mtime until back under. Each evicted sidecar will be
    re-created on the next sweeper tick if ``embeddings_enabled`` and the
    source parquet still exists - this is a soft "you exceeded the cache"
    signal, not a destructive prune.

    Independent budget - NEVER touches non-sidecar parquets, NEVER walks
    into ``IMMUTABLE/`` (where slice 3 already excludes from sweeping).

    Returns
    -------
    list of (deleted_path, reason) tuples for telemetry.
    """
    if max_total_gb is None:
        try:
            from global_settings import get_settings
            max_total_gb = get_settings().get("max_embeddings_size_gb")
        except Exception:
            pass
    if max_total_gb is None:
        max_total_gb = DEFAULT_MAX_EMBEDDINGS_SIZE_GB

    max_total_bytes = max_total_gb * GIGABYTE

    if indexes_dir is not None:
        root = Path(indexes_dir)
    else:
        try:
            from global_settings import get_settings
            root = get_settings().indexes_dir()
        except Exception:
            root = INDEXES_DIR

    if not root.exists():
        return []

    # Collect every sidecar under root, skipping IMMUTABLE/ entirely.
    # The sidecar convention (slice 2) is `<source>.embeddings.parquet`
    # - we filter on the suffix rather than glob `**/*.embeddings.parquet`
    # so a future suffix change in embedding_sidecar.py only needs to be
    # mirrored in one constant.
    try:
        from functionality.embedding_sidecar import SIDECAR_SUFFIX, is_sidecar_path
    except ImportError:
        # Module is hard-dep but be defensive - a partial install
        # shouldn't crash the cleanup loop.
        return []

    sidecars: list[tuple[Path, int, float]] = []
    total_size = 0
    for path in root.rglob(f"*{SIDECAR_SUFFIX}"):
        if not path.is_file():
            continue
        if not is_sidecar_path(path):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == "IMMUTABLE":
            # Defense in depth: IMMUTABLE/ is not in the sweeper's scope,
            # but if a sidecar somehow ended up there, never evict.
            continue
        try:
            st = path.stat()
            sidecars.append((path, st.st_size, st.st_mtime))
            total_size += st.st_size
        except OSError:
            continue

    deleted: list[tuple[str, str]] = []
    if total_size <= max_total_bytes:
        return deleted

    # Evict oldest-first until back under budget.
    sidecars.sort(key=lambda x: x[2])
    for path, sz, _ in sidecars:
        if total_size <= max_total_bytes:
            break
        try:
            path.unlink(missing_ok=True)
            total_size -= sz
            deleted.append((str(path), "embeddings total over limit"))
            logger.info(
                "[i] Embeddings cleanup: deleted %s (over %d GB)",
                path, max_total_gb,
            )
        except OSError as exc:
            logger.error(
                "[x] Embeddings cleanup: could not delete %s: %s", path, exc,
            )

    if deleted:
        logger.info(
            "[i] Embeddings cleanup complete: %d sidecars removed (%.2f GB freed)",
            len(deleted),
            sum(sz for _, sz, _ in sidecars[: len(deleted)]) / GIGABYTE,
        )

    return deleted
