"""
SemanticHandler - implements the ``| nearest`` and ``| dedup_semantic``
SPQL pipes (slice 4 of Phase 1 / Bet 2).

These pipes compose with everything that came before:

* ``| nearest "<query>"`` - embeds the query, embeds each row's text
  columns, computes cosine similarity, sorts descending, optionally
  applies a threshold, optionally limits to top-K. Adds a
  ``_similarity`` column.
* ``| dedup_semantic threshold=F`` - collapses near-duplicate rows
  using greedy first-seen-wins (the row that appears first wins the
  cluster). Filters the DataFrame; does not add a similarity column.

Both pipes embed the input DataFrame on the fly via the slice 1
embedder primitive. There's no sidecar dependency; that fast path is
a future optimization. Working on any DataFrame (even one synthesised
via ``| makeresults | eval text=...``) is a deliberate design choice.

Cost model
----------
For ``N`` rows, both pipes encode ``N + 1`` strings (the rows + the
query for nearest, just the rows for dedup_semantic). On M-series CPU
with all-MiniLM-L6-v2:

* 100 rows  ≈ 0.5 – 1 second
* 1000 rows ≈ 5 – 10 seconds
* 10000+ rows: consider sidecar fast path (slice 5+)
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────

class SemanticPipeError(ValueError):
    """Raised on misuse of the semantic pipes (bad threshold, missing
    field, no text columns to embed, etc.).

    Subclasses :class:`ValueError` so SPQL's existing error-formatting
    paths surface a clean message rather than a stack trace.
    """


# ── Text-column discovery on pandas DataFrames ───────────────────────

# Column names that look text-y but should never be embedded.
_EXCLUDED_TEXT_COLUMNS = frozenset({
    "_epoch",
    "_similarity",   # output of nearest - don't recurse into prior runs
    "_row_id",       # sidecar bookkeeping
})


def _is_text_dtype(series: pd.Series) -> bool:
    """Return True if the pandas Series holds text-shaped values."""
    if series.dtype == "object":
        return True
    if pd.api.types.is_string_dtype(series):
        return True
    return False


def _df_text_columns(df: pd.DataFrame) -> list[str]:
    """Return the column names of ``df`` whose dtype is text-like."""
    cols: list[str] = []
    for c in df.columns:
        if c in _EXCLUDED_TEXT_COLUMNS:
            continue
        if _is_text_dtype(df[c]):
            cols.append(c)
    return cols


def _extract_texts(
    df: pd.DataFrame, columns: Optional[Sequence[str]] = None,
) -> list[str]:
    """Materialise one document per row by joining selected columns.

    ``None`` / NaN cells become empty strings; the embedding model
    handles empty input fine (returns a normalized vector).
    """
    if columns is None:
        columns = _df_text_columns(df)
    if not columns:
        return [""] * len(df)
    pieces: list[list[str]] = []
    for c in columns:
        if c not in df.columns:
            pieces.append([""] * len(df))
            continue
        s = df[c].fillna("").astype(str).tolist()
        pieces.append(s)
    n = len(df)
    return ["\n".join(p[i] for p in pieces) for i in range(n)]


def _resolve_columns(
    df: pd.DataFrame, field: Optional[str],
) -> list[str]:
    """Resolve which columns to embed based on the optional ``field`` kwarg.

    * ``field`` supplied → embed exactly that column (after existence check).
    * ``field`` omitted → use auto-detected text columns. Raise if none.
    """
    if field is not None:
        if field not in df.columns:
            raise SemanticPipeError(
                f"field={field!r} does not exist in the input "
                f"(columns: {list(df.columns)})"
            )
        return [field]
    cols = _df_text_columns(df)
    if not cols:
        raise SemanticPipeError(
            "No text columns to embed. Either the input has no string "
            "columns or all of them are reserved (_epoch, _similarity, "
            "_row_id). Use field=<column> to override, or pre-process "
            "with `| eval text=tostring(<col>)` to produce a text column."
        )
    return cols


def _validate_threshold(threshold: float) -> float:
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise SemanticPipeError(
            f"threshold must be a number, got {type(threshold).__name__}"
        )
    t = float(threshold)
    if not (-1.0 <= t <= 1.0):
        raise SemanticPipeError(
            f"threshold={t} is out of range [-1.0, 1.0] "
            "(cosine similarity bounds)"
        )
    return t


# ── Sidecar fast path (Phase 1 / Bet 2 slice 6) ─────────────────────

def _try_sidecar_lookup(df: pd.DataFrame, embedder) -> Optional[np.ndarray]:
    """Return a precomputed ``(N, dim)`` embedding matrix if the input
    DataFrame's rows align 1:1 with sidecar entries; ``None`` otherwise.

    The fast path applies when:

    * ``df`` has a ``_source_file`` column (set by the index= loader)
    * Every unique source has a sidecar that matches the current
      embedder's ``model_name`` and ``dim``
    * For each source, the row count in ``df`` equals the source
      parquet's row count (no upstream filter has dropped rows)

    Returns ``None`` (slow path fallback) on ANY uncertainty:
    missing column, missing sidecar, model/dim mismatch, row-count
    mismatch, source moved/deleted, sidecar staler than source. Wrong
    embeddings would silently produce wrong rankings - slower-but-correct
    is preferred to faster-but-wrong every time.

    Caller MUST ensure ``field is None`` before calling - sidecars are
    built from the default text-column concatenation, NOT from a single
    explicit field.
    """
    if "_source_file" not in df.columns:
        return None

    # Lazy imports - both modules are hard deps but import cost is real.
    from pathlib import Path
    import pyarrow.parquet as pq
    from functionality import embedding_sidecar as sc

    # Resolve the indexes root (where _source_file is relative to).
    try:
        from global_settings import get_settings
        indexes_root = get_settings().indexes_dir().resolve()
    except Exception:
        # Fall back to the project's default indexes/ path.
        indexes_root = (Path(__file__).resolve().parent.parent / "indexes").resolve()

    # Group df row indices by source. We rely on the index= loader's
    # convention that rows from a single source come back in source-file
    # order (DuckDB preserves order absent ORDER BY).
    source_to_indices: dict[str, list[int]] = {}
    for i, src in enumerate(df["_source_file"].tolist()):
        if src is None:
            return None  # Mid-pipe loss of provenance - abort
        source_to_indices.setdefault(str(src), []).append(i)

    out_embeddings = np.empty((len(df), int(embedder.dim)), dtype=np.float32)

    for src_str, indices in source_to_indices.items():
        src_path = (indexes_root / src_str).resolve()
        if not src_path.exists():
            return None  # Source file gone

        # Read sidecar - None means absent (no fast path) or unreadable
        try:
            frame = sc.read_sidecar(src_path)
        except sc.SidecarError:
            # Corrupt sidecar - fall back rather than fail the query
            return None
        if frame is None:
            return None  # No sidecar yet (sweeper hasn't reached it)

        # Model identity + dim must match the current embedder
        if frame.model_name != embedder.model_name:
            return None
        if frame.dim != int(embedder.dim):
            return None

        # Source-row-count alignment. If the df has fewer rows for this
        # source than the source parquet contains, an upstream filter
        # (earliest=, where, head, etc.) has dropped rows and the row-
        # position-to-sidecar-_row_id mapping is no longer reliable.
        try:
            source_rows = int(pq.read_metadata(src_path).num_rows)
        except Exception:
            return None
        if len(indices) != source_rows:
            return None

        # The sidecar must also cover the full source (sweeper might
        # have written a partial sidecar in some pathological case).
        if frame.n_rows != source_rows:
            return None

        # Final defensive check: source mtime > sidecar mtime means the
        # source was rewritten since the sidecar was made - the sweeper
        # will refresh on its next tick, but until then the sidecar is
        # untrustworthy.
        if sc.is_stale(
            src_path,
            expected_model_name=embedder.model_name,
            expected_dim=int(embedder.dim),
        ):
            return None

        # Map: indices[k] is the df row position that came from the
        # source's k-th row (0-indexed). Place sidecar embeddings into
        # the matching df slots.
        for sidecar_pos, df_idx in enumerate(indices):
            out_embeddings[df_idx] = frame.vectors[sidecar_pos]

    return out_embeddings


# ── Public pipe implementations ──────────────────────────────────────

def nearest(
    df: pd.DataFrame,
    query: str,
    *,
    topk: Optional[int] = 10,
    threshold: Optional[float] = None,
    field: Optional[str] = None,
) -> pd.DataFrame:
    """Rank rows of ``df`` by cosine similarity to ``query``.

    Parameters
    ----------
    df :
        Input DataFrame (the upstream pipe's output).
    query :
        The query string to embed.
    topk :
        If positive, keep the top ``topk`` rows. ``None`` or 0 means
        "keep all" (just sorted by similarity).
    threshold :
        If supplied, drop rows below this cosine similarity. Must be in
        ``[-1.0, 1.0]``.
    field :
        If supplied, embed only that column. Default: concatenate all
        auto-detected text columns.

    Returns
    -------
    DataFrame
        The input rows sorted descending by ``_similarity``, with the
        new ``_similarity`` column added. Threshold and ``topk`` are
        applied after sorting.

    Raises
    ------
    SemanticPipeError
        On bad inputs (missing field, no text columns, bad threshold).
    """
    if df is None or len(df) == 0:
        # Empty input is a valid state - return well-shaped empty
        # DataFrame with the new column tacked on.
        out = df.copy() if df is not None else pd.DataFrame()
        out["_similarity"] = pd.array([], dtype="float64")
        return out

    if not isinstance(query, str) or query == "":
        raise SemanticPipeError("nearest requires a non-empty query string.")

    if threshold is not None:
        threshold = _validate_threshold(threshold)

    from analyzers.embedder import get_embedder, cosine_similarity_matrix
    embedder = get_embedder()

    # Slice 6 fast path: when sidecars cover the input DataFrame 1:1
    # AND the user hasn't pinned a specific field (sidecars embed the
    # default text-column concatenation), reuse the precomputed vectors
    # and skip encode_batch entirely.
    row_embeddings = None
    if field is None:
        row_embeddings = _try_sidecar_lookup(df, embedder)
        if row_embeddings is not None:
            logger.info(
                "[i] nearest: sidecar fast path hit (%d rows, %d sources)",
                len(df), df["_source_file"].nunique(),
            )

    if row_embeddings is None:
        cols = _resolve_columns(df, field)
        texts = _extract_texts(df, cols)
        row_embeddings = embedder.encode_batch(texts)

    query_embedding = embedder.encode(query)
    sims = cosine_similarity_matrix(query_embedding, row_embeddings)

    out = df.copy()
    out["_similarity"] = np.asarray(sims, dtype=np.float64)

    out = out.sort_values("_similarity", ascending=False, kind="mergesort")
    if threshold is not None:
        out = out[out["_similarity"] >= threshold]
    if topk is not None and topk > 0:
        out = out.head(int(topk))
    out = out.reset_index(drop=True)
    return out


def dedup_semantic(
    df: pd.DataFrame,
    *,
    threshold: float = 0.85,
    field: Optional[str] = None,
) -> pd.DataFrame:
    """Greedy first-seen-wins near-duplicate filter.

    Walks rows in order; keeps a row only if its cosine similarity to
    every previously-kept row is below ``threshold``. The first row in
    the input is always kept (it has no prior to compare against).

    Parameters
    ----------
    df :
        Input DataFrame.
    threshold :
        Cosine-similarity cutoff. Pairs with similarity ``≥ threshold``
        are considered duplicates. Default ``0.85``.
    field :
        If supplied, dedup using only that column. Default: concatenate
        all auto-detected text columns.

    Returns
    -------
    DataFrame
        Filtered DataFrame with near-duplicates removed. Order of
        surviving rows matches the input's order.
    """
    if df is None or len(df) == 0:
        return df.copy() if df is not None else pd.DataFrame()

    threshold = _validate_threshold(threshold)

    from analyzers.embedder import get_embedder, cosine_similarity_matrix
    embedder = get_embedder()

    # Slice 6 fast path: same conservative sidecar lookup as nearest().
    # Skipped when caller pinned a specific field (sidecar embeds the
    # default text-column concatenation, not a single field).
    embeddings = None
    if field is None:
        embeddings = _try_sidecar_lookup(df, embedder)
        if embeddings is not None:
            logger.info(
                "[i] dedup_semantic: sidecar fast path hit (%d rows, %d sources)",
                len(df), df["_source_file"].nunique(),
            )

    if embeddings is None:
        cols = _resolve_columns(df, field)
        texts = _extract_texts(df, cols)
        embeddings = embedder.encode_batch(texts)

    n = len(df)
    keep_mask = np.zeros(n, dtype=bool)
    keep_mask[0] = True
    kept_indices = [0]

    for i in range(1, n):
        kept_arr = embeddings[kept_indices]
        sims = cosine_similarity_matrix(embeddings[i], kept_arr)
        if float(sims.max()) < threshold:
            keep_mask[i] = True
            kept_indices.append(i)

    out = df.iloc[np.where(keep_mask)[0]].reset_index(drop=True)
    logger.info(
        "[i] dedup_semantic threshold=%.3f kept %d / %d rows "
        "(%d duplicates dropped)",
        threshold, int(keep_mask.sum()), n, n - int(keep_mask.sum()),
    )
    return out


__all__ = [
    "SemanticPipeError",
    "dedup_semantic",
    "nearest",
]
