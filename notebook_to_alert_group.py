"""
Notebook → Alert Group converter - Phase 3 / Bet 4 slice 9 (the headliner)
─────────────────────────────────────────────────────────────────────────
The dev → production gap collapses to one cell: ``promote_to_alert_group``.
The cell carries the AG metadata (name, schedule, recipient, search_names,
prompt_cell ref) in ``cell.metadata``; the notebook engine renders a
DRY-RUN preview at execution time (it never writes to ``alert_groups/``);
the explicit ``POST /api/notebooks/<id>/promote/<cell_id>`` endpoint
performs the actual save.

Three load-bearing concerns split across the public surface:

* :func:`build_promote_preview` - engine path. Returns a structured
  preview (``would_create_or_update`` + ``target_yaml`` + ``validation``
  + ``feeder_status`` + ``current_ag`` for diff). NEVER mutates AG state.

* :func:`promote_cell_to_ag` - deploy path. Calls
  :meth:`AlertGroupStore.save_group` / :meth:`update_group`. The ONLY
  function in this module that may mutate AG state. The drift-guard
  ``test_engine_path_does_not_invoke_save_group`` patches save/update
  with ``AssertionError("CONFIG LEAK")`` and runs a notebook with a
  promote cell; both invocations must stay zero.

* :func:`alert_group_to_notebook` - round-trip the other direction. Build
  a synthetic notebook record from an existing AG so the operator can
  open ``existing_ag → notebook → tweak → re-promote``. Pure function;
  no persistence side effects.

Every helper here is import-light (lazy import for AG store / saved-search
store / log writer) so the validators / docs path doesn't spin up the
full app on import.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Cell extraction helpers
# ─────────────────────────────────────────────────────────────────────

def _find_cell(notebook: dict, cell_id: str) -> Optional[dict]:
    """Locate a cell by id within a notebook record. Returns ``None``
    if the cell isn't present (callers surface that as a 404 / structured
    error).
    """
    for cell in (notebook.get("cells") or []):
        if cell.get("id") == cell_id:
            return cell
    return None


def _resolve_prompt_text(notebook: dict, prompt_cell_id: str) -> str:
    """Return the source of the cell whose id matches ``prompt_cell_id``.

    This becomes the AG's ``prompt_text``. We deliberately use the cell's
    LITERAL source - not the executed output - so deploying is fully
    deterministic and doesn't depend on whether the operator clicked Run
    All recently. The prompt is the cell text, period.

    Raises ``ValueError`` if the cell doesn't exist (the schema validator
    already catches this at notebook save time, but the deploy path
    re-checks defensively).
    """
    cell = _find_cell(notebook, prompt_cell_id)
    if cell is None:
        raise ValueError(
            f"prompt_cell={prompt_cell_id!r} not found in notebook "
            f"{notebook.get('id', '<unknown>')!r}."
        )
    src = (cell.get("source") or "").strip()
    if not src:
        raise ValueError(
            f"prompt_cell={prompt_cell_id!r} has empty source - the AG "
            f"prompt_text would be empty."
        )
    return src


def extract_ag_payload(notebook: dict, cell_id: str) -> dict:
    """Build the canonical AG payload (the dict that would be passed
    into ``AlertGroupStore.save_group`` / ``update_group``).

    Pure transformation: notebook record + cell_id → AG payload dict.
    No I/O. Raises ``ValueError`` on bad config.
    """
    cell = _find_cell(notebook, cell_id)
    if cell is None:
        raise ValueError(
            f"Cell {cell_id!r} not found in notebook "
            f"{notebook.get('id', '<unknown>')!r}."
        )
    if cell.get("type") != "promote_to_alert_group":
        raise ValueError(
            f"Cell {cell_id!r} is type {cell.get('type')!r}, expected "
            f"'promote_to_alert_group'."
        )

    metadata = dict(cell.get("metadata") or {})
    prompt_cell_id = metadata.get("prompt_cell")
    if not prompt_cell_id:
        raise ValueError(
            f"Cell {cell_id!r} is missing required metadata.prompt_cell."
        )

    prompt_text = _resolve_prompt_text(notebook, prompt_cell_id)

    payload = {
        "name": metadata["name"],
        "description": metadata.get("description", ""),
        "search_names": list(metadata.get("search_names") or []),
        "prompt_text": prompt_text,
        "schedule": metadata["schedule"],
        "timezone": metadata.get("timezone", "UTC"),
        "max_rows": int(metadata.get("max_rows", 200)),
        "email_address": metadata.get("email_address", ""),
        "admin_error_email": metadata.get("admin_error_email", ""),
        "error_email_disabled": bool(
            metadata.get("error_email_disabled", False),
        ),
        "delivery_mode": metadata.get("delivery_mode", "api"),
        "disabled": bool(metadata.get("disabled", False)),
    }

    # Pass through optional production-hardening fields if the operator
    # set them in the cell metadata. Omitted → AG inherits global
    # defaults at dispatch time. Only fields present in
    # ``AlertGroupStore.update_group``'s ``updatable`` tuple flow
    # through; anything else stays in the cell metadata only.
    PASS_THROUGH = (
        "max_cost_usd_per_run", "max_cost_usd_per_day",
        "max_dispatches_per_day", "min_interval_between_runs_hours",
        "max_output_tokens", "max_feeder_staleness_hours",
        "fail_on_stale_feeder", "email_template_override",
    )
    for key in PASS_THROUGH:
        if key in metadata and metadata.get(key) is not None:
            payload[key] = metadata[key]

    return payload


# ─────────────────────────────────────────────────────────────────────
# Pre-flight feeder + AG resolution (read-only)
# ─────────────────────────────────────────────────────────────────────

def _feeder_status(search_names: list) -> list:
    """Resolve each referenced saved_search and report its existence
    plus last-run telemetry. Read-only - no mutation.

    Per-feeder dict shape:
      {name, exists: bool, has_data: bool|None,
       cron_schedule: str, last_run_at: str|None, error: str|None}

    ``has_data`` is informational only - the deploy never blocks on
    missing data (an AG with a fresh feeder is a valid first-run state).
    """
    out = []
    if not search_names:
        return out
    try:
        from saved_search_store import SavedSearchStore
    except Exception as exc:
        logger.warning("[!] _feeder_status: SavedSearchStore import failed: %s", exc)
        # Best-effort: report unknown for every feeder rather than raise
        return [
            {
                "name": name, "exists": None, "has_data": None,
                "cron_schedule": "", "last_run_at": None,
                "error": f"introspection_failed: {exc}",
            }
            for name in search_names
        ]
    store = SavedSearchStore()
    try:
        store.initialize()
    except Exception as exc:
        logger.warning("[!] _feeder_status: SavedSearchStore init failed: %s", exc)
    for name in search_names:
        entry: dict = {
            "name": name, "exists": False, "has_data": None,
            "cron_schedule": "", "last_run_at": None, "error": None,
        }
        try:
            ss = store.get_search(name)
            entry["exists"] = True
            entry["cron_schedule"] = (ss.get("cron_schedule") or "").strip()
        except FileNotFoundError:
            entry["error"] = "saved_search_not_found"
        except Exception as exc:
            entry["error"] = f"introspection_failed: {exc}"
        out.append(entry)
    return out


def _existing_ag(name: str) -> Optional[dict]:
    """Return the current AG record matching ``name`` (or ``None``).
    Used to compute the create-vs-update decision + diff."""
    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        return store.get_group(name)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("[!] _existing_ag(%r): introspection failed: %s", name, exc)
        return None


def _diff_payload_vs_existing(
    payload: dict, existing: Optional[dict],
) -> dict:
    """Compute a per-field diff between the would-be payload and the
    current AG. ``existing=None`` → would_create. Identical → no_change.
    Returns a dict with ``decision`` + ``changed_fields``.
    """
    if existing is None:
        return {"decision": "create", "changed_fields": []}
    changed = []
    # Compare every field the payload sets. Fields the payload omits
    # would carry the AG store's defaults (or remain unchanged via the
    # update path), so they don't count as changes for diff purposes.
    for key, new_value in payload.items():
        old_value = existing.get(key)
        # Trim string whitespace for comparison so ``"foo"`` vs ``"foo "``
        # doesn't produce a spurious diff.
        if isinstance(new_value, str) and isinstance(old_value, str):
            if new_value.strip() == old_value.strip():
                continue
        elif new_value == old_value:
            continue
        changed.append({"field": key, "old": old_value, "new": new_value})
    if not changed:
        return {"decision": "no_change", "changed_fields": []}
    return {"decision": "update", "changed_fields": changed}


# ─────────────────────────────────────────────────────────────────────
# Public surface - preview / deploy / round-trip
# ─────────────────────────────────────────────────────────────────────

def build_promote_preview(notebook: dict, cell_id: str) -> dict:
    """Build a structured preview of the AG that WOULD be created/updated
    by deploying this cell. Pure function - never writes.

    Returned dict (``schema_version: 1``, ``kind:
    "promote_to_alert_group_preview"``):

    * ``decision`` - one of ``create | update | no_change``
    * ``changed_fields`` - list of {field, old, new} when decision=update
    * ``target_payload`` - the dict that would be saved
    * ``current_ag`` - the existing AG record (if any) for diff context
    * ``feeder_status`` - list of pre-flight feeder dicts
    * ``validation`` - {errors: [...], warnings: [...]}
    * ``deploy_endpoint`` - convenience hint for the SPA / AI consumer

    The dual-audience contract: humans see a preview pane in the SPA;
    AI agents introspect the structured dict directly. Same shape as
    the slice-7 ``| llm`` dry-run sentinel row.
    """
    errors: list = []
    warnings: list = []

    # Layer 1: extract + canonicalise. Either succeeds or surfaces the
    # bad config.
    try:
        payload = extract_ag_payload(notebook, cell_id)
    except ValueError as exc:
        return {
            "schema_version": 1,
            "kind": "promote_to_alert_group_preview",
            "decision": "blocked",
            "changed_fields": [],
            "target_payload": None,
            "current_ag": None,
            "feeder_status": [],
            "validation": {"errors": [str(exc)], "warnings": []},
            "deploy_endpoint": None,
        }

    # Layer 2: re-validate the payload through the AG store's own
    # validators. Catches anything the cell-metadata validator missed
    # (or anything that drifted between the two surfaces).
    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        # AlertGroupStore._validate is a private method - we call it by
        # name because we want the EXACT same rules the save path uses,
        # not a re-implementation. If the AG store ever stops exposing
        # _validate, the test ``test_payload_validates_through_ag_store``
        # will fail loud.
        store._validate(payload)  # noqa: SLF001 - load-bearing reuse
    except Exception as exc:
        errors.append(f"AG validation: {exc}")

    # Layer 3: pre-flight feeders. Missing feeders are warnings (the
    # AG can be saved + start firing once feeders exist) but listed
    # explicitly so the operator decides knowingly.
    fs = _feeder_status(payload.get("search_names") or [])
    for entry in fs:
        if entry.get("exists") is False:
            warnings.append(
                f"feeder {entry['name']!r} does not exist as a saved "
                f"search - create it before the AG fires, or this run "
                f"will fail at dispatch."
            )

    existing = _existing_ag(payload["name"])
    diff = _diff_payload_vs_existing(payload, existing)

    return {
        "schema_version": 1,
        "kind": "promote_to_alert_group_preview",
        "decision": diff["decision"] if not errors else "blocked",
        "changed_fields": diff["changed_fields"],
        "target_payload": payload,
        "current_ag": existing,
        "feeder_status": fs,
        "validation": {"errors": errors, "warnings": warnings},
        "deploy_endpoint": (
            f"/api/notebooks/{notebook.get('id', '')}/promote/{cell_id}"
        ),
    }


def promote_cell_to_ag(
    notebook: dict, cell_id: str, *,
    overwrite_existing: bool = True,
) -> dict:
    """Actually save (or update) the AG. The ONLY function in this
    module that mutates AG state.

    ``overwrite_existing=True`` (default) lets a re-deploy of a cell
    update an AG that already has the same name - the headliner
    workflow. Pass ``False`` to fail when the name is taken (useful
    for "I want a fresh AG" intent).

    Returns the saved AG record (the same dict shape as
    :meth:`AlertGroupStore.save_group` / ``update_group``).

    Raises ``ValueError`` on bad config; ``FileExistsError`` on
    ``overwrite_existing=False`` collisions.

    Drift-guarded by ``test_engine_path_does_not_invoke_save_group``:
    this is the ONLY function in this module + the cell-engine path
    that may invoke ``save_group`` / ``update_group``. The notebook
    execution path goes through ``build_promote_preview`` only.
    """
    payload = extract_ag_payload(notebook, cell_id)

    from alert_group_store import AlertGroupStore
    store = AlertGroupStore()
    store.initialize()

    existing = _existing_ag(payload["name"])

    if existing is None:
        # Fresh save. ``overwrite_existing`` doesn't apply - there's
        # nothing to overwrite.
        record = store.save_group(payload, overwrite=False)
        action = "created"
    else:
        if not overwrite_existing:
            raise FileExistsError(
                f'Alert group "{payload["name"]}" already exists; pass '
                f"overwrite_existing=True to update."
            )
        record = store.update_group(payload["name"], payload)
        action = "updated"

    logger.info(
        "[+] promote_to_alert_group: %s AG %r from notebook %r/cell %r",
        action, payload["name"], notebook.get("id"), cell_id,
    )

    # Emit a structured config-log row tying the AG back to its source
    # notebook + cell so the operator can trace "where did this AG come
    # from?" months later. Best-effort - never blocks the deploy.
    try:
        from functionality.log_writer import log_config_change
        log_config_change(
            subject=payload["name"],
            action=f"promote_from_notebook_{action}",
            subject_type="alert_group",
            old_value=existing,
            new_value=record,
            actor="notebook",
            source=(
                f"notebook:{notebook.get('id', '')}/cell:{cell_id}"
            ),
        )
    except Exception as exc:
        logger.warning(
            "[!] promote_to_alert_group: config log write failed: %s", exc,
        )

    return record


# ─────────────────────────────────────────────────────────────────────
# Round-trip: AG → notebook
# ─────────────────────────────────────────────────────────────────────

def alert_group_to_notebook(ag: dict) -> dict:
    """Synthesise a notebook record from an existing AG. The caller can
    save the result via the normal notebook store CRUD path to get an
    editable copy.

    Layout of the synthesised notebook:

    1. ``intro`` (markdown) - describes the source AG + how to use this
       notebook.
    2. ``feeder_<i>`` (spql) - one cell per saved-search feeder, source
       loaded from the saved_search_store. Cells are named
       ``feeder_1`` / ``feeder_2`` / ... so they're Python-identifier-
       compatible (the cell id rules require lowercase-leading).
    3. ``prompt`` (pipe) - the AG's prompt_text.
    4. ``deploy`` (promote_to_alert_group) - pre-filled metadata
       matching the source AG. Re-deploying overwrites the original.

    The notebook id mirrors ``from_ag_<sanitized-ag-name>`` so the
    operator can have multiple round-trip notebooks open at once
    without filename collisions. Notebook is NOT saved - caller
    decides whether to persist.
    """
    if not isinstance(ag, dict):
        raise ValueError(
            f"alert_group_to_notebook: expected dict, got {type(ag).__name__}."
        )
    name = (ag.get("name") or "").strip()
    if not name:
        raise ValueError("alert_group_to_notebook: AG record has no name.")

    # Sanitise into a notebook-id-safe slug.
    import re as _re
    nb_slug = _re.sub(r"[^a-z0-9._\-]+", "_", name.lower()).strip("_-.")
    nb_id = f"from_ag_{nb_slug}" if nb_slug else "from_ag"

    cells: list[dict] = [
        {
            "id": "intro",
            "type": "markdown",
            "source": (
                f"# Round-trip from alert group `{name}`\n\n"
                f"This notebook was generated from the existing AG\n"
                f"`{name}`.\n\n"
                "* `feeder_*` cells load each referenced saved-search query\n"
                "  for inspection / iteration.\n"
                "* `prompt` carries the AG's `prompt_text` verbatim - edit\n"
                "  it freely.\n"
                "* `deploy` is a `promote_to_alert_group` cell pre-filled\n"
                "  with the source AG's metadata. Re-running it (via\n"
                "  the Deploy button on the cell) overwrites the\n"
                "  original AG.\n"
            ),
            "metadata": {},
        },
    ]

    # Feeder cells. Best-effort: if a feeder doesn't resolve, we still
    # write a placeholder cell so the operator sees what was referenced.
    search_names = list(ag.get("search_names") or [])
    feeder_cell_ids: list[str] = []
    try:
        from saved_search_store import SavedSearchStore
        ss_store = SavedSearchStore()
        try:
            ss_store.initialize()
        except Exception:
            pass
    except Exception:
        ss_store = None

    for i, ss_name in enumerate(search_names, start=1):
        cid = f"feeder_{i}"
        feeder_cell_ids.append(cid)
        query_body = ""
        if ss_store is not None:
            try:
                ss = ss_store.get_search(ss_name)
                query_body = (ss.get("query") or "").strip()
            except FileNotFoundError:
                query_body = (
                    f"# Saved search '{ss_name}' was referenced but does "
                    f"not exist. Create it before deploying."
                )
            except Exception as exc:
                query_body = f"# Could not load saved search {ss_name!r}: {exc}"
        cells.append({
            "id": cid,
            "type": "spql",
            "source": query_body or f"# Empty query for saved search {ss_name!r}",
            "metadata": {"source_saved_search": ss_name},
        })

    # Prompt cell carries the AG's prompt verbatim.
    cells.append({
        "id": "prompt",
        "type": "pipe",
        "source": (ag.get("prompt_text") or "").rstrip() + "\n",
        "metadata": {},
    })

    # Deploy cell - every AG field that the converter passes through
    # gets reflected back here so a no-op re-deploy is bit-identical
    # to the source AG.
    deploy_metadata: dict[str, Any] = {
        "name": name,
        "description": ag.get("description", ""),
        "schedule": ag.get("schedule", ""),
        "timezone": ag.get("timezone", "UTC"),
        "email_address": ag.get("email_address", ""),
        "admin_error_email": ag.get("admin_error_email", ""),
        "error_email_disabled": bool(ag.get("error_email_disabled", False)),
        "delivery_mode": ag.get("delivery_mode", "api"),
        "max_rows": int(ag.get("max_rows", 200)),
        "search_names": list(search_names),
        "prompt_cell": "prompt",
        "disabled": bool(ag.get("disabled", False)),
    }
    # Optional pass-through fields - only included if the source AG
    # had them set, to keep the round-tripped notebook minimal.
    for key in (
        "max_cost_usd_per_run", "max_cost_usd_per_day",
        "max_dispatches_per_day", "min_interval_between_runs_hours",
        "max_output_tokens", "max_feeder_staleness_hours",
        "fail_on_stale_feeder", "email_template_override",
    ):
        if key in ag and ag.get(key) not in (None, ""):
            deploy_metadata[key] = ag[key]

    # Source carries the YAML form of the AG config (Monaco-editable
    # in the SPA). Metadata carries the same dict (programmatic
    # consumers don't have to re-parse). The validator merges them
    # with metadata-takes-precedence; for the round-trip case both
    # are identical so the merge is a no-op.
    import yaml as _yaml
    deploy_source_yaml = _yaml.dump(
        deploy_metadata, default_flow_style=False, sort_keys=False,
        allow_unicode=True,
    )
    cells.append({
        "id": "deploy",
        "type": "promote_to_alert_group",
        "source": deploy_source_yaml,
        "metadata": deploy_metadata,
    })

    notebook = {
        "id": nb_id,
        "schema_version": 1,
        "name": f"AG round-trip: {name}",
        "description": (
            f"Generated from alert group `{name}` for editing. Re-deploy via the "
            f"`deploy` cell to update the source AG, or save under a new name "
            f"to spawn a sibling AG."
        ),
        "default_max_cost_usd": 0.0,
        "cells": cells,
    }
    return notebook


# ─────────────────────────────────────────────────────────────────────
# Module-level export list
# ─────────────────────────────────────────────────────────────────────

__all__ = [
    "extract_ag_payload",
    "build_promote_preview",
    "promote_cell_to_ag",
    "alert_group_to_notebook",
]
