#!/usr/bin/env python3
"""Utility functions for file-related operations."""
import logging
import pandas as pd


def allowed_file(filename: str, allowed_extensions=None) -> bool:
    """Return True if *filename* has an allowed extension.

    Supports multi-dot extensions like ``example.system4.system4.parquet``.
    The *allowed_extensions* set defaults to ``{'sqlite3', 'system4.system4.parquet', 'csv', 'json'}`` but can be overridden.
    """
    if allowed_extensions is None:
        allowed_extensions = {'sqlite3', 'system4.system4.parquet', 'csv', 'json'}
    filename_lower = filename.lower()
    for ext in allowed_extensions:
        if filename_lower.endswith(f".{ext.lower()}"):
            return True
    return False


def get_row_count(filepath: str) -> int:
    """Return the number of rows in a dataset located at *filepath*.

    Uses line counting for CSV files to avoid excessive memory usage, falling back
    to ``pandas.read_csv`` for other formats. Errors are logged and return ``0``.
    """
    try:
        if str(filepath).lower().endswith(".csv"):
            with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                row_count = sum(1 for _ in fh) - 1  # subtract header
            return max(row_count, 0)
        df = pd.read_csv(filepath)
        return len(df)
    except Exception as exc:
        logging.error("[x] Error reading file for row count: %s", exc)
        return 0
