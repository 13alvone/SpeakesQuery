"""
Alert Group Store
─────────────────
YAML-based CRUD for alert group configurations, with soft-delete
recovery via last_chance.sqlite (30-day retention).

Each alert group is a single .yaml file in alert_groups/.

Follows the same structural pattern as SavedSearchStore.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml
from croniter import croniter

from functionality.atomic_write import write_text_atomic
from validation.AlertGroupValidation import AlertGroupValidation

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
GROUPS_DIR = _PROJECT_ROOT / "alert_groups"
DEFAULTS_DIR = _PROJECT_ROOT / "default_alert_groups"
LAST_CHANCE_DB = _PROJECT_ROOT / "last_chance.sqlite"
RUNS_DB = _PROJECT_ROOT / "alert_group_runs.sqlite"

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9 _.\-]+$")


class AlertGroupStore:
    """Manages alert group YAML files, soft-delete backup, and run history."""

    def __init__(self):
        self._dir = GROUPS_DIR
        # Instance attribute so tests can isolate seeding by overriding
        # _defaults_dir on the fixture. Module-level DEFAULTS_DIR is only
        # the default; mirrors the SavedSearchStore pattern.
        self._defaults_dir = DEFAULTS_DIR
        self._db = str(LAST_CHANCE_DB)
        self._runs_db = str(RUNS_DB)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the groups directory, last_chance DB table, and runs DB."""
        os.makedirs(self._dir, exist_ok=True)
        self._init_db()
        self._init_runs_db()
        self._seed_defaults()
        self._cleanup_last_chance()
        logger.info("[i] AlertGroupStore initialised (dir=%s)", self._dir)

    def _seed_defaults(self):
        """Copy default AG YAMLs from default_alert_groups/ into alert_groups/.

        Idempotent and NEVER overwrites - only fills in missing files. This is
        the seed pattern that keeps user customisations safe across upgrades:

        * Defaults ship under ``default_alert_groups/`` (tracked in git) so a
          fresh clone has a working set out of the box.
        * The runtime store reads from ``alert_groups/`` (gitignored as of
          2026-04-30), so user edits via the UI are never overwritten by a
          ``git pull`` that updates a default YAML.
        * On every ``initialize()``, defaults absent from ``alert_groups/`` are
          copied in. If the user previously deleted an AG, it stays deleted
          unless they explicitly re-install via the Feeder Health UI.

        Mirrors the existing ``saved_search_store._seed_defaults`` pattern.
        Pinned by ``tests/test_alert_group_seed_defaults.py``.
        """
        if not self._defaults_dir.is_dir():
            logger.warning(
                "[!] default_alert_groups/ missing at %s - skipping seed",
                self._defaults_dir,
            )
            return
        seeded = 0
        for default_path in sorted(self._defaults_dir.glob("*.yaml")):
            target = self._dir / default_path.name
            if target.exists():
                continue  # Never overwrite - protects user edits
            try:
                shutil.copy2(default_path, target)
                seeded += 1
                logger.info("[i] Seeded default alert group: %s", default_path.name)
            except OSError as exc:
                logger.warning(
                    "[!] Could not seed %s: %s", default_path.name, exc,
                )
        if seeded:
            logger.info("[i] _seed_defaults copied %d default AG YAML(s)", seeded)

    def install_default(self, name: str, *, overwrite: bool = False) -> bool:
        """Install a single default AG by name (without the .yaml suffix).

        Returns True if the file was written, False if skipped (already exists
        and ``overwrite=False``, or default not found). Used by the Feeder
        Health "Install missing" UI button so the user can pull in a default
        AG that they previously deleted, without re-running ``initialize()``
        for everything.
        """
        if not name:
            return False
        default_path = self._defaults_dir / f"{name}.yaml"
        if not default_path.is_file():
            logger.warning(
                "[!] install_default(%r): no such default at %s",
                name, default_path,
            )
            return False
        target = self._dir / f"{name}.yaml"
        if target.exists() and not overwrite:
            logger.info(
                "[i] install_default(%r): target exists, not overwriting",
                name,
            )
            return False
        try:
            shutil.copy2(default_path, target)
            logger.info("[i] Installed default alert group: %s", name)
            return True
        except OSError as exc:
            logger.error("[x] install_default(%r) failed: %s", name, exc)
            return False

    def list_defaults(self) -> list:
        """Return the list of available default AG names (without .yaml suffix)."""
        if not self._defaults_dir.is_dir():
            return []
        return sorted(p.stem for p in self._defaults_dir.glob("*.yaml"))

    def _init_db(self):
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS last_chance_alert_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    description TEXT,
                    search_names TEXT,
                    prompt_text TEXT,
                    schedule TEXT,
                    max_rows INTEGER,
                    email_address TEXT,
                    disabled INTEGER,
                    created_at TEXT,
                    updated_at TEXT,
                    deleted_at REAL,
                    yaml_raw TEXT
                )
                """
            )
            # Migration: older installs created the table with
            # ``prompt_name`` instead of ``prompt_text``. Soft-delete
            # archiving now writes ``prompt_text`` so we must ensure the
            # column exists on upgrade. ALTER TABLE ... ADD COLUMN is
            # idempotent via the PRAGMA check below - never fails on a
            # freshly-created table.
            existing_cols = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(last_chance_alert_groups)"
                )
            }
            if "prompt_text" not in existing_cols:
                conn.execute(
                    "ALTER TABLE last_chance_alert_groups "
                    "ADD COLUMN prompt_text TEXT"
                )
                # Back-fill from the legacy column if it exists so the
                # archive history is preserved after upgrade.
                if "prompt_name" in existing_cols:
                    conn.execute(
                        "UPDATE last_chance_alert_groups "
                        "SET prompt_text = prompt_name "
                        "WHERE prompt_text IS NULL AND prompt_name IS NOT NULL"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lcag_deleted_at "
                "ON last_chance_alert_groups(deleted_at)"
            )
            conn.commit()

    def _init_runs_db(self):
        """Create the alert_group_runs table for audit trail."""
        with sqlite3.connect(self._runs_db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_group_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
                    status TEXT NOT NULL,
                    searches_used TEXT,
                    estimated_tokens INTEGER,
                    actual_tokens INTEGER,
                    cost_usd REAL,
                    error_message TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agr_group_name "
                "ON alert_group_runs(group_name)"
            )
            conn.commit()

    # ------------------------------------------------------------------
    # YAML helpers
    # ------------------------------------------------------------------

    def _sanitize_filename(self, title: str) -> str:
        """Convert a title to a safe filename (no extension)."""
        safe = re.sub(r"[^a-zA-Z0-9 _.\-]", "_", title.strip())
        safe = re.sub(r"_+", "_", safe).strip("_. ")
        if not safe:
            raise ValueError("Title produces an empty filename after sanitisation.")
        return safe

    def _yaml_path(self, name: str) -> Path:
        return self._dir / f"{self._sanitize_filename(name)}.yaml"

    def _read_yaml(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _write_yaml(self, path: Path, data: dict):
        text = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        write_text_atomic(path, text, encoding="utf-8")

    # ------------------------------------------------------------------
    # Next-run computation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_next_run(cron_schedule: str, timezone_name: str = "UTC") -> str:
        """Return the next fire time as a TZ-aware ISO 8601 string.

        The cron expression is interpreted in ``timezone_name`` (an IANA zone
        like ``"America/New_York"``) and the returned ISO string carries an
        explicit ``+HH:MM`` offset so the SPA's ``new Date(iso)`` parser
        converts to the browser's local clock correctly. A naive ISO string
        (no offset) was the source of a 7-hour display lie caught
        2026-04-27 - see ``reference_naive_iso_to_browser_misparse`` in the
        team's auto-memory.
        """
        if not cron_schedule or not cron_schedule.strip():
            return ""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(timezone_name or "UTC")
        except Exception:
            tz = None  # fall through to UTC below
        try:
            anchor = datetime.now(tz) if tz is not None else datetime.now()
            cron = croniter(cron_schedule, anchor)
            return cron.get_next(datetime).isoformat()
        except Exception:
            return "invalid cron"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, data: dict):
        """Validate all required fields.  Raises ValueError on failure."""
        AlertGroupValidation.validate_name(data.get("name", ""))
        AlertGroupValidation.validate_search_names(data.get("search_names", []))
        AlertGroupValidation.validate_prompt_text(data.get("prompt_text", ""))
        AlertGroupValidation.validate_schedule(data.get("schedule", ""))
        AlertGroupValidation.validate_max_rows(data.get("max_rows", 200))
        # Optional IANA timezone for the cron schedule. Empty / missing is
        # treated as ``"UTC"`` for backward compat with every AG written
        # before this field existed.
        AlertGroupValidation.validate_timezone(data.get("timezone"))

        delivery_mode = AlertGroupValidation.validate_delivery_mode(
            data.get("delivery_mode"),
        )
        email = data.get("email_address", "").strip()
        if email:
            AlertGroupValidation.validate_email(email)
        # prompt_only mode emails the built prompt instead of calling Claude -
        # no email_address means nowhere to deliver, so reject at save time
        # with an actionable message rather than failing silently in the
        # dispatcher.
        if delivery_mode == "prompt_only" and not email:
            raise ValueError(
                "delivery_mode='prompt_only' requires email_address to be "
                "set - the prompt is delivered by email since no Claude API "
                "call is made."
            )

        # Wave 5 (2026-04-26): per-AG admin error recipient. Optional;
        # falls back to global alert_group_failure_email_to in the
        # dispatcher when blank. Validated only when set so existing
        # YAMLs without the field load cleanly.
        admin_email = (data.get("admin_error_email") or "").strip()
        if admin_email:
            AlertGroupValidation.validate_email(admin_email)

        # Slice A (2026-06-23): optional registry model_id for routing this
        # AG through the provider-agnostic LLM router instead of Claude.
        # Validated only when set; existence in the model registry is
        # checked best-effort so a typo is caught at save time.
        AlertGroupValidation.validate_model_id(data.get("model_id"))

        # Headroom (2026-06-23): optional tri-state override for whether this
        # AG's Claude call routes through the compression proxy. Only the
        # shape is validated; resolution against the global default happens
        # at dispatch time. Absent / None = inherit the global default.
        AlertGroupValidation.validate_use_headroom(data.get("use_headroom"))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_group(self, data: dict, overwrite: bool = False) -> dict:
        """
        Create a new alert group YAML.  Returns the saved dict.
        Raises FileExistsError if the name is taken and overwrite is False.

        Side effect: every saved search referenced by ``search_names`` is
        auto-toggled from ``purpose: standalone`` to
        ``purpose: alert_group_feeder`` at the moment of AG create/update.
        The flip is logged to ``indexes/logs/config/*.parquet`` with
        ``action="auto_toggle_to_feeder"`` and ``source="alert_group:<name>"``
        so the user can always trace why a saved search became a feeder.
        """
        self._validate(data)

        now = datetime.now().isoformat()
        record = {
            "name": data["name"].strip(),
            "description": data.get("description", "").strip(),
            "search_names": data["search_names"],
            "prompt_text": data["prompt_text"].strip(),
            "schedule": data.get("schedule", "").strip(),
            "timezone": AlertGroupValidation.validate_timezone(
                data.get("timezone"),
            ),
            "max_rows": int(data.get("max_rows", 200)),
            "email_address": data.get("email_address", "").strip(),
            "admin_error_email": (data.get("admin_error_email") or "").strip(),
            # 2026-04-27: opt this AG out of failure emails entirely.
            # When ``True`` the dispatcher's ``_maybe_send_failure_email``
            # short-circuits BEFORE looking at admin_error_email or the
            # global ``alert_group_failure_email_to`` fallback. Use for
            # AGs whose owners watch dashboards instead of inboxes, or
            # for low-priority feeders whose failures shouldn't page.
            "error_email_disabled": bool(data.get("error_email_disabled", False)),
            "disabled": bool(data.get("disabled", False)),
            "delivery_mode": AlertGroupValidation.validate_delivery_mode(
                data.get("delivery_mode"),
            ),
            # Slice A (2026-06-23): optional registry model_id. When set,
            # the dispatcher routes this AG through analyzers.llm_router to
            # a local/registry model (e.g. llamacpp-qwen35-122b-a10b,
            # $0/token) instead of the Claude API. Empty = Claude (default).
            "model_id": AlertGroupValidation.validate_model_id(
                data.get("model_id"),
            ),
            # Slice C1 (2026-06-23): diff-style AGs skip cleanly (no error,
            # no failure email, no breaker tick) when all feeders are empty.
            "skip_on_empty": bool(data.get("skip_on_empty", False)),
            # Headroom (2026-06-23): tri-state override (True / False / None).
            # None = inherit the global_use_headroom_default setting.
            "use_headroom": AlertGroupValidation.validate_use_headroom(
                data.get("use_headroom"),
            ),
            "created_at": now,
            "updated_at": now,
        }

        path = self._yaml_path(record["name"])

        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f'An alert group named "{record["name"]}" already exists.'
                )

            # If overwriting, preserve original created_at
            if path.exists() and overwrite:
                try:
                    existing = self._read_yaml(path)
                    record["created_at"] = existing.get("created_at", now)
                except Exception:
                    pass

            self._write_yaml(path, record)

        logger.info("[+] Alert group written: %s", path.name)
        self._auto_toggle_feeders(record["name"], record["search_names"])
        self._emit_config_event(record["name"], "create", None, record)
        record["next_run_time"] = self._get_next_run(
            record["schedule"], record.get("timezone", "UTC"),
        )
        return record

    def list_groups(self) -> list:
        """Return all alert groups with computed next_run_time."""
        results = []
        if not self._dir.exists():
            return results

        for path in sorted(self._dir.glob("*.yaml")):
            try:
                data = self._read_yaml(path)
                data["next_run_time"] = self._get_next_run(
                    data.get("schedule", ""),
                    data.get("timezone", "UTC"),
                )
                results.append(data)
            except Exception as exc:
                logger.warning("[!] Failed to read %s: %s", path.name, exc)

        return results

    def get_group(self, name: str) -> dict:
        """Return a single alert group by name."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Alert group "{name}" not found.')
        data = self._read_yaml(path)
        data["next_run_time"] = self._get_next_run(
            data.get("schedule", ""), data.get("timezone", "UTC"),
        )
        return data

    def get_group_yaml(self, name: str) -> str:
        """Return the raw YAML text for display."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Alert group "{name}" not found.')
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def update_group(self, name: str, data: dict) -> dict:
        """Update an existing alert group.  Returns the updated dict."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Alert group "{name}" not found.')

        before = self._read_yaml(path)
        existing = dict(before)
        updatable = (
            "description", "search_names", "prompt_text", "schedule",
            "timezone",
            "max_rows", "email_address", "disabled",
            # Production-hardening fields (2026-04-20):
            "max_cost_usd_per_run", "max_cost_usd_per_day",
            "max_feeder_staleness_hours", "fail_on_stale_feeder",
            "email_template_override", "circuit_breaker_tripped",
            # Deduplication + output-size fields (2026-04-20 round 2):
            "max_dispatches_per_day", "min_interval_between_runs_hours",
            "max_output_tokens",
            # Budget-friendly mode (2026-04-22): email the built prompt
            # instead of dispatching to Claude. See
            # AlertGroupValidation.DELIVERY_MODES.
            "delivery_mode",
            # Wave 5 (2026-04-26): per-AG admin error recipient (optional).
            # See ``alert_groups/dispatcher.py::_maybe_send_failure_email``.
            "admin_error_email",
            # 2026-04-27: per-AG opt-out for failure-alert emails. Bool.
            "error_email_disabled",
            # 2026-05-16: dry-run gate + output routing discriminator.
            # Without these in the updatable allowlist, the AG edit form
            # (or API PUT) silently drops toggle attempts - caught when an
            # AG's first `dry_run: true → false` flip via PUT returned
            # status=success but the field never changed.
            "dry_run",
            "output_kind",
            # Slice A (2026-06-23): route this AG through the LLM router to
            # a local/registry model instead of Claude. See
            # AlertGroupValidation.validate_model_id.
            "model_id",
            # Slice C1 (2026-06-23): clean-skip on empty feeders for diff AGs.
            "skip_on_empty",
            # Headroom (2026-06-23): per-AG tri-state route override.
            "use_headroom",
        )
        for key in updatable:
            if key in data:
                existing[key] = data[key]

        existing["updated_at"] = datetime.now().isoformat()

        # Re-validate the merged record
        self._validate(existing)

        with self._lock:
            self._write_yaml(path, existing)

        logger.info("[~] Alert group updated: %s", path.name)
        self._auto_toggle_feeders(existing["name"], existing.get("search_names", []))
        self._emit_config_event(existing["name"], "update", before, existing)
        existing["next_run_time"] = self._get_next_run(
            existing.get("schedule", ""), existing.get("timezone", "UTC"),
        )
        return existing

    def delete_group(self, name: str):
        """Soft-delete: archive into last_chance.sqlite, then remove the YAML file."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Alert group "{name}" not found.')

        data = self._read_yaml(path)
        raw = path.read_text(encoding="utf-8")

        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO last_chance_alert_groups
                    (name, description, search_names, prompt_text,
                     schedule, max_rows, email_address, disabled,
                     created_at, updated_at, deleted_at, yaml_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name", name),
                    data.get("description", ""),
                    json.dumps(data.get("search_names", [])),
                    data.get("prompt_text", ""),
                    data.get("schedule", ""),
                    data.get("max_rows", 200),
                    data.get("email_address", ""),
                    int(data.get("disabled", False)),
                    data.get("created_at", ""),
                    data.get("updated_at", ""),
                    time.time(),
                    raw,
                ),
            )
            conn.commit()

        with self._lock:
            path.unlink()

        logger.info("[x] Alert group soft-deleted: %s (archived in last_chance.sqlite)", name)
        self._emit_config_event(name, "delete", data, None)

    # ------------------------------------------------------------------
    # Feeder auto-toggle + config logging
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_toggle_feeders(group_name: str, search_names: list) -> None:
        """Flip each referenced saved search to ``purpose: alert_group_feeder``.

        Idempotent - already-feeder searches are left alone. Saved searches
        that don't exist are silently skipped (the caller's validation is
        upstream of this). Every flip produces an ``auto_toggle_to_feeder``
        config-log row so the user can retrace exactly why a saved search
        changed purpose and which AG drove it.
        """
        try:
            from saved_search_store import SavedSearchStore
            store = SavedSearchStore()
            store.initialize()
            for name in (search_names or []):
                try:
                    store.mark_as_alert_group_feeder(name, group_name)
                except Exception as exc:
                    logger.warning(
                        "[!] Auto-toggle failed for saved search '%s' (AG '%s'): %s",
                        name, group_name, exc,
                    )
        except Exception as exc:
            logger.warning(
                "[!] Auto-toggle path errored for AG '%s': %s", group_name, exc,
            )

    @staticmethod
    def _emit_config_event(
        group_name: str, action: str,
        old_value: dict | None, new_value: dict | None,
    ) -> None:
        """Record a create/update/delete event to the config log stream."""
        try:
            from functionality.log_writer import log_config_change
            log_config_change(
                subject=group_name,
                action=action,
                subject_type="alert_group",
                old_value=old_value,
                new_value=new_value,
                actor="api",
                source="alert_group_store",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------

    def log_run(self, group_name: str, status: str, searches_used: list = None,
                estimated_tokens: int = None, actual_tokens: int = None,
                cost_usd: float = None, error_message: str = None) -> int:
        """Insert a run record and return the row ID."""
        with sqlite3.connect(self._runs_db) as conn:
            cursor = conn.execute(
                """
                INSERT INTO alert_group_runs
                    (group_name, status, searches_used, estimated_tokens,
                     actual_tokens, cost_usd, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_name,
                    status,
                    json.dumps(searches_used) if searches_used else None,
                    estimated_tokens,
                    actual_tokens,
                    cost_usd,
                    error_message,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def list_runs(self, group_name: str = None, limit: int = 50) -> list:
        """Return recent run records, optionally filtered by group name."""
        with sqlite3.connect(self._runs_db) as conn:
            conn.row_factory = sqlite3.Row
            if group_name:
                rows = conn.execute(
                    "SELECT * FROM alert_group_runs WHERE group_name = ? "
                    "ORDER BY triggered_at DESC LIMIT ?",
                    (group_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM alert_group_runs "
                    "ORDER BY triggered_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Last-chance cleanup
    # ------------------------------------------------------------------

    def _cleanup_last_chance(self, max_age_days: int = 30):
        """Purge last_chance records older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        try:
            with sqlite3.connect(self._db) as conn:
                cursor = conn.execute(
                    "DELETE FROM last_chance_alert_groups WHERE deleted_at < ?",
                    (cutoff,),
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "[i] Purged %d expired alert group records from last_chance.sqlite",
                        cursor.rowcount,
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("[!] Alert group last_chance cleanup failed: %s", exc)
