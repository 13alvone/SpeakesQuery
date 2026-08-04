#!/usr/bin/env python3
"""
Validation tests for the DuckDB-based process_index_calls replacement.

Tests the new duckdb_index_call module against known data in indexes/archive/
to verify it produces correct results for all token patterns that the
C++ cpp_index_call module handled.
"""

import os
import sys
import pytest
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.chdir(PROJECT_ROOT)

from functionality.duckdb_index_call import process_index_calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Reference data - loaded once for comparison
SYSTEM4_PATH = os.path.join(
    PROJECT_ROOT, "indexes", "archive", "system_logs", "system4.parquet"
)
SYSTEM4_EXISTS = os.path.isfile(SYSTEM4_PATH)

if SYSTEM4_EXISTS:
    REF_DF = pd.read_parquet(SYSTEM4_PATH)
else:
    REF_DF = pd.DataFrame()

needs_system4 = pytest.mark.skipif(
    not SYSTEM4_EXISTS, reason="system4.parquet not found in archive"
)

# Pre-compute reference counts for deterministic assertions
if SYSTEM4_EXISTS:
    REF_ROW_COUNT = len(REF_DF)  # 1000
    REF_ERROR_COUNT = len(REF_DF[REF_DF["status"] == "error"])  # 337
    REF_WARNING_COUNT = len(REF_DF[REF_DF["status"] == "warning"])  # 335
    REF_ERROR_OR_WARNING = len(REF_DF[REF_DF["status"].isin(["error", "warning"])])
    REF_ERROR_AND_ADMIN = len(
        REF_DF[(REF_DF["status"] == "error") & (REF_DF["userRole"] == "admin")]
    )
    REF_ADMIN_GUEST = len(REF_DF[REF_DF["userRole"].isin(["admin", "guest"])])
    REF_ECODE_GT_400 = len(REF_DF[REF_DF["errorCode"] > 400])
    REF_ECODE_LT_502 = len(REF_DF[REF_DF["errorCode"] < 502])
    REF_ECODE_GTE_500 = len(REF_DF[REF_DF["errorCode"] >= 500])
    REF_ECODE_LTE_501 = len(REF_DF[REF_DF["errorCode"] <= 501])
    REF_EPOCH_GTE_1735M = len(REF_DF[REF_DF["_epoch"] >= 1735000000])
    REF_EPOCH_LTE_1704200K = len(REF_DF[REF_DF["_epoch"] <= 1704200000])
    REF_EPOCH_WINDOW = len(
        REF_DF[
            (REF_DF["_epoch"] >= 1704067200) & (REF_DF["_epoch"] <= 1704200000)
        ]
    )
    REF_EPOCH_WINDOW_ERROR = len(
        REF_DF[
            (REF_DF["_epoch"] >= 1704067200)
            & (REF_DF["_epoch"] <= 1710000000)
            & (REF_DF["status"] == "error")
        ]
    )


# ---------------------------------------------------------------------------
# Basic index loading
# ---------------------------------------------------------------------------

