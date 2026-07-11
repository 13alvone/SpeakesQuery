"""
Notebook Store - Phase 3 / Bet 4 slice 1
─────────────────────────────────────────
YAML-based CRUD for ``.spqnb`` notebook files. Each notebook is one
``notebooks/<id>.spqnb`` file containing the full cell stream. The
notebook is the cell-stream YAML; per-cell content (SPQL, Python,
markdown, chart spec, parameter spec, pipe stage) lives inside each
cell's ``source`` field.

Slice 1 ships persistence only - no execution engine, no UI. Slice 2
adds the reactive engine; slice 4+ adds the Monaco-backed SPA. The
schema is forward-compatible: every future slice's additions are
optional fields the validator already tolerates.

User edits live in ``notebooks/`` (gitignored, RW). Defaults ship
under ``default_notebooks/`` (tracked in git, RO mounted in Docker)
and are seeded into ``notebooks/`` missing-only on first
:meth:`NotebookStore.initialize` via :meth:`_seed_defaults` - never
overwriting user edits. Mirrors the alert-group / saved-search /
model-store seeding pattern.

Slice 1 ships zero defaults (the ``default_notebooks/`` dir contains
only a ``.gitkeep``). The shipped ``getting_started.spqnb`` arrives
in a later Phase 3 slice once the cell-engine + UI are in place to
make it executable from the SPA.
"""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from functionality.atomic_write import write_text_atomic
from validation.NotebookValidation import NotebookValidation

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).parent.resolve()
NOTEBOOKS_DIR = _PROJECT_ROOT / "notebooks"
DEFAULTS_DIR = _PROJECT_ROOT / "default_notebooks"

# File extension for the notebook YAML. ``.spqnb`` mirrors the ROADMAP
# spec; chosen to be unique enough for editor-association rules without
# colliding with anything else in the tree.
NOTEBOOK_EXT = ".spqnb"


