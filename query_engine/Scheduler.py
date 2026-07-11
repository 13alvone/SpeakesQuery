#!/usr/bin/env python3

import asyncio
import logging
import os
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiosqlite import connect

from functionality.cron_compat import linux_dow_to_apscheduler

logger = logging.getLogger(__name__)

current_script_dir = Path(__file__).resolve().parent
SEARCHES_DB = current_script_dir.parent / 'saved_searches.db'
HISTORY_DB = current_script_dir.parent / 'saved_search_history.db'


# Task fetching function
async def fetch_tasks():
    try:
        async with connect(SEARCHES_DB) as db:
            async with db.execute("SELECT id, query, cron_schedule FROM saved_searches") as cursor:
                tasks = await cursor.fetchall()
                logger.info(f"[i] Retrieved {len(tasks)} tasks from the saved_searches.db.")
                return tasks
    except Exception as e:
        logger.error(f"[x] Error fetching tasks from the database: {str(e)}")
        return []


# Task execution function
async def execute_task(task_id, query):
    try:
        logger.info(f"[i] Executing task {task_id} - {query}")

        # This assumes `CmdExecutionBackend.py` handles the actual execution of the query.
        proc = await asyncio.create_subprocess_exec(
            'python3', 'CmdExecutionBackend.py', query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            logger.info(f"[i] Task {task_id} completed successfully.")
        else:
            logger.error(f"[x] Task {task_id} failed with error: {stderr.decode().strip()}")

    except Exception as e:
        logger.error(f"[x] Error executing task {task_id}: {str(e)}")


# Task scheduling function
async def schedule_tasks():
    scheduler = AsyncIOScheduler()

    tasks = await fetch_tasks()
    for task_id, query, cron_schedule in tasks:
        scheduler.add_job(
            execute_task,
            CronTrigger.from_crontab(linux_dow_to_apscheduler(cron_schedule)),
            args=[task_id, query],
            id=str(task_id)
        )
        logger.info(f"[i] Scheduled task {task_id} with cron: {cron_schedule}")

    scheduler.start()
    logger.info("[i] Scheduler started. Awaiting tasks...")

    try:
        await asyncio.Event().wait()  # Keep the script running
    except (KeyboardInterrupt, SystemExit):
        pass


# Main event loop
if __name__ == "__main__":
    asyncio.run(schedule_tasks())
