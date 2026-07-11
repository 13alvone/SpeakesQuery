#!/usr/bin/env python3

import os
import pyarrow as pa
import pyarrow.parquet as pq
from functionality.duckdb_index_call import _parse_date_to_epoch
import logging

# Configure logging for this module
logger = logging.getLogger(__name__)


class ParquetEpochAdder:
    """A class to add an '_epoch' column to a Parquet file based on a date field.

    The '_epoch' column contains epoch timestamps derived from the specified date field.
    """

    def __init__(self, parquet_file_path, date_field_name):
        """Initializes the ParquetEpochAdder.

        Args:
            parquet_file_path (str): The file path to the Parquet file.
            date_field_name (str): The name of the date field to convert to epoch timestamps.
        """
        self.parquet_file_path = parquet_file_path
        self.date_field_name = date_field_name
        self.table = None

    def read_parquet(self):
        """Reads the Parquet file and loads it into a PyArrow Table.

        Raises:
            FileNotFoundError: If the Parquet file does not exist.
            ValueError: If the specified date field is not found in the Parquet file.
        """
        if not os.path.exists(self.parquet_file_path):
            logger.error(f"[x] Parquet file not found: {self.parquet_file_path}")
            raise FileNotFoundError(f"Parquet file not found: {self.parquet_file_path}")

        # Read the table
        self.table = pq.read_table(self.parquet_file_path)
        logger.info(f"[i] Successfully read Parquet file: {self.parquet_file_path}")

        # Verify that the date field exists in the table
        if self.date_field_name not in self.table.column_names:
            logger.error(f"[x] Date field '{self.date_field_name}' not found in the Parquet file.")
            raise ValueError(f"Date field '{self.date_field_name}' not found in the Parquet file.")

    def add_epoch_column(self):
        """Adds an '_epoch' column to the table based on the specified date field.

        - Checks if the '_epoch' column already exists.
        - Converts the date field to epoch timestamps.
        - Adds the '_epoch' column to the table.

        Raises:
            ValueError: If the timestamp unit is unsupported.
            TypeError: If the date field is not of a supported type.
            Exception: For any other errors during processing.
        """
        # Check if '_epoch' column exists
        if '_epoch' in self.table.column_names:
            logger.info("[i] The '_epoch' column already exists. Skipping addition.")
            return  # Skip adding the '_epoch' column

        date_column = self.table[self.date_field_name]
        date_type = date_column.type

        try:
            # Handle different date field types
            if pa.types.is_timestamp(date_type):
                # Convert timestamp to epoch in seconds
                unit = date_type.unit
                timestamp_array = date_column.cast(pa.int64())
                if unit == 's':
                    epoch_timestamps = timestamp_array.to_pylist()
                elif unit == 'ms':
                    epoch_timestamps = [int(ts / 1_000) for ts in timestamp_array.to_pylist()]
                elif unit == 'us':
                    epoch_timestamps = [int(ts / 1_000_000) for ts in timestamp_array.to_pylist()]
                elif unit == 'ns':
                    epoch_timestamps = [int(ts / 1_000_000_000) for ts in timestamp_array.to_pylist()]
                else:
                    logger.error(f"[x] Unsupported timestamp unit: {unit}")
                    raise ValueError(f"Unsupported timestamp unit: {unit}")

            elif pa.types.is_integer(date_type) or pa.types.is_floating(date_type):
                # Assume integers/floats are epoch timestamps
                epoch_timestamps = date_column.cast(pa.int64()).to_pylist()

            elif pa.types.is_string(date_type) or pa.types.is_large_string(date_type):
                date_strings = date_column.to_pylist()
                epoch_timestamps = [_parse_date_to_epoch(s) for s in date_strings]

            else:
                logger.error(
                    f"[x] Field '{self.date_field_name}' must be of string, timestamp, integer, or float type."
                )
                raise TypeError(
                    f"Field '{self.date_field_name}' must be of string, timestamp, integer, or float type."
                )

            # Add the '_epoch' column
            epoch_column = pa.array(epoch_timestamps, type=pa.int64())
            self.table = self.table.append_column('_epoch', epoch_column)
            logger.info("[i] Added '_epoch' column to the table.")

        except Exception as e:
            logger.error(f"[x] Error processing date field '{self.date_field_name}': {e}")
            raise

    def write_parquet(self, output_file_path=None):
        """Writes the updated table back to a Parquet file.

        Args:
            output_file_path (str, optional): The output file path. If None, overwrites the original file.
        """
        if output_file_path is None:
            output_file_path = self.parquet_file_path  # Overwrite the original file
        pq.write_table(self.table, output_file_path)
        logger.info(f"[i] Parquet file written to: {output_file_path}")

    def process(self, output_file_path=None):
        """Processes the Parquet file by reading it, adding the '_epoch' column, and writing it back.

        Args:
            output_file_path (str, optional): The output file path. If None, overwrites the original file.
        """
        self.read_parquet()
        self.add_epoch_column()
        self.write_parquet(output_file_path=output_file_path)


# Example usage as a standalone script
if __name__ == '__main__':
    import argparse

    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='Add an _epoch column to a Parquet file.')
    parser.add_argument('parquet_file', help='Path to the Parquet file.')
    parser.add_argument('date_field', help='Name of the date field to convert.')
    args = parser.parse_args()

    adder = ParquetEpochAdder(args.parquet_file, args.date_field)
    adder.process(output_file_path=args.parquet_file)
