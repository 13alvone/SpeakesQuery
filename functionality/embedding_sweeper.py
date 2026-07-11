"""
Embedding Sweeper
─────────────────
Slice 3 of Phase 1 / Bet 2.

Walks ``indexes/`` for source parquets, finds those whose sidecar is
missing or stale, and embeds the missing rows in batch. Decouples the
embedding cost from ingestion latency: an ingestion run writes its
parquet immediately and returns; the sweeper picks up new rows on its
own cadence.

The sweeper is **standalone-callable** in this slice - slice 5 will
wire it into the scheduled-input engine alongside a feature flag, the
``max_embeddings_size_gb`` budget, and the corresponding UI input. For
now, callers (tests + the future ``tools.embed_backfill`` CLI) drive
``EmbeddingSweeper.sweep_once()`` manually.

Design notes
------------
* Source parquets are immutable in the SpeakesQuery convention (each
  ingestion writes a new ``<epoch>_<uuid>.system4.system4.parquet``);
  the sweeper therefore embeds each source exactly once and the sidecar
  becomes a permanent companion. ``functionality.embedding_sidecar.is_stale``
  still catches edge cases (manual rewrite, model swap, dim mismatch).
* Text extraction picks all ``string`` / ``utf8`` / ``large_string``
  columns and joins them with a newline. Numeric / timestamp / boolean
  columns are skipped - they don't carry searchable meaning. Slice 5
  may add a per-source ``embedding_text_columns`` override; until then,
  the default extractor handles the common case (news headline + body,
  Polymarket question, Kalshi market title, etc.).
* The embedder is loaded lazily on the first source that actually needs
  embedding, mirroring the rest of the codebase's "boot cleanly without
  the model present" stance.
* Failures in one source do not stop the sweep - the bad source lands
  in ``SweepReport.failures`` and the sweep continues. Slice 5 will
  decide how to alert on persistent failure (email-on-N-consecutive,
  similar to the alert-group circuit breaker).

The IMMUTABLE namespace (``indexes/IMMUTABLE/...``) is excluded by
default. Pick journals and other audit streams are not searchable
corpora; embedding them would waste budget and clutter ``| nearest``
results.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from functionality import embedding_sidecar as sidecar

logger = logging.getLogger(__name__)


# ── Public dataclasses ───────────────────────────────────────────────

@dataclass
class SourceResult:
    """Per-source outcome of one sweep pass."""

    source: Path
    status: str  # "embedded" | "skipped_fresh" | "skipped_empty" | "skipped_no_text" | "failed"
    rows: int = 0
    elapsed_ms: int = 0
    error_class: str = ""
    error_message: str = ""


@dataclass
class SweepReport:
    """Telemetry for a single ``sweep_once()`` invocation."""

    started_epoch: float
    finished_epoch: float = 0.0
    sources_seen: int = 0
    sources_embedded: int = 0
    sources_skipped_fresh: int = 0
    sources_skipped_empty: int = 0
    sources_skipped_no_text: int = 0
    sources_failed: int = 0
    rows_embedded: int = 0
    elapsed_ms: int = 0
    per_source: list[SourceResult] = field(default_factory=list)

    @property
    def failures(self) -> list[SourceResult]:
        return [r for r in self.per_source if r.status == "failed"]


# ── Discovery ────────────────────────────────────────────────────────

_EXCLUDED_TOP_DIRS = {"IMMUTABLE", "logs"}
"""Top-level subdirectories of ``indexes/`` to skip during discovery.

* ``IMMUTABLE`` - pick journals and audit streams; not searchable corpora.
* ``logs`` - the structured log stream lives here. Embedding it adds
  cost without value (logs are queried by SPQL but not by semantic
  similarity) and would inflate the budget.

