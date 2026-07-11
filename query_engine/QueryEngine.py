#!/usr/bin/env python3

import asyncio
import logging
import aiosqlite
import pandas as pd
import uuid
import time
import sys
import os
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from .Alert import email_results
from functionality.cron_compat import linux_dow_to_apscheduler

# Add current directory to PYTHONPATH and import custom classes
current_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_script_dir)

# Adjust the project root path
project_root = os.path.abspath(os.path.join(current_script_dir, '..'))
sys.path.append(project_root)

try:
    # The diagnostics variant distinguishes "query produced zero rows"
    # from "query failed" - the scheduler logs the two differently.
    from CmdExecutionBackend import process_query_with_diagnostics
    from functionality.ParquetEpochAdder import ParquetEpochAdder  # Import ParquetEpochAdder
except Exception as e:
    raise e

# Reuse the logger configuration
logger = logging.getLogger(__name__)

# Retry parameters
MAX_RETRIES = 3
BACKOFF_FACTOR = 2  # Exponential backoff factor

# File output and database locations
SEARCHES_DB = f'{current_script_dir}/../saved_searches.db'
HISTORY_DB = f'{current_script_dir}/../saved_search_history.db'
RESULTS_DIR = Path(f'{current_script_dir}/../executed_scheduled_searches')
RESULTS_DIR.mkdir(parents=True, exist_ok=True)  # Ensure the results directory exists or create it

# APScheduler instance.
#
# H-CE-2: job_defaults pin the misfire / coalesce / max-instances behaviour
# explicitly. Without these, APScheduler silently drops missed cron fires
# (server pause, event-loop backpressure, restart past a cron minute) with
# only a DEBUG log - the operator sees nothing in saved-search history.
# ScheduledInputEngine's BackgroundScheduler already carries the same trio;
# this brings the saved-search scheduler to parity.
#
#   - ``coalesce=True``             collapse multiple pending fires into one
#   - ``max_instances=1``           prevent overlap for long-running queries
#   - ``misfire_grace_time=300``    still fire if we're within 5 minutes
scheduler = AsyncIOScheduler(
    timezone="UTC",
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    },
)


# L-CE-13 (2026-04-22): reusable hook to reconcile the batch-poller job
# with current settings. Previously the poller was registered ONCE at
# startup; disabling ``claude_analyzer_enable_batch`` at runtime left
# the job running (wasted CPU polling) until the next server restart.
# Call this from both startup (``register_saved_searches``) AND any
# settings-update endpoint that touches the batch-mode toggle.
BATCH_POLLER_JOB_ID = "batch_poller"


def sync_batch_poller_job(sched=None) -> None:
    """Add or remove the batch-poller job to match current settings.

    Idempotent. Safe to call repeatedly. The scheduler argument defaults
    to the module-level singleton so callers outside this module don't
    need to import it.
    """
    try:
        from global_settings import get_settings
        settings = get_settings()
        enabled = (
            settings.get("claude_analyzer_enabled")
            and settings.get("claude_analyzer_enable_batch")
        )
    except Exception as exc:
        logger.warning(
            "[!] Could not read batch-poller settings (%s): %s",
            type(exc).__name__, exc,
        )
        return

    sched = sched or scheduler
    existing = None
    try:
        existing = sched.get_job(BATCH_POLLER_JOB_ID)
    except Exception:
        existing = None

    if enabled:
        try:
            from analyzers.batch_poller import poll_pending_batches
            poll_minutes = int(
                settings.get("claude_analyzer_batch_poll_interval_minutes") or 5
            )
        except Exception as exc:
            logger.warning("[!] Batch poller import failed (%s): %s", type(exc).__name__, exc)
            return
        if existing is None:
            sched.add_job(
                poll_pending_batches,
                "interval",
                minutes=poll_minutes,
                id=BATCH_POLLER_JOB_ID,
            )
            logger.info(
                "[i] Batch poller registered (interval: %d min).", poll_minutes,
            )
    else:
        if existing is not None:
            try:
                sched.remove_job(BATCH_POLLER_JOB_ID)
                logger.info(
                    "[i] Batch poller removed - claude_analyzer_enable_batch "
                    "is disabled."
                )
            except Exception as exc:
                logger.warning(
                    "[!] Could not remove batch poller (%s): %s",
                    type(exc).__name__, exc,
                )


