"""
Scheduled Input Engine
──────────────────────
Single BackgroundScheduler on a daemon thread with a 4-worker thread pool.
Manages the full task lifecycle: scheduling, execution, retry, and telemetry.

Enhancements over the initial rewrite:
  - ParquetWriter for atomic writes + periodic compaction
  - CredentialVault for per-script API key encrypt/decrypt lifecycle
  - Combined maintenance job: compaction → cleanup → telemetry
  - GlobalSettings integration (configurable limits, intervals, timeouts)
  - Mandatory test gate via enhanced execute_test()
"""

import asyncio
import concurrent.futures
import logging
import threading
import time
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import JobLookupError

from functionality.cron_compat import linux_dow_to_apscheduler

from .store import ScheduledInputStore
from .executor import CodeExecutor
from .cleanup import cleanup_indexes, cleanup_logs
from .cache import get_cached_or_fetch, reset_budget, BudgetAwareRequests
from .subprocess_runner import run_in_subprocess
from .parquet_writer import ParquetWriter
from .credentials import CredentialVault
from ._redact import redact_credentials, redact_subprocess_output

logger = logging.getLogger(__name__)

INPUT_REPOS_ROOT = (Path(__file__).parent.parent / "input_repos").resolve()

MAX_RETRIES = 3
MAINTENANCE_JOB_ID = "maintenance_job"
EMBEDDING_SWEEPER_JOB_ID = "embedding_sweeper_job"


def _emit_ingestion_log(
    task_id,
    title: str,
    status: str,
    *,
    duration_ms: int | None = None,
    error_message: str | None = None,
    row_count: int | None = None,
    attempt: int | None = None,
    trust_level: str | None = None,
) -> None:
    """Append one ingestion row to ``indexes/logs/ingestion/``.

    Wrapped in a try so a log-writer hiccup never stops a task from being
    marked success/failure in the canonical SQLite telemetry table.
    """
    try:
        from functionality.log_writer import log_ingestion_run
        log_ingestion_run(
            task_id=task_id,
            title=title,
            status=status,
            duration_ms=duration_ms,
            error_message=error_message,
            row_count=row_count,
            attempt=attempt,
            trust_level=trust_level,
        )
    except Exception:
        pass


def _load_settings():
    """Import and return global settings.  Returns None on import failure."""
    try:
        from global_settings import get_settings
        return get_settings()
    except Exception:
        return None


