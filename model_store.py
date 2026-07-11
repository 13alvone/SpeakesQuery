"""
Model Store - Phase 2 / Bet 3 slice 1
─────────────────────────────────────
YAML-based CRUD for the LLM model registry. Each model is one
``models/<id>.yaml`` file describing a single endpoint the slice-2
``analyzers/llm_router.py`` will dispatch to: id, provider, model_name,
endpoint (Ollama URL or empty for SDK-default), pricing, operational
defaults (max_output_tokens, default_timeout_seconds), and an optional
``sampling`` block (allowlisted sampler params - e.g. presence_penalty -
forwarded verbatim into the Chat Completions payload; see
:meth:`ModelValidation.validate_sampling`).

User edits live in ``models/`` (gitignored). Defaults ship under
``default_models/`` (tracked in git) and are seeded into ``models/``
on first init via :meth:`ModelStore._seed_defaults` - never overwriting
user edits. Mirrors the alert-group / saved-search seeding pattern so a
``git pull`` that updates a default never clobbers the user's customised
record.

The ``id`` field is the canonical key. Filenames are derived from the
id via :meth:`_yaml_path`. ``id`` must match
``[a-z0-9._-]+`` (filename-safe, lowercase only) for portable cross-
platform storage.

Slice 1 ships the registry only - no router, no SPQL pipes. Slice 2
adds ``llm_router.py``; slices 4+ add the user-visible ``| llm`` /
``| llm_batch`` / ``| switch`` pipes.
"""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from functionality.atomic_write import write_text_atomic
from validation.ModelValidation import ModelValidation

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).parent.resolve()
MODELS_DIR = _PROJECT_ROOT / "models"
DEFAULTS_DIR = _PROJECT_ROOT / "default_models"


