"""
Alert Group Result Serializer
─────────────────────────────
Loads the most recent cached result for a saved search, applies a row cap,
and serializes to JSON or CSV for embedding in the Claude API prompt.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path
from typing import Literal

import pandas as pd

from alert_groups.models import SerializedResult

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DB = _PROJECT_ROOT / "saved_search_history.db"


class SearchNotFoundError(Exception):
    """No cached result exists for the given search name."""


class EmptyResultError(Exception):
    """The cached result set is empty (zero rows)."""


class ResultSerializer:
    """Serialize the last cached result for a saved search."""

    def __init__(self, max_rows: int = 200, fmt: Literal["json", "csv"] = "json"):
        self.max_rows = max_rows
        self.fmt = fmt

    def serialize(self, search_name: str) -> SerializedResult:
        """Load + serialize the most recent result for *search_name*.

        Reads from ``saved_search_history.db``. Raises ``SearchNotFoundError``
        when no cache exists and ``EmptyResultError`` when it does but the
        rows are gone. For on-demand execution bypassing the cache (typical
        for manual AG dispatches) use :meth:`serialize_df`.
        """
        df = self._load_last_result(search_name)
        return self.serialize_df(search_name, df)

    def serialize_df(
        self,
        search_name: str,
        df: pd.DataFrame | None,
    ) -> SerializedResult:
        """Serialize a precomputed DataFrame (no history DB lookup).

        Used by the AG dispatcher's on-demand execution path so a fresh
        `process_query(...)` result can be sent to Claude without waiting
        for the saved search's own cron to fire. Empty / None raises
        :class:`EmptyResultError` with a descriptive message so the
        dispatcher can trip the circuit breaker / failure email path.
        """
        if df is None or df.empty:
            raise EmptyResultError(
                f'Result for search "{search_name}" is empty.'
            )

        if len(df) > self.max_rows:
            logger.info(
                "[i] Truncating '%s' results from %d to %d rows.",
                search_name, len(df), self.max_rows,
            )
            df = df.head(self.max_rows)

        content = self._serialize_df(df)
        estimated = self.estimate_tokens(content)

        return SerializedResult(
            search_name=search_name,
            row_count=len(df),
            estimated_tokens=estimated,
            format=self.fmt,
            content=content,
        )

    def _load_last_result(self, search_name: str) -> pd.DataFrame:
        """Load the most recent Parquet result for a search from the history DB."""
        if not HISTORY_DB.exists():
            raise SearchNotFoundError(
                f'No execution history found (database does not exist).'
            )

        try:
            with sqlite3.connect(str(HISTORY_DB)) as conn:
                row = conn.execute(
                    "SELECT saved_search_path FROM execution_history "
                    "WHERE query_name = ? ORDER BY execution_start_time DESC LIMIT 1",
                    (search_name,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            raise SearchNotFoundError(f"History DB query failed: {exc}")

        if row is None:
            raise SearchNotFoundError(
                f'No cached result found for search "{search_name}".'
            )

        parquet_path = Path(row[0])
        if not parquet_path.exists():
            raise SearchNotFoundError(
                f'Result file for search "{search_name}" no longer exists: {parquet_path}'
            )

        return pd.read_parquet(parquet_path)

    def _serialize_df(self, df: pd.DataFrame) -> str:
        """Convert DataFrame to JSON or CSV string."""
        if self.fmt == "csv":
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            return buf.getvalue()
        # Default: JSON
        return df.to_json(orient="records", date_format="iso", default_handler=str)

    @staticmethod
    def estimate_tokens(content: str) -> int:
        """Conservative heuristic: ~3.5 chars per token.

        Use on a SINGLE serialized block (one feeder's CSV or JSON). For
        the full built prompt (multiple blocks + wrapper markdown), use
        :meth:`estimate_prompt_tokens` instead - it floors at a larger
        value and is the number the budget gate should use.
        """
        return max(1, int(len(content) / 3.5))

    @staticmethod
    def estimate_prompt_tokens(built_content: str) -> int:
        """Estimate tokens for the fully-rendered user_content prompt.

        H-AN-5 (2026-04-21): the per-block :meth:`estimate_tokens` misses
        the builder's wrapper markdown - section headers like
        ``## Search: <name> (<n> rows, CSV)``, the code-fence lines
        around each block, the ``**Alert Group:** … **Timestamp:** …``
        metadata, and the ``---`` separators. For a 10-feeder AG that
        overhead is easily several hundred tokens; gating the budget on
        the sum of per-block estimates therefore under-counted and let
        prompts through that exceeded the per-AG cap.

        Same ~3.5 chars-per-token heuristic. Separate method so future
        tuning can diverge - e.g. different slope for prompts that include
        long system-prompt preambles.
        """
        return max(1, int(len(built_content) / 3.5))
