#!/usr/bin/env python3
"""
SPQL Test Framework - parametrized YAML-driven test runner.

Discovers all YAML test definitions under tests/yaml/ and executes them
against the live query engine, asserting expected outcomes.
"""

import math
import pytest
from tests.conftest import collect_all_yaml_tests, make_test_id


# ---------------------------------------------------------------------------
# Collect every test case from every YAML file
# ---------------------------------------------------------------------------

ALL_TESTS = collect_all_yaml_tests(exclude=["tier5_api", "tier6_ui", "tier7_ui_regression"])


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def assert_row_count(df, expect):
    if "row_count" in expect:
        assert len(df) == expect["row_count"], (
            f"Expected {expect['row_count']} rows, got {len(df)}"
        )
    if "min_rows" in expect:
        assert len(df) >= expect["min_rows"], (
            f"Expected >= {expect['min_rows']} rows, got {len(df)}"
        )
    if "max_rows" in expect:
        assert len(df) <= expect["max_rows"], (
            f"Expected <= {expect['max_rows']} rows, got {len(df)}"
        )


def assert_columns(df, expect):
    actual_cols = list(df.columns)

    if "columns" in expect:
        assert set(actual_cols) == set(expect["columns"]), (
            f"Column mismatch.\n  Expected: {sorted(expect['columns'])}"
            f"\n  Actual:   {sorted(actual_cols)}"
        )

    if "columns_include" in expect:
        missing = set(expect["columns_include"]) - set(actual_cols)
        assert not missing, f"Missing expected columns: {missing}"

    if "columns_exclude" in expect:
        present = set(expect["columns_exclude"]) & set(actual_cols)
        assert not present, f"Columns should not be present: {present}"

    if "column_count" in expect:
        assert len(actual_cols) == expect["column_count"], (
            f"Expected {expect['column_count']} columns, got {len(actual_cols)}"
        )


def assert_values(df, expect):
    if "values" not in expect:
        return
    for check in expect["values"]:
        row = check["row"]
        col = check["column"]
        assert col in df.columns, f"Column '{col}' not in result"
        assert row < len(df), f"Row {row} out of range (only {len(df)} rows)"
        actual = df.iloc[row][col]

        if "value" in check:
            expected = check["value"]
            # Coerce types for comparison
            if isinstance(expected, bool) and not isinstance(actual, bool):
                actual = bool(actual)
            elif isinstance(expected, (int, float)) and isinstance(actual, str):
                try:
                    actual = float(actual)
                    expected = float(expected)
                except (ValueError, TypeError):
                    pass
            assert actual == expected, (
                f"Row {row}, col '{col}': expected {expected!r}, got {actual!r}"
            )

        if "approx" in check:
            expected = float(check["approx"])
            actual_f = float(actual)
            assert math.isclose(actual_f, expected, rel_tol=1e-2), (
                f"Row {row}, col '{col}': expected ~{expected}, got {actual_f}"
            )

        if "contains" in check:
            assert check["contains"] in str(actual), (
                f"Row {row}, col '{col}': expected to contain "
                f"{check['contains']!r}, got {actual!r}"
            )

        if "not_empty" in check and check["not_empty"]:
            assert actual != "" and actual is not None, (
                f"Row {row}, col '{col}': expected non-empty value"
            )


def assert_sorted(df, expect):
    if "sorted_by" not in expect:
        return
    col = expect["sorted_by"]
    order = expect.get("sorted_order", "asc")
    vals = df[col].tolist()
    if order == "asc":
        assert vals == sorted(vals), f"Column '{col}' not sorted ascending"
    else:
        assert vals == sorted(vals, reverse=True), (
            f"Column '{col}' not sorted descending"
        )


def assert_column_values(df, expect):
    """Check that a specific column contains exactly the given list of values."""
    if "column_values" not in expect:
        return
    for check in expect["column_values"]:
        col = check["column"]
        expected = check["values"]
        assert col in df.columns, f"Column '{col}' not in result"
        actual = df[col].tolist()
        # Coerce to strings for comparison if expected values are strings
        if expected and isinstance(expected[0], str):
            actual = [str(v) for v in actual]
        assert actual == expected, (
            f"Column '{col}' values mismatch.\n"
            f"  Expected: {expected}\n  Actual:   {actual}"
        )


# ---------------------------------------------------------------------------
# Main parametrized test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tc",
    ALL_TESTS,
    ids=[make_test_id(tc) for tc in ALL_TESTS],
)
def test_spql(run_query, tc):
    """Execute a single SPQL test case defined in YAML."""
    query = tc["query"].strip()
    expect_error = tc.get("expect_error", False)

    df, job_id = run_query(query)

    # --- Negative tests: query should fail ---
    if expect_error:
        assert df is None, (
            f"Expected query to fail but got {len(df)} rows.\n"
            f"Query: {query}"
        )
        return

    # --- Positive tests: query should succeed ---
    assert df is not None, (
        f"Query returned None (error).\nQuery: {query}"
    )

    expect = tc.get("expect", {})
    if not expect:
        return  # No assertions beyond "it didn't crash"

    assert_row_count(df, expect)
    assert_columns(df, expect)
    assert_values(df, expect)
    assert_sorted(df, expect)
    assert_column_values(df, expect)

    if expect.get("not_empty"):
        assert len(df) > 0, "Expected non-empty result"