# Function to recursively find all Parquet files in the indexes directory
def find_parquet_files(indexes_dir):
    parquet_files = []
    for root, dirs, files in os.walk(indexes_dir):
        for file in files:
            if file.endswith('.system4.system4.parquet'):
                file_path = os.path.join(root, file)
                parquet_files.append(file_path)
    return parquet_files


# Function to process Parquet files and add '_epoch' column
def process_parquet_files(parquet_files, date_field_name='timestamp'):
    for parquet_file in parquet_files:
        try:
            adder = ParquetEpochAdder(parquet_file, date_field_name)
            adder.process(output_file_path=parquet_file)  # Overwrite the original file
            logger.info(f"Processed Parquet file: {parquet_file}")
        except Exception as e:
            logger.error(f"Error processing Parquet file {parquet_file}: {str(e)}")


# Function to execute a task (which in this case is a query)
async def execute_query(task_id, query, title, retry_count=0, search_metadata=None):
    # Emit 'cron_fired' on first attempt only (not on retries) so the user
    # can SPQL-confirm every scheduled saved search actually triggered.
    if retry_count == 0:
        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="query_engine",
                event="cron_fired",
                message=f"cron triggered for saved search '{title}' (task_id={task_id})",
            )
        except Exception:
            pass
    try:
        logger.info(f"[i] Executing task {task_id}:\nattempt {retry_count + 1}\n{query}\n")

        # Capture execution start time
        execution_start_time = time.time()

        # Execute the query using the diagnostics variant so a
        # legitimately-empty result is distinguishable from a real
        # failure. The legacy ``process_query`` collapses both cases
        # into ``(None, None)`` - which made every quiet day land in
        # ``search_runs`` as ``status="error", error_message=
        # "process_query returned None"`` and left genuine failures
        # indistinguishable from dry sources. Caught 2026-07-01 via the
        # schedule report: four feeders showed all-null row counts and
        # the operator couldn't tell which (if any) were broken.
        result_df, _job_id, diagnostic = process_query_with_diagnostics(query)

        # Log the result for debugging
        if result_df is None:
            duration_ms = int((time.time() - execution_start_time) * 1000)
            if diagnostic and diagnostic.startswith("empty:"):
                logger.info(
                    f"[i] Task {task_id} - {title} produced zero rows "
                    f"({diagnostic}). Skipping save and telemetry."
                )
                _emit_search_run_log(
                    title, "empty",
                    row_count=0,
                    duration_ms=duration_ms,
                )
            else:
                logger.error(
                    f"[x] Task {task_id} - {title} failed: "
                    f"{diagnostic or 'process_query returned None'}. "
                    f"Skipping save and telemetry."
                )
                _emit_search_run_log(
                    title, "error",
                    duration_ms=duration_ms,
                    error_message=(diagnostic or "process_query returned None")[:500],
                )
            return

        # Check if the execution was successful
        if isinstance(result_df, pd.DataFrame) and not result_df.empty:
            execution_end_time = time.time()
            runtime = execution_end_time - execution_start_time
            _emit_search_run_log(
                title, "success",
                row_count=len(result_df),
                duration_ms=int(runtime * 1000),
            )

            # ── Claude analysis (optional post-processor) ────────
            claude_analysis = _run_claude_analysis(
                result_df, title, search_metadata, execution_start_time,
            )

            # ── Alert suppression via filter gate ────────────────
            alert_suppressed = False
            if claude_analysis is not None and not claude_analysis.filter_passed:
                alert_suppressed = True
                logger.info(
                    "[i] Alert suppressed for '%s' by analyzer filter gate "
                    "(answer: %s).", title, claude_analysis.filter_answer,
                )

            # Generate a unique filename for the results
            filename = f"{int(execution_start_time)}.{uuid.uuid4()}.system4.system4.parquet"
            saved_search_path = RESULTS_DIR / filename

            # Save the result dataframe to a Parquet file (efficient storage)
            try:
                result_df.to_parquet(saved_search_path, index=False, compression='gzip')
                logger.info(f"[i] Task {task_id} - {title} results saved to {saved_search_path}.")
            except Exception as e:
                logger.error(f"[x] Error saving results for task {task_id} - {title}: {str(e)}")
                raise e

            # Store telemetry in the history database
            try:
                await store_execution_telemetry(
                    task_id, title, runtime, execution_start_time, execution_end_time, saved_search_path, len(result_df)
                )
                logger.info(f"[i] Task {task_id} - {title} telemetry stored.")
            except Exception as e:
                logger.error(f"[x] Error storing telemetry for task {task_id} - {title}: {str(e)}")
                raise e

        else:
            _emit_search_run_log(
                title, "empty",
                row_count=0,
                duration_ms=int((time.time() - execution_start_time) * 1000),
            )
            raise Exception(f"No data returned from query {task_id} - {title}")

    except Exception as e:
        logger.error(f"[x] Task {task_id} - {title} encountered an error: {str(e)}")
        if retry_count < MAX_RETRIES:
            backoff_time = BACKOFF_FACTOR ** retry_count
            logger.warning(f"[!] Task {task_id} - {title} failed. Retrying in {backoff_time} seconds...")
            await asyncio.sleep(backoff_time)
            await execute_query(task_id, query, title, retry_count + 1, search_metadata)
        else:
            logger.error(f"[x] Task {task_id} - {title} failed after {MAX_RETRIES} attempts. Error: {str(e)}")
            _emit_search_run_log(
                title, "error",
                duration_ms=int((time.time() - execution_start_time) * 1000),
                error_message=str(e)[:500],
            )