Slice 5 may expose this as a setting; for now it's a constant.
"""


def discover_sources(
    indexes_root: str | Path,
    *,
    excluded_top_dirs: Iterable[str] = _EXCLUDED_TOP_DIRS,
) -> list[Path]:
    """Yield candidate source parquets under ``indexes_root``.

    Excludes:
    * Sidecar files themselves (``*.embeddings.parquet``)
    * Anything inside the ``IMMUTABLE/`` and ``logs/`` top-level subtrees
    * Hidden files (leading dot) - these are atomic-write temp siblings
    """
    root = Path(indexes_root).resolve()
    if not root.exists():
        return []
    skip = {s.strip("/") for s in excluded_top_dirs}
    out: list[Path] = []
    for path in root.rglob("*.parquet"):
        if not path.is_file():
            continue
        if sidecar.is_sidecar_path(path):
            continue
        if path.name.startswith("."):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in skip:
            continue
        out.append(path)
    out.sort()
    return out


# ── Text extraction ──────────────────────────────────────────────────

# Pyarrow type predicates that identify "interesting text" columns.
def _is_text_type(dtype) -> bool:
    return (
        pa.types.is_string(dtype)
        or pa.types.is_large_string(dtype)
        or pa.types.is_unicode(dtype)
    )


# Column names that look like text but should be excluded.
_EXCLUDED_TEXT_COLUMNS = frozenset({
    "_epoch",         # numeric, but defensive - won't be string typed
})


def discover_text_columns(table: pa.Table) -> list[str]:
    """Return the column names whose dtype is text-like and which we
    consider safe to embed.

    ``_epoch`` and any other excluded-by-name column are filtered out
    even if they happen to be string-typed.
    """
    cols: list[str] = []
    # Loop variable is named ``f`` to avoid shadowing
    # ``dataclasses.field`` imported at module top.
    for f in table.schema:
        if f.name in _EXCLUDED_TEXT_COLUMNS:
            continue
        if _is_text_type(f.type):
            cols.append(f.name)
    return cols


def extract_texts(
    table: pa.Table,
    columns: Optional[Sequence[str]] = None,
) -> list[str]:
    """Materialise one document per row by joining text-typed columns.

    ``None`` cells become empty strings; the embedding model handles
    empty input fine (returns a normalized vector). The join character
    is a literal newline so the model sees structured boundaries
    between fields.
    """
    if columns is None:
        columns = discover_text_columns(table)
    if not columns:
        return [""] * len(table)
    pieces_per_col: list[list[str]] = []
    for name in columns:
        if name not in table.column_names:
            pieces_per_col.append([""] * len(table))
            continue
        col = table.column(name).to_pylist()
        pieces_per_col.append(["" if v is None else str(v) for v in col])
    out: list[str] = []
    for i in range(len(table)):
        out.append("\n".join(p[i] for p in pieces_per_col))
    return out


# ── Sweeper ──────────────────────────────────────────────────────────

class EmbeddingSweeper:
    """Drives one or many sweep passes over an indexes tree.

    Holds the embedder reference and the configuration knobs. Created
    fresh per sweep in the standalone path so settings changes between
    runs propagate.
    """

    def __init__(
        self,
        indexes_root: str | Path,
        *,
        embedder=None,
        text_columns: Optional[Sequence[str]] = None,
        excluded_top_dirs: Iterable[str] = _EXCLUDED_TOP_DIRS,
    ) -> None:
        self.indexes_root = Path(indexes_root).resolve()
        self._embedder = embedder
        self._text_columns_override = (
            list(text_columns) if text_columns else None
        )
        self._excluded_top_dirs = set(excluded_top_dirs)

    # -- lazy embedder accessor --------------------------------------

    def _get_embedder(self):
        if self._embedder is None:
            from analyzers.embedder import get_embedder
            self._embedder = get_embedder()
        return self._embedder

    # -- per-source ---------------------------------------------------

    def embed_source(self, source: Path) -> SourceResult:
        """Embed one source parquet end-to-end.

        Idempotent: a fresh sidecar that already matches the current
        model's name + dim is left alone. A stale or missing sidecar
        triggers a full (re)embed of every row in the source.
        """
        started = time.monotonic()
        try:
            embedder = self._get_embedder()
            if not sidecar.is_stale(
                source,
                expected_model_name=embedder.model_name,
                expected_dim=embedder.dim,
            ):
                return SourceResult(
                    source=source, status="skipped_fresh", rows=0,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            try:
                table = pq.read_table(source)
            except Exception as exc:
                return SourceResult(
                    source=source, status="failed", rows=0,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_class=type(exc).__name__,
                    error_message=str(exc)[:500],
                )

            n = len(table)
            if n == 0:
                # Write an empty sidecar so subsequent passes skip cheaply.
                sidecar.write_sidecar(
                    source,
                    row_ids=[], epochs=[],
                    embeddings=np.zeros((0,), dtype=np.float32),
                    model_name=embedder.model_name,
                    model_dim=embedder.dim,
                )
                return SourceResult(
                    source=source, status="skipped_empty", rows=0,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            cols = self._text_columns_override or discover_text_columns(table)
            if not cols:
                # No text columns at all - this source is purely numeric.
                # Nothing to embed; record as skipped_no_text and don't
                # write an empty sidecar (a column-set change later may
                # add text columns).
                return SourceResult(
                    source=source, status="skipped_no_text", rows=0,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            texts = extract_texts(table, columns=cols)
            embeddings = embedder.encode_batch(texts)

            # _epoch column is conventional; default to zero if missing
            # rather than fail - slice 4's | nearest can still rank by
            # similarity even without per-row epochs.
            if "_epoch" in table.column_names:
                epochs = table.column("_epoch").to_pylist()
                epochs = [int(e) if e is not None else 0 for e in epochs]
            else:
                epochs = [0] * n

            row_ids = list(range(n))
            sidecar.write_sidecar(
                source,
                row_ids=row_ids, epochs=epochs,
                embeddings=embeddings,
                model_name=embedder.model_name,
            )
            return SourceResult(
                source=source, status="embedded", rows=n,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            return SourceResult(
                source=source, status="failed", rows=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_class=type(exc).__name__,
                error_message=str(exc)[:500],
            )

    # -- full pass ---------------------------------------------------

    def sweep_once(self) -> SweepReport:
        """Run one full pass over discovered sources.

        Catches per-source exceptions so one bad parquet doesn't stop
        the rest. Emits ``log_system_event`` rows at start and end so
        the operator can timeline sweeps from SPQL.
        """
        started = time.time()
        report = SweepReport(started_epoch=started)

        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="embedding_sweeper",
                event="sweep_start",
                message=f"indexes_root={self.indexes_root}",
            )
        except Exception:
            pass

        sources = discover_sources(
            self.indexes_root,
            excluded_top_dirs=self._excluded_top_dirs,
        )
        report.sources_seen = len(sources)
        clock = time.monotonic()

        for src in sources:
            res = self.embed_source(src)
            report.per_source.append(res)
            if res.status == "embedded":
                report.sources_embedded += 1
                report.rows_embedded += res.rows
            elif res.status == "skipped_fresh":
                report.sources_skipped_fresh += 1
            elif res.status == "skipped_empty":
                report.sources_skipped_empty += 1
            elif res.status == "skipped_no_text":
                report.sources_skipped_no_text += 1
            elif res.status == "failed":
                report.sources_failed += 1
                logger.warning(
                    "[!] embedding_sweeper failed on %s: %s - %s",
                    src, res.error_class, res.error_message,
                )

        report.elapsed_ms = int((time.monotonic() - clock) * 1000)
        report.finished_epoch = time.time()

        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="embedding_sweeper",
                event="sweep_complete",
                message=(
                    f"seen={report.sources_seen} "
                    f"embedded={report.sources_embedded} "
                    f"rows={report.rows_embedded} "
                    f"fresh={report.sources_skipped_fresh} "
                    f"empty={report.sources_skipped_empty} "
                    f"no_text={report.sources_skipped_no_text} "
                    f"failed={report.sources_failed} "
                    f"elapsed_ms={report.elapsed_ms}"
                ),
            )
        except Exception:
            pass

        logger.info(
            "[i] embedding_sweeper: seen=%d embedded=%d (%d rows) "
            "fresh=%d empty=%d no_text=%d failed=%d in %dms",
            report.sources_seen, report.sources_embedded,
            report.rows_embedded, report.sources_skipped_fresh,
            report.sources_skipped_empty, report.sources_skipped_no_text,
            report.sources_failed, report.elapsed_ms,
        )
        return report


__all__ = [
    "EmbeddingSweeper",
    "SourceResult",
    "SweepReport",
    "discover_sources",
    "discover_text_columns",
    "extract_texts",
]
