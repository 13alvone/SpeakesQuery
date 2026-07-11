#!/usr/bin/env python3
"""Generate fake SQLite and Parquet data for testing."""

import argparse
import logging
import pandas as pd
import os
import json
from faker import Faker
import random
import uuid
from datetime import datetime, timedelta, timezone

# Set up logger
logger = logging.getLogger(__name__)

# Allowed datetime formats from the TimeParser class
datetime_formats = [
    "%Y-%m-%d %H:%M:%S.%f",     # YYYY-MM-DD HH:MM:SS.mmmmmm
    "%Y-%m-%d %H:%M:%S",        # YYYY-MM-DD HH:MM:SS
    "%m/%d/%Y %H:%M:%S.%f",     # MM/DD/YYYY HH:MM:SS.mmmmmm
    "%m/%d/%Y %H:%M:%S",        # MM/DD/YYYY HH:MM:SS
    "%m-%d-%Y %H:%M:%S.%f",     # MM-DD-YYYY HH:MM:SS.mmmmmm
    "%m-%d-%Y %H:%M:%S",        # MM-DD-YYYY HH:MM:SS
    "%m/%d/%Y",                 # MM/DD/YYYY
    "%m-%d-%Y",                 # MM-DD-YYYY
    "%m/%d/%y",                 # MM/DD/YY
    "%m-%d-%y",                 # MM-DD-YY
    "%d-%m-%Y %H:%M:%S.%f",     # DD-MM-YYYY HH:MM:SS.mmmmmm
    "%d-%m-%Y %H:%M:%S",        # DD-MM-YYYY HH:MM:SS
    "%d/%m/%Y %H:%M:%S.%f",     # DD/MM/YYYY HH:MM:SS.mmmmmm
    "%d/%m/%Y %H:%M:%S",        # DD/MM/YYYY HH:MM:SS
    "%Y/%m/%d %H:%M:%S.%f",     # YYYY/MM/DD HH:MM:SS.mmmmmm
    "%Y/%m/%d %H:%M:%S",        # YYYY/MM/DD HH:MM:SS
    "%Y-%m-%d",                 # YYYY-MM-DD
    "%Y-%m-%dT%H:%M:%S.%f",     # YYYY-MM-DDTHH:MM:SS.mmmmmm
    "%Y-%m-%dT%H:%M:%S",        # YYYY-MM-DDTHH:MM:SS
    "%B %d, %Y %H:%M:%S.%f",    # October 23, 2023 14:20:30.123456
    "%B %d, %Y %H:%M:%S",       # October 23, 2023 14:20:30
    "%d %B %Y %H:%M:%S.%f",     # 23 October 2023 14:20:30.123456
    "%d %B %Y %H:%M:%S",        # 23 October 2023 14:20:30
    "%m/%d/%Y %I:%M:%S %p",     # 10/23/2023 02:20:30 PM
    "%m-%d-%Y %I:%M:%S %p",     # 10-23-2023 02:20:30 PM
    "%Y%m%d%H%M%S",             # 20231023142030
    "%Y-W%W-%w %H:%M:%S.%f",    # 2023-W43-1 14:20:30.123456 (Monday as first day)
    "%Y-W%U-%w %H:%M:%S.%f",    # 2023-W42-7 14:20:30.123456 (Sunday as first day)
]


def generate_random_datetime_string():
    """Generate a datetime string in a random format."""
    fake = Faker()
    # Generate a random datetime
    random_datetime = fake.date_time_between(start_date="-5y", end_date="now", tzinfo=timezone.utc)
    random_format = random.choice(datetime_formats)  # Choose a random format
    return random_datetime.strftime(random_format)  # Return formatted datetime string


def generate_data(x, y):
    fake = Faker()
    headers = ['TIMESTAMP'] + [f'header_{i}' for i in range(y)]  # Include TIME column
    # Adjust data type choices to manage proportions
    base_column_types = ['integer', 'string']
    complex_column_types = ['array', 'dictionary']
    # Calculate the number of complex types based on the total number of fields
    total_fields = x * (y + 1)  # +1 for the TIMESTAMP column
    max_complex_fields = total_fields // 4  # 25% of total fields
    complex_fields_count = 0

    column_types = ['string']  # TIMESTAMP column is always string
    for _ in range(y):
        if complex_fields_count < max_complex_fields:
            col_type = random.choice(base_column_types + complex_column_types)
            if col_type in complex_column_types:
                complex_fields_count += x  # Assume each complex column contributes x complex fields
        else:
            col_type = random.choice(base_column_types)
        column_types.append(col_type)

    data = []

    for _ in range(x):
        row = [generate_random_datetime_string()]
        for col_type in column_types[1:]:  # Skip TIMESTAMP column
            if col_type == 'integer':
                row.append(random.randint(0, 1000))
            elif col_type == 'array':
                row.append(json.dumps([fake.word() for _ in range(random.randint(1, 5))]))
            elif col_type == 'dictionary':
                row.append(json.dumps({fake.word(): fake.word() for _ in range(random.randint(1, 3))}))
            else:  # 'string'
                row.append(fake.word())
        data.append(row)

    df = pd.DataFrame(data, columns=headers)
    for i, col_type in enumerate(column_types):
        df[headers[i]] = df[headers[i]].astype('int64' if col_type == 'integer' else 'str')

    return df


def write_data_to_parquet(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)  # Ensure the directory exists
    df.to_parquet(path, index=False, engine='pyarrow')
    full_path = os.path.abspath(path)
    logger.info(f"[i] Data written to {full_path}")


def main():
    parser = argparse.ArgumentParser(description='Generate fake data and write to Parquet files.')
    parser.add_argument('x', type=int, help='Number of rows for the table')
    parser.add_argument('y', type=int, help='Number of columns for the table')
    args = parser.parse_args()

    df = generate_data(args.x, args.y)  # Generate data
    parquet_path = f'../indexes/test_parquets/{uuid.uuid4()}.system4.system4.parquet'  # Define Parquet file path
    write_data_to_parquet(df, parquet_path)  # Write data to Parquet


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()
