"""
Alert Group Scheduler
─────────────────────
Registers alert group jobs with the existing APScheduler instance.
"""

from __future__ import annotations

import logging

from apscheduler.triggers.cron import CronTrigger

from functionality.cron_compat import linux_dow_to_apscheduler

logger = logging.getLogger(__name__)


def _run_group_by_name(group_name: str):
    """Callback for APScheduler - dispatches a single alert group run.

    Wrapped in a defensive outer try/except so that **any** exception -
    from store load, dispatcher construction, or dispatcher internals -
    produces both a ``logs/alert_groups/*.parquet`` audit row AND a
    failure-alert email. Without this, a raised exception inside
    ``dispatcher.run()`` is caught by APScheduler's job runner and logged
    to stdout only - the user sees nothing on the Last Run pill and never
    receives a failure email, which is exactly the silent-failure mode
    that burned the user twice during the 2026-04-19 → 04-20 hardening.
    """
    import time
    from alert_groups.dispatcher import AlertGroupDispatcher
    from alert_groups.models import AlertGroupRunResult

    started = time.monotonic()

    # Emit a 'cron_fired' system event BEFORE any work happens so the user
    # can SPQL-confirm the scheduled job actually triggered, independent
    # of whether the dispatcher later succeeded or crashed. This closes a
    # blind spot from the 2026-04-20 debugging: if no log row appears for
    # a cron that should have fired, we now know it's the scheduler (not
    # the dispatcher) that failed.
    try:
        from functionality.log_writer import log_system_event
        log_system_event(
            component="alert_groups",
            event="cron_fired",
            message=f"cron triggered for '{group_name}'",
        )
    except Exception:
        pass

    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
    except Exception as exc:
        _emit_scheduler_failure(
            group_name, started,
            error=f"AlertGroupStore initialise failed: {exc}",
        )
        return

    try:
        group = store.get_group(group_name)
    except FileNotFoundError:
        logger.warning(
            "[!] Scheduled alert group '%s' no longer exists; skipping.", group_name
        )
        return
    except Exception as exc:
        _emit_scheduler_failure(
            group_name, started,
            error=f"Could not load alert group '{group_name}': {exc}",
        )
        return

    try:
        dispatcher = AlertGroupDispatcher()
        dispatcher.run(group)
    except BaseException as exc:
        # dispatcher.run() documents itself as "never raises" but if it
        # ever does - dependency import error, out-of-memory, anything -
        # the user still gets an audit row + failure email.
        _emit_scheduler_failure(
            group_name, started,
            error=f"Dispatcher crashed for '{group_name}': "
                  f"{type(exc).__name__}: {exc}",
        )
        return


def _emit_scheduler_failure(
    group_name: str, started_monotonic: float, *, error: str,
) -> None:
    """Emit log row + failure email when the scheduler callback itself fails.

    Separate from ``dispatcher._emit_log`` / ``_maybe_send_failure_email``
    because we may never have successfully constructed a dispatcher. Uses
    the same underlying helpers so the two telemetry streams look identical
    from the user's perspective.
    """
    import time as _time
    try:
        from functionality.log_writer import log_alert_group_event
        log_alert_group_event(
            group_name=group_name,
            status="error",
            searches_used=None,
            estimated_tokens=None,
            actual_tokens=None,
            cost_usd=None,
            error_message=error[:500],
            duration_ms=int((_time.monotonic() - started_monotonic) * 1000),
            dry_run=False,
        )
    except Exception as log_exc:
        logger.warning("[!] Scheduler-failure log emit failed: %s", log_exc)

    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        store.log_run(
            group_name=group_name,
            status="error",
            error_message=error[:500],
        )
    except Exception as audit_exc:
        logger.warning(
            "[!] Scheduler-failure audit DB write failed: %s", audit_exc
        )

    try:
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.models import AlertGroupRunResult
        synthetic = AlertGroupRunResult(
            group_name=group_name,
            status="error",
            error_message=error,
        )
        AlertGroupDispatcher._maybe_send_failure_email(synthetic)
    except Exception as email_exc:
        logger.warning(
            "[!] Scheduler-failure email dispatch failed: %s", email_exc
        )

    logger.error("[x] Alert group scheduler failure: %s", error)