class NotebookStore:
    """Manages ``.spqnb`` notebook YAML files.

    Mirrors the existing model_store / alert_group_store / saved_search_store
    CRUD shape: instance attributes (`_dir`, `_defaults_dir`) so tests
    can inject tmp paths; thread-safe via `self._lock`; atomic writes
    via `write_text_atomic`.
    """

    def __init__(self):
        self._dir = NOTEBOOKS_DIR
        self._defaults_dir = DEFAULTS_DIR
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self):
        """Create the notebooks directory and seed any shipped defaults."""
        os.makedirs(self._dir, exist_ok=True)
        self._seed_defaults()
        logger.info("[i] NotebookStore initialised (dir=%s)", self._dir)

    def _seed_defaults(self):
        """Copy ``default_notebooks/*.spqnb`` into ``notebooks/``,
        missing-only.

        NEVER overwrites - fresh clones get the shipped templates, but
        a user's customisation is preserved across upgrades. Same
        idempotent contract as :meth:`AlertGroupStore._seed_defaults`.

        Slice 1 ships zero defaults; this method is a no-op until a
        later slice drops a ``getting_started.spqnb`` into
        ``default_notebooks/``.
        """
        if not self._defaults_dir.is_dir():
            logger.info(
                "[i] default_notebooks/ missing at %s - skipping seed",
                self._defaults_dir,
            )
            return
        seeded = 0
        for default_path in sorted(self._defaults_dir.glob(f"*{NOTEBOOK_EXT}")):
            target = self._dir / default_path.name
            if target.exists():
                continue
            try:
                self._copy_default(default_path, target)
                seeded += 1
                logger.info("[i] Seeded default notebook: %s", default_path.name)
            except OSError as exc:
                logger.warning(
                    "[!] Could not seed %s: %s", default_path.name, exc,
                )
        if seeded:
            logger.info(
                "[i] _seed_defaults copied %d default notebook(s)", seeded,
            )

    def _copy_default(self, src: Path, dst: Path) -> None:
        """Copy via atomic_write so a crash mid-seed never leaves a
        partial file in notebooks/.
        """
        text = src.read_text(encoding="utf-8")
        write_text_atomic(dst, text, encoding="utf-8")

    def install_default(
        self, notebook_id: str, *, overwrite: bool = False,
    ) -> bool:
        """Install a single default by id. Returns True if written.

        Used when a user has previously deleted a default and wants to
        re-install it without re-running ``_seed_defaults`` for the
        whole tree.
        """
        if not notebook_id:
            return False
        default_path = self._defaults_dir / f"{notebook_id}{NOTEBOOK_EXT}"
        if not default_path.is_file():
            logger.warning(
                "[!] install_default(%r): no such default at %s",
                notebook_id, default_path,
            )
            return False
        target = self._spqnb_path(notebook_id)
        if target.exists() and not overwrite:
            logger.info(
                "[i] install_default(%r): target exists, not overwriting",
                notebook_id,
            )
            return False
        with self._lock:
            self._copy_default(default_path, target)
        logger.info("[i] Installed default notebook: %s", notebook_id)
        return True

    def list_default_ids(self) -> list[str]:
        """Return ids of every default notebook available in default_notebooks/."""
        if not self._defaults_dir.is_dir():
            return []
        return sorted(
            p.stem for p in self._defaults_dir.glob(f"*{NOTEBOOK_EXT}")
        )

    # ------------------------------------------------------------------
    # YAML helpers
    # ------------------------------------------------------------------

    def _spqnb_path(self, notebook_id: str) -> Path:
        validated = NotebookValidation.validate_notebook_id(notebook_id)
        return self._dir / f"{validated}{NOTEBOOK_EXT}"

    @staticmethod
    def _read_yaml(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _write_yaml(path: Path, data: dict) -> None:
        text = yaml.dump(
            data, default_flow_style=False, allow_unicode=True,
            sort_keys=False,
        )
        # Notebook-level size cap. The validator already enforced per-cell
        # caps; this is a belt-and-braces check on the serialised form.
        if len(text.encode("utf-8")) > NotebookValidation.MAX_NOTEBOOK_BYTES:
            raise ValueError(
                f"Serialised notebook exceeds "
                f"{NotebookValidation.MAX_NOTEBOOK_BYTES} bytes."
            )
        write_text_atomic(path, text, encoding="utf-8")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save_notebook(self, data: dict, *, overwrite: bool = False) -> dict:
        """Validate, normalise, and persist a notebook record.

        ``overwrite=False`` (default) refuses to clobber an existing
        file, so a typo in the id at create-time never silently
        replaces a different notebook. Updates go through
        :meth:`update_notebook`.
        """
        validated = NotebookValidation.validate_record(data)
        target = self._spqnb_path(validated["id"])

        with self._lock:
            if target.exists() and not overwrite:
                raise FileExistsError(
                    f"Notebook {validated['id']!r} already exists. "
                    "Use update_notebook() or pass overwrite=True."
                )
            now = datetime.now().isoformat()
            existing_created_at = (
                self._read_yaml(target).get("created_at", now)
                if target.exists() else now
            )
            record = {
                **validated,
                "created_at": existing_created_at,
                "updated_at": now,
            }
            self._write_yaml(target, record)

        logger.info(
            "[i] Saved notebook %s (cells=%d)",
            record["id"], len(record["cells"]),
        )
        return record

    def update_notebook(self, notebook_id: str, patch: dict) -> dict:
        """Apply a partial update to an existing notebook.

        Merges ``patch`` over the on-disk record, re-validates, and
        writes atomically. Raises FileNotFoundError if the notebook
        doesn't exist (callers should use :meth:`save_notebook` for
        create).

        Note: ``cells`` is replaced wholesale when present in ``patch``,
        not merged cell-by-cell. Per-cell editing happens at the engine
        layer (slice 2+).
        """
        target = self._spqnb_path(notebook_id)
        with self._lock:
            if not target.exists():
                raise FileNotFoundError(
                    f"Notebook {notebook_id!r} does not exist. Use "
                    "save_notebook() to create."
                )
            existing = self._read_yaml(target)
            patch = dict(patch)
            patch.pop("id", None)  # id cannot change via update - it's the filename
            merged = {**existing, **patch, "id": existing["id"]}
            validated = NotebookValidation.validate_record(merged)
            now = datetime.now().isoformat()
            record = {
                **validated,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }
            self._write_yaml(target, record)
        logger.info(
            "[i] Updated notebook %s (cells=%d)",
            record["id"], len(record["cells"]),
        )
        return record

    def get_notebook(self, notebook_id: str) -> Optional[dict]:
        """Return the notebook record for ``notebook_id`` or ``None``
        if absent. Validation errors during read are logged at WARNING
        and surface as ``None`` so the caller can fall through to
        re-create / restore-from-default flows.
        """
        try:
            target = self._spqnb_path(notebook_id)
        except ValueError:
            return None
        if not target.exists():
            return None
        try:
            data = self._read_yaml(target)
            return NotebookValidation.validate_record(data)
        except Exception as exc:
            logger.warning("[!] Could not read %s: %s", target, exc)
            return None

    def list_notebooks(self) -> list[dict]:
        """Return every notebook record, sorted by ``id`` ascending.

        Notebooks that fail validation on read are SKIPPED (with a
        WARNING) rather than poisoning the list - the operator can
        still see + edit the others.
        """
        if not self._dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(self._dir.glob(f"*{NOTEBOOK_EXT}")):
            try:
                rec = self._read_yaml(path)
                if not rec:
                    continue
                out.append(NotebookValidation.validate_record(rec))
            except Exception as exc:
                logger.warning("[!] Could not read %s: %s", path, exc)
        out.sort(key=lambda r: r.get("id", ""))
        return out

    def list_notebook_ids(self) -> list[str]:
        """Lighter-weight listing: just the ids on disk. Skips reading
        cell content. Useful for nav menus / autocomplete.
        """
        if not self._dir.is_dir():
            return []
        return sorted(p.stem for p in self._dir.glob(f"*{NOTEBOOK_EXT}"))

    def delete_notebook(self, notebook_id: str) -> bool:
        """Hard-delete a notebook. Returns True if a file was removed.

        Notebooks are configuration + working state, not append-only data
        - no soft-delete recovery (mirroring model_store / macro_store).
        The user can re-install a deleted default via
        :meth:`install_default`; their own work is gone.
        """
        try:
            target = self._spqnb_path(notebook_id)
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
        logger.info("[i] Deleted notebook %s", notebook_id)
        return True


# ── Singleton ────────────────────────────────────────────────────────

_instance: Optional[NotebookStore] = None
_instance_lock = threading.Lock()


def get_store() -> NotebookStore:
    """Return the process-wide NotebookStore singleton, lazily initialised."""
    global _instance
    with _instance_lock:
        if _instance is None:
            store = NotebookStore()
            store.initialize()
            _instance = store
        return _instance


def reset_for_tests() -> None:
    """Clear the cached singleton. Tests should call this before
    patching paths so a stale instance doesn't bleed between fixtures.
    """
    global _instance
    with _instance_lock:
        _instance = None


__all__ = [
    "NOTEBOOKS_DIR",
    "DEFAULTS_DIR",
    "NOTEBOOK_EXT",
    "NotebookStore",
    "get_store",
    "reset_for_tests",
]
