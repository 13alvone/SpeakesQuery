"""Atomic file write helpers.

A crash partway through ``open(path, "w")`` leaves the target file truncated
or empty.  These helpers stage the new contents in a sibling temp file in
the same directory, fsync to flush kernel buffers, then ``os.replace`` -
which is atomic on POSIX and Windows.  If anything fails the temp file is
removed and the original target is untouched.

Use these for any persistent state file (YAML stores, JSON indexes,
settings overrides) so a SIGKILL or power loss can't corrupt the file.

Parquet writes have their own atomic implementation in
``scheduled_input_engine/parquet_writer.py`` because they need binary mode
plus the same-directory rename invariant - these helpers exist for the
text-mode persistence layer.
"""
from __future__ import annotations

import errno
import logging
import os
import tempfile
from pathlib import Path
from typing import Union

PathLike = Union[str, os.PathLike]

logger = logging.getLogger(__name__)

# Errnos where ``os.replace`` legitimately fails on a bind-mounted file or
# across filesystems - Docker mounts a single file into the container, so the
# mount point itself cannot be replaced (EBUSY on Linux, EPERM on some
# configurations). EXDEV is the cross-device move case. In all three cases
# an in-place truncate+write is the safe fallback.
_REPLACE_FALLBACK_ERRNOS = frozenset({errno.EBUSY, errno.EXDEV, errno.EPERM})


def _inplace_write(target: Path, data: bytes, mode: int | None) -> None:
    """Write *data* to *target* in place (non-atomic fallback).

    Used when ``os.replace`` fails because the target is a bind-mount point
    inside a container. Truncating and writing the same file preserves the
    inode, so the mount stays intact, at the cost of a short window where a
    crash would leave the file partially written.
    """
    with open(target, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        os.chmod(target, mode)


def write_text_atomic(
    path: PathLike,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomically write ``text`` to ``path``.

    Parameters
    ----------
    path : str | os.PathLike
        Destination file.  Parent directory is created if it does not exist.
    text : str
        Full file contents.
    encoding : str, default "utf-8"
        Text encoding for the write.
    mode : int | None, default None
        If given, ``chmod`` the temp file to this mode before the rename so
        the final file has the requested permissions atomically.

    Notes
    -----
    The temp file is created in the same directory as ``path`` so the rename
    is atomic on the same filesystem.  ``os.replace`` is used (not
    ``os.rename``) so the call also works on Windows when the target exists.
    The file is ``fsync``'d before the rename to flush dirty pages - without
    this, a power loss after rename but before kernel writeback could leave
    the file empty.

    Bind-mount fallback: Docker mounts a single file at ``path`` by binding
    its inode into the container namespace. ``os.replace`` cannot replace
    the mount point (kernel returns EBUSY). When this happens we fall back
    to an in-place truncate+write so saves still land; atomicity is lost in
    that path but the alternative is unsaveable settings.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    data = text.encode(encoding)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        try:
            os.replace(tmp_path, p)
        except OSError as exc:
            if exc.errno not in _REPLACE_FALLBACK_ERRNOS:
                raise
            logger.warning(
                "[!] atomic replace failed for %s (%s); falling back to "
                "in-place write. This is expected for Docker bind-mounted "
                "files.", p, errno.errorcode.get(exc.errno, exc.errno),
            )
            _inplace_write(p, data, mode)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
    except BaseException:
        # Clean up the temp file on any failure (including KeyboardInterrupt
        # so users don't accumulate .tmp files after a Ctrl-C).
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_bytes_atomic(
    path: PathLike,
    data: bytes,
    *,
    mode: int | None = None,
) -> None:
    """Atomically write ``data`` to ``path`` in binary mode.

    See :func:`write_text_atomic` for semantics.  Use this when the payload
    is already bytes (e.g. encoded JSON, encrypted blobs, captured HTTP
    responses) and you want to avoid an unnecessary encode/decode round-trip.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        try:
            os.replace(tmp_path, p)
        except OSError as exc:
            if exc.errno not in _REPLACE_FALLBACK_ERRNOS:
                raise
            logger.warning(
                "[!] atomic replace failed for %s (%s); falling back to "
                "in-place write. This is expected for Docker bind-mounted "
                "files.", p, errno.errorcode.get(exc.errno, exc.errno),
            )
            _inplace_write(p, data, mode)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
