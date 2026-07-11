#!/usr/bin/env python3
import logging
import antlr4
import uuid
import time
import sys
import os
import re
import pandas as pd
from pathlib import Path

# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from lexers.antlr4_active.speakesQueryLexer import speakesQueryLexer
from lexers.antlr4_active.speakesQueryParser import speakesQueryParser
from lexers.speakesQueryListener import speakesQueryListener

from handlers.MacroHandler import MacroHandler
from macro_store import MacroStore
from job_store import JobStore

CURRENT_SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_SCRIPT_DIR.parent


def _fillna_dtype_aware(df: pd.DataFrame) -> pd.DataFrame:
    """Fill NaN with a dtype-appropriate default.

    M-CE-8 (2026-04-22): the legacy ``df.fillna('')`` converted numeric
    columns containing NaN into object-dtype columns of mixed numbers
    and empty strings. Downstream ``stats sum`` / ``avg`` either silently
    coerced empty strings to zero or raised a TypeError depending on the
    pandas version. Fill numeric columns with ``0`` (or ``pd.NaT`` for
    datetimes) and string-like columns with ``""``. Bool stays bool,
    with NaN → False (matches the old empty-string coercion).
    """
    out = df.copy()
    for col in out.columns:
        series = out[col]
        try:
            if pd.api.types.is_bool_dtype(series):
                out[col] = series.fillna(False)
            elif pd.api.types.is_numeric_dtype(series):
                # Integer columns with NaN are already object/float; fill
                # with 0 keeps them numeric.
                out[col] = series.fillna(0)
            elif pd.api.types.is_datetime64_any_dtype(series):
                out[col] = series.fillna(pd.NaT)
            else:
                out[col] = series.fillna("")
        except Exception:
            # Any unexpected dtype (categorical, period, etc.) falls back
            # to the legacy empty-string fill.
            out[col] = series.fillna("")
    return out


# Module-level macro expansion singletons
_macro_store = MacroStore()
_macro_store.initialize()
_macro_handler = MacroHandler(_macro_store)

# Module-level job store singleton
_job_store = JobStore()
_job_store.initialize()