class ModelStore:
    """Manages model-registry YAML files."""

    def __init__(self):
        # Instance attributes so tests can inject tmp paths; mirrors the
        # alert_group_store / saved_search_store pattern.
        self._dir = MODELS_DIR
        self._defaults_dir = DEFAULTS_DIR
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the models directory and seed missing defaults."""
        os.makedirs(self._dir, exist_ok=True)
        self._seed_defaults()
        logger.info("[i] ModelStore initialised (dir=%s)", self._dir)

    def _seed_defaults(self):
        """Copy ``default_models/*.yaml`` into ``models/``, missing-only.

        NEVER overwrites - fresh clones get a working set, but a user's
        UI customisation is preserved across upgrades. Same idempotent
        no-overwrite contract as :meth:`AlertGroupStore._seed_defaults`.
        """
        if not self._defaults_dir.is_dir():
            logger.warning(
                "[!] default_models/ missing at %s - skipping seed",
                self._defaults_dir,
            )
            return
        seeded = 0
        for default_path in sorted(self._defaults_dir.glob("*.yaml")):
            target = self._dir / default_path.name
            if target.exists():
                continue
            try:
                self._copy_default(default_path, target)
                seeded += 1
                logger.info("[i] Seeded default model: %s", default_path.name)
            except OSError as exc:
                logger.warning(
                    "[!] Could not seed %s: %s", default_path.name, exc,
                )
        if seeded:
            logger.info("[i] _seed_defaults copied %d default model YAML(s)", seeded)

    def _copy_default(self, src: Path, dst: Path) -> None:
        """Copy via atomic_write so a crash mid-seed never leaves a
        partial file in models/.
        """
        text = src.read_text(encoding="utf-8")
        write_text_atomic(dst, text, encoding="utf-8")

    def install_default(self, model_id: str, *, overwrite: bool = False) -> bool:
        """Install a single default by id. Returns True if written.

        Used when a user has previously deleted a default and wants to
        re-install it without re-running ``_seed_defaults`` for the
        whole tree.
        """
        if not model_id:
            return False
        default_path = self._defaults_dir / f"{model_id}.yaml"
        if not default_path.is_file():
            logger.warning(
                "[!] install_default(%r): no such default at %s",
                model_id, default_path,
            )
            return False
        target = self._yaml_path(model_id)
        if target.exists() and not overwrite:
            logger.info(
                "[i] install_default(%r): target exists, not overwriting",
                model_id,
            )
            return False
        with self._lock:
            self._copy_default(default_path, target)
        logger.info("[i] Installed default model: %s", model_id)
        return True

    def list_default_ids(self) -> list[str]:
        """Return ids of every default YAML available in default_models/."""
        if not self._defaults_dir.is_dir():
            return []
        return sorted(p.stem for p in self._defaults_dir.glob("*.yaml"))

    # ------------------------------------------------------------------
    # YAML helpers
    # ------------------------------------------------------------------

    def _yaml_path(self, model_id: str) -> Path:
        validated = ModelValidation.validate_id(model_id)
        return self._dir / f"{validated}.yaml"

    @staticmethod
    def _read_yaml(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _write_yaml(path: Path, data: dict) -> None:
        text = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        write_text_atomic(path, text, encoding="utf-8")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_model(self, data: dict, *, overwrite: bool = False) -> dict:
        """Validate, normalise, and persist a model record.

        ``overwrite=False`` (default) refuses to clobber an existing
        file, so a typo in the id at create-time never silently
        replaces a different model. Updates go through :meth:`update_model`.
        """
        validated = ModelValidation.validate_record(data)
        target = self._yaml_path(validated["id"])

        with self._lock:
            if target.exists() and not overwrite:
                raise FileExistsError(
                    f"Model {validated['id']!r} already exists. "
                    "Use update_model() or pass overwrite=True."
                )
            now = datetime.now().isoformat()
            record = {
                **validated,
                "created_at": (
                    self._read_yaml(target).get("created_at", now)
                    if target.exists() else now
                ),
                "updated_at": now,
            }
            self._write_yaml(target, record)

        logger.info(
            "[i] Saved model %s (provider=%s, name=%s)",
            record["id"], record["provider"], record["model_name"],
        )
        return record

    def update_model(self, model_id: str, patch: dict) -> dict:
        """Apply a partial update to an existing model.

        Merges ``patch`` over the on-disk record, re-validates, and
        writes atomically. Raises FileNotFoundError if the model
        doesn't exist (callers should use ``save_model`` for create).
        """
        target = self._yaml_path(model_id)
        with self._lock:
            if not target.exists():
                raise FileNotFoundError(
                    f"Model {model_id!r} does not exist. Use save_model() to create."
                )
            existing = self._read_yaml(target)
            # ``id`` cannot change via update - that would orphan the file
            patch = dict(patch)
            patch.pop("id", None)
            merged = {**existing, **patch, "id": existing["id"]}
            validated = ModelValidation.validate_record(merged)
            now = datetime.now().isoformat()
            record = {
                **validated,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }
            self._write_yaml(target, record)
        logger.info("[i] Updated model %s", record["id"])
        return record

    def get_model(self, model_id: str) -> Optional[dict]:
        """Return the model record for ``model_id`` or ``None`` if absent."""
        try:
            target = self._yaml_path(model_id)
        except ValueError:
            return None
        if not target.exists():
            return None
        try:
            return self._read_yaml(target)
        except Exception as exc:
            logger.warning("[!] Could not read %s: %s", target, exc)
            return None

    def list_models(self) -> list[dict]:
        """Return every model record, sorted by ``id`` ascending."""
        if not self._dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(self._dir.glob("*.yaml")):
            try:
                rec = self._read_yaml(path)
                if rec:
                    out.append(rec)
            except Exception as exc:
                logger.warning("[!] Could not read %s: %s", path, exc)
        out.sort(key=lambda r: r.get("id", ""))
        return out

    def delete_model(self, model_id: str) -> bool:
        """Hard-delete a model. Returns True if a file was removed.

        Models are configuration, not data - no soft-delete recovery
        (mirroring the macro_store / email_group_store pattern). The
        user can re-install a deleted default via :meth:`install_default`.
        """
        try:
            target = self._yaml_path(model_id)
        except ValueError:
            return False
        with self._lock:
            if not target.exists():
                return False
            try:
                target.unlink()
            except OSError as exc:
                logger.warning("[!] Could not delete %s: %s", target, exc)
                return False
        logger.info("[i] Deleted model %s", model_id)
        return True


# ── Singleton ────────────────────────────────────────────────────────

_instance: Optional[ModelStore] = None
_instance_lock = threading.Lock()


def get_store() -> ModelStore:
    """Return the process-wide ModelStore singleton, lazily initialised."""
    global _instance
    with _instance_lock:
        if _instance is None:
            store = ModelStore()
            store.initialize()
            _instance = store
        return _instance


def reset_for_tests() -> None:
    """Clear the cached singleton. Tests should call this before patching
    paths so a stale instance doesn't bleed between fixtures.
    """
    global _instance
    with _instance_lock:
        _instance = None


__all__ = [
    "MODELS_DIR",
    "DEFAULTS_DIR",
    "ModelStore",
    "get_store",
    "reset_for_tests",
]
