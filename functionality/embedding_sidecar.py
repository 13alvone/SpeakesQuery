"""
Embedding Sidecar Storage
─────────────────────────
Per-source-parquet sidecar files: alongside ``path/to/data.parquet`` we
write ``path/to/data.embeddings.parquet`` containing
``(_row_id, _epoch, embedding)`` for every row of the source. Reading and
writing live here; the embedding *content* is produced by
:mod:`analyzers.embedder`, and the population schedule will be driven by
the slice 3 sweeper task.

Schema (dim is parameterized - different embedding models pick different
dimensions, so the on-disk shape adapts):

* ``_row_id``    INT64                       row position in the source parquet
* ``_epoch``     INT64                       mirrors the source's ``_epoch``
* ``embedding``  FIXED_SIZE_LIST<float, dim> normalized vector

Parquet key-value metadata (used by the reader to detect model swaps and
by the sweeper to decide whether to re-embed):

* ``model_name``      embedder identifier (e.g. ``sentence-transformers/all-MiniLM-L6-v2``)
* ``dim``             embedding dimension as a decimal string
* ``created_epoch``   unix seconds when the sidecar was written

The atomic-write pattern mirrors :class:`scheduled_input_engine.parquet_writer.ParquetWriter`:
stage to a hidden ``.<name>.tmp`` sibling, then ``os.replace`` (atomic on
POSIX and Windows). On any failure the temp file is removed and the
target is untouched.

Why FixedSizeList over variable-length list
-------------------------------------------
DuckDB's ``vss`` extension expects fixed-shape arrays (``FLOAT[N]``) for
HNSW indexing. Variable-size lists round-trip fine but force a copy
into a contiguous buffer at query time. For the planned ``| nearest``
pipe we want the on-disk shape to match the engine's preferred layout.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


SIDECAR_SUFFIX = ".embeddings.parquet"
"""Filename extension that turns a source path into its sidecar path.

A source parquet ``foo.parquet`` becomes ``foo.embeddings.parquet`` when
:func:`sidecar_path_for` runs. The double-dotted form (``.embeddings.parquet``
rather than just ``.embeddings``) keeps the suffix recognisable as a
parquet so generic tooling that filters on ``*.parquet`` still finds it.
"""


# ── Path helpers ─────────────────────────────────────────────────────

def sidecar_path_for(source_parquet: str | os.PathLike) -> Path:
    """Return the sidecar path for ``source_parquet``.

    Idempotent: passing a path that already ends with
    :data:`SIDECAR_SUFFIX` returns it unchanged so callers can be sloppy
    about whether they hold a source or sidecar reference.
    """
    src = Path(source_parquet)
    if src.name.endswith(SIDECAR_SUFFIX):
        return src
    return src.with_suffix(SIDECAR_SUFFIX)


def is_sidecar_path(path: str | os.PathLike) -> bool:
    """Return True if ``path`` is itself a sidecar parquet."""
    return Path(path).name.endswith(SIDECAR_SUFFIX)


# ── Errors ───────────────────────────────────────────────────────────

class SidecarError(RuntimeError):
    """Base class for sidecar I/O failures."""


class SidecarSchemaError(SidecarError):
    """Raised when an on-disk sidecar's schema disagrees with expectations.

    Most common cause: model swap left a stale sidecar with a different
    ``dim`` from the current model. The slice 3 sweeper detects this via
    :func:`is_stale` + :attr:`SidecarFrame.dim` and triggers a re-embed.
    """


# ── Frame dataclass ──────────────────────────────────────────────────

@dataclass(frozen=True)
class SidecarFrame:
    """Materialised view of a sidecar parquet.

    Numpy arrays are returned as views over the parquet column buffers
    where possible; callers that mutate them must ``.copy()`` first.
    """

    vectors: np.ndarray
    """Embeddings as a contiguous ``(N, dim)`` float32 ndarray."""

    row_ids: np.ndarray
    """``(N,)`` int64 array of source-parquet row positions."""

    epochs: np.ndarray
    """``(N,)`` int64 array mirroring the source's ``_epoch`` column."""

    model_name: str
    """Embedder identifier recorded at write time."""

    dim: int
    """Embedding dimension."""

    created_epoch: int
    """Unix seconds when the sidecar was written."""

    @property
    def n_rows(self) -> int:
        return int(self.vectors.shape[0])


