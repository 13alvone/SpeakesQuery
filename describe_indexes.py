#!/usr/bin/env python3
"""Utility script for inspecting parquet indexes.

This module avoids shelling out to external commands for security and
portability reasons. Instead of using ``subprocess`` with ``shell=True``
to walk directories, the code relies solely on Python's ``os`` module.
"""

import os
import logging
from typing import Generator

import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


def explore_directory() -> str:
    """Return a listing of all directories under the current path."""
    lines = []
    for root, dirs, _ in os.walk("."):
        for directory in sorted(dirs):
            full_path = os.path.join(root, directory)
            try:
                stat = os.stat(full_path)
                lines.append(f"{stat.st_mode:o} {stat.st_size:8d} {full_path}")
            except OSError as exc:
                logger.warning("[!] Unable to stat %s: %s", full_path, exc)
    return "\n".join(lines)


def find_parquet_files(base_dir: str | None = None) -> Generator[str, None, None]:
    """Yield all parquet files under ``base_dir`` excluding ``archive``.

    Parameters
    ----------
    base_dir:
        Optional directory to search. If ``None`` the path ``indexes`` relative
        to this script is used. Using an explicit base directory avoids relying
        on the caller's working directory.
    """

    if base_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        base_dir = os.path.join(script_dir, "indexes")

    for root, _, files in os.walk(base_dir):
        if "archive" in os.path.relpath(root, base_dir).split(os.sep):
            continue
        for file in files:
            if file.endswith(".parquet"):
                yield os.path.join(root, file)


def read_parquet_preview(filepath: str, max_rows: int = 20) -> str:
    """Read and format parquet file contents with preview."""
    try:
        df = pq.read_table(filepath).to_pandas()
        total_rows = len(df)
        preview = df.head(max_rows)

        output = [f"\n=== PARQUET FILE: {filepath} ==="]
        output.append(str(preview))

        if total_rows > max_rows:
            output.append(f"\n[...{total_rows - max_rows} more rows truncated...]")

        return '\n'.join(output)
    except Exception as e:
        return f"Error reading {filepath}: {str(e)}"


def main():
    """Print information about available parquet indexes."""

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Step 1: print directory structure if needed
    # print(explore_directory())

    # Step 2: Print parquet file contents
    for parquet_file in find_parquet_files():
        logging.info(read_parquet_preview(parquet_file))


if __name__ == "__main__":
    main()