def _emit_search_run_log(
    search_name: str,
    status: str,
    *,
    row_count: int | None = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
) -> None:
    """Emit one Parquet log row per scheduled search execution.

    Wrapped in a broad try so a log-writer hiccup never breaks the
    scheduler. The Parquet rows become queryable as
    ``index="indexes/logs/search_runs/*.parquet"``.
    """
    try:
        from functionality.log_writer import log_search_run
        log_search_run(
            search_name=search_name,
            status=status,
            row_count=row_count,
            duration_ms=duration_ms,
            error_message=error_message,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Claude analysis integration
# ---------------------------------------------------------------------------

def _run_claude_analysis(result_df, query_name, search_metadata, execution_start_time):
    """Run Claude analysis if enabled and an analyzer prompt is configured.

    This is a non-blocking post-processor.  On any failure it logs a
    warning and returns None - it never prevents the pipeline from
    saving results or sending alerts.

    Returns an AnalysisResult or None.
    """
    try:
        from global_settings import get_settings
        settings = get_settings()

        if not settings.get("claude_analyzer_enabled"):
            return None

        # Check if this saved search has an analyzer prompt assigned
        if not search_metadata:
            logger.info("[i] No search metadata for '%s'; skipping Claude analysis.", query_name)
            return None

        prompt_name = (search_metadata.get("analyzer_prompt") or "").strip()
        if not prompt_name:
            return None

        # Lazy-load the prompt store and analyzer to avoid import-time cost
        from analyzer_prompt_store import AnalyzerPromptStore
        from analyzers.claude_analyzer import (
            ClaudeAnalyzer,
            resolve_analyzer_prompt,
        )
        from analyzers.models import AnalyzerConfig

        # Load the prompt template
        prompt_store = AnalyzerPromptStore()
        prompt_store.initialize()
        try:
            prompt_record = prompt_store.get_prompt(prompt_name)
        except FileNotFoundError:
            logger.warning(
                "[!] Analyzer prompt '%s' not found for search '%s'; skipping.",
                prompt_name, query_name,
            )
            return None

        # Initialize persistent storage for results + budget
        from analyzers.storage import AnalyzerStorage
        storage = AnalyzerStorage()

        # Retrieve API key from credential vault (script_id=-1 = system creds)
        api_key = ""
        try:
            from scheduled_input_engine.credentials import CredentialVault
            vault_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "credentials.sqlite")
            vault = CredentialVault(vault_db, settings.get("credential_key_dir"))
            api_key = vault.retrieve(-1, "ANTHROPIC_API_KEY")
        except (KeyError, RuntimeError, Exception) as exc:
            logger.warning("[!] Could not retrieve analyzer API key from vault: %s", exc)

        # Build AnalyzerConfig from global settings
        config = AnalyzerConfig(
            api_key=api_key,
            model_primary=settings.get("claude_analyzer_model_primary"),
            model_triage=settings.get("claude_analyzer_model_triage"),
            max_output_tokens=settings.get("claude_analyzer_max_output_tokens"),
            max_input_rows=settings.get("claude_analyzer_max_input_rows"),
            enable_cache=settings.get("claude_analyzer_enable_cache"),
            enable_batch=settings.get("claude_analyzer_enable_batch"),
            daily_budget_cents=settings.get("claude_analyzer_daily_budget_cents"),
            spike_threshold_for_upgrade=settings.get("claude_analyzer_spike_threshold"),
            min_liquidity=settings.get("claude_analyzer_min_liquidity"),
            mv_truncate_limit=settings.get("claude_analyzer_mv_truncate_limit"),
        )

        # Resolve tokens in the prompt text
        from datetime import datetime
        execution_time = datetime.fromtimestamp(execution_start_time).isoformat()
        mv_limit = search_metadata.get("mv_truncate_limit", config.mv_truncate_limit)

        resolved_prompt = resolve_analyzer_prompt(
            prompt_text=prompt_record["prompt_text"],
            result_df=result_df,
            search_metadata=search_metadata,
            execution_time=execution_time,
            mv_truncate_limit=int(mv_limit),
        )

        # Convert rows for gate checks (list of dicts)
        results_list = result_df.to_dict(orient="records")

        # Run the analyzer
        boilerplate = settings.get("claude_analyzer_boilerplate_prompt") or ""
        analyzer = ClaudeAnalyzer(config, storage=storage)
        analysis = analyzer.analyze(
            query_name=query_name,
            results=results_list,
            result_df=result_df,
            system_prompt=resolved_prompt,
            boilerplate_prompt=boilerplate,
            search_metadata=search_metadata,
        )

        if analysis.status == "batch_pending":
            # Store the batch request with all context needed for deferred processing
            filter_enabled = search_metadata.get("analyzer_filter_enabled", False)
            filter_question = (search_metadata.get("analyzer_filter_question") or "").strip()
            try:
                storage.create_batch_request(
                    custom_id=analysis.batch_custom_id,
                    batch_id=analysis.batch_id,
                    search_name=query_name,
                    model=analysis.model_used,
                    system_prompt=resolved_prompt,
                    user_content="",  # Not stored - batch API already has it
                    search_metadata=search_metadata,
                    result_parquet_path="",  # Parquet path set by caller
                    filter_enabled=bool(filter_enabled),
                    filter_question=filter_question,
                )
                logger.info(
                    "[i] Batch request stored for '%s': batch_id=%s",
                    query_name, analysis.batch_id,
                )
            except Exception as batch_exc:
                logger.warning(
                    "[!] Failed to store batch request for '%s': %s",
                    query_name, batch_exc,
                )

        elif analysis.status == "analyzed":
            logger.info(
                "[i] Claude analysis complete for '%s': priority=%s, cost=%.2f cents",
                query_name, analysis.alert_priority, analysis.cost_cents,
            )

            # ── Filter gate (optional) ───────────────────────
            # If enabled, ask a boolean question against the analysis.
            # YES → send alert.  NO → suppress alert.
            # Default is disabled (always send).
            filter_enabled = search_metadata.get("analyzer_filter_enabled", False)
            filter_question = (search_metadata.get("analyzer_filter_question") or "").strip()
            if filter_enabled and filter_question:
                analysis = analyzer.evaluate_filter(analysis, filter_question)
                if analysis.filter_passed:
                    logger.info(
                        "[i] Filter gate PASSED for '%s' - alert will be sent.",
                        query_name,
                    )
                else:
                    logger.info(
                        "[i] Filter gate BLOCKED for '%s' - alert suppressed. "
                        "Answer: %s", query_name, analysis.filter_answer,
                    )

        elif analysis.status == "skipped":
            logger.info(
                "[i] Claude analysis skipped for '%s': %s",
                query_name, analysis.skip_reason,
            )
        elif analysis.status == "error":
            logger.warning(
                "[!] Claude analysis error for '%s': %s",
                query_name, analysis.error_message,
            )

        # Persist the analysis result (non-blocking)
        try:
            storage.store_result(query_name, execution_time, analysis)
        except Exception as store_exc:
            logger.warning("[!] Failed to persist analysis result: %s", store_exc)

        return analysis

    except Exception as exc:
        logger.warning("[!] Claude analysis failed (non-blocking) for '%s': %s", query_name, exc)
        return None


