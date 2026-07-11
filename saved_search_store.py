"""
Saved Search Store
──────────────────
YAML-based CRUD for scheduled search configurations, with soft-delete
recovery via last_chance.sqlite (30-day retention).

Each saved search is a single .yaml file in saved_searches/.
"""

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml
from croniter import croniter

from functionality.atomic_write import write_text_atomic

from validation.SavedSearchValidation import SavedSearchValidation

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
SEARCHES_DIR = _PROJECT_ROOT / "saved_searches"
DEFAULT_SEARCHES_DIR = _PROJECT_ROOT / "default_saved_searches"
LAST_CHANCE_DB = _PROJECT_ROOT / "last_chance.sqlite"

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9 _.\-]+$")


class SavedSearchStore:
    """Manages saved search YAML files and the last_chance.sqlite backup DB."""

    def __init__(self):
        self._dir = SEARCHES_DIR
        self._defaults_dir = DEFAULT_SEARCHES_DIR
        self._db = str(LAST_CHANCE_DB)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the searches directory and last_chance DB table."""
        os.makedirs(self._dir, exist_ok=True)
        self._init_db()
        self._cleanup_last_chance()
        self._seed_defaults()
        logger.info("[i] SavedSearchStore initialised (dir=%s)", self._dir)

    # ------------------------------------------------------------------
    # Project-shipped defaults
    # ------------------------------------------------------------------
    #
    # `default_saved_searches/` is a version-controlled directory that ships
    # template feeder searches for default alert groups.  `_seed_defaults`
    # is idempotent: it only copies a template if the user's `_dir` does
    # not already contain a YAML with the same filename.  User edits and
    # deletions are respected - a deleted default reappears only after a
    # restart if the file is truly absent (not soft-deleted - that case is
    # handled by the delete/recover path keeping the file around).

    def list_defaults(self) -> list[str]:
        """Return names of every default feeder search available for install."""
        if not self._defaults_dir.exists():
            return []
        return sorted(p.stem for p in self._defaults_dir.glob("*.yaml"))

    def has_default(self, name: str) -> bool:
        return (
            self._defaults_dir.exists()
            and (self._defaults_dir / f"{self._sanitize_filename(name)}.yaml").exists()
        )

    def _seed_defaults(self) -> list[str]:
        """
        Copy any default-dir YAML whose filename doesn't already exist in
        `_dir`.  Returns the list of names newly seeded.
        """
        if not self._defaults_dir.exists():
            return []
        seeded: list[str] = []
        for src in sorted(self._defaults_dir.glob("*.yaml")):
            dst = self._dir / src.name
            if dst.exists():
                continue
            try:
                with open(src, "r", encoding="utf-8") as fh:
                    content = fh.read()
                write_text_atomic(str(dst), content)
                seeded.append(src.stem)
            except Exception as exc:
                logger.warning(
                    "[!] SavedSearchStore: failed to seed default %s: %s",
                    src.name, exc,
                )
        if seeded:
            logger.info("[i] Seeded %d default saved search(es): %s",
                        len(seeded), ", ".join(seeded))
        return seeded

    def install_default(self, name: str, *, overwrite: bool = False) -> dict:
        """
        Copy a single default saved search into the user's directory.

        Raises FileNotFoundError if the default doesn't exist. By default,
        raises FileExistsError if the user already has that search
        (safeguard against accidental clobbering of user edits).

        Pass ``overwrite=True`` to force-replace the installed YAML with
        the current default template. Use this when the installed version
        has drifted out of sync with the template (e.g. the user is on
        the Docker volume-mounted ``saved_searches/`` and the default
        template has been fixed by a later commit). Atomic write guards
        against a half-written file.

        Added 2026-04-21 after a Daily Opportunity Brief dispatch produced
        an empty brief because 4 of 10 installed feeder YAMLs had stale
        queries (``sort -amount_usd`` referencing a column dropped by a
        prior ``| table``, ``is_edge_zone=true`` missing proper SPQL
        quoting, etc.) while the git-tracked templates were already
        correct. The operator had no way to re-sync short of manually
        ``rm`` + re-install each file.
        """
        src = self._defaults_dir / f"{self._sanitize_filename(name)}.yaml"
        if not src.exists():
            raise FileNotFoundError(f'No default saved search named "{name}".')
        dst = self._yaml_path(name)
        with self._lock:
            if dst.exists() and not overwrite:
                raise FileExistsError(
                    f'Saved search "{name}" already exists; will not '
                    f'overwrite. Pass overwrite=True (or click "Sync '
                    f'Template" in the UI) to force-replace with the '
                    f'current default.'
                )
            with open(src, "r", encoding="utf-8") as fh:
                content = fh.read()
            write_text_atomic(str(dst), content)
        action = "resynced" if overwrite and dst.exists() else "installed"
        logger.info(
            "[i] %s default saved search '%s' from template", action, name
        )
        return self.get_search(name)

    def template_drift(self, name: str) -> dict | None:
        """
        Report whether the installed saved search differs from the current
        default template.

        Returns a dict ``{"installed_query": ..., "template_query": ...,
        "diff": ...}`` when they differ, or ``None`` when they match (or
        when either side is missing - the dispatcher's
        ``not_installed``/``no_template`` states cover those cases).

        Used by the Feeder Health resolver to show a "Sync Template"
        affordance next to feeders whose installed YAMLs have drifted.
        """
        src = self._defaults_dir / f"{self._sanitize_filename(name)}.yaml"
        dst = self._yaml_path(name)
        if not src.exists() or not dst.exists():
            return None
        try:
            template_text = src.read_text(encoding="utf-8")
            installed_text = dst.read_text(encoding="utf-8")
        except OSError:
            return None
        if template_text == installed_text:
            return None
        # Compare on query field specifically - whitespace / comment
        # differences in other metadata fields are not worth nagging
        # about. If only non-``query`` fields differ, we treat them as
        # in-sync for drift purposes (but still report the full-text
        # diff in the return value for UI display).
        installed_query = self._extract_query_field(installed_text)
        template_query = self._extract_query_field(template_text)
        if installed_query == template_query:
            return None
        return {
            "installed_query": installed_query,
            "template_query": template_query,
        }

    @staticmethod
    def _extract_query_field(yaml_text: str) -> str:
        """Extract the ``query:`` value from a saved-search YAML blob.

        Used by :py:meth:`template_drift`. Kept as a pure-text helper so
        it's fast and doesn't need a full YAML parse on every feeder
        health resolve. Handles both block-scalar (``query: |-``) and
        single-line formats.
        """
        import yaml as _yaml
        try:
            parsed = _yaml.safe_load(yaml_text) or {}
        except Exception:
            return ""
        q = parsed.get("query", "")
        if isinstance(q, str):
            return q.strip()
        return str(q).strip()

    def _init_db(self):
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS last_chance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    description TEXT,
                    query TEXT,
                    cron_schedule TEXT,
                    lookback TEXT,
                    trigger_type TEXT,
                    email_address TEXT,
                    send_email TEXT,
                    disabled INTEGER,
                    created_at TEXT,
                    updated_at TEXT,
                    deleted_at REAL,
                    yaml_raw TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lc_deleted_at "
                "ON last_chance(deleted_at)"
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
    # Validation
    # ------------------------------------------------------------------

    # Valid values for the ``purpose`` field - drives UI rendering and
    # validation branches.
    VALID_PURPOSES = frozenset({"standalone", "alert_group_feeder"})
    DEFAULT_PURPOSE = "standalone"

    def _validate(self, data: dict):
        """Validate all required fields. Raises ValueError on failure.

        ``purpose`` (added 2026-04-20) drives two branches:
          * ``standalone`` - saved search with its own schedule AND its own
            email (the historical default).
          * ``alert_group_feeder`` - data-only feeder for one or more alert
            groups. Has its own cron (so fresh data lands on disk before the
            AG fires) but never sends its own email, so the email_address,
            email_body, and analyzer_* fields are relaxed and the UI hides
            them. See ``docs/lang/12_alert_groups.md`` for the design.
        """
        name = data.get("name", "").strip()
        if not name:
            raise ValueError("Name is required.")
        if not _SAFE_NAME.match(name):
            raise ValueError(
                "Name may only contain letters, digits, spaces, hyphens, "
                "underscores, and periods."
            )

        query = data.get("query", "").strip()
        if not query:
            raise ValueError("Query is required.")

        SavedSearchValidation.validate_cron_schedule(data.get("cron_schedule", ""))
        SavedSearchValidation.validate_lookback(data.get("lookback", ""))
        # Optional IANA timezone for the cron schedule. Empty / missing is
        # treated as ``"UTC"`` for backward compat with every saved search
        # written before this field existed.
        SavedSearchValidation.validate_timezone(data.get("timezone"))

        purpose = (data.get("purpose") or self.DEFAULT_PURPOSE).strip().lower()
        if purpose not in self.VALID_PURPOSES:
            raise ValueError(
                f"purpose must be one of {sorted(self.VALID_PURPOSES)}, "
                f"got {purpose!r}"
            )

        # Feeders never send their own email, so email_address is optional.
        # Provide a benign sentinel if the user left it blank (we keep the
        # field for back-compat with the serializer + any future export).
        if purpose == "alert_group_feeder":
            email = (data.get("email_address") or "").strip()
            if not email:
                data["email_address"] = "noreply@speakesquery.local"
        else:
            SavedSearchValidation.validate_email(data.get("email_address", ""))

        # Wave 5 (2026-04-26): per-search admin error recipient. Optional;
        # validated only when set. Routes error/diagnostic notices away
        # from the customer-facing recipient list. Will be wired into
        # the alert-send path when the saved-search alert delivery is
        # next refactored - schema-only landing today so existing YAMLs
        # don't need migration.
        admin_email = (data.get("admin_error_email") or "").strip()
        if admin_email:
            SavedSearchValidation.validate_email(admin_email)

        trigger = data.get("trigger", "once")
        SavedSearchValidation.validate_trigger(trigger)

    # ------------------------------------------------------------------
    # Next-run computation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_next_run(cron_schedule: str, timezone_name: str = "UTC") -> str:
        """Return the next fire time as a TZ-aware ISO 8601 string.

        The cron expression is interpreted in ``timezone_name`` (an IANA zone
        like ``"America/New_York"``) and the returned ISO carries an explicit
        ``+HH:MM`` offset so the SPA's ``new Date(iso)`` parser converts to
        the browser's local clock correctly. See
        ``alert_group_store._get_next_run`` for the back-story.
        """
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(timezone_name or "UTC")
        except Exception:
            tz = None
        try:
            anchor = datetime.now(tz) if tz is not None else datetime.now()
            cron = croniter(cron_schedule, anchor)
            return cron.get_next(datetime).isoformat()
        except Exception:
            return "invalid cron"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_search(self, data: dict, overwrite: bool = False) -> dict:
        """
        Create a new saved search YAML. Returns the saved dict.
        Raises FileExistsError if the name is taken and overwrite is False.
        """
        self._validate(data)

        now = datetime.now().isoformat()
        purpose = (data.get("purpose") or self.DEFAULT_PURPOSE).strip().lower()
        record = {
            "name": data["name"].strip(),
            "description": data.get("description", "").strip(),
            "purpose": purpose,
            "query": data["query"].strip(),
            "cron_schedule": data["cron_schedule"].strip(),
            "timezone": SavedSearchValidation.validate_timezone(
                data.get("timezone"),
            ),
            "lookback": data["lookback"].strip(),
            "trigger": data.get("trigger", "once").lower(),
            "email_address": data.get("email_address", "").strip()
                              or "noreply@speakesquery.local",
            # Wave 5 (2026-04-26): per-search admin recipient (optional).
            "admin_error_email": (data.get("admin_error_email") or "").strip(),
            "email_body": data.get("email_body", "").strip(),
            "mv_truncate_limit": int(data.get("mv_truncate_limit", 3)),
            "attach_csv": data.get("attach_csv", "no").lower(),
            "token_validation_days": int(data.get("token_validation_days", 30)),
            # Feeders auto-force send_email=no regardless of what was posted.
            "send_email": "no" if purpose == "alert_group_feeder"
                                else data.get("send_email", "yes"),
            "analyzer_prompt": data.get("analyzer_prompt", "").strip(),
            "analyzer_filter_enabled": bool(data.get("analyzer_filter_enabled", False)),
            "analyzer_filter_question": data.get("analyzer_filter_question", "").strip(),
            "disabled": bool(data.get("disabled", False)),
            "created_at": now,
            "updated_at": now,
        }

        path = self._yaml_path(record["name"])

        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f'A saved search named "{record["name"]}" already exists.'
                )

            # If overwriting, preserve original created_at
            if path.exists() and overwrite:
                try:
                    existing = self._read_yaml(path)
                    record["created_at"] = existing.get("created_at", now)
                except Exception:
                    pass

            self._write_yaml(path, record)

        logger.info("[+] Saved search written: %s", path.name)
        self._emit_config_event(record["name"], "create", None, record)
        record["next_run_time"] = self._get_next_run(
            record["cron_schedule"], record.get("timezone", "UTC"),
        )
        return record

    def list_searches(self) -> list:
        """Return all saved searches with computed next_run_time."""
        results = []
        if not self._dir.exists():
            return results

        for path in sorted(self._dir.glob("*.yaml")):
            try:
                data = self._read_yaml(path)
                data["next_run_time"] = self._get_next_run(
                    data.get("cron_schedule", ""),
                    data.get("timezone", "UTC"),
                )
                results.append(data)
            except Exception as exc:
                logger.warning("[!] Failed to read %s: %s", path.name, exc)

        return results

    def get_search(self, name: str) -> dict:
        """Return a single saved search by name."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Saved search "{name}" not found.')
        data = self._read_yaml(path)
        data["next_run_time"] = self._get_next_run(
            data.get("cron_schedule", ""), data.get("timezone", "UTC"),
        )
        return data

    def get_search_yaml(self, name: str) -> str:
        """Return the raw YAML text for display."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Saved search "{name}" not found.')
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def update_search(self, name: str, data: dict) -> dict:
        """Update an existing saved search. Returns the updated dict."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Saved search "{name}" not found.')

        before = self._read_yaml(path)
        existing = dict(before)
        # Merge updates into existing record
        updatable = (
            "description", "purpose", "query", "cron_schedule", "timezone",
            "lookback", "trigger", "email_address", "email_body",
            "mv_truncate_limit", "attach_csv", "token_validation_days",
            "send_email", "analyzer_prompt", "analyzer_filter_enabled",
            "analyzer_filter_question", "disabled",
            # Wave 5 (2026-04-26): admin error recipient.
            "admin_error_email",
        )
        for key in updatable:
            if key in data:
                existing[key] = data[key]
        # Ensure purpose is one of the valid values; default to standalone
        # for pre-2026-04-20 records that never had the field.
        existing["purpose"] = (
            existing.get("purpose") or self.DEFAULT_PURPOSE
        ).strip().lower()

        existing["updated_at"] = datetime.now().isoformat()

        # Re-validate the merged record
        self._validate(existing)

        with self._lock:
            self._write_yaml(path, existing)

        logger.info("[~] Saved search updated: %s", path.name)
        self._emit_config_event(name, "update", before, existing)
        existing["next_run_time"] = self._get_next_run(
            existing["cron_schedule"], existing.get("timezone", "UTC"),
        )
        return existing

    @staticmethod
    def _emit_config_event(
        name: str, action: str,
        old_value: dict | None, new_value: dict | None,
    ) -> None:
        """Record a CRUD event to the config log stream - never raises."""
        try:
            from functionality.log_writer import log_config_change
            log_config_change(
                subject=name,
                action=action,
                subject_type="saved_search",
                old_value=old_value,
                new_value=new_value,
                actor="api",
                source="saved_search_store",
            )
        except Exception:
            pass

    def mark_as_alert_group_feeder(self, name: str, group_name: str) -> bool:
        """Flip a saved search's ``purpose`` to ``alert_group_feeder`` at the
        moment an alert group is created/updated to reference it.

        Returns ``True`` when the search was flipped (i.e. was previously
        ``standalone``), ``False`` when it was already a feeder or does not
        exist. Idempotent - calling twice is safe.

        The ``group_name`` is recorded in the config-change log so the user
        can trace exactly which AG caused the auto-toggle:
        ``index="indexes/logs/config/*.parquet" | search action="auto_toggle_to_feeder"``.
        """
        path = self._yaml_path(name)
        if not path.exists():
            return False
        try:
            existing = self._read_yaml(path)
        except Exception as exc:
            logger.warning("[!] Auto-toggle failed to read %s: %s", path.name, exc)
            return False
        if (existing.get("purpose") or self.DEFAULT_PURPOSE) == "alert_group_feeder":
            return False

        old_purpose = existing.get("purpose", self.DEFAULT_PURPOSE)
        existing["purpose"] = "alert_group_feeder"
        # Feeder always sends no email; force the derived field so the user
        # sees consistent behaviour after the toggle.
        existing["send_email"] = "no"
        existing["updated_at"] = datetime.now().isoformat()
        with self._lock:
            self._write_yaml(path, existing)

        try:
            from functionality.log_writer import log_config_change
            log_config_change(
                subject=name,
                action="auto_toggle_to_feeder",
                subject_type="saved_search",
                old_value=old_purpose,
                new_value="alert_group_feeder",
                actor="system",
                source=f"alert_group:{group_name}",
            )
        except Exception:
            pass

        logger.info(
            "[~] Saved search '%s' auto-toggled to alert_group_feeder "
            "(referenced by AG '%s').", name, group_name,
        )
        return True

    def delete_search(self, name: str):
        """Soft-delete: archive into last_chance.sqlite, then remove the YAML file."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Saved search "{name}" not found.')

        data = self._read_yaml(path)
        raw = path.read_text(encoding="utf-8")
        self._emit_config_event(name, "delete", data, None)

        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO last_chance
                    (name, description, query, cron_schedule, lookback,
                     trigger_type, email_address, send_email, disabled,
                     created_at, updated_at, deleted_at, yaml_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name", name),
                    data.get("description", ""),
                    data.get("query", ""),
                    data.get("cron_schedule", ""),
                    data.get("lookback", ""),
                    data.get("trigger", ""),
                    data.get("email_address", ""),
                    data.get("send_email", ""),
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

        logger.info("[x] Saved search soft-deleted: %s (archived in last_chance.sqlite)", name)

    # ------------------------------------------------------------------
    # Last-chance cleanup
    # ------------------------------------------------------------------

    def _cleanup_last_chance(self, max_age_days: int = 30):
        """Purge last_chance records older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        try:
            with sqlite3.connect(self._db) as conn:
                cursor = conn.execute(
                    "DELETE FROM last_chance WHERE deleted_at < ?", (cutoff,)
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "[i] Purged %d expired records from last_chance.sqlite",
                        cursor.rowcount,
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("[!] last_chance cleanup failed: %s", exc)
