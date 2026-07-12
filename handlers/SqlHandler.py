"""``| sql`` passthrough pipe (weakness audit W14, 2026-07-12).

The escape hatch out of SPQL: run one DuckDB SQL statement against the
current pipeline DataFrame. The DataFrame is registered as the view
``pipeline``, so:

    index="indexes/sample/app_logs/*"
    | sql "SELECT service, count(*) AS n FROM pipeline GROUP BY service ORDER BY n DESC"
    | head 5

composes with everything upstream AND downstream - the statement's
result becomes the new pipeline DataFrame.

Security posture (the due-diligence line, pinned by
tests/test_sql_pipe.py::TestExternalAccessLockdown):

- Per-call in-memory connection (never the module-level ``duckdb.sql``
  default connection - see the 2026-05-18 thread-safety incident in
  CLAUDE.md).
- ``SET enable_external_access=false`` BEFORE the user statement runs,
  so ``read_parquet()`` / ``read_csv()`` / ``ATTACH`` cannot touch
  arbitrary filesystem paths. DuckDB hard-refuses to re-enable the
  flag on a connection that disabled it.
- ``SET lock_configuration=true`` after, so the statement cannot flip
  any other setting either.

DDL/DML against the in-memory temp database (CREATE TABLE, INSERT) is
harmless by construction: the connection is discarded after the call.
"""

import logging

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

PIPELINE_VIEW_NAME = "pipeline"


class SqlPipeError(Exception):
    """Raised when the user's SQL statement fails; carries the DuckDB
    message so process_query_with_diagnostics surfaces the real reason."""


class SqlHandler:
    def execute_sql(self, df: "pd.DataFrame | None", sql_text: str) -> pd.DataFrame:
        """Run ``sql_text`` against ``df`` (exposed as view ``pipeline``).

        Empty-input tolerant per the SPQL handler convention: a None or
        empty DataFrame still registers as an empty ``pipeline`` view,
        so ``SELECT * FROM pipeline`` on a zero-row day returns an
        empty well-shaped result instead of raising.
        """
        if df is None:
            df = pd.DataFrame()
        if len(df.columns) == 0:
            # DuckDB refuses to register a zero-column DataFrame. The
            # project's empty-day convention is a schema of just
            # ``_epoch`` (see CLAUDE.md "handlers must tolerate empty
            # input"), so give the empty view that same shape.
            df = pd.DataFrame({"_epoch": pd.Series(dtype="int64")})
        sql_text = (sql_text or "").strip()
        if not sql_text:
            raise SqlPipeError("sql: statement is empty")

        con = duckdb.connect(database=":memory:")
        try:
            con.execute("PRAGMA threads=1")
            # The due-diligence line: no filesystem or network reach for
            # user SQL, and no way to turn it back on afterwards.
            con.execute("SET enable_external_access=false")
            con.execute("SET lock_configuration=true")
            con.register(PIPELINE_VIEW_NAME, df)
            try:
                result = con.execute(sql_text).df()
            except duckdb.Error as exc:
                raise SqlPipeError(f"sql: {exc}") from exc
            logger.debug(
                "[i] sql pipe: %d rows x %d cols in, %d rows x %d cols out",
                len(df.index), len(df.columns),
                len(result.index), len(result.columns),
            )
            return result
        finally:
            con.close()
