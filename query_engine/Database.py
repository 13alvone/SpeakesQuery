#!/usr/bin/env python3

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import aiosqlite
import pandas as pd

# Reuse the logger configuration from previous components
logger = logging.getLogger(__name__)

current_script_dir = Path(__file__).resolve().parent
SEARCHES_DB = current_script_dir.parent / 'saved_searches.db'
HISTORY_DB = current_script_dir.parent / 'saved_search_history.db'
RESULTS_DIR = current_script_dir.parent / 'executed_scheduled_searches'


# Custom function placeholder for executing query logic
def execute_query_logic(query):
    # This function is expected to return a pandas DataFrame
    # Implementation should be replaced with the actual function
    return pd.DataFrame()  # Placeholder return value


# Function to query the saved searches
async def query_saved_searches():
    try:
        async with aiosqlite.connect(SEARCHES_DB) as db:
            async with db.execute("SELECT id, title, query, cron_schedule, author FROM saved_searches") as cursor:
                searches = await cursor.fetchall()
                logger.info(f"[i] Retrieved {len(searches)} scheduled searches.")
                return searches
    except Exception as e:
        logger.error(f"[x] Error querying saved searches: {str(e)}")
        return []


# Function to store execution telemetry
async def store_execution_telemetry(metadata):
    try:
        async with aiosqlite.connect(HISTORY_DB) as db:
            await db.execute('''
                INSERT INTO execution_history (
                    saved_search_filename, runtime, execution_start_time, execution_end_time, 
                    expected_start_time, query_name, saved_search_path, original_result_count, author
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                metadata['saved_search_filename'], metadata['runtime'], metadata['execution_start_time'],
                metadata['execution_end_time'], metadata['expected_start_time'], metadata['query_name'],
                metadata['saved_search_path'], metadata['original_result_count'], metadata['author']
            ))
            await db.commit()
            logger.info(f"[i] Logged execution telemetry for {metadata['saved_search_filename']}.")
    except Exception as e:
        logger.error(f"[x] Error storing execution telemetry: {str(e)}")


# Function to execute a scheduled search
async def execute_search(search):
    try:
        search_id, title, query, cron_schedule, author = search

        # Expected start time (this would normally be derived from cron schedule)
        expected_start_time = time.time()

        logger.info(f"[i] Executing search '{title}' for {author}.")

        # Start execution
        execution_start_time = time.time()
        result_df = execute_query_logic(query)
        execution_end_time = time.time()

        # Generate unique filename
        filename = f"{int(execution_start_time)}.{uuid.uuid4()}.csv"
        saved_search_path = RESULTS_DIR / filename

        # Save result to file
        result_df.to_csv(saved_search_path, index=False)

        # Prepare metadata for telemetry
        metadata = {
            'saved_search_filename': filename,
            'runtime': execution_end_time - execution_start_time,
            'execution_start_time': execution_start_time,
            'execution_end_time': execution_end_time,
            'expected_start_time': expected_start_time,
            'query_name': title,
            'saved_search_path': str(saved_search_path),
            'original_result_count': len(result_df),
            'author': author
        }

        # Store telemetry
        await store_execution_telemetry(metadata)

    except Exception as e:
        logger.error(f"[x] Error executing search '{search.title}': {str(e)}")


# Function to periodically clean up old results
async def cleanup_old_results(retention_limit):
    try:
        if not RESULTS_DIR.exists():
            logger.info(f"[i] Results directory {RESULTS_DIR} does not exist, skipping cleanup.")
            return

        files = sorted(RESULTS_DIR.iterdir(), key=lambda f: f.stat().st_mtime)

        if len(files) > retention_limit:
            for file in files[:-retention_limit]:
                logger.info(f"[i] Deleting old result file {file}")
                file.unlink()
    except Exception as e:
        logger.error(f"[x] Error during cleanup: {str(e)}")


# Function to run the query and execution loop
async def periodic_query_and_execution(interval_seconds, retention_limit, cleanup_frequency):
    run_count = 0
    while True:
        searches = await query_saved_searches()
        for search in searches:
            await execute_search(search)

        run_count += 1
        if run_count % cleanup_frequency == 0:
            await cleanup_old_results(retention_limit)

        await asyncio.sleep(interval_seconds)


# Main event loop
if __name__ == "__main__":
    try:
        asyncio.run(periodic_query_and_execution(interval_seconds=60, retention_limit=5, cleanup_frequency=10))
    except (KeyboardInterrupt, SystemExit):
        logger.info("[i] Service terminated.")
