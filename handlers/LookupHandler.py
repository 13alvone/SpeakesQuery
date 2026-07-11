#!/usr/bin/env python3
import pandas as pd
from defusedxml import ElementTree as ET
import yaml
import sqlite3
import logging
import os


class LookupHandler:
    def __init__(self, logger=None):
        """
        Initialize the DataLoader with optional logging.

        Args:
        logger (logging.Logger): Optional logger instance. If None, a default logger will be configured.
        """
        if logger is None:
            self.logger = logging.getLogger('DataLoader')
        else:
            self.logger = logger

    def load_data(self, file_path):
        """
        Load data from a file into a pandas DataFrame based on the file extension.
        Supports CSV, JSON, XML, YAML, and SQLite formats.

        Args:
        filename (str): The file path including the extension.

        Returns:
        pandas.DataFrame: Data loaded into DataFrame, or None if an error occurs.
        """
        if not os.path.exists(file_path):  # Check if the file exists
            self.logger.error("[x] File does not exist: {}".format(file_path))
            return None

        _, file_extension = os.path.splitext(file_path)  # Extract file extension
        file_extension = file_extension.lower()

        try:
            if file_extension == '.csv':
                return pd.read_csv(file_path)
            elif file_extension == '.json':
                return pd.read_json(file_path)
            elif file_extension == '.xml':
                tree = ET.parse(file_path)
                root = tree.getroot()
                data = [{child.tag: child.text for child in element} for element in root]
                return pd.DataFrame(data)
            elif file_extension == '.yaml' or file_extension == '.yml':
                with open(file_path, 'r') as file:
                    data = yaml.safe_load(file)
                return pd.DataFrame(data)
            elif file_extension == '.parquet':
                return pd.read_parquet(file_path)
            elif file_extension == '.sqlite' or file_extension == '.db':
                return self._load_sqlite_data(file_path)
            else:
                self.logger.error("[x] Unsupported file format: {}".format(file_extension))
                return None
        except Exception as e:
            self.logger.error("[x] Error loading data from {}: {}".format(file_path, str(e)))
            return None

    @staticmethod
    def _load_sqlite_data(filename):
        """
        Load data from all tables in an SQLite database file into a single pandas DataFrame.
        Columns are prefixed with the table name to prevent overlap.

        Args:
        filename (str): Path to the SQLite database file.

        Returns:
        pandas.DataFrame: Combined data from all tables, or None if an error occurs.
        """
        with sqlite3.connect(filename) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")  # Get all table names
            tables = cursor.fetchall()

            dfs = []  # Load all tables
            for table_name in tables:
                table_name = table_name[0]
                df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
                df.columns = [f"{table_name}_{col}" for col in df.columns]  # Prefix columns with table name
                dfs.append(df)


        if dfs:  # Combine all dataframes horizontally
            combined_df = pd.concat(dfs, axis=1)
            return combined_df
        else:
            return pd.DataFrame()
