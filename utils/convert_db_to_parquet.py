#!/usr/bin/env python3

import argparse
import sqlite3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import os
import logging

logger = logging.getLogger(__name__)


def convert_sqlite_to_arrow(db_path, output_dir):
    """
    Convert all tables from a SQLite database to Apache Arrow format and save them as Parquet files.
    :param db_path: Path to the SQLite database file.
    :param output_dir: Directory where the Arrow/Parquet files will be saved.
    """
    # Connect to the SQLite database
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # Retrieve a list of all tables in the database
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        for table_name in tables:
            table_name = table_name[0]
            logging.info(f"[i] Processing table: {table_name}")

            # Read table into a pandas DataFrame
            df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)

            # Convert DataFrame to Arrow Table
            arrow_table = pa.Table.from_pandas(df)

            # Save Arrow Table as Parquet file
            parquet_path = os.path.join(output_dir, f"{table_name}.system4.system4.parquet")
            pq.write_table(arrow_table, parquet_path)

            logging.info(f"[i] {table_name} saved to {parquet_path}")

    # Connection is automatically closed by context manager
    logging.info("[i] All tables have been processed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert SQLite DB tables to Apache Arrow format.")
    parser.add_argument("db_path", type=str, help="Path to the SQLite database file.")
    parser.add_argument("output_dir", type=str, help="Directory to save the Apache Arrow (Parquet) files.")

    args = parser.parse_args()

    convert_sqlite_to_arrow(args.db_path, args.output_dir)
