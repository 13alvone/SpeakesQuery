"""``| sql`` pipe security + robustness pins (weakness audit W14, 2026-07-12).

The YAML tiers cover behavior through the full engine; this file pins
the properties that must never regress at the unit level:

1. External-access lockdown - the due-diligence line from the design
   decision: user SQL runs with ``enable_external_access=false`` on a
   per-call connection, so it can never read_parquet()/read_csv()
   arbitrary filesystem paths, ATTACH databases, or re-enable the flag.
2. Empty-input tolerance (the SPQL handler convention).
3. Per-call connection - the handler must never touch the module-level
   ``duckdb.sql`` default connection (thread-safety incident,
   2026-05-18, see CLAUDE.md).
4. Grammar + vocab parity: 'sql' is a real grammar rule and shows up
   in the console autocomplete vocabulary.
"""

import pandas as pd
import pytest

from handlers.SqlHandler import SqlHandler, SqlPipeError


@pytest.fixture()
def sample_df():
    return pd.DataFrame({
        "level": ["INFO", "ERROR", "INFO"],
        "service": ["web", "api", "web"],
        "response_ms": [12, 900, 40],
    })


class TestBasicExecution:
    def test_select_star(self, sample_df):
        out = SqlHandler().execute_sql(sample_df, "SELECT * FROM pipeline")
        assert len(out) == 3
        assert list(out.columns) == ["level", "service", "response_ms"]

    def test_aggregation(self, sample_df):
        out = SqlHandler().execute_sql(
            sample_df,
            "SELECT service, count(*) AS n FROM pipeline "
            "GROUP BY service ORDER BY n DESC",
        )
        assert out.iloc[0]["service"] == "web"
        assert out.iloc[0]["n"] == 2

    def test_result_replaces_pipeline(self, sample_df):
        out = SqlHandler().execute_sql(
            sample_df, "SELECT 1 AS solo"
        )
        assert list(out.columns) == ["solo"]
        assert len(out) == 1


class TestExternalAccessLockdown:
    def test_read_parquet_blocked(self, sample_df):
        with pytest.raises(SqlPipeError, match="(?i)permission|disabled"):
            SqlHandler().execute_sql(
                sample_df,
                "SELECT * FROM read_parquet('indexes/sample/app_logs/*.parquet')",
            )

    def test_read_csv_blocked(self, sample_df):
        with pytest.raises(SqlPipeError, match="(?i)permission|disabled"):
            SqlHandler().execute_sql(
                sample_df, "SELECT * FROM read_csv('/etc/passwd')"
            )

    def test_copy_to_filesystem_blocked(self, sample_df, tmp_path):
        target = tmp_path / "exfil.csv"
        with pytest.raises(SqlPipeError, match="(?i)permission|disabled"):
            SqlHandler().execute_sql(
                sample_df, f"COPY pipeline TO '{target}' (FORMAT CSV)"
            )
        assert not target.exists()

    def test_attach_blocked(self, sample_df, tmp_path):
        with pytest.raises(SqlPipeError, match="(?i)permission|disabled"):
            SqlHandler().execute_sql(
                sample_df, f"ATTACH '{tmp_path / 'x.db'}' AS ext"
            )

    def test_reenable_external_access_blocked(self, sample_df):
        with pytest.raises(SqlPipeError, match="(?i)locked|cannot change"):
            SqlHandler().execute_sql(
                sample_df, "SET enable_external_access=true"
            )

    def test_config_fully_locked(self, sample_df):
        with pytest.raises(SqlPipeError, match="(?i)locked|cannot change"):
            SqlHandler().execute_sql(sample_df, "SET memory_limit='100GB'")


class TestHandlerConventions:
    def test_empty_dataframe_is_valid_input(self):
        out = SqlHandler().execute_sql(
            pd.DataFrame(), "SELECT * FROM pipeline"
        )
        assert isinstance(out, pd.DataFrame)
        assert len(out) == 0

    def test_none_input_treated_as_empty(self):
        out = SqlHandler().execute_sql(None, "SELECT count(*) AS n FROM pipeline")
        assert out.iloc[0]["n"] == 0

    def test_empty_statement_raises_cleanly(self):
        with pytest.raises(SqlPipeError, match="empty"):
            SqlHandler().execute_sql(pd.DataFrame(), "   ")

    def test_does_not_use_module_level_duckdb_connection(self):
        # The 2026-05-18 incident: duckdb.sql() shares one global
        # connection across threads. The handler source must never
        # reference it.
        import inspect
        import handlers.SqlHandler as module
        source = inspect.getsource(module)
        assert "duckdb.sql(" not in source, (
            "SqlHandler must use a per-call duckdb.connect() connection, "
            "never the module-level duckdb.sql() default connection"
        )
        assert "duckdb.connect(" in source

    def test_temp_ddl_is_isolated_per_call(self, sample_df):
        # DDL against the in-memory database is allowed but discarded
        # with the connection - a second call must not see the table.
        handler = SqlHandler()
        handler.execute_sql(
            sample_df,
            "CREATE TABLE scratch AS SELECT * FROM pipeline",
        )
        with pytest.raises(SqlPipeError):
            handler.execute_sql(sample_df, "SELECT * FROM scratch")


class TestGrammarParity:
    def test_sql_rule_in_grammar(self):
        from pathlib import Path
        grammar = (
            Path(__file__).resolve().parent.parent / "lexers" / "speakesQuery.g4"
        ).read_text(encoding="utf-8")
        assert "SQL DOUBLE_QUOTED_STRING" in grammar

    def test_sql_in_autocomplete_vocab(self):
        from lexers.grammar_vocab import get_vocab
        commands = get_vocab().get("commands", [])
        names = {
            (c.get("name") if isinstance(c, dict) else c) for c in commands
        }
        assert any(str(n).lower() == "sql" for n in names)