# Function to store telemetry in the history database
async def store_execution_telemetry(task_id, query_name, runtime, execution_start_time, execution_end_time, saved_search_path, original_result_count):
    try:
        async with aiosqlite.connect(HISTORY_DB) as db:
            await db.execute('''
                INSERT INTO execution_history (
                    saved_search_filename, runtime, execution_start_time, execution_end_time, 
                    query_name, saved_search_path, original_result_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                saved_search_path.name, runtime, execution_start_time, execution_end_time,
                query_name, str(saved_search_path), original_result_count
            ))
            await db.commit()
            logger.info(f"[i] Logged execution telemetry for task {task_id}.")

    except Exception as e:
        logger.error(f"[x] Error storing telemetry for task {task_id}: {str(e)}")


# Function to initialize the history database and ensure the required table exists
async def initialize_history_db():
    """Create execution_history if absent; migrate legacy single-column PK in place.

    H-CE-4 (2026-04-22): the original schema declared
    ``saved_search_filename TEXT PRIMARY KEY``. Two saved searches with the
    same filename in different folders (e.g. ``alpha/daily.yaml`` and
    ``beta/daily.yaml``, both filtered by ``saved_search_path.name`` at
    insert time) silently overwrote each other's telemetry. This is an
    unlikely-but-real collision - every existing caller happens to
    generate UUID-bearing filenames per run, but the schema allowed the
    silent overwrite and a future caller could trip it.

    Fix: composite primary key on ``(saved_search_filename,
    execution_start_time)`` so repeated runs are both preserved and the
    (rare) same-filename-different-folder case no longer collides at the
    same start time (each run has its own ``execution_start_time``).

    SQLite does not support ``ALTER TABLE ... DROP CONSTRAINT``, so the
    migration rebuilds the table: CREATE new, INSERT FROM old, DROP old,
    RENAME new → execution_history. Legacy rows are preserved.
    """
    try:
        async with aiosqlite.connect(HISTORY_DB) as db:
            # Ensure table exists (creates with the CURRENT composite-PK
            # schema for fresh installs).
            await db.execute('''
                CREATE TABLE IF NOT EXISTS execution_history (
                    saved_search_filename TEXT,
                    runtime REAL,
                    execution_start_time REAL,
                    execution_end_time REAL,
                    query_name TEXT,
                    saved_search_path TEXT,
                    original_result_count INTEGER,
                    PRIMARY KEY (saved_search_filename, execution_start_time)
                )
            ''')

            # Detect legacy single-column PK and migrate if needed. pk>0
            # on PRAGMA table_info indicates primary-key position; a
            # composite PK marks two columns with pk=1 and pk=2. A legacy
            # schema has pk=1 on filename only and pk=0 on every other
            # column.
            async with db.execute("PRAGMA table_info(execution_history)") as cur:
                cols = await cur.fetchall()
            # cols shape: [(cid, name, type, notnull, dflt_value, pk), ...]
            pk_cols = [c[1] for c in cols if c[5] > 0]
            if pk_cols == ["saved_search_filename"]:
                logger.warning(
                    "[!] execution_history: migrating legacy single-column "
                    "PRIMARY KEY → composite (saved_search_filename, "
                    "execution_start_time)."
                )
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute('''
                        CREATE TABLE execution_history_new (
                            saved_search_filename TEXT,
                            runtime REAL,
                            execution_start_time REAL,
                            execution_end_time REAL,
                            query_name TEXT,
                            saved_search_path TEXT,
                            original_result_count INTEGER,
                            PRIMARY KEY (saved_search_filename, execution_start_time)
                        )
                    ''')
                    # Copy columns that exist on both sides; COALESCE any
                    # NULL execution_start_time to a distinct fallback so
                    # legacy rows don't collide on (filename, NULL).
                    await db.execute('''
                        INSERT INTO execution_history_new (
                            saved_search_filename, runtime,
                            execution_start_time, execution_end_time,
                            query_name, saved_search_path,
                            original_result_count
                        )
                        SELECT
                            saved_search_filename, runtime,
                            COALESCE(execution_start_time, rowid * -1.0),
                            execution_end_time,
                            query_name, saved_search_path,
                            original_result_count
                        FROM execution_history
                    ''')
                    await db.execute("DROP TABLE execution_history")
                    await db.execute(
                        "ALTER TABLE execution_history_new "
                        "RENAME TO execution_history"
                    )
                    await db.commit()
                    logger.info(
                        "[i] execution_history: migration complete."
                    )
                except Exception as mig_exc:
                    await db.execute("ROLLBACK")
                    logger.error(
                        "[x] execution_history migration failed: %s", mig_exc,
                    )
                    raise

            await db.commit()
            logger.info("[i] Initialized history database and ensured the execution_history table exists.")
    except Exception as e:
        logger.error(f"[x] Error initializing the history database: {str(e)}")


# Function to initialize the saved searches database
async def initialize_saved_searches_db():
    """Ensure the saved_searches table exists in SEARCHES_DB."""
    try:
        async with aiosqlite.connect(SEARCHES_DB) as db:
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS saved_searches (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    description TEXT,
                    query TEXT,
                    cron_schedule TEXT,
                    trigger TEXT,
                    lookback TEXT,
                    throttle TEXT,
                    throttle_time_period TEXT,
                    throttle_by TEXT,
                    event_message TEXT,
                    send_email TEXT,
                    email_address TEXT,
                    email_content TEXT,
                    file_location TEXT,
                    owner_id INTEGER REFERENCES users(id)
                )
                '''
            )
            await db.commit()
            logger.info(
                "[i] Initialized saved searches database and ensured the saved_searches table exists."
            )
    except Exception as e:
        logger.error(f"[x] Error initializing the saved searches database: {str(e)}")


