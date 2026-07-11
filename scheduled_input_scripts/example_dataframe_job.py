#!/usr/bin/env python3
"""Example scheduled input script that fetches a CSV and stores it as a Parquet file.

This script downloads a small sample dataset, caches the HTTP response using
``get_cached_or_fetch`` and then loads it into a pandas DataFrame. Finally it
calls ``GENERATE_RESULTS`` so the scheduled input engine writes the Parquet
output. Pass ``--help`` for usage information.
"""
import argparse
import logging
from io import StringIO
import pandas as pd
import requests
from scheduled_input_engine.cache import get_cached_or_fetch

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
DEFAULT_CSV = "https://people.sc.fsu.edu/~jburkardt/data/csv/airtravel.csv"

def download_csv(url: str, ttl: int) -> pd.DataFrame:
    """Download ``url`` with caching and return it as a DataFrame."""
    try:
        data = get_cached_or_fetch(url, ttl)
        return pd.read_csv(StringIO(data.decode()))
    except Exception as exc:
        logging.error("[x] Failed to fetch CSV: %s", exc)
        raise

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a CSV and generate a Parquet results file")
    parser.add_argument("--csv_url", default=DEFAULT_CSV, help="URL of the CSV file to download")
    parser.add_argument(
        "--cache_ttl",
        type=int,
        default=3600,
        help="Seconds to cache downloaded data",
    )
    args = parser.parse_args()
    df = download_csv(args.csv_url, args.cache_ttl)
    logging.info("[i] Retrieved %d rows", len(df))
    try:
        GENERATE_RESULTS(df)
    except Exception as exc:
        logging.error("[x] GENERATE_RESULTS failed: %s", exc)
        raise

if __name__ == "__main__":
    main()
