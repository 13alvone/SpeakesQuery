"""
Boilerplate Prompt Store
────────────────────────
YAML-based CRUD for alert group boilerplate prompt templates, with soft-delete
recovery via last_chance.sqlite (30-day retention).

Each boilerplate prompt is a single .yaml file in boilerplate_prompts/.

Follows the same structural pattern as AnalyzerPromptStore.
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

from functionality.atomic_write import write_text_atomic
from validation.BoilerplatePromptValidation import BoilerplatePromptValidation

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
PROMPTS_DIR = _PROJECT_ROOT / "boilerplate_prompts"
LAST_CHANCE_DB = _PROJECT_ROOT / "last_chance.sqlite"

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9 _.\-]+$")

# Default analyst brief template - seeded on first initialisation.
DEFAULT_ANALYST_BRIEF = {
    "name": "analyst_brief",
    "description": "Default multi-search analyst brief for alert groups.",
    "template": (
        "You are a market analyst assistant integrated into SpeakesQuery, "
        "a signal monitoring pipeline.\n\n"
        "Run timestamp: {run_timestamp}\n"
        "Group: {group_name}\n"
        "Searches included: {search_count}\n\n"
        "Below are the most recent results from {search_count} monitored "
        "search(es). Each block is labeled with the search name and row count.\n\n"
        "{search_blocks}\n\n"
        "---\n\n"
        "Based solely on the data above, identify the five highest-conviction "
        "opportunities available within the next 24 hours. For each, provide:\n\n"
        "1. What the opportunity is and which data signal supports it\n"
        "2. Your estimated probability of the favorable outcome (as a percentage)\n"
        "3. The implied probability from the market (if present in the data)\n"
        "4. Expected value assessment: positive, neutral, or negative\n"
        "5. Confidence tier: HIGH / MEDIUM / LOW\n"
        "6. The single biggest risk that would invalidate this pick\n\n"
        "Order by confidence tier descending, then by expected value. "
        "Be direct. Do not hedge unless the data genuinely warrants it."
    ),
}


class BoilerplatePromptStore:
    """Manages boilerplate prompt YAML files and soft-delete backup."""

    def __init__(self):
        self._dir = PROMPTS_DIR
        self._db = str(LAST_CHANCE_DB)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the prompts directory, last_chance DB table, and seed defaults."""
        os.makedirs(self._dir, exist_ok=True)
        self._init_db()
        self._cleanup_last_chance()
        self._seed_defaults()
        logger.info("[i] BoilerplatePromptStore initialised (dir=%s)", self._dir)

    def _init_db(self):
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS last_chance_boilerplate_prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    description TEXT,
                    template TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    deleted_at REAL,
                    yaml_raw TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lcbp_deleted_at "
                "ON last_chance_boilerplate_prompts(deleted_at)"
            )
            conn.commit()

    def _seed_defaults(self):
        """Create the default analyst brief prompt if it doesn't exist."""
        path = self._yaml_path(DEFAULT_ANALYST_BRIEF["name"])
        if not path.exists():
            now = datetime.now().isoformat()
            record = {
                "name": DEFAULT_ANALYST_BRIEF["name"],
                "description": DEFAULT_ANALYST_BRIEF["description"],
                "template": DEFAULT_ANALYST_BRIEF["template"],
                "created_at": now,
                "updated_at": now,
            }
            self._write_yaml(path, record)
            logger.info("[+] Seeded default boilerplate prompt: %s", record["name"])

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

    def _validate(self, data: dict):
        """Validate all required fields.  Raises ValueError on failure."""
        BoilerplatePromptValidation.validate_name(data.get("name", ""))
        BoilerplatePromptValidation.validate_template(data.get("template", ""))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_prompt(self, data: dict, overwrite: bool = False) -> dict:
        """
        Create a new boilerplate prompt YAML.  Returns the saved dict.
        Raises FileExistsError if the name is taken and overwrite is False.
        """
        self._validate(data)

        now = datetime.now().isoformat()
        record = {
            "name": data["name"].strip(),
            "description": data.get("description", "").strip(),
            "template": data["template"].strip(),
            "created_at": now,
            "updated_at": now,
        }

        path = self._yaml_path(record["name"])

        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f'A boilerplate prompt named "{record["name"]}" already exists.'
                )

            # If overwriting, preserve original created_at
            if path.exists() and overwrite:
                try:
                    existing = self._read_yaml(path)
                    record["created_at"] = existing.get("created_at", now)
                except Exception:
                    pass

            self._write_yaml(path, record)

        logger.info("[+] Boilerplate prompt written: %s", path.name)
        _emit_config_event(record["name"], "create", None, record)
        return record

    def list_prompts(self) -> list:
        """Return all boilerplate prompts sorted by name."""
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
        """Return a single boilerplate prompt by name."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Boilerplate prompt "{name}" not found.')
        return self._read_yaml(path)

    def get_prompt_yaml(self, name: str) -> str:
        """Return the raw YAML text for display."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Boilerplate prompt "{name}" not found.')
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def update_prompt(self, name: str, data: dict) -> dict:
        """Update an existing boilerplate prompt.  Returns the updated dict."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Boilerplate prompt "{name}" not found.')

        before = self._read_yaml(path)
        existing = dict(before)
        updatable = ("description", "template")
        for key in updatable:
            if key in data:
                existing[key] = data[key]

        existing["updated_at"] = datetime.now().isoformat()

        # Re-validate the merged record
        self._validate(existing)

        with self._lock:
            self._write_yaml(path, existing)

        logger.info("[~] Boilerplate prompt updated: %s", path.name)
        _emit_config_event(name, "update", before, existing)
        return existing

    def delete_prompt(self, name: str):
        """Soft-delete: archive into last_chance.sqlite, then remove the YAML file."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Boilerplate prompt "{name}" not found.')

        data = self._read_yaml(path)
        raw = path.read_text(encoding="utf-8")
        _emit_config_event(name, "delete", data, None)

        with sqlite3.connect(self._db) as conn:
            conn.execute(
                """
                INSERT INTO last_chance_boilerplate_prompts
                    (name, description, template,
                     created_at, updated_at, deleted_at, yaml_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name", name),
                    data.get("description", ""),
                    data.get("template", ""),
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
            "[x] Boilerplate prompt soft-deleted: %s (archived in last_chance.sqlite)", name
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
                    "DELETE FROM last_chance_boilerplate_prompts WHERE deleted_at < ?",
                    (cutoff,),
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "[i] Purged %d expired boilerplate prompt records from last_chance.sqlite",
                        cursor.rowcount,
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("[!] Boilerplate prompt last_chance cleanup failed: %s", exc)


def _emit_config_event(
    name: str, action: str,
    old_value: dict | None, new_value: dict | None,
) -> None:
    """Record a boilerplate-prompt CRUD event to the config log stream."""
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=name,
            action=action,
            subject_type="boilerplate_prompt",
            old_value=old_value,
            new_value=new_value,
            actor="api",
            source="boilerplate_prompt_store",
        )
    except Exception:
        pass
