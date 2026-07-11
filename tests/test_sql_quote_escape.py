"""H-CE-1 regression: SQL filter values must escape single quotes.

Before the 2026-04-21 fix, ``_parse_in_clause`` and ``_parse_operand`` wrapped
filter values with ``f"'{t.value}'"`` - a bareword f-string that produced
invalid SQL whenever the value contained a single quote. Example:

    | search name="O'Brien"

became ``(name = 'O'Brien')``, which DuckDB rejected at parse time. The
``_load_and_filter`` try/except swallowed the error and returned an empty
DataFrame; the user saw "no results" with no indication of the cause.

After the fix, ``_sql_quote(value)`` doubles embedded single quotes per the
ANSI SQL standard, so apostrophes in filter values behave as literal string
content.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from functionality.duckdb_index_call import (  # noqa: E402
    _sql_quote,
    process_index_calls,
)


# ----------------------------------------------------------------------
# Unit-level: _sql_quote contract
# ----------------------------------------------------------------------

class TestSqlQuoteHelper:

    def test_no_apostrophe_unchanged(self):
        assert _sql_quote("error") == "'error'"

    def test_single_apostrophe_doubled(self):
        assert _sql_quote("O'Brien") == "'O''Brien'"

    def test_multiple_apostrophes_all_doubled(self):
        assert _sql_quote("'hello' and 'goodbye'") == "'''hello'' and ''goodbye'''"

    def test_non_string_coerced(self):
        # _sql_quote accepts any token value; str() is applied.
        assert _sql_quote(42) == "'42'"

    def test_empty_string(self):
        assert _sql_quote("") == "''"


# ----------------------------------------------------------------------
# Integration: query a parquet with apostrophe values end-to-end
# ----------------------------------------------------------------------

@pytest.fixture
def apostrophe_parquet(tmp_path: Path) -> Path:
    """Write a small parquet under indexes/ with apostrophe-bearing values."""
    indexes_dir = PROJECT_ROOT / "indexes"
    fixture_dir = indexes_dir / "_test_quote_escape"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = fixture_dir / "names.parquet"

    df = pd.DataFrame({
        "name": ["O'Brien", "D'Artagnan", "ACME", "plain"],
        "role": ["captain", "musketeer", "corp", "user"],
        "_epoch": [1_700_000_000, 1_700_000_001, 1_700_000_002, 1_700_000_003],
    })
    df.to_parquet(path, index=False, compression="gzip")
    yield path
    # Cleanup - this is a real path under indexes/, remove on teardown.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    try:
        fixture_dir.rmdir()
    except OSError:
        pass


def _rel_to_indexes(path: Path) -> str:
    """Return the index pattern (relative to project root, quoted)."""
    rel = path.relative_to(PROJECT_ROOT)
    return f'"{rel}"'


class TestApostropheEquality:

    def test_equality_with_apostrophe_returns_row(self, apostrophe_parquet):
        """``name = "O'Brien"`` must return the matching row, not an empty DF."""
        tokens = [
            "index", "=", _rel_to_indexes(apostrophe_parquet),
            "name", "=", "\"O'Brien\"",
        ]
        df = process_index_calls(tokens)
        assert len(df) == 1, (
            f"Expected 1 row for name=O'Brien; got {len(df)}. "
            f"Pre-fix this returned an empty DataFrame due to SQL parse error."
        )
        assert df.iloc[0]["name"] == "O'Brien"

    def test_equality_without_apostrophe_still_works(self, apostrophe_parquet):
        """Regression-proof the quote helper against non-apostrophe values."""
        tokens = [
            "index", "=", _rel_to_indexes(apostrophe_parquet),
            "name", "=", '"plain"',
        ]
        df = process_index_calls(tokens)
        assert len(df) == 1
        assert df.iloc[0]["name"] == "plain"

    def test_inequality_with_apostrophe_excludes_row(self, apostrophe_parquet):
        """``name != "O'Brien"`` must exclude exactly the apostrophe row."""
        tokens = [
            "index", "=", _rel_to_indexes(apostrophe_parquet),
            "name", "!=", "\"O'Brien\"",
        ]
        df = process_index_calls(tokens)
        assert len(df) == 3
        assert "O'Brien" not in set(df["name"].tolist())


class TestApostropheInClause:

    def test_in_clause_with_two_apostrophe_values(self, apostrophe_parquet):
        """``name in ("O'Brien", "D'Artagnan")`` must return both rows."""
        tokens = [
            "index", "=", _rel_to_indexes(apostrophe_parquet),
            "name", "in", "(", "\"O'Brien\"", ",", "\"D'Artagnan\"", ")",
        ]
        df = process_index_calls(tokens)
        assert len(df) == 2, (
            f"Expected 2 apostrophe-bearing rows via IN clause; got {len(df)}."
        )
        assert set(df["name"].tolist()) == {"O'Brien", "D'Artagnan"}

    def test_in_clause_mixed_values(self, apostrophe_parquet):
        """IN clause works with a mix of apostrophe and plain values."""
        tokens = [
            "index", "=", _rel_to_indexes(apostrophe_parquet),
            "name", "in", "(", "\"O'Brien\"", ",", '"plain"', ")",
        ]
        df = process_index_calls(tokens)
        assert len(df) == 2
        assert set(df["name"].tolist()) == {"O'Brien", "plain"}