class ScheduledInputEngine:
    """Singleton engine that manages all scheduled data ingestion."""

    # Phase 4 / Bet 4 slice 8a - failed-feeder patch drafter dedup cache.
    # Maps task_id → most-recent error_hash for which we've already
    # drafted a patch suggestion. Identical hash on a subsequent
    # failure means the script is failing the SAME way; no point
    # asking Claude for the same diff twice. The cache is process-
    # local; restarting the engine resets it. That's intentional -
    # each restart is a chance to surface the suggestion again in
    # case the operator missed the previous one.
    _patch_drafter_dedup: dict[str, str] = {}
    _patch_drafter_dedup_lock = threading.Lock()

    def __init__(self):
        self.store = ScheduledInputStore()
        self.store.initialize_databases()

        # Load settings
        self._settings = _load_settings()

        # Parquet writer
        indexes_dir = self._get_indexes_dir()
        target_mb = self._setting("max_parquet_file_mb", 128)
        self._writer = ParquetWriter(indexes_dir, target_file_mb=target_mb)

        # Credential vault
        key_dir = self._setting("credential_key_dir", "~/.speakes-query")
        creds_db = Path(__file__).parent.parent / "credentials.sqlite"
        self._vault = CredentialVault(creds_db, key_dir=key_dir)

        # Scheduler - forced to UTC so cron expressions are predictable
        # regardless of the Docker host's system TZ. Before 2026-04-21
        # this was the implicit tzlocal default; a container set to
        # US/Eastern fired `30 11 * * *` at 11:30 ET, which surprised
        # users who assumed UTC. The UI's /api/system/clock endpoint
        # surfaces the scheduler's TZ so there's no ambiguity. If you
        # need to schedule in a different TZ, keep the scheduler at UTC
        # and pass ``timezone=...`` on individual CronTrigger calls.
        self._scheduler = BackgroundScheduler(
            timezone="UTC",
            executors={
                "default": APThreadPoolExecutor(max_workers=4),
            },
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _setting(self, key: str, fallback):
        """Read a global setting, falling back to *fallback* if unavailable."""
        if self._settings is not None:
            try:
                return self._settings.get(key)
            except Exception:
                pass
        return fallback

    def _get_indexes_dir(self) -> Path:
        """Resolve the indexes directory from settings."""
        if self._settings is not None:
            try:
                return self._settings.indexes_dir()
            except Exception:
                pass
        return (Path(__file__).parent.parent / "indexes").resolve()

    def _get_logs_dir(self) -> Path:
        """Resolve the logs directory from settings."""
        if self._settings is not None:
            try:
                return self._settings.logs_dir()
            except Exception:
                pass
        return (Path(__file__).parent.parent / "indexes" / "logs").resolve()

    def _get_immutable_dir(self) -> Path:
        """Resolve the immutable namespace directory from settings.

        Wave 2 of Options Edge Brief (2026-04-26). This tree is excluded
        from BOTH the indexes/ and logs/ cleanup budgets so mtime-based
        eviction never deletes the trading record.
        """
        if self._settings is not None:
            try:
                return self._settings.immutable_dir()
            except Exception:
                pass
        return (Path(__file__).parent.parent / "indexes" / "IMMUTABLE").resolve()

    def _logs_relative_skip(self) -> list[str]:
        """Return top-level subdir names under indexes/ to exclude from main cleanup.

        When the logs tree is nested under indexes/ (the default:
        ``indexes/logs/``), its files would otherwise get counted - and
        potentially deleted - by the main indexes cleanup. Same applies to
        the immutable namespace at ``indexes/IMMUTABLE/`` (Wave 2 of
        Options Edge Brief, 2026-04-26) - that tree is explicitly
        protected from any garbage collection because it stores the
        long-horizon trading record. Return the relative first-segment of
        EACH protected tree so the main cleanup can skip them all.
        """
        # Seed the conventional names unconditionally so a failure in any
        # path lookup below fails SAFE (tree skipped) rather than open
        # (protected tree exposed to cleanup). Skipping a subdir that
        # does not exist is a no-op.
        skip: list[str] = ["logs", "IMMUTABLE"]
        try:
            indexes_dir = self._get_indexes_dir()
        except Exception:
            return skip
        for getter in (self._get_logs_dir, self._get_immutable_dir):
            try:
                d = getter()
            except Exception:
                continue
            try:
                if d.is_relative_to(indexes_dir):
                    rel = d.relative_to(indexes_dir)
                    if rel.parts and rel.parts[0] not in skip:
                        skip.append(rel.parts[0])
            except Exception:
                continue
        return skip

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _migrate_ag_picks_to_immutable(self) -> int:
        """One-shot file migration from logs/ag_picks/ to IMMUTABLE/ag_picks/.

        Wave 2 of Options Edge Brief (2026-04-26) moved the pick journal
        from the cleanup-budgeted ``indexes/logs/ag_picks/`` to the
        protected ``indexes/IMMUTABLE/ag_picks/``. Existing parquet files
        at the old path get physically moved on the next engine startup.
        Idempotent - running again with no files at the old path is a
        no-op. Files at the new path are never overwritten; if the same
        filename exists at both paths, the old file is left in place
        (and a warning is logged) so an operator can adjudicate.
        """
        try:
            old_dir = self._get_logs_dir() / "ag_picks"
            new_dir = self._get_immutable_dir() / "ag_picks"
        except Exception:
            return 0
        if not old_dir.exists():
            return 0
        old_files = [
            f for f in old_dir.iterdir()
            if f.is_file() and f.suffix == ".parquet"
        ]
        if not old_files:
            return 0
        new_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for src in old_files:
            dst = new_dir / src.name
            if dst.exists():
                logger.warning(
                    "[!] ag_picks migration: %s already exists at IMMUTABLE/, "
                    "leaving %s in place for manual adjudication",
                    dst.name, src,
                )
                continue
            try:
                src.rename(dst)
                moved += 1
            except OSError as exc:
                logger.warning(
                    "[!] ag_picks migration: could not move %s -> %s: %s",
                    src, dst, exc,
                )
        if moved:
            logger.info(
                "[i] ag_picks migration: moved %d parquet file(s) from "
                "logs/ag_picks/ to IMMUTABLE/ag_picks/",
                moved,
            )
        return moved

    def start(self):
        """Load all jobs from DB, schedule maintenance, start the scheduler."""
        self._check_optional_deps()
        # Wave 2 of Options Edge Brief - relocate the pick journal to
        # the protected immutable namespace if any legacy files remain.
        try:
            self._migrate_ag_picks_to_immutable()
        except Exception as exc:
            logger.warning("[!] ag_picks migration failed: %s", exc)
        self._load_all_jobs()
        self._schedule_maintenance()
        self._schedule_embedding_sweep()
        self._scheduler.start()
        job_count = len(self._scheduler.get_jobs())
        logger.info("[i] Scheduler started with %d jobs", job_count)
        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="scheduled_input_engine",
                event="start",
                message=f"Scheduler started with {job_count} jobs",
            )
        except Exception:
            pass

    @staticmethod
    def _check_optional_deps():
        """Log warnings for optional but recommended packages."""
        for pkg, install in [("bs4", "beautifulsoup4"), ("lxml", "lxml")]:
            try:
                __import__(pkg)
            except ImportError:
                logger.warning(
                    "[!] Optional package '%s' not installed. "
                    "Web scraping scripts will fail. Install with: pip install %s",
                    pkg, install,
                )

    def shutdown(self):
        """Gracefully stop the scheduler, allowing in-flight jobs to finish."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("[i] Scheduler shut down")
            try:
                from functionality.log_writer import (
                    log_system_event, flush_all,
                )
                log_system_event(
                    component="scheduled_input_engine",
                    event="shutdown",
                    message="Scheduler shut down cleanly",
                )
                flush_all()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Job loading
    # ------------------------------------------------------------------

    def _load_all_jobs(self):
        """Load scheduled inputs and repo scripts from the database."""
        for task in self.store.list_scheduled_inputs(enabled_only=True):
            self._add_job_for_task(task)

        for script in self.store.list_repo_scripts(active_only=True):
            self._add_job_for_repo_script(script)

    def _add_job_for_task(self, task):
        """Register a scheduled input as an APScheduler job."""
        self._scheduler.add_job(
            self._run_task,
            CronTrigger.from_crontab(linux_dow_to_apscheduler(task["cron_schedule"])),
            args=[task],
            id=f"si_{task['id']}",
            name=task.get("title", f"si_{task['id']}"),
            replace_existing=True,
        )

    def _add_job_for_repo_script(self, script):
        """Register a repo script as an APScheduler job."""
        self._scheduler.add_job(
            self._run_repo_script,
            CronTrigger.from_crontab(linux_dow_to_apscheduler(script["cron_schedule"])),
            args=[script],
            id=f"repo_{script['id']}",
            name=script.get("script_name", f"repo_{script['id']}"),
            replace_existing=True,
        )

    def _schedule_maintenance(self):
        """Schedule the combined compaction → cleanup → telemetry job."""
        interval_hours = self._setting("cleanup_interval_hours", 6)
        # Enforce floor of 1 hour
        interval_hours = max(1, interval_hours)

        self._scheduler.add_job(
            self._run_maintenance,
            IntervalTrigger(hours=interval_hours),
            id=MAINTENANCE_JOB_ID,
            name="maintenance_compaction_cleanup",
            replace_existing=True,
        )
        logger.info("[i] Maintenance job scheduled every %d hours", interval_hours)

    def _schedule_embedding_sweep(self):
        """Schedule the embedding sweeper (Phase 1 / Bet 2 slice 5).

        Gated by ``embeddings_enabled`` - when false, no job is registered
        and any prior job is removed (operator may have flipped the switch
        off; we honour it on next engine boot).

        When true, the sweeper runs every ``embedding_sweep_interval_minutes``
        and walks ``indexes/`` for source parquets without a current
        sidecar (or with stale model_name / dim metadata), embedding new
        rows in batch. Failures in one source don't stop the rest;
        per-source telemetry lands in ``indexes/logs/system/``.
        """
        enabled = bool(self._setting("embeddings_enabled", False))
        if not enabled:
            # Belt-and-suspenders: drop any pre-existing job so a flip
            # from on→off in Settings actually stops the sweeper without
            # a process restart. Mirrors the alert-group disabled-job
            # cleanup pattern.
            try:
                if self._scheduler.get_job(EMBEDDING_SWEEPER_JOB_ID):
                    self._scheduler.remove_job(EMBEDDING_SWEEPER_JOB_ID)
                    logger.info(
                        "[i] embeddings_enabled=False; removed prior "
                        "embedding sweeper job"
                    )
            except Exception:
                pass
            return

        interval_min = int(self._setting("embedding_sweep_interval_minutes", 15))
        interval_min = max(1, min(interval_min, 1440))

        self._scheduler.add_job(
            self._run_embedding_sweep,
            IntervalTrigger(minutes=interval_min),
            id=EMBEDDING_SWEEPER_JOB_ID,
            name="embedding_sweeper",
            replace_existing=True,
        )
        logger.info(
            "[i] Embedding sweeper scheduled every %d minutes", interval_min,
        )

    def _run_embedding_sweep(self) -> None:
        """Execute one sweep pass; never raises back to the scheduler."""
        try:
            from functionality.embedding_sweeper import EmbeddingSweeper
            indexes_dir = self._get_indexes_dir()
            sweeper = EmbeddingSweeper(indexes_dir)
            sweeper.sweep_once()
        except Exception as exc:
            # The sweeper already logs per-source failures internally;
            # this catches the rare case where sweep_once itself raises
            # (unimported deps, misconfigured paths, etc.) so the
            # APScheduler thread doesn't die.
            logger.error("[x] Embedding sweep failed: %s", exc)

    # ------------------------------------------------------------------
    # Task execution (runs in thread pool)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase 4 / Bet 4 slice 8a - failed-feeder patch drafter dispatch
    # ------------------------------------------------------------------
    def _maybe_dispatch_patch_drafter(
        self,
        task: dict,
        title: str,
        error_message: str,
    ) -> None:
        """Fire-and-forget patch-drafter dispatch on a task failure.

        Gated by ``patch_drafter_enabled`` (default False - opt-in).
        Deduplicated per-task by error_message hash so a script that
        fails identically every cron tick produces ONE patch
        suggestion per error variant, not N.

        The drafter call runs on a daemon thread so the worker thread
        is freed immediately. The result lands in the
        ``patch_suggestions`` Parquet log via
        ``log_patch_suggestion(...)`` - operators read it via SPQL
        (``index="indexes/logs/patch_suggestions/*"``). Slice 8b will
        add UI surfacing on top of this log.

        Errors here NEVER bubble back to the caller. The engine's
        primary job is to record the failure; surfacing a patch
        suggestion is a value-add that must not destabilise the
        ingestion pipeline.
        """
        try:
            if not self._setting("patch_drafter_enabled", False):
                return
            try:
                from analyzers.patch_drafter import compute_error_hash
            except Exception as exc:
                logger.warning(
                    "[!] patch_drafter unavailable; skipping dispatch: %s", exc,
                )
                return

            task_id = task.get("id")
            error_hash = compute_error_hash(error_message)

            # Dedup by per-task last-suggested error hash. A script
            # failing the SAME way each tick should not flood Claude.
            cache_key = str(task_id)
            with self._patch_drafter_dedup_lock:
                last = self._patch_drafter_dedup.get(cache_key)
                if last == error_hash:
                    logger.debug(
                        "[i] patch drafter skipped - task %s, identical "
                        "error_hash=%s already suggested",
                        task_id, error_hash,
                    )
                    return
                # Reserve the slot BEFORE dispatching so a fast-
                # repeating failure doesn't double-fire while the
                # background thread is in flight.
                self._patch_drafter_dedup[cache_key] = error_hash

            script_source = task.get("code", "") or ""
            script_title = title or ""

            def _runner():
                try:
                    from analyzers.patch_drafter import (
                        draft_patch_for_failed_task,
                    )
                    from functionality.log_writer import log_patch_suggestion

                    result = draft_patch_for_failed_task(
                        script_source=script_source,
                        error_message=error_message,
                        script_title=script_title,
                        task_id=task_id,
                    )
                    log_patch_suggestion(
                        task_id=task_id,
                        title=script_title,
                        error_hash=error_hash,
                        status=result.status,
                        model=result.model,
                        cost_usd=result.cost_usd,
                        latency_ms=result.latency_ms,
                        patch=result.patch,
                        explanation=result.explanation,
                        request_id=result.request_id,
                        error_message=error_message[:1000] if error_message else "",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        drafter_error_class=result.error_class,
                        drafter_error_message=result.error_message,
                    )
                    logger.info(
                        "[i] patch drafter - task %s '%s' status=%s "
                        "cost=$%.4f latency=%dms",
                        task_id, script_title, result.status,
                        result.cost_usd, result.latency_ms,
                    )
                except Exception as exc:
                    logger.warning(
                        "[!] patch drafter dispatch failed for task %s: %s",
                        task_id, exc,
                    )

            t = threading.Thread(
                target=_runner,
                name=f"patch-drafter-task-{task_id}",
                daemon=True,
            )
            t.start()
        except Exception as exc:
            # Belt-and-suspenders: any failure in the dispatch path
            # MUST NOT propagate back into the engine's failure path.
            logger.warning(
                "[!] patch drafter dispatch error (suppressed): %s", exc,
            )

    def _run_task(self, task):
        """Execute a scheduled input with retry logic and credential injection."""
        task_id = task["id"]
        title = task.get("title", str(task_id))
        overwrite = bool(task.get("overwrite"))
        subdirectory = task.get("subdirectory", "")
        max_retries = self._setting("max_retries", MAX_RETRIES)
        # Per-task timeout_seconds wins over the global default. Added
        # 2026-04-23 so long-running scripts (options_unusual_activity_pro
        # with 10 tickers × Yahoo pacing + Black-Scholes greeks) can
        # have a generous allowance without forcing the operator to
        # raise the global default for every short-run script.
        global_timeout = self._setting("default_script_timeout_seconds", 600)
        per_task_timeout = task.get("timeout_seconds")
        script_timeout = (
            int(per_task_timeout) if per_task_timeout else global_timeout
        )
        max_output_rows = self._setting("max_output_rows", 500_000)
        max_requests = self._setting("max_requests_per_execution", 50)
        max_response_mb = self._setting("max_response_size_mb", 10)
        allowed_domains = self._setting("allowed_api_domains", [])
        timestamp_fields = None
        if task.get("timestamp_fields"):
            timestamp_fields = task["timestamp_fields"]

        # H-SV-1: resolve the effective trust tier once and log when we fell
        # back to the default. A legacy DB row or a manual edit that leaves
        # ``trust_level`` NULL / empty would otherwise silently run a pro
        # script in the sandbox (obscure import errors downstream) or
        # vice-versa. The `store._init_inputs_db` migration backfills
        # existing rows; this log catches anything that slips through at
        # run time.
        raw_trust = task.get("trust_level")
        effective_trust = raw_trust or "sandboxed"
        if not raw_trust:
            logger.warning(
                "[!] Task %s ('%s') has no explicit trust_level; defaulting to 'sandboxed'. "
                "Verify this is intentional - set the field in the task record to silence.",
                task_id, title,
            )

        # Decrypt credentials for this script
        creds = None
        try:
            creds = self._vault.decrypt_for_script(task_id)
        except Exception as exc:
            logger.warning("[!] Could not load credentials for task %s: %s", task_id, exc)

        for attempt in range(max_retries + 1):
            start = time.monotonic()
            try:
                logger.info(
                    "[i] Executing task %s '%s' (attempt %d)",
                    task_id, title, attempt + 1,
                )
                executor = CodeExecutor(
                    task["code"],
                    timestamp_fields=timestamp_fields,
                    trust_level=effective_trust,
                )

                # Reset per-execution resource budgets + domain allowlist
                reset_budget(
                    max_requests=max_requests,
                    max_response_mb=max_response_mb,
                    allowed_domains=allowed_domains,
                )

                # Build extra globals with cache helper, credentials, and modules
                # Inject budget-aware requests proxy so direct requests.get() calls
                # also count against the budget
                extra = {
                    "get_cached_or_fetch": get_cached_or_fetch,
                    "requests": BudgetAwareRequests(),
                }
                if creds is not None:
                    extra["CREDENTIALS"] = creds

                # Run script with wall-clock timeout enforcement.
                # M-CE-7 (2026-04-22): drop the CREDENTIALS entry out of
                # ``extra`` as soon as the script returns / fails. Without
                # this the dict lives in the local frame until ``extra``
                # itself falls out of scope - a window that can be minutes
                # long for a retry-looping task, and the dict is reachable
                # via gc / frame inspection for the entire retry loop.
                # The ``try/finally`` ensures the pop runs on every exit
                # path (timeout, exception, successful return).
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(executor.execute, extra_globals=extra)
                        try:
                            result_df = future.result(timeout=script_timeout)
                        except concurrent.futures.TimeoutError:
                            raise RuntimeError(
                                f"Script exceeded {script_timeout}s timeout"
                            )
                finally:
                    extra.pop("CREDENTIALS", None)

                # Enforce per-execution row cap
                if len(result_df) > max_output_rows:
                    logger.warning(
                        "[!] Task %s output %d rows, capping to %d",
                        task_id, len(result_df), max_output_rows,
                    )
                    result_df = result_df.head(max_output_rows)

                # Write via ParquetWriter (atomic)
                self._writer.write_atomic(
                    result_df,
                    subdirectory=subdirectory,
                    filename=executor.output_path,
                    overwrite=overwrite,
                )

                elapsed = time.monotonic() - start
                self.store.record_execution(
                    task_id, title, elapsed, "success", attempt=attempt
                )
                _emit_ingestion_log(
                    task_id, title, "success",
                    duration_ms=int(elapsed * 1000),
                    row_count=len(result_df),
                    attempt=attempt + 1,
                    trust_level=effective_trust,
                )
                logger.info("[i] Task %s '%s' completed in %.2fs", task_id, title, elapsed)
                return

            except (ValueError, SyntaxError) as exc:
                # Non-retryable errors (bad code, bad config)
                elapsed = time.monotonic() - start
                # H-SV-2: scrub credential values out of the exception message
                # before it lands in Parquet telemetry / SQLite history /
                # docker logs. KeyError({'GITHUB_TOKEN': 'ghp_...'}) and
                # friends otherwise exfiltrate secrets via the log pipeline.
                safe_msg = redact_credentials(str(exc), creds)
                self.store.record_execution(
                    task_id, title, elapsed, "failed", safe_msg, attempt=attempt
                )
                _emit_ingestion_log(
                    task_id, title, "error",
                    duration_ms=int(elapsed * 1000),
                    error_message=safe_msg[:500],
                    attempt=attempt + 1,
                    trust_level=effective_trust,
                )
                logger.error(
                    "[x] Task %s '%s' failed (non-retryable): %s",
                    task_id, title, safe_msg,
                )
                # Slice 8a: opt-in patch drafter dispatch on terminal failure.
                self._maybe_dispatch_patch_drafter(task, title, safe_msg)
                return

            except Exception as exc:
                elapsed = time.monotonic() - start
                # H-SV-2: scrub credential values out of exception text for
                # every log emission below, including the retry-path log.
                safe_msg = redact_credentials(str(exc), creds)
                if attempt < max_retries:
                    backoff = min(2 ** attempt, 60)
                    logger.warning(
                        "[!] Task %s '%s' failed (attempt %d), retrying in %ds: %s",
                        task_id, title, attempt + 1, backoff, safe_msg,
                    )
                    time.sleep(backoff)
                else:
                    self.store.record_execution(
                        task_id, title, elapsed, "failed", safe_msg, attempt=attempt
                    )
                    _emit_ingestion_log(
                        task_id, title, "error",
                        duration_ms=int(elapsed * 1000),
                        error_message=safe_msg[:500],
                        attempt=attempt + 1,
                        trust_level=effective_trust,
                    )
                    logger.error(
                        "[x] Task %s '%s' failed after %d attempts: %s",
                        task_id, title, max_retries + 1, safe_msg,
                    )
                    # Slice 8a: opt-in patch drafter dispatch on terminal
                    # failure (after all retries exhausted).
                    self._maybe_dispatch_patch_drafter(task, title, safe_msg)

        # M-CE-6 (2026-04-22): removed a dead ``finally: if attempt ==
        # max_retries or True: pass`` block that did nothing; Python GC
        # reclaims the local ``creds`` as the function returns. The
        # explicit ``del creds`` below serves as documentation that we
        # intentionally drop the reference before the function frame
        # unwinds.
        del creds

    def _run_repo_script(self, script):
        """Execute a repo script in an isolated subprocess."""
        script_id = script["id"]
        script_name = script["script_name"]
        repo_path = Path(script["repo_path"]).resolve()
        output_subdir = script.get("output_subdir", "")
        overwrite = bool(script.get("overwrite"))

        # Security: ensure repo is under input_repos/
        if not repo_path.is_relative_to(INPUT_REPOS_ROOT):
            logger.error("[x] Repo path %s outside allowed root %s", repo_path, INPUT_REPOS_ROOT)
            return

        script_path = (repo_path / script_name).resolve()
        if not script_path.is_relative_to(repo_path):
            logger.error("[x] Script path %s escapes repo root %s", script_path, repo_path)
            return

        start = time.monotonic()
        try:
            indexes_dir = self._get_indexes_dir()
            output_dir = indexes_dir
            if output_subdir:
                output_dir = indexes_dir / output_subdir
            output_dir.mkdir(parents=True, exist_ok=True)

            env = {
                "SPEAKESQUERY_OUTPUT_DIR": str(output_dir),
                "SPEAKESQUERY_OVERWRITE": "1" if overwrite else "0",
            }

            # Inject credentials as env vars for subprocess scripts. Keep a
            # dict-shaped reference (``creds_for_redact``) visible outside
            # the try so the stderr scrubber below can dereference it even
            # when vault access failed.
            creds_for_redact: dict = {}
            try:
                # M-SV-4 (2026-04-22): decrypt_for_script now returns
                # ``None`` when the script has no credentials registered
                # (vs. an empty Mapping for "all rows failed to decrypt").
                # Treat None as "skip env injection entirely"; the older
                # empty-Mapping branch still iterates cleanly (zero
                # items) and keeps ``creds_for_redact`` empty.
                creds = self._vault.decrypt_for_script(f"repo-{script_id}")
                if creds is not None:
                    # M-SV-8 (2026-04-22): detect case-only collisions
                    # BEFORE injecting env vars. ``SPEAKESQUERY_CRED_{k.upper()}``
                    # collapses ``api_key`` and ``API_KEY`` into the same
                    # key; the second write would silently stomp the first.
                    # Raise a clear error so the operator fixes the
                    # ambiguous credential set in the vault rather than
                    # debugging a subtle "wrong value used" bug.
                    upper_names = [str(k).upper() for k in creds.keys()]
                    if len(set(upper_names)) != len(upper_names):
                        seen: dict = {}
                        dup_groups = {}
                        for orig in creds.keys():
                            u = str(orig).upper()
                            seen.setdefault(u, []).append(orig)
                        dup_groups = {
                            u: names for u, names in seen.items()
                            if len(names) > 1
                        }
                        raise ValueError(
                            f"Credential name collision after case-"
                            f"normalisation for repo-script {script_id}: "
                            f"{dup_groups}. Env-var injection uses "
                            f"``SPEAKESQUERY_CRED_<KEY.upper()>`` as the "
                            f"binding name, so names that differ only in "
                            f"case silently stomp each other. Rename the "
                            f"colliding credentials in the vault."
                        )
                    for k, v in creds.items():
                        env[f"SPEAKESQUERY_CRED_{k.upper()}"] = v
                    creds_for_redact = dict(creds)
            except ValueError:
                # M-SV-8: propagate collision errors - they indicate a
                # configuration problem the operator must fix. The
                # outer except Exception below would otherwise swallow
                # them silently.
                raise
            except Exception:
                pass

            # Run async subprocess from sync thread
            timeout = self._setting("default_script_timeout_seconds", 600)
            result = asyncio.run(run_in_subprocess(script_path, timeout=timeout, env=env))

            elapsed = time.monotonic() - start
            status = "success" if result.returncode == 0 else "failed"
            # H-SV-3: scrub credential values and SPEAKESQUERY_CRED_<KEY>=<value>
            # env-dumps from subprocess stderr before it lands in
            # execution_history. Repo scripts are user-authored and may
            # accidentally log ``os.environ`` or pipe ``env`` to stderr;
            # without this scrub the creds travel straight into the SQLite
            # telemetry store.
            error_msg = None
            if result.returncode != 0 and result.stderr:
                error_msg = redact_subprocess_output(result.stderr, creds_for_redact)

            self.store.record_execution(
                f"repo-{script_id}", script_name, elapsed, status, error_msg
            )
        except FileNotFoundError:
            logger.error("[x] Repo script not found: %s", script_path)
        except Exception as exc:
            elapsed = time.monotonic() - start
            # Scrub creds out of the outer-except diagnostic too - creds may
            # have been populated before the failure.
            safe_msg = redact_subprocess_output(
                str(exc),
                locals().get("creds_for_redact") or {},
            )
            self.store.record_execution(
                f"repo-{script_id}", script_name, elapsed, "failed", safe_msg
            )
            logger.error("[x] Repo script %s failed: %s", script_name, safe_msg)

    # ------------------------------------------------------------------
    # Maintenance: compaction → cleanup → telemetry
    # ------------------------------------------------------------------

    def _run_maintenance(self):
        """Combined maintenance job: compact, then clean indexes, then clean logs."""
        cycle_start = time.monotonic()
        logger.info("[i] Starting maintenance cycle")
        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="scheduled_input_engine",
                event="maintenance_start",
                message="compaction + indexes cleanup + logs cleanup starting",
            )
        except Exception:
            pass

        # Flush any buffered log rows before cleanup so we don't count stale
        # buffer content but also don't lose new rows during the sweep.
        try:
            from functionality.log_writer import flush_all
            flush_all()
        except Exception:
            pass

        # Step 1: Compaction (indexes only - logs are small and not compacted)
        try:
            merged = self._writer.compact_all()
            if merged:
                logger.info("[i] Compaction removed %d small files", merged)
        except Exception as exc:
            logger.error("[x] Compaction failed: %s", exc)

        # Step 2: Cleanup indexes (exclude logs subtree if nested)
        try:
            skip = self._logs_relative_skip()
            deleted = cleanup_indexes(
                indexes_dir=self._get_indexes_dir(),
                skip_subdirs=skip,
            )
            for filepath, reason in deleted:
                self.store.record_deletion(filepath, reason)
        except Exception as exc:
            logger.error("[x] Cleanup failed: %s", exc)

        # Step 3: Cleanup logs (separate budget)
        try:
            deleted_logs = cleanup_logs(logs_dir=self._get_logs_dir())
            for filepath, reason in deleted_logs:
                self.store.record_deletion(filepath, reason)
        except Exception as exc:
            logger.error("[x] Logs cleanup failed: %s", exc)

        # Step 4: Cleanup embeddings sidecars (separate budget - Phase 1 slice 5).
        # Only enforced when embeddings_enabled, otherwise sidecars are
        # operator-managed (legacy data from a deactivated sweeper) and we
        # don't auto-evict. Caller can manually invoke cleanup_embeddings
        # via tools/embed_backfill.py --cleanup if needed.
        try:
            if self._setting("embeddings_enabled", False):
                from scheduled_input_engine.cleanup import cleanup_embeddings
                deleted_embed = cleanup_embeddings(indexes_dir=self._get_indexes_dir())
                for filepath, reason in deleted_embed:
                    self.store.record_deletion(filepath, reason)
        except Exception as exc:
            logger.error("[x] Embeddings cleanup failed: %s", exc)

        duration_ms = int((time.monotonic() - cycle_start) * 1000)
        logger.info("[i] Maintenance cycle complete in %dms", duration_ms)
        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="scheduled_input_engine",
                event="maintenance_complete",
                message=f"cycle completed in {duration_ms}ms",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # CRUD wrappers (sync scheduler state with DB)
    # ------------------------------------------------------------------

    def add_task(self, **kwargs):
        """Create a scheduled input and register it with the scheduler.

        Accepts ``timeout_seconds`` (optional) - per-task wall-clock
        cap that overrides the global ``default_script_timeout_seconds``.
        Library scripts carrying a ``suggested_timeout_seconds`` JSON
        hint auto-populate this on deploy.
        """
        task = self.store.add_scheduled_input(**kwargs)
        if not task.get("disabled"):
            self._add_job_for_task(task)
        logger.info("[i] Added task %s '%s'", task["id"], task["title"])
        return task

    def run_task_now(self, task_id):
        """Execute a scheduled input immediately, synchronously, and return
        the execution history row that was persisted.

        This is the real ingestion path (same code as the APScheduler
        trigger) - writes parquet, decrypts credentials, records the
        run. Used by:

          * the "Run Now" button on the Ingestion Scripts table
          * the "Run immediately after save" flow in POST /api/si/add
          * the /api/si/<id>/run endpoint for ad-hoc triggers

        Added 2026-04-23 so the operator can seed the parquet + schema
        at task creation time rather than waiting for the first cron
        tick (which could be hours away depending on the cadence).
        """
        task = self.store.get_scheduled_input(task_id)
        if task is None:
            raise ValueError(f"Scheduled input {task_id} not found.")
        # _run_task records execution_history itself, so after it
        # returns the store has a fresh row for this run. We then fetch
        # that row so the caller can report success/failure + runtime.
        self._run_task(task)
        return self.store.get_last_run(task_id)

    def update_task(self, task_id, **kwargs):
        """Update a scheduled input and re-sync the scheduler."""
        task = self.store.update_scheduled_input(task_id, **kwargs)
        job_id = f"si_{task_id}"
        if task.get("disabled"):
            try:
                self._scheduler.remove_job(job_id)
            except JobLookupError:
                pass
        else:
            self._add_job_for_task(task)
        logger.info("[i] Updated task %s", task_id)
        return task

    def delete_task(self, task_id):
        """Delete a scheduled input and remove it from the scheduler."""
        self.store.delete_scheduled_input(task_id)
        # Also clean up any stored credentials
        try:
            self._vault.delete(task_id)
        except Exception:
            pass
        try:
            self._scheduler.remove_job(f"si_{task_id}")
        except JobLookupError:
            pass
        logger.info("[i] Deleted task %s", task_id)

    # ------------------------------------------------------------------
    # Test endpoint
    # ------------------------------------------------------------------

    def test_task(self, code: str, task_id: int | None = None, **kwargs) -> dict:
        """Run the mandatory test gate for an ingestion script.

        Optionally injects stored credentials for *task_id* so the test runs
        with real API keys.  ``trust_level`` (default ``"sandboxed"``) selects
        between the RestrictedPython and plain-compile execution paths.
        """
        timestamp_fields = kwargs.get("timestamp_fields")
        trust_level = kwargs.get("trust_level") or "sandboxed"
        executor = CodeExecutor(
            code,
            test_mode=True,
            timestamp_fields=timestamp_fields,
            trust_level=trust_level,
        )

        # Apply budgets during test runs too
        max_requests = self._setting("max_requests_per_execution", 50)
        max_response_mb = self._setting("max_response_size_mb", 10)
        allowed_domains = self._setting("allowed_api_domains", [])
        reset_budget(
            max_requests=max_requests,
            max_response_mb=max_response_mb,
            allowed_domains=allowed_domains,
        )

        extra: dict = {
            "get_cached_or_fetch": get_cached_or_fetch,
            "requests": BudgetAwareRequests(),
        }
        creds = None
        if task_id is not None:
            try:
                creds = self._vault.decrypt_for_script(task_id)
                extra["CREDENTIALS"] = creds
            except Exception as exc:
                logger.warning("[!] Could not load credentials for test: %s", exc)

        try:
            # Enforce timeout on test runs too.
            # Prefer the per-task timeout when testing a saved task so
            # the Test button respects the operator's configured cap
            # (set via library ``suggested_timeout_seconds`` hint or
            # manual override in the edit form). Falls back to global
            # ``default_script_timeout_seconds`` when no per-task value
            # is set or when testing arbitrary code (no task_id).
            global_timeout = self._setting("default_script_timeout_seconds", 600)
            explicit_timeout = kwargs.get("timeout_seconds")
            if explicit_timeout:
                script_timeout = int(explicit_timeout)
            elif task_id is not None:
                try:
                    task_row = self.store.get_scheduled_input(task_id)
                    per_task = task_row.get("timeout_seconds")
                    script_timeout = int(per_task) if per_task else global_timeout
                except Exception:
                    script_timeout = global_timeout
            else:
                script_timeout = global_timeout
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(executor.execute_test, extra_globals=extra)
                try:
                    return future.result(timeout=script_timeout)
                except concurrent.futures.TimeoutError:
                    return {
                        "status": "fail",
                        "columns": [],
                        "row_count": 0,
                        "head": [],
                        "dtypes": {},
                        "has_epoch": False,
                        "epoch_source": None,
                        "errors": [
                            f"Script exceeded the {script_timeout}s timeout. "
                            f"Check for slow HTTP calls or infinite loops. "
                            f"Edit this task and raise 'Timeout (seconds)' "
                            f"if it legitimately needs more wall time."
                        ],
                        "duration_ms": script_timeout * 1000,
                    }
        finally:
            if creds is not None:
                del creds

    # ------------------------------------------------------------------
    # Credential management (exposed for API layer)
    # ------------------------------------------------------------------

    def store_credential(self, script_id: int, key_name: str, value: str) -> None:
        """Store an encrypted credential for a script."""
        self._vault.store(script_id, key_name, value)

    def delete_credential(self, script_id: int, key_name: str | None = None) -> int:
        """Delete credential(s) for a script."""
        return self._vault.delete(script_id, key_name)

    def list_credentials(self, script_id: int) -> list[str]:
        """List credential key names (never values) for a script.

        Returns the merged per-task + global key list - same semantics
        as ``decrypt_for_script``.
        """
        return self._vault.list_keys(script_id)

    def list_credentials_split(self, script_id: int) -> dict:
        """Return per-task and global credential keys separately so the
        UI can render each layer distinctly (Wave-7 followup,
        2026-04-26).

        Shape::

            {
              "per_script": ["FOO_API_KEY", ...],   # per-task only
              "global":     ["BAR_API_KEY", ...],   # globals available to this script
              "merged":     [...],                  # back-compat
            }
        """
        per_script = self._vault.list_keys(script_id, include_global=False)
        global_keys = self._vault.list_global_keys()
        merged = sorted(set(per_script) | set(global_keys))
        return {
            "per_script": per_script,
            "global": global_keys,
            "merged": merged,
        }

    def promote_credential_to_global(
        self, script_id: int, key_name: str,
    ) -> None:
        """Promote a per-task credential to the global vault.

        Decrypts the per-task value, re-stores it as a global, and
        deletes the per-task entry. Used by the "Make global" button on
        the Create Ingestion form so an operator can reuse a stored API
        key across all future scripts without re-typing the value.
        """
        self._vault.promote_to_global(script_id, key_name)

    def migrate_staging_credentials(self, target_script_id: int) -> int:
        """Move staging (script_id=0) credentials to the real script."""
        return self._vault.migrate_staging(target_script_id)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self):
        """Return status of all scheduler jobs for the UI."""
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": (
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
            })
        return jobs
