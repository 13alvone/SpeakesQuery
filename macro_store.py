"""
Macro Store
───────────
YAML-based CRUD for macro definitions.

Each macro is a single .yaml file in macros/.
"""

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml

from functionality.atomic_write import write_text_atomic
from validation.MacroValidation import MacroValidation

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.resolve()
MACROS_DIR = _PROJECT_ROOT / "macros"

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_]+$")


class MacroStore:
    """Manages macro YAML files."""

    def __init__(self):
        self._dir = MACROS_DIR
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the macros directory if it does not exist."""
        os.makedirs(self._dir, exist_ok=True)
        logger.info("[i] MacroStore initialised (dir=%s)", self._dir)

    # ------------------------------------------------------------------
    # YAML helpers
    # ------------------------------------------------------------------

    def _sanitize_filename(self, title: str) -> str:
        """Convert a title to a safe filename (no extension)."""
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", title.strip())
        safe = re.sub(r"_+", "_", safe).strip("_")
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
        """Validate all required fields. Raises ValueError on failure."""
        name = data.get("name", "").strip()
        MacroValidation.validate_name(name)

        definition = data.get("definition", "").strip()
        MacroValidation.validate_definition(definition)

        parameters = data.get("parameters", [])
        MacroValidation.validate_parameters(parameters, definition)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_macro(self, data: dict, overwrite: bool = False) -> dict:
        """
        Create a new macro YAML. Returns the saved dict.
        Raises FileExistsError if the name is taken and overwrite is False.
        """
        self._validate(data)

        now = datetime.now().isoformat()
        record = {
            "name": data["name"].strip(),
            "definition": data["definition"].strip(),
            "parameters": data.get("parameters", []),
            "description": data.get("description", "").strip(),
            "created_at": now,
            "updated_at": now,
        }

        path = self._yaml_path(record["name"])

        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f'A macro named "{record["name"]}" already exists.'
                )

            # If overwriting, preserve original created_at
            if path.exists() and overwrite:
                try:
                    existing = self._read_yaml(path)
                    record["created_at"] = existing.get("created_at", now)
                except Exception:
                    pass

            self._write_yaml(path, record)

        logger.info("[+] Macro written: %s", path.name)
        _emit_config_event(record["name"], "create", None, record)
        return record

    def list_macros(self) -> list:
        """Return all macros sorted by name."""
        results = []
        if not self._dir.exists():
            return results

        for path in sorted(self._dir.glob("*.yaml")):
            try:
                data = self._read_yaml(path)
                results.append(data)
            except Exception as exc:
                logger.warning("[!] Failed to read %s: %s", path.name, exc)

        results.sort(key=lambda m: m.get("name", ""))
        return results

    def get_macro(self, name: str) -> dict:
        """Return a single macro by name."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Macro "{name}" not found.')
        return self._read_yaml(path)

    def update_macro(self, name: str, data: dict) -> dict:
        """Update an existing macro. Returns the updated dict."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Macro "{name}" not found.')

        before = self._read_yaml(path)
        existing = dict(before)
        # Merge updates into existing record
        updatable = ("definition", "parameters", "description")
        for key in updatable:
            if key in data:
                existing[key] = data[key]

        existing["updated_at"] = datetime.now().isoformat()

        # Re-validate the merged record
        self._validate(existing)

        with self._lock:
            self._write_yaml(path, existing)

        logger.info("[~] Macro updated: %s", path.name)
        _emit_config_event(name, "update", before, existing)
        return existing

    def delete_macro(self, name: str):
        """Hard-delete: remove the YAML file."""
        path = self._yaml_path(name)
        if not path.exists():
            raise FileNotFoundError(f'Macro "{name}" not found.')

        try:
            data = self._read_yaml(path)
        except Exception:
            data = {"name": name}

        with self._lock:
            path.unlink()

        logger.info("[x] Macro deleted: %s", name)
        _emit_config_event(name, "delete", data, None)


def _emit_config_event(
    name: str, action: str,
    old_value: dict | None, new_value: dict | None,
) -> None:
    """Record a macro CRUD event to the config log stream - never raises."""
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=name,
            action=action,
            subject_type="macro",
            old_value=old_value,
            new_value=new_value,
            actor="api",
            source="macro_store",
        )
    except Exception:
        pass