# ── Internal helpers ─────────────────────────────────────────────────

def _build_schema(dim: int) -> pa.Schema:
    """Return the pyarrow schema for a sidecar with the given dimension."""
    if dim <= 0:
        raise ValueError(f"dim must be positive, got {dim}")
    return pa.schema([
        ("_row_id", pa.int64()),
        ("_epoch", pa.int64()),
        ("embedding", pa.list_(pa.float32(), dim)),
    ])


def _validate_inputs(
    *,
    row_ids,
    epochs,
    embeddings: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Coerce write-time inputs into matched-length numpy arrays.

    Raises
    ------
    ValueError
        If the three inputs disagree on length, or ``embeddings`` is
        not 2-D.
    """
    rid_arr = np.asarray(row_ids, dtype=np.int64).reshape(-1)
    eps_arr = np.asarray(epochs, dtype=np.int64).reshape(-1)
    emb_arr = np.asarray(embeddings, dtype=np.float32)
    n = len(rid_arr)
    if len(eps_arr) != n:
        raise ValueError(
            f"row_ids length {n} != epochs length {len(eps_arr)}"
        )
    if n == 0:
        # Empty case: ``embeddings`` may be any shape (caller often
        # passes ``np.zeros((0,))`` or ``np.zeros((0, dim))``). Don't
        # try to reshape - the writer resolves the dim from ``model_dim``
        # and skips the row-count cross-check.
        return rid_arr, eps_arr, emb_arr
    if emb_arr.ndim != 2:
        raise ValueError(
            f"embeddings must be 2-D (N, dim) for non-empty input, "
            f"got shape {emb_arr.shape}"
        )
    if emb_arr.shape[0] != n:
        raise ValueError(
            f"row_ids length {n} != embeddings rows {emb_arr.shape[0]}"
        )
    return rid_arr, eps_arr, np.ascontiguousarray(emb_arr)


def _embedding_array(emb: np.ndarray, dim: int) -> pa.Array:
    """Build the FixedSizeList<float32, dim> array from an ``(N, dim)`` ndarray."""
    n = emb.shape[0]
    if n == 0:
        # Empty FixedSizeList still needs the right type tag.
        return pa.FixedSizeListArray.from_arrays(
            pa.array([], type=pa.float32()), dim,
        )
    if emb.shape[1] != dim:
        raise ValueError(
            f"embeddings column count {emb.shape[1]} != declared dim {dim}"
        )
    flat = pa.array(emb.reshape(-1), type=pa.float32())
    return pa.FixedSizeListArray.from_arrays(flat, dim)


# ── Write ────────────────────────────────────────────────────────────

def write_sidecar(
    source_parquet: str | os.PathLike,
    *,
    row_ids,
    epochs,
    embeddings: np.ndarray,
    model_name: str,
    model_dim: Optional[int] = None,
    created_epoch: Optional[int] = None,
) -> Path:
    """Atomically write the sidecar parquet for ``source_parquet``.

    Parameters
    ----------
    source_parquet :
        Path to the source parquet. The sidecar is written next to it
        as ``<source>.embeddings.parquet`` (idempotent if a sidecar
        path is passed in).
    row_ids :
        Sequence of source row positions (int64).
    epochs :
        Sequence of source ``_epoch`` values (int64), aligned with ``row_ids``.
    embeddings :
        ``(N, dim)`` float32 ndarray of L2-normalized vectors. Empty
        input (``N == 0``) is allowed; ``model_dim`` must be supplied
        in that case so the schema records the right shape.
    model_name :
        Identifier of the embedder that produced these vectors. Stored
        in the parquet's key-value metadata so the sweeper can detect
        model swaps.
    model_dim :
        Override for the embedding dimension. Required when
        ``embeddings`` is empty; otherwise inferred from ``embeddings.shape[1]``.
    created_epoch :
        Override for the timestamp written to metadata. Defaults to
        ``int(time.time())``; tests pass explicit values for determinism.

    Returns
    -------
    Path
        Resolved path of the written sidecar.

    Raises
    ------
    ValueError
        If shape constraints are violated or ``dim`` cannot be determined
        for an empty input.
    """
    rid_arr, eps_arr, emb_arr = _validate_inputs(
        row_ids=row_ids, epochs=epochs, embeddings=embeddings,
    )
    # Resolve dim. Prefer the embeddings ndarray shape when it carries
    # a real second axis (covers both populated and pre-shaped-empty
    # inputs like ``np.zeros((0, 384))``). Fall back to ``model_dim``
    # only when the input is shapeless - typical for callers that pass
    # ``np.zeros((0,))`` or ``[]``.
    if emb_arr.ndim == 2 and emb_arr.shape[1] > 0:
        dim = int(emb_arr.shape[1])
        if model_dim is not None and int(model_dim) != dim:
            raise ValueError(
                f"model_dim {model_dim} disagrees with embeddings shape "
                f"dim {dim}"
            )
    else:
        if model_dim is None:
            raise ValueError(
                "model_dim is required when writing a sidecar from "
                "shapeless empty input (no embeddings dim to infer)"
            )
        dim = int(model_dim)
        if emb_arr.size > 0:
            raise ValueError(
                "embeddings have data but cannot be interpreted as "
                f"(N, dim); got shape {emb_arr.shape}"
            )

    schema = _build_schema(dim)
    md = {
        "model_name": str(model_name),
        "dim": str(dim),
        "created_epoch": str(
            int(created_epoch) if created_epoch is not None else int(time.time())
        ),
    }
    schema_with_md = schema.with_metadata({k.encode(): v.encode() for k, v in md.items()})

    table = pa.table(
        {
            "_row_id": pa.array(rid_arr, type=pa.int64()),
            "_epoch": pa.array(eps_arr, type=pa.int64()),
            "embedding": _embedding_array(emb_arr, dim),
        },
        schema=schema_with_md,
    )

    target = sidecar_path_for(source_parquet).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp")

    try:
        pq.write_table(table, tmp_path, compression="gzip")
        os.replace(tmp_path, target)
    except BaseException:
        # Any failure (including KeyboardInterrupt) leaves the original
        # target untouched; clean up the partial temp file.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    logger.info(
        "[i] Wrote sidecar %s (%d rows × %d dim, model=%s)",
        target, len(rid_arr), dim, model_name,
    )
    return target


# ── Read ─────────────────────────────────────────────────────────────

def read_sidecar(source_parquet: str | os.PathLike) -> Optional[SidecarFrame]:
    """Load the sidecar for ``source_parquet`` or return ``None`` if absent.

    The path argument may be either the source parquet or its sidecar -
    :func:`sidecar_path_for` is idempotent, so callers don't need to
    track which form they have.
    """
    sidecar = sidecar_path_for(source_parquet)
    if not sidecar.exists():
        return None

    try:
        table = pq.read_table(sidecar)
    except Exception as exc:
        raise SidecarError(
            f"Failed to read sidecar {sidecar}: {exc}"
        ) from exc

    md_raw = table.schema.metadata or {}
    md = {k.decode(): v.decode() for k, v in md_raw.items()}
    model_name = md.get("model_name", "")
    declared_dim = int(md.get("dim", "0"))
    created = int(md.get("created_epoch", "0"))

    # Cross-check the schema's embedding type against declared dim.
    embed_field = table.schema.field("embedding")
    schema_dim = (
        embed_field.type.list_size
        if pa.types.is_fixed_size_list(embed_field.type) else 0
    )
    if declared_dim and schema_dim and declared_dim != schema_dim:
        raise SidecarSchemaError(
            f"Sidecar {sidecar} metadata dim={declared_dim} disagrees "
            f"with schema dim={schema_dim}"
        )
    dim = declared_dim or schema_dim
    if dim <= 0:
        raise SidecarSchemaError(
            f"Sidecar {sidecar} declares no embedding dimension"
        )

    n = len(table)
    row_ids = (
        table.column("_row_id").to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    )
    epochs = (
        table.column("_epoch").to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    )

    if n == 0:
        vectors = np.zeros((0, dim), dtype=np.float32)
    else:
        embed_col = table.column("embedding")
        # FixedSizeList → flatten then reshape; this materialises the
        # contiguous (N, dim) buffer the | nearest pipe wants.
        chunks = embed_col.chunks if hasattr(embed_col, "chunks") else [embed_col]
        flat_pieces = []
        for ch in chunks:
            try:
                flat_pieces.append(ch.values.to_numpy(zero_copy_only=False))
            except AttributeError:
                # ChunkedArray fallback path - flatten via pyarrow then numpy
                flat_pieces.append(ch.flatten().to_numpy(zero_copy_only=False))
        flat = np.concatenate(flat_pieces) if flat_pieces else np.zeros(0, dtype=np.float32)
        vectors = flat.astype(np.float32, copy=False).reshape(n, dim)

    return SidecarFrame(
        vectors=np.ascontiguousarray(vectors),
        row_ids=row_ids,
        epochs=epochs,
        model_name=model_name,
        dim=dim,
        created_epoch=created,
    )


# ── Drift detection ──────────────────────────────────────────────────

def is_stale(
    source_parquet: str | os.PathLike,
    *,
    expected_model_name: Optional[str] = None,
    expected_dim: Optional[int] = None,
) -> bool:
    """Return ``True`` if the sidecar should be re-embedded.

    Conditions that mark a sidecar as stale:

    * The sidecar does not exist.
    * The source parquet's mtime is newer than the sidecar's
      (rows have been added / rewritten since the last embed pass).
    * ``expected_model_name`` is supplied and does not match the
      metadata in the sidecar (operator swapped models).
    * ``expected_dim`` is supplied and does not match the sidecar's
      schema dim (model swap with different output dimension).

    A non-existent source returns ``False`` - there's nothing to
    re-embed against, so the sidecar is conservatively kept; the
    sweeper will skip it. This avoids churning sidecars during
    transient I/O glitches.
    """
    src = Path(source_parquet)
    sidecar = sidecar_path_for(src)
    if not sidecar.exists():
        return True
    if not src.exists():
        return False
    if src.stat().st_mtime > sidecar.stat().st_mtime:
        return True

    # Lazy metadata-only read for model/dim checks - full table read is
    # wasteful when we only need to compare two scalars.
    if expected_model_name is None and expected_dim is None:
        return False
    try:
        meta = pq.read_metadata(sidecar)
        kvm = meta.metadata or {}
        kv = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in kvm.items()
        }
        sc_model = kv.get("model_name", "")
        sc_dim = int(kv.get("dim", "0"))
    except Exception as exc:
        logger.warning(
            "[!] Could not read sidecar metadata for staleness check on "
            "%s: %s - treating as stale", sidecar, exc,
        )
        return True

    if expected_model_name is not None and sc_model != expected_model_name:
        return True
    if expected_dim is not None and sc_dim != int(expected_dim):
        return True
    return False


__all__ = [
    "SIDECAR_SUFFIX",
    "SidecarError",
    "SidecarFrame",
    "SidecarSchemaError",
    "is_sidecar_path",
    "is_stale",
    "read_sidecar",
    "sidecar_path_for",
    "write_sidecar",
]
