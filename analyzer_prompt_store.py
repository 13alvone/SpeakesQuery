"""
Analyzer Prompt Store
─────────────────────
YAML-based CRUD for Claude analyzer prompt definitions, with soft-delete
recovery via last_chance.sqlite (30-day retention).

Each analyzer prompt is a single .yaml file in analyzer_prompts/.

Follows the same structural pattern as MacroStore and SavedSearchStore.
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

from validation.AnalyzerPromptValidation import AnalyzerPromptValidation

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
PROMPTS_DIR = _PROJECT_ROOT / "analyzer_prompts"
LAST_CHANCE_DB = _PROJECT_ROOT / "last_chance.sqlite"

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9 _.\-]+$")


class AnalyzerPromptStore:
    """Manages analyzer prompt YAML files and soft-delete backup."""

    def __init__(self):
        self._dir = PROMPTS_DIR
        self._db = str(LAST_CHANCE_DB)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the prompts directory and last_chance DB table."""
        os.makedirs(self._dir, exist_ok=True)
        self._init_db()
        self._cleanup_last_chance()
        logger.info("[i] AnalyzerPromptStore initialised (dir=%s)", self._dir)

    def _init_db(self):
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS last_chance_analyzer_prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    description TEXT,
                    prompt_text TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    deleted_at REAL,
                    yaml_raw TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lcap_deleted_at "
                "ON last_chance_analyzer_prompts(deleted_at)"
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
        # Atomic write per project convention - stage to sibling .tmp and
        # os.replace() into place. Prevents truncation on crash. Used in
        # every other *_store.py; this file was overlooked until the
        # 2026-04-21 audit.
        from functionality.atomic_write import write_text_atomic
        text = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        write_text_atomic(str(path), text)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, data: dict):
        """Validate all required fields.  Raises ValueError on failure."""
        AnalyzerPromptValidation.validate_name(data.get("name", ""))
        AnalyzerPromptValidation.validate_prompt_text(data.get("prompt_text", ""))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_prompt(self, data: dict, overwrite: bool = False) -> dict:
        """
        Create a new analyzer prompt YAML.  Returns the saved dict.
        Raises FileExistsError if the name is taken and overwrite is False.
        """
        self._validate(data)

        now = datetime.now().isoformat()
        record = {
            "name": data["name"].strip(),
            "description": data.get("description", "").strip(),
            "prompt_text": data["prompt_text"].strip(),
            "created_at": now,
            "updated_at": now,
        }

        path = self._yaml_path(record["name"])

        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f'An analyzer prompt named "{record["name"]}" already exists.'
                )

            # If overwriting, preserve original created_at
            if path.exists() and overwrite:
                try:
                    existing = self._read_yaml(path)
                    record["created_at"] = existing.get("created_at", now)
                except Exception:
                    pass

            self._write_yaml(path, record)

        logger.info("[+] Analyzer prompt written: %s", path.name)
        _emit_config_event(record["name"], "create", None, record)
        return record

    def list_prompts(self) -> list:
        """Return all analyzer prompts sorted by name."""
        results = []
        if not self._dir.exists():
            return results

        for path in sorted(self._dir.glob("*.yaml")):
            try:
                data = self._read_yaml(path)
                results.append(data)
            except Exception as exc:
                logger.warning("[!] Failed to read %s: %s", path.name, exc)

        results.sort(key=lambda p: p.get("name", ""))
        return results

    def get_prompt(self, name: str) -> dict:
        """Return a single analyzer prompt by name."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Analyzer prompt "{name}" not found.')
        return self._read_yaml(path)

    def get_prompt_yaml(self, name: str) -> str:
        """Return the raw YAML text for display."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Analyzer prompt "{name}" not found.')
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def update_prompt(self, name: str, data: dict) -> dict:
        """Update an existing analyzer prompt.  Returns the updated dict."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Analyzer prompt "{name}" not found.')

        before = self._read_yaml(path)
        existing = dict(before)
        updatable = ("description", "prompt_text")
        for key in updatable:
            if key in data:
                existing[key] = data[key]

        existing["updated_at"] = datetime.now().isoformat()

        # Re-validate the merged record
        self._validate(existing)

        with self._lock:
            self._write_yaml(path, existing)

        logger.info("[~] Analyzer prompt updated: %s", path.name)
        _emit_config_event(name, "update", before, existing)
        return existing

    def delete_prompt(self, name: str):
        """Soft-delete: archive into last_chance.sqlite, then remove the YAML file."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Analyzer prompt "{name}" not found.')

        data = self._read_yaml(path)
        raw = path.read_text(encoding="utf-8")
        _emit_config_event(name, "delete", data, None)

        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO last_chance_analyzer_prompts
                    (name, description, prompt_text,
                     created_at, updated_at, deleted_at, yaml_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name", name),
                    data.get("description", ""),
                    data.get("prompt_text", ""),
                    data.get("created_at", ""),
                    data.get("updated_at", ""),
                    time.time(),
                    raw,
                ),
            )
            conn.commit()

        with self._lock:
            path.unlink()

        logger.info(
            "[x] Analyzer prompt soft-deleted: %s (archived in last_chance.sqlite)", name
        )

    # ------------------------------------------------------------------
    # Last-chance cleanup
    # ------------------------------------------------------------------

    def _cleanup_last_chance(self, max_age_days: int = 30):
        """Purge last_chance records older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        try:
            with sqlite3.connect(self._db) as conn:
                cursor = conn.execute(
                    "DELETE FROM last_chance_analyzer_prompts WHERE deleted_at < ?",
                    (cutoff,),
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "[i] Purged %d expired analyzer prompt records from last_chance.sqlite",
                        cursor.rowcount,
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("[!] Analyzer prompt last_chance cleanup failed: %s", exc)


def _emit_config_event(
    name: str, action: str,
    old_value: dict | None, new_value: dict | None,
) -> None:
    """Record an analyzer-prompt CRUD event to the config log stream."""
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=name,
            action=action,
            subject_type="analyzer_prompt",
            old_value=old_value,
            new_value=new_value,
            actor="api",
            source="analyzer_prompt_store",
        )
    except Exception:
        pass