async def fetch_tasks():
    """Return scheduled-search task tuples ``(id, title, query, cron_schedule)``.

    Source of truth is ``saved_searches/*.yaml`` via ``SavedSearchStore``
    (the canonical YAML CRUD store used everywhere else in the app).
    The legacy ``saved_searches.db`` SQLite file is still on disk on most
    installs for back-compat but hasn't been populated by the UI since
    the YAML migration - reading it returned 0 rows, which silently
    disabled every saved-search cron AND left alert groups with no data
    to dispatch. Reading YAML fixes that; the SQLite path is kept only
    as a last-resort fallback so a user whose YAMLs are absent but whose
    SQLite DB still has rows can limp along until they migrate.
    """
    try:
        from saved_search_store import SavedSearchStore
        ss_store = SavedSearchStore()
        ss_store.initialize()
        searches = ss_store.list_searches()
    except Exception as exc:
        logger.error("[x] SavedSearchStore read failed: %s", exc)
        searches = []

    tasks: list[tuple] = []
    for s in searches:
        if s.get("disabled", False):
            continue
        name = (s.get("name") or "").strip()
        query = (s.get("query") or "").strip()
        cron_schedule = (s.get("cron_schedule") or "").strip()
        if not (name and query and cron_schedule):
            logger.warning(
                "[!] Skipping incomplete saved search (name=%r, has_query=%s, cron=%r)",
                name, bool(query), cron_schedule,
            )
            continue
        # task_id doubles as the APScheduler job id (must be unique str)
        # and as the YAML store lookup key - name fills both roles.
        tasks.append((name, name, query, cron_schedule))

    if tasks:
        logger.info(
            "[i] Retrieved %d saved search(es) from YAML store.", len(tasks),
        )
        return tasks

    # Fallback - try the legacy SQLite table (pre-YAML installs).
    try:
        async with aiosqlite.connect(SEARCHES_DB) as db:
            async with db.execute(
                "SELECT id, title, query, cron_schedule FROM saved_searches"
            ) as cursor:
                rows = await cursor.fetchall()
                if rows:
                    logger.warning(
                        "[!] YAML store had 0 saved searches; falling back to "
                        "legacy saved_searches.db (%d row(s)). Migrate via the UI.",
                        len(rows),
                    )
                    return list(rows)
    except Exception as exc:
        logger.error("[x] Legacy SQLite fallback failed: %s", exc)

    logger.info("[i] No saved searches configured (YAML + SQLite both empty).")
    return []