def register_alert_group_jobs(scheduler):
    """
    Register all enabled alert groups that have a schedule with the
    existing APScheduler instance.

    Called from the same place existing alert/search jobs are registered.
    """
    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        groups = store.list_groups()
    except Exception as exc:
        logger.warning("[!] Could not load alert groups for scheduling: %s", exc)
        return

    # Build the set of job IDs that SHOULD exist after this pass - every
    # AG that is currently enabled AND has a schedule. Any APScheduler
    # job under the alert_group_* prefix that is NOT in this set is a
    # stale registration left over from a previous run and MUST be
    # removed; otherwise a UI "Disable" click would only stop new
    # registrations while the OLD job kept firing on its original cron
    # until the next container restart. The dispatcher's disabled-gate
    # would still skip the call to Claude (so no money leaks), but the
    # job firing pointlessly is wasted CPU + log noise + a footgun if
    # anyone ever removes the dispatcher gate. Caught 2026-04-30.
    desired_job_ids: set[str] = set()
    for g in groups:
        if g.get("disabled", False):
            continue
        if not (g.get("schedule") or "").strip():
            continue
        desired_job_ids.add(f"alert_group_{g['name']}")

    # Sweep stale jobs. ``get_jobs`` on APScheduler returns Job objects;
    # check ``id.startswith("alert_group_")`` so we never touch other
    # subsystems' jobs (saved searches, ingestion, etc.).
    try:
        existing_jobs = scheduler.get_jobs() if hasattr(scheduler, "get_jobs") else []
    except Exception as exc:
        logger.warning("[!] Could not enumerate existing scheduler jobs: %s", exc)
        existing_jobs = []
    removed = 0
    for job in existing_jobs:
        try:
            jid = getattr(job, "id", None) or ""
        except Exception:
            continue
        if not jid.startswith("alert_group_"):
            continue
        if jid not in desired_job_ids:
            try:
                scheduler.remove_job(jid)
                removed += 1
                logger.info(
                    "[i] Removed stale alert_group scheduler job: %s "
                    "(AG is disabled, missing schedule, or deleted)",
                    jid,
                )
            except Exception as exc:
                logger.warning(
                    "[!] Failed to remove stale job %s: %s", jid, exc,
                )

    registered = 0
    for group in groups:
        if group.get("disabled", False):
            continue
        schedule = (group.get("schedule") or "").strip()
        if not schedule:
            continue

        # Per-AG timezone (added 2026-04-27). Empty / missing → "UTC" so
        # every AG written before this field existed keeps its current
        # behavior. The scheduler itself is pinned to UTC; passing
        # ``timezone=`` on the trigger is the documented escape hatch
        # (see scheduled_input_engine/engine.py:111).
        tz_name = (group.get("timezone") or "UTC").strip() or "UTC"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            logger.warning(
                "[!] Alert group '%s' has invalid timezone '%s' (%s); "
                "falling back to UTC.",
                group["name"], tz_name, exc,
            )
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")
            tz_name = "UTC"

        job_id = f"alert_group_{group['name']}"
        try:
            # Concurrency guard: ``max_instances=1`` prevents a slow-running
            # dispatch from stacking up if the cron fires again before the
            # previous run finishes. ``misfire_grace_time=600`` tolerates a
            # 10-minute miss without skipping - if the process was down
            # when the cron should have fired, it'll still run on startup.
            scheduler.add_job(
                _run_group_by_name,
                CronTrigger.from_crontab(linux_dow_to_apscheduler(schedule), timezone=tz),
                kwargs={"group_name": group["name"]},
                id=job_id,
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=600,
                coalesce=True,
            )
            registered += 1
            logger.info(
                "[i] Scheduled alert group '%s' with cron: %s (tz=%s)",
                group["name"], schedule, tz_name,
            )
            try:
                from functionality.log_writer import log_system_event
                log_system_event(
                    component="alert_groups",
                    event="job_registered",
                    message=(
                        f"alert_group '{group['name']}' cron='{schedule}' "
                        f"timezone='{tz_name}' job_id='{job_id}' "
                        f"max_instances=1 grace=600s"
                    ),
                )
            except Exception:
                pass
        except Exception as exc:
            logger.warning(
                "[!] Failed to schedule alert group '%s': %s", group["name"], exc
            )

    if registered or removed:
        logger.info(
            "[i] AG scheduler sync: %d job(s) registered, %d stale job(s) removed.",
            registered, removed,
        )
    try:
        from functionality.log_writer import log_system_event
        log_system_event(
            component="alert_groups",
            event="jobs_registered",
            message=(
                f"Registered {registered} alert group cron job(s), "
                f"removed {removed} stale job(s) "
                f"on scheduler {type(scheduler).__name__}"
            ),
        )
    except Exception:
        pass
