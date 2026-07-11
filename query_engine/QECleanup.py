#!/usr/bin/env python3

import asyncio
import logging
from pathlib import Path

# Reuse the logger configuration from previous components
logger = logging.getLogger(__name__)

# Configuration for cleanup
RESULTS_DIR = Path("../executed_saved_searches")
RETENTION_LIMIT = 5  # Keep only the latest 5 results


async def cleanup_task_results():
    try:
        if not RESULTS_DIR.exists():
            logger.info(f"[i] Results directory {RESULTS_DIR} does not exist, skipping cleanup.")
            return

        # List all result files sorted by modified time
        files = sorted(RESULTS_DIR.iterdir(), key=lambda f: f.stat().st_mtime)

        # Determine how many files to delete
        files_to_delete = len(files) - RETENTION_LIMIT
        if files_to_delete > 0:
            logger.info(f"[i] Cleaning up {files_to_delete} old result files.")
            for file in files[:files_to_delete]:
                logger.info(f"[i] Deleting {file}")
                file.unlink()
        else:
            logger.info("[i] No old results to clean up.")

    except Exception as e:
        logger.error(f"[x] Error during cleanup: {str(e)}")


# Example integration in the main loop
async def periodic_cleanup(interval_seconds):
    while True:
        await cleanup_task_results()
        await asyncio.sleep(interval_seconds)

# Main event loop to demonstrate cleanup
if __name__ == "__main__":
    try:
        asyncio.run(periodic_cleanup(3600))  # Run cleanup every hour
    except (KeyboardInterrupt, SystemExit):
        logger.info("[i] Cleanup process terminated.")