# Function to schedule tasks with APScheduler
async def schedule_tasks():
    tasks = await fetch_tasks()

    # Load full search metadata from YAML store so the analyzer has
    # access to analyzer_prompt, filter settings, email config, etc.
    try:
        from saved_search_store import SavedSearchStore
        ss_store = SavedSearchStore()
        ss_store.initialize()
    except Exception as exc:
        logger.warning("[!] Could not initialise SavedSearchStore: %s", exc)
        ss_store = None

    for task in tasks:
        task_id, title, query, cron_schedule = task

        # Attempt to load the full YAML metadata for this search
        search_metadata = None
        if ss_store:
            try:
                search_metadata = ss_store.get_search(title)
                logger.debug("[i] Loaded search metadata for '%s'", title)
            except FileNotFoundError:
                logger.debug(
                    "[i] No YAML metadata for '%s' (legacy SQLite-only task); "
                    "analyzer will be skipped.", title,
                )

        # Per-search timezone (added 2026-04-27). Empty / missing → "UTC"
        # so legacy YAMLs without the field keep their current behavior.
        # Mirrors the alert-group scheduler's per-AG timezone handling.
        tz_name = "UTC"
        if search_metadata:
            tz_name = (search_metadata.get("timezone") or "UTC").strip() or "UTC"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            logger.warning(
                "[!] Saved search '%s' has invalid timezone '%s' (%s); "
                "falling back to UTC.",
                title, tz_name, exc,
            )
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")
            tz_name = "UTC"

        scheduler.add_job(
            execute_query,
            CronTrigger.from_crontab(linux_dow_to_apscheduler(cron_schedule), timezone=tz),
            args=[task_id, query, title],
            kwargs={"search_metadata": search_metadata},
            id=str(task_id),
        )
        logger.info(
            "[i] Scheduled task %s - %s with cron: %s (tz=%s)",
            task_id, title, cron_schedule, tz_name,
        )

    # ── Register batch poller if batch mode is enabled ────────────
    try:
        sync_batch_poller_job(scheduler)
    except Exception as exc:
        logger.warning("[!] Could not register batch poller: %s", exc)

    # Note: alert group cron jobs are registered on the ScheduledInputEngine's
    # BackgroundScheduler by ``start_background_scheduling`` below, not here -
    # that way alert groups don't double-fire when both schedulers start in
    # the same process (which happens in Docker now that the Flask entrypoint
    # wires up both).

    # Start the scheduler
    scheduler.start()
    logger.info(
        "[i] Saved-search AsyncIOScheduler started with %d search(es).",
        len(scheduler.get_jobs()),
    )