class TestBasicIndexLoad:
    """Verify that files are loaded and all rows/columns are returned."""

    @needs_system4
    def test_single_file_load(self):
        tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        assert len(df) == REF_ROW_COUNT
        for col in REF_DF.columns:
            assert col in df.columns, f"Missing column: {col}"
        assert "_source_file" in df.columns

    @needs_system4
    def test_source_file_column_value(self):
        tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        assert df["_source_file"].unique().tolist() == [
            "archive/system_logs/system4.parquet"
        ]

    @needs_system4
    def test_indexes_prefix_stripped(self):
        """index= values with 'indexes/' prefix should work identically."""
        tokens = ['index', '=', '"indexes/archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        assert len(df) == REF_ROW_COUNT

    @needs_system4
    def test_wildcard_glob(self):
        tokens = ['index', '=', '"archive/system_logs/*"']
        df = process_index_calls(tokens)
        # Only system4.parquet lives directly under system_logs/ (not recursive)
        assert len(df) == REF_ROW_COUNT

    @needs_system4
    def test_deep_glob(self):
        tokens = ['index', '=', '"archive/system_logs/**"']
        df = process_index_calls(tokens)
        # system4.parquet (1000) + error_tracking/error1 (3) + error2 (2) = 1005
        assert len(df) == 1005

    @needs_system4
    def test_directory_index(self):
        tokens = ['index', '=', '"archive/system_logs"']
        df = process_index_calls(tokens)
        # Directory without glob should recursively load all parquets
        assert len(df) == 1005

    @needs_system4
    def test_glob_excludes_embedding_sidecars(self):
        # error_tracking/ ships error1.embeddings.parquet + error2.embeddings.parquet
        # next to the sources. Wildcard and directory loads must skip them -
        # sidecars are sweeper infrastructure, not data - or every swept
        # source double-counts (caught 2026-07-25: expected 8, got 16).
        tokens = ['index', '=', '"archive/system_logs/error_tracking/*.parquet"']
        df = process_index_calls(tokens)
        # error1.parquet (3) + error2.parquet (2), sidecars excluded
        assert len(df) == 5

    @needs_system4
    def test_explicit_sidecar_path_still_loads(self):
        # Naming a sidecar file directly is the deliberate inspection
        # escape hatch - exclusion applies only to glob/directory expansion.
        # Sidecars are machine-generated cache (untracked from git
        # 2026-08-04): on a fresh clone this file exists only after the
        # embedding sweeper or `python -m tools.embed_backfill` has run.
        sidecar = (
            "indexes/archive/system_logs/error_tracking/"
            "error1.embeddings.parquet"
        )
        if not os.path.exists(sidecar):
            pytest.skip(
                "embedding sidecar not generated on this host yet "
                "(run: python -m tools.embed_backfill)"
            )
        tokens = ['index', '=', f'"{sidecar}"']
        df = process_index_calls(tokens)
        assert len(df) > 0
        assert "embedding" in df.columns

    def test_default_index_no_explicit_pattern(self):
        """When no index= is specified, defaults to system_logs/**."""
        tokens = ['status', '=', '"error"']
        df = process_index_calls(tokens)
        assert isinstance(df, pd.DataFrame)

    def test_empty_tokens(self):
        df = process_index_calls([])
        assert isinstance(df, pd.DataFrame)

    @needs_system4
    def test_combined_index_token(self):
        """Handle shlex combined form: index=path (no spaces around =)."""
        tokens = ['index="archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        assert len(df) == REF_ROW_COUNT

    @needs_system4
    def test_deep_glob_source_file_tracking(self):
        """Deep glob should track which rows came from which file."""
        tokens = ['index', '=', '"archive/system_logs/**"']
        df = process_index_calls(tokens)
        source_files = sorted(df["_source_file"].unique().tolist())
        assert source_files == [
            "archive/system_logs/error_tracking/error1.parquet",
            "archive/system_logs/error_tracking/error2.parquet",
            "archive/system_logs/system4.parquet",
        ]
        # Verify per-file counts
        counts = df.groupby("_source_file").size().to_dict()
        assert counts["archive/system_logs/system4.parquet"] == 1000
        assert counts["archive/system_logs/error_tracking/error1.parquet"] == 3
        assert counts["archive/system_logs/error_tracking/error2.parquet"] == 2


# ---------------------------------------------------------------------------
# Equality filters - exact row counts
# ---------------------------------------------------------------------------

class TestEqualityFilters:

    @needs_system4
    def test_string_equality(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ERROR_COUNT
        assert set(df["status"].unique()) == {"error"}

    @needs_system4
    def test_not_equals(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '!=', '"error"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ROW_COUNT - REF_ERROR_COUNT
        assert "error" not in df["status"].unique()

    @needs_system4
    def test_numeric_greater_than(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'errorCode', '>', '400',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ECODE_GT_400
        assert df["errorCode"].min() > 400

    @needs_system4
    def test_numeric_less_than(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'errorCode', '<', '502',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ECODE_LT_502
        assert df["errorCode"].max() < 502

    @needs_system4
    def test_gte(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'errorCode', '>=', '500',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ECODE_GTE_500
        assert df["errorCode"].min() >= 500

    @needs_system4
    def test_lte(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'errorCode', '<=', '501',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ECODE_LTE_501
        assert df["errorCode"].max() <= 501


# ---------------------------------------------------------------------------
# Logical operators - exact counts
# ---------------------------------------------------------------------------

class TestLogicalOperators:

    @needs_system4
    def test_or_expression(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"', 'OR', 'status', '=', '"warning"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ERROR_OR_WARNING
        assert set(df["status"].unique()) == {"error", "warning"}

    @needs_system4
    def test_implicit_and(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"', 'userRole', '=', '"admin"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ERROR_AND_ADMIN
        assert set(df["status"].unique()) == {"error"}
        assert set(df["userRole"].unique()) == {"admin"}

    @needs_system4
    def test_explicit_and(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"', 'AND', 'userRole', '=', '"admin"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ERROR_AND_ADMIN
        assert set(df["status"].unique()) == {"error"}
        assert set(df["userRole"].unique()) == {"admin"}

    @needs_system4
    def test_in_expression(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'userRole', 'IN', '(', '"admin"', ',', '"guest"', ')',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_ADMIN_GUEST
        assert set(df["userRole"].unique()) == {"admin", "guest"}

    @needs_system4
    def test_and_or_precedence(self):
        """AND should bind tighter than OR: error AND admin OR warning
        means (error AND admin) OR warning."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"', 'AND', 'userRole', '=', '"admin"',
            'OR', 'status', '=', '"warning"',
        ]
        df = process_index_calls(tokens)
        expected = len(
            REF_DF[
                ((REF_DF["status"] == "error") & (REF_DF["userRole"] == "admin"))
                | (REF_DF["status"] == "warning")
            ]
        )
        assert len(df) == expected
        # Must include warnings regardless of userRole
        assert "warning" in df["status"].values

    @needs_system4
    def test_parenthesized_or_then_and(self):
        """(error OR warning) AND admin - parens override precedence."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            '(', 'status', '=', '"error"', 'OR', 'status', '=', '"warning"', ')',
            'AND', 'userRole', '=', '"admin"',
        ]
        df = process_index_calls(tokens)
        expected = len(
            REF_DF[
                REF_DF["status"].isin(["error", "warning"])
                & (REF_DF["userRole"] == "admin")
            ]
        )
        assert len(df) == expected
        assert set(df["userRole"].unique()) == {"admin"}

    @needs_system4
    def test_multiple_filters_same_column(self):
        """Range filter: errorCode >= 500 AND errorCode <= 502."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'errorCode', '>=', '500', 'AND', 'errorCode', '<=', '502',
        ]
        df = process_index_calls(tokens)
        expected = len(
            REF_DF[
                (REF_DF["errorCode"] >= 500) & (REF_DF["errorCode"] <= 502)
            ]
        )
        assert len(df) == expected
        assert df["errorCode"].min() >= 500
        assert df["errorCode"].max() <= 502


# ---------------------------------------------------------------------------
# Time filtering (earliest / latest) - exact counts
# ---------------------------------------------------------------------------

class TestTimeFiltering:

    @needs_system4
    def test_earliest_only(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest', '=', '"1735000000"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_EPOCH_GTE_1735M
        assert df["_epoch"].min() >= 1735000000

    @needs_system4
    def test_latest_only(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'latest', '=', '"1704200000"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_EPOCH_LTE_1704200K
        assert df["_epoch"].max() <= 1704200000

    @needs_system4
    def test_earliest_and_latest(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest', '=', '"1704067200"',
            'latest', '=', '"1704200000"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_EPOCH_WINDOW
        assert df["_epoch"].min() >= 1704067200
        assert df["_epoch"].max() <= 1704200000

    @needs_system4
    def test_time_with_date_string(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest', '=', '"2024-01-01"',
            'latest', '=', '"2024-01-03"',
        ]
        df = process_index_calls(tokens)
        # 2024-01-01 = 1704067200, 2024-01-03 = 1704240000
        assert len(df) > 0
        assert df["_epoch"].min() >= 1704067200
        assert df["_epoch"].max() <= 1704240000

    @needs_system4
    def test_time_filter_combined_with_field_filter(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest', '=', '"1704067200"',
            'latest', '=', '"1710000000"',
            'status', '=', '"error"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_EPOCH_WINDOW_ERROR
        assert set(df["status"].unique()) == {"error"}
        assert df["_epoch"].min() >= 1704067200
        assert df["_epoch"].max() <= 1710000000

    @needs_system4
    def test_combined_earliest_token(self):
        """Handle combined form: earliest=value (no spaces around =)."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest=1735000000',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_EPOCH_GTE_1735M
        assert df["_epoch"].min() >= 1735000000

    @needs_system4
    def test_combined_latest_token(self):
        """Handle combined form: latest=value (no spaces around =)."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'latest=1704200000',
        ]
        df = process_index_calls(tokens)
        assert len(df) == REF_EPOCH_LTE_1704200K
        assert df["_epoch"].max() <= 1704200000

    @needs_system4
    def test_time_with_iso_datetime(self):
        """Test ISO 8601 datetime format: 2024-01-01T00:00:00."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest', '=', '"2024-01-01T00:00:00"',
            'latest', '=', '"2024-01-02T00:00:00"',
        ]
        df = process_index_calls(tokens)
        # 2024-01-01T00:00:00 = 1704067200, 2024-01-02T00:00:00 = 1704153600
        assert len(df) > 0
        assert df["_epoch"].min() >= 1704067200
        assert df["_epoch"].max() <= 1704153600

    def test_time_filter_on_file_without_epoch_column(self):
        """Files with 'timestamp' but no '_epoch' should use TRY_CAST."""
        tokens = [
            'index', '=', '"archive/system_logs/error_tracking/error1.parquet"',
            'earliest', '=', '"2024-01-01"',
        ]
        df = process_index_calls(tokens)
        # error1.parquet has timestamp column (str type), no _epoch column.
        # If timestamps are parseable, should return results; if not, file is
        # skipped. Either way, should not crash.
        assert isinstance(df, pd.DataFrame)


# ---------------------------------------------------------------------------
# Cross-schema concat (deep glob with heterogeneous schemas)
# ---------------------------------------------------------------------------

class TestCrossSchemaConcat:
    """Deep globs may hit files with different column sets. Verify concat
    fills missing columns with NaN and doesn't lose rows."""

    @needs_system4
    def test_deep_glob_preserves_all_columns(self):
        """All columns from all files should appear in the concat result."""
        tokens = ['index', '=', '"archive/system_logs/**"']
        df = process_index_calls(tokens)
        # system4 has: _epoch, timestamp, status, errorCode, warningCode, userRole
        # error_tracking files have: timestamp, status, errorCode, priority, action
        for col in ("_epoch", "timestamp", "status", "errorCode", "warningCode",
                     "userRole", "priority", "action"):
            assert col in df.columns, f"Missing column from concat: {col}"

    @needs_system4
    def test_deep_glob_nan_fill_for_missing_columns(self):
        """Rows from files missing a column should have NaN for that column."""
        tokens = ['index', '=', '"archive/system_logs/**"']
        df = process_index_calls(tokens)
        # error_tracking files don't have 'warningCode'; those rows should be NaN
        et_rows = df[df["_source_file"].str.contains("error_tracking")]
        assert et_rows["warningCode"].isna().all()
        # system4 doesn't have 'priority'; those rows should be NaN
        s4_rows = df[df["_source_file"] == "archive/system_logs/system4.parquet"]
        assert s4_rows["priority"].isna().all()

    @needs_system4
    def test_filter_on_deep_glob_skips_schema_mismatch(self):
        """Filtering on a column that only exists in some files should
        skip files that lack the column and return rows from the rest."""
        tokens = [
            'index', '=', '"archive/system_logs/**"',
            'warningCode', '>=', '100',
        ]
        df = process_index_calls(tokens)
        # Only system4.parquet has warningCode. error_tracking files are skipped.
        assert len(df) > 0
        assert set(df["_source_file"].unique()) == {
            "archive/system_logs/system4.parquet"
        }
        assert df["warningCode"].min() >= 100


# ---------------------------------------------------------------------------
# Edge cases and error handling
# ---------------------------------------------------------------------------

class TestEdgeCases:

    @needs_system4
    def test_no_match_returns_empty(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"completely_nonexistent_value"',
        ]
        df = process_index_calls(tokens)
        assert df.empty

    @needs_system4
    def test_missing_column_skips_file(self):
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'nonexistent_column', '=', '"value"',
        ]
        df = process_index_calls(tokens)
        assert df.empty

    def test_nonexistent_index_returns_empty(self):
        tokens = ['index', '=', '"this/does/not/exist"']
        df = process_index_calls(tokens)
        assert df.empty

    @needs_system4
    def test_returns_pandas_dataframe(self):
        tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        assert isinstance(df, pd.DataFrame)

    @needs_system4
    def test_multiple_files_concat(self):
        """Deep glob that hits multiple files should concat them."""
        tokens = ['index', '=', '"archive/system_logs/**"']
        df = process_index_calls(tokens)
        source_files = df["_source_file"].unique()
        assert len(source_files) == 3

    @needs_system4
    def test_malformed_filter_degrades_gracefully(self):
        """Garbage filter tokens should not crash - just skip the filter."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'AND', 'AND', 'OR',
        ]
        df = process_index_calls(tokens)
        # Should either return all rows (filter ignored) or empty (parse fail)
        assert isinstance(df, pd.DataFrame)

    @needs_system4
    def test_empty_filter_returns_all_rows(self):
        """Index with no filter should return all rows."""
        tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        assert len(df) == REF_ROW_COUNT

    @needs_system4
    def test_dtype_consistency_with_pandas(self):
        """Loaded dtypes should match pd.read_parquet for non-nullable columns."""
        tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        df = process_index_calls(tokens)
        # _epoch should be numeric
        assert pd.api.types.is_numeric_dtype(df["_epoch"])
        # status should be object (string)
        assert df["status"].dtype == object
        # errorCode in the reference is float64 (has NaN values)
        assert pd.api.types.is_numeric_dtype(df["errorCode"])


# ---------------------------------------------------------------------------
# Determinism & repeatability
# ---------------------------------------------------------------------------

class TestDeterminism:
    """Verify that identical queries produce identical results - critical
    for production reliability with Polymarket data."""

    @needs_system4
    def test_repeated_load_identical(self):
        """Two identical calls must produce identical DataFrames."""
        tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"',
        ]
        df1 = process_index_calls(tokens)
        df2 = process_index_calls(tokens)
        assert len(df1) == len(df2)
        # Reset index for comparison
        pd.testing.assert_frame_equal(
            df1.reset_index(drop=True), df2.reset_index(drop=True)
        )

    @needs_system4
    def test_repeated_glob_identical(self):
        """Two identical glob calls must produce identical results."""
        tokens = ['index', '=', '"archive/system_logs/**"']
        df1 = process_index_calls(tokens)
        df2 = process_index_calls(tokens)
        assert len(df1) == len(df2)
        assert sorted(df1["_source_file"].unique()) == sorted(
            df2["_source_file"].unique()
        )

    @needs_system4
    def test_filter_result_is_strict_subset(self):
        """Filtered result must be a strict subset of unfiltered result."""
        all_tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        filtered_tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'status', '=', '"error"',
        ]
        df_all = process_index_calls(all_tokens)
        df_filtered = process_index_calls(filtered_tokens)
        assert len(df_filtered) < len(df_all)
        # Every filtered _epoch value must exist in the full set
        assert set(df_filtered["_epoch"]).issubset(set(df_all["_epoch"]))

    @needs_system4
    def test_time_range_is_strict_subset(self):
        """Time-filtered result must be a subset of unfiltered."""
        all_tokens = ['index', '=', '"archive/system_logs/system4.parquet"']
        time_tokens = [
            'index', '=', '"archive/system_logs/system4.parquet"',
            'earliest', '=', '"1710000000"',
            'latest', '=', '"1720000000"',
        ]
        df_all = process_index_calls(all_tokens)
        df_time = process_index_calls(time_tokens)
        assert len(df_time) < len(df_all)
        assert df_time["_epoch"].min() >= 1710000000
        assert df_time["_epoch"].max() <= 1720000000