uuid_regex = re.compile(r'^[0-9]{10}\.[0-9a-fA-F]{6,7}_[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
target_db = 'saved_searches.db'
conn = ''

logger = logging.getLogger(__name__)


# Matches a full line whose first non-whitespace character is '#'. The grammar
# has a COMMENT lexer rule that also skips '#'-lines, but stripping here makes
# the behaviour explicit and independent of ANTLR edge cases around the last
# line of a query (no trailing newline).
_LINE_COMMENT_RE = re.compile(r"^[ \t]*#[^\r\n]*(?:\r?\n|$)", re.MULTILINE)


def _strip_line_comments(query: str) -> str:
    """Remove full-line '#' comments from a SPQL query.

    Hash characters inside double-quoted strings must be preserved because
    index paths, regex patterns, and search values legitimately contain
    them. Only lines whose first non-whitespace char is '#' are removed.
    """
    out_lines = []
    in_dq = False
    for line in query.splitlines(keepends=True):
        if not in_dq and _LINE_COMMENT_RE.match(line):
            continue
        # Track whether we end the line still inside a double-quoted string.
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '\\' and i + 1 < len(line):
                i += 2
                continue
            if ch == '"':
                in_dq = not in_dq
            i += 1
        out_lines.append(line)
    return "".join(out_lines)


def run_query_and_return_results_df(query):
    """Execute a query and return ``(DataFrame, job_id)`` or ``(None, None)``."""
    logging.info(f"[i] Query received: {repr(query)}")
    try:
        query = query.replace("\r\n", "\n")  # Clean newlines from non-unix formats
        query = _strip_line_comments(query)
        query = query.strip()
        if not query:
            logging.info("[i] Query empty after stripping comments/whitespace.")
            return None, None
        result_df = execute_query(f'{query}\n')
        # Log the DataFrame shape only - stringifying a large DataFrame
        # for the log emitter can consume hundreds of MB of heap and
        # pollute docker logs. Full content goes into the JobStore
        # ring buffer instead (SPQL-inspectable via ``loadjob``).
        if isinstance(result_df, pd.DataFrame):
            logging.info(
                "[i] Query result shape: %d row(s) × %d col(s) before processing",
                len(result_df.index), len(result_df.columns),
            )

        if result_df is None:
            error_msg = "[!] No data returned from query. Received NoneType result."
            logging.error(error_msg)
            return None, None

        if isinstance(result_df, pd.DataFrame) and result_df.empty:
            error_msg = "[!] No data returned from query. DataFrame is empty."
            logging.error(error_msg)
            return None, None

        result_df = _fillna_dtype_aware(result_df)

        sanitized_df = sanitize_dataframe(result_df)

        # Auto-save to the job store (ring buffer of last 10 results)
        job_id = _job_store.save_auto(sanitized_df, query)

        return sanitized_df, job_id

    except Exception as e:
        # H-CE-3 (2026-04-22): include exception class name so operators
        # grepping docker logs can distinguish an ANTLR SyntaxError from a
        # DuckDB InvalidInputException from an OOM MemoryError. The old
        # message elided the class, making every query crash look the same.
        # User-facing callers should prefer ``process_query_with_diagnostics``
        # which returns the class + message as structured data; this
        # bare-except swallow is for the legacy ``process_query`` signature
        # whose contract is ``(None, None)`` on any failure.
        logging.error(
            "[x] %s while processing query: %s", type(e).__name__, e,
        )
        return None, None


def execute_query(_speakes_query):
    logging.info("[i] Starting the parsing process.")
    if not isinstance(_speakes_query, str):
        raise ValueError("Query must be a string")

    # ── Strip annotation comments (pre-parse) ────────────────────────
    # Triple-backtick annotation lines may be present if the user used
    # "Expand Macros" in the UI.  Remove them before processing.
    _speakes_query = _macro_handler.strip_annotations(_speakes_query)

    # ── Macro expansion (pre-parse) ──────────────────────────────────
    # Replace all backtick-delimited macro calls with their definitions
    # before the ANTLR4 lexer/parser ever sees the query.
    try:
        _speakes_query = _macro_handler.expand(_speakes_query)
    except (ValueError, RecursionError) as exc:
        logging.error("[x] Macro expansion failed: %s", exc)
        raise

    input_stream = antlr4.InputStream(_speakes_query)
    lexer = speakesQueryLexer(input_stream)
    stream = antlr4.CommonTokenStream(lexer)
    parser = speakesQueryParser(stream)
    tree = parser.speakesQuery()
    listener = speakesQueryListener(_speakes_query)
    walker = antlr4.ParseTreeWalker()
    walker.walk(listener, tree)

    # Assuming listener.main_df is the DataFrame that contains the query result
    return listener.main_df if hasattr(listener, 'main_df') else pd.DataFrame()


def sanitize_dataframe(df):
    # Identity pass; Java normalization was removed with jpype in 2026-04-21.
    # See tests/test_no_jpype_and_dispatch_logging.py if a future backend
    # reintroduces Java types.
    return df


def get_job_store() -> JobStore:
    """Return the module-level JobStore singleton."""
    return _job_store


# Add this function to allow direct import and usage in other scripts
def process_query(query):
    """Execute a query and return ``(DataFrame, job_id)`` or ``(None, None)``."""
    return run_query_and_return_results_df(query)


def process_query_with_diagnostics(query):
    """Like :func:`process_query` but propagates the failure reason.

    Returns a 3-tuple ``(df, job_id, diagnostic)``:

    * On success: ``(df, job_id, None)``.
    * On empty/no-rows result: ``(None, None, "empty: <reason>")``.
    * On any exception: ``(None, None, "<exception type>: <message>")``.

    Existed as a private helper for the alert group dispatcher's feeder
    loop, which previously saw ``(None, None)`` and could not tell the
    difference between "query errored" and "cache miss" - the operator
    saw a misleading ``No cached result found`` while the real cause
    (e.g. ``sort -amount_usd`` referencing a column dropped by a
    prior ``| table``) was logged only as a separate ``[x] Error
    processing query`` line. See tests/test_alert_group_feeder_diagnostics.py.
    """
    logging.info(f"[i] Query received: {repr(query)}")
    try:
        query = query.replace("\r\n", "\n")
        query = _strip_line_comments(query)
        query = query.strip()
        if not query:
            return None, None, "empty: query was blank after comment/whitespace stripping"
        result_df = execute_query(f'{query}\n')
        if result_df is None:
            return None, None, "empty: listener returned no DataFrame"
        if isinstance(result_df, pd.DataFrame) and result_df.empty:
            return None, None, "empty: query produced zero rows"
        result_df = _fillna_dtype_aware(result_df)
        sanitized_df = sanitize_dataframe(result_df)
        job_id = _job_store.save_auto(sanitized_df, query)
        return sanitized_df, job_id, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


# If this script is executed directly, call the main function
if __name__ == '__main__':
    query = ' '.join(sys.argv[1:])
    process_query(query)