# ─────────────────────────────────────────────────────────────────────
# In-process startup helper used by ``desktop_app/server.py`` (Docker /
# single-process deployments). Bare-metal ``run_all.sh`` still launches
# this file as its own process; that path hits ``main()`` below and works
# as before.
# ─────────────────────────────────────────────────────────────────────


def start_background_scheduling(background_scheduler=None):
    """Wire saved-search + alert-group cron schedulers into the current process.

    The QueryEngine historically ran as its own Python process and owned an
    ``AsyncIOScheduler`` that registered both saved-search executions and
    alert-group dispatches. Docker never starts that second process - only
    ``desktop_app/server.py`` - which meant saved searches and alert groups
    silently never auto-fired on any Docker deployment.

    This helper, called once from the Flask entrypoint, restores both:

    * **Saved-search executions** run on a dedicated asyncio event loop
      hosted on a daemon thread. The existing ``AsyncIOScheduler`` +
      ``execute_query`` coroutine plumbing is preserved verbatim - we just
      host it inside the Flask process instead of a separate one.
    * **Alert-group cron jobs** register on the ScheduledInputEngine's
      ``BackgroundScheduler`` (which is already running for ingestion). The
      alert-group callback is sync, so BackgroundScheduler works fine and
      we avoid a second asyncio loop for that path.

    ``background_scheduler`` should be the ScheduledInputEngine's
    ``_scheduler`` attribute. When ``None``, the alert-group registration
    is skipped with a warning (caller is expected to start the engine first).
    """
    import threading

    # ── (A) Saved-search scheduler on a dedicated asyncio loop ──────
    def _saved_search_loop():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(initialize_history_db())
            loop.run_until_complete(initialize_saved_searches_db())
            loop.run_until_complete(schedule_tasks())
            try:
                from functionality.log_writer import log_system_event
                log_system_event(
                    component="query_engine",
                    event="saved_search_scheduler_started",
                    message=f"AsyncIOScheduler started with "
                    f"{len(scheduler.get_jobs())} saved search(es)",
                )
            except Exception:
                pass
            loop.run_forever()
        except BaseException as exc:
            logger.error("[x] Saved-search scheduler crashed: %s", exc)

    t = threading.Thread(
        target=_saved_search_loop,
        name="saved-search-scheduler",
        daemon=True,
    )
    t.start()

    # ── (B) Alert group cron jobs on the BackgroundScheduler ────────
    if background_scheduler is None:
        logger.warning(
            "[!] start_background_scheduling(): no BackgroundScheduler "
            "supplied; alert groups will not auto-fire."
        )
        return t

    try:
        from alert_groups.scheduler import register_alert_group_jobs
        register_alert_group_jobs(background_scheduler)
        try:
            from functionality.log_writer import log_system_event
            ag_job_count = sum(
                1 for j in background_scheduler.get_jobs()
                if str(j.id).startswith("alert_group_")
            )
            log_system_event(
                component="alert_groups",
                event="scheduler_registered",
                message=f"Alert group scheduler registered with "
                        f"{ag_job_count} job(s) on BackgroundScheduler",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.warning("[!] Could not register alert group jobs: %s", exc)

    return t


# Main function to start everything
async def main():
    # Process Parquet files first
    indexes_dir = os.path.abspath(os.path.join(project_root, 'indexes'))
    parquet_files = find_parquet_files(indexes_dir)
    logger.info(f"Found {len(parquet_files)} Parquet files to process.")
    process_parquet_files(parquet_files, date_field_name='timestamp')  # Adjust 'timestamp' if needed

    await initialize_history_db()  # Initialize the history database
    await initialize_saved_searches_db()  # Ensure saved searches table exists
    await schedule_tasks()  # Schedule and run tasks

    # Run indefinitely
    while True:
        await asyncio.sleep(3600)  # Sleep for an hour and keep the loop alive


def crank_query_engine():
    try:
        asyncio.run(main())  # Start the main function with asyncio.run
    except (KeyboardInterrupt, SystemExit):
        logger.info("[i] Execution process terminated.")
        scheduler.shutdown()  # Properly shutdown the scheduler


if __name__ == "__main__":
    try:
        asyncio.run(main())  # Start the main function with asyncio.run
    except (KeyboardInterrupt, SystemExit):
        logger.info("[i] Execution process terminated.")
        scheduler.shutdown()  # Properly shutdown the scheduler
