"""
Notebook Validation
───────────────────
Static validators for ``.spqnb`` notebook YAML records (Phase 3 / Bet 4
slice 1).

A notebook is a *cell stream*: an ordered list of cells where each
cell's output is the typed DataFrame input to the next. The cell-type
enum is closed and stable (slice 1 ships the schema; slice 2 wires
execution; slice 3 wires reactive caching).

Notebooks are admin tools - the audience is the same as a developer
running VS Code. The ``python`` cell type runs **full Python**, NOT
RestrictedPython. The RestrictedPython sandbox stays scoped to
ingestion scripts (different threat model - those are user-supplied
data feeders that can come from the script library; notebooks are
operator-authored on a trusted local machine).

The id field doubles as the filename stem (sanitized) so it must be
filename-safe AND unique. Cell ids must be Python-identifier-like
because reactive execution exposes them as variable names in Python
cells (``cell_1``, ``cell_2``, ``candidates``, etc.).

Schema versioning: the ``schema_version`` field starts at ``1``; every
future change to the cell record shape (new optional fields, new cell
types) increments by 1 and remains backward-compatible (additive only).
"""

import re

import yaml

# Notebook id - filename-safe identifier (lowercase letters, digits,
# hyphen, underscore, dot for version markers like "news_triage_v2").
# Mirrors model_store ID_REGEX exactly (same filename-on-disk constraint).
_NOTEBOOK_ID_REGEX = re.compile(r"^[a-z0-9._\-]+$")

# Cell id - Python-identifier-like so reactive execution can expose it
# as a variable name in downstream Python cells. Lowercase only to keep
# the case-insensitive YAML-key habit from leaking into runtime.
# Excludes leading digits (Python identifier rule).
_CELL_ID_REGEX = re.compile(r"^[a-z][a-z0-9_]*$")


# Closed enum of cell types. Order matters only for documentation;
# any future addition is a slice-level change with backward-compat
# guarantees (additive only - no type ever removed).
ALLOWED_CELL_TYPES = frozenset({
    # SPQL pipe expression. Slice 2 evaluates via the existing
    # CmdExecutionBackend pathway. Output is a pandas DataFrame.
    "spql",
    # Full Python code. Slice 2 evaluates via ``exec()`` in a per-
    # notebook namespace; previous cells exposed as named variables.
    # NOT RestrictedPython - this is admin-only by design (per user
    # direction 2026-05-08; the RestrictedPython sandbox is reserved
    # for the ingestion-script use case where untrusted code runs).
    "python",
    # Chart spec. Slice 7 wires the renderer; the cell source contains
    # a chart definition (Vega-Lite spec by default). Output is a
    # rendered figure image / HTML.
    "chart",
    # Markdown text. Output is the rendered HTML; cells can interpolate
    # values from prior cells via standard string formatting in slice 7.
    "markdown",
    # Parameter input - a typed input form (dropdown, slider, text)
    # exposed as a variable to downstream cells. Slice 6 wires the form
    # rendering; slice 1 just persists the spec.
    "param",
    # LLM pipe stage. Distinguished from ``spql`` for UI rendering only -
    # the underlying execution still goes through CmdExecutionBackend
    # (a ``pipe`` cell typically starts with ``| llm`` or ``| llm_batch``).
    # Slice 5 (Phase 3) renders this with LLM-aware affordances (model
    # picker, prompt editor) on top of the SPQL primitive.
    "pipe",
    # Promote-to-alert-group (Phase 3 / Bet 4 slice 9 - the headliner).
    # The cell carries AG metadata in ``cell.metadata`` (name, schedule,
    # search_names, prompt_cell ref, recipient, etc.). Engine execution
    # is ALWAYS DRY-RUN - the cell returns a structured preview of the
    # AG that WOULD be created/updated; it never calls ``save_group`` /
    # ``update_group`` from the notebook-execution path. Actual deploy
    # is a separate, explicit operator action via
    # ``POST /api/notebooks/<id>/promote/<cell_id>``. This mirrors the
    # slice-7 ``| llm`` budget-gate pattern: every billable / state-
    # mutating notebook surface ships with a "config-leak canary"
    # drift guard so a re-run / cache-miss can never silently mutate
    # production state.
    "promote_to_alert_group",
})


class NotebookValidation:
    """Static validators for ``.spqnb`` notebook YAML records."""

    NOTEBOOK_ID_REGEX = _NOTEBOOK_ID_REGEX
    CELL_ID_REGEX = _CELL_ID_REGEX
    ALLOWED_CELL_TYPES = ALLOWED_CELL_TYPES

    # Limits - generous so legitimate use never bumps them, strict
    # enough that a malformed input fails loud.
    MAX_NOTEBOOK_ID_LEN = 64
    MAX_CELL_ID_LEN = 32
    MAX_NAME_LEN = 200
    MAX_DESCRIPTION_LEN = 4000
    MAX_CELL_SOURCE_BYTES = 100 * 1024        # 100 KB per cell
    MAX_NOTEBOOK_BYTES = 5 * 1024 * 1024      # 5 MB per .spqnb (post-serialise)
    MAX_CELLS_PER_NOTEBOOK = 200

    # Schema versioning. Slice 1 ships v1. Bump on additive-only
    # changes; the store's read path tolerates older versions forever.
    CURRENT_SCHEMA_VERSION = 1

    # ── Field-level validators ─────────────────────────────────────

    @staticmethod
    def validate_notebook_id(notebook_id):
        if not isinstance(notebook_id, str) or not notebook_id.strip():
            raise ValueError("Notebook id must be a non-empty string.")
        s = notebook_id.strip()
        if not _NOTEBOOK_ID_REGEX.match(s):
            raise ValueError(
                f"Invalid notebook id: {s!r}. "
                "Only lowercase letters, digits, underscore, hyphen, "
                "and dot are permitted (no spaces, no uppercase)."
            )
        if len(s) > NotebookValidation.MAX_NOTEBOOK_ID_LEN:
            raise ValueError(
                f"Notebook id must be {NotebookValidation.MAX_NOTEBOOK_ID_LEN} "
                "characters or fewer."
            )
        return s

    @staticmethod
    def validate_cell_id(cell_id):
        if not isinstance(cell_id, str) or not cell_id.strip():
            raise ValueError("Cell id must be a non-empty string.")
        s = cell_id.strip()
        if not _CELL_ID_REGEX.match(s):
            raise ValueError(
                f"Invalid cell id: {s!r}. "
                "Must be a Python-identifier-like name "
                "(lowercase letters, digits, underscore; "
                "no leading digit; no hyphen)."
            )
        if len(s) > NotebookValidation.MAX_CELL_ID_LEN:
            raise ValueError(
                f"Cell id must be {NotebookValidation.MAX_CELL_ID_LEN} "
                "characters or fewer."
            )
        return s

    @staticmethod
    def validate_cell_type(cell_type):
        if not isinstance(cell_type, str) or not cell_type.strip():
            raise ValueError("Cell type is required (string).")
        s = cell_type.strip().lower()
        if s not in ALLOWED_CELL_TYPES:
            raise ValueError(
                f"Unknown cell type: {cell_type!r}. "
                f"Allowed: {sorted(ALLOWED_CELL_TYPES)}."
            )
        return s

    @staticmethod
    def validate_cell_source(source):
        if source is None:
            return ""
        if not isinstance(source, str):
            raise ValueError(
                f"Cell source must be a string when provided, got "
                f"{type(source).__name__}."
            )
        if len(source.encode("utf-8")) > NotebookValidation.MAX_CELL_SOURCE_BYTES:
            raise ValueError(
                f"Cell source exceeds {NotebookValidation.MAX_CELL_SOURCE_BYTES} bytes."
            )
        return source

    @staticmethod
    def validate_optional_string(value, *, name, max_len):
        """Validate optional name / description fields."""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string when provided.")
        s = value.strip() if name in ("name",) else value
        if len(s) > max_len:
            raise ValueError(
                f"{name} must be {max_len} characters or fewer."
            )
        return s

    @staticmethod
    def validate_default_max_cost_usd(value):
        """Per-notebook implicit budget cap. Mirrors the slice-7
        ``llm_default_max_cost_usd`` global setting: ``0.0`` = uncapped.
        """
        if value is None:
            return 0.0
        if isinstance(value, bool):
            raise ValueError("default_max_cost_usd must be a number, got bool.")
        if not isinstance(value, (int, float)):
            raise ValueError(
                f"default_max_cost_usd must be a number, got "
                f"{type(value).__name__}."
            )
        if value < 0:
            raise ValueError(
                f"default_max_cost_usd must be non-negative, got {value}."
            )
        if value > 1000.0:
            raise ValueError(
                f"default_max_cost_usd ceiling is 1000.0 USD, got {value}."
            )
        return float(value)

    # ── promote_to_alert_group metadata validator (slice 9) ────────

    # Fields that the promote_to_alert_group cell.metadata MUST carry.
    # Anything required by the eventual AG record (name, schedule,
    # email, search_names, prompt source) is checked here so a bad
    # config fails LOUD at notebook save time - not weeks later when
    # the operator finally clicks Deploy.
    PROMOTE_REQUIRED_METADATA_FIELDS = (
        "name", "schedule", "email_address", "search_names", "prompt_cell",
    )

    # Optional metadata fields the converter passes through to the AG
    # record. Listed here for the validator's "unknown field" check -
    # the canonical schema lives in ``alert_group_store.update_group``'s
    # ``updatable`` tuple, kept in sync via the test in
    # ``tests/test_notebook_slice9_promote.py``.
    PROMOTE_OPTIONAL_METADATA_FIELDS = (
        "description", "timezone", "admin_error_email",
        "error_email_disabled", "delivery_mode", "max_rows",
        "disabled",
        # Production-hardening pass-through (per-AG cost / staleness
        # gates from the Phase 2 hardening pass). Optional; omit to
        # inherit the global defaults at AG dispatch time.
        "max_cost_usd_per_run", "max_cost_usd_per_day",
        "max_dispatches_per_day", "min_interval_between_runs_hours",
        "max_output_tokens", "max_feeder_staleness_hours",
        "fail_on_stale_feeder", "email_template_override",
    )

    @classmethod
    def validate_promote_metadata(cls, metadata, *, sibling_cell_ids=None):
        """Validate the metadata dict on a ``promote_to_alert_group`` cell.

        ``sibling_cell_ids`` is the set of cell ids in the same notebook;
        the validator uses it to confirm ``prompt_cell`` resolves to a
        real cell. Pass ``None`` (default) to skip that cross-check -
        useful when validating a cell in isolation (e.g. unit tests).
        Returns a normalised metadata dict.
        """
        if not isinstance(metadata, dict):
            raise ValueError(
                f"promote_to_alert_group metadata must be a mapping, got "
                f"{type(metadata).__name__}."
            )

        # Required-field presence check first - surfaces the clearest
        # error message when the operator forgets one.
        missing = [
            f for f in cls.PROMOTE_REQUIRED_METADATA_FIELDS
            if f not in metadata or metadata.get(f) in (None, "", [])
        ]
        if missing:
            raise ValueError(
                f"promote_to_alert_group metadata missing required field(s): "
                f"{', '.join(missing)}. Required: "
                f"{', '.join(cls.PROMOTE_REQUIRED_METADATA_FIELDS)}."
            )

        # Reject unknown fields LOUD - better than silently dropping a
        # typo'd ``schedul:`` and shipping an unscheduled AG.
        allowed = (
            set(cls.PROMOTE_REQUIRED_METADATA_FIELDS)
            | set(cls.PROMOTE_OPTIONAL_METADATA_FIELDS)
        )
        unknown = [k for k in metadata.keys() if k not in allowed]
        if unknown:
            raise ValueError(
                f"promote_to_alert_group metadata has unknown field(s): "
                f"{', '.join(sorted(unknown))}. Allowed: "
                f"{', '.join(sorted(allowed))}."
            )

        # Lazy import - keeps NotebookValidation import-light for the
        # CRUD path that doesn't touch promote cells.
        from validation.AlertGroupValidation import AlertGroupValidation

        # Re-use AG validators field-by-field so the rules stay in lockstep
        # with the alert-group store's own validation.
        normalised = dict(metadata)
        normalised["name"] = AlertGroupValidation.validate_name(
            metadata.get("name", ""),
        )
        normalised["schedule"] = AlertGroupValidation.validate_schedule(
            metadata.get("schedule", ""),
        )
        if not normalised["schedule"]:
            raise ValueError(
                "promote_to_alert_group metadata 'schedule' must be a "
                "non-empty cron expression - an AG without a schedule "
                "would never fire."
            )
        normalised["email_address"] = AlertGroupValidation.validate_email(
            metadata.get("email_address", ""),
        )
        normalised["search_names"] = AlertGroupValidation.validate_search_names(
            metadata.get("search_names", []),
        )
        if "timezone" in metadata:
            normalised["timezone"] = AlertGroupValidation.validate_timezone(
                metadata.get("timezone"),
            )
        if "delivery_mode" in metadata:
            normalised["delivery_mode"] = (
                AlertGroupValidation.validate_delivery_mode(
                    metadata.get("delivery_mode"),
                )
            )
        if "max_rows" in metadata:
            normalised["max_rows"] = AlertGroupValidation.validate_max_rows(
                metadata.get("max_rows"),
            )
        if "admin_error_email" in metadata and metadata.get("admin_error_email"):
            normalised["admin_error_email"] = (
                AlertGroupValidation.validate_email(
                    metadata.get("admin_error_email"),
                )
            )

        # ``prompt_cell`` must look like a cell id and (when sibling ids
        # are supplied) actually resolve to one.
        prompt_cell = metadata.get("prompt_cell", "")
        normalised["prompt_cell"] = cls.validate_cell_id(prompt_cell)
        if sibling_cell_ids is not None:
            if normalised["prompt_cell"] not in sibling_cell_ids:
                raise ValueError(
                    f"promote_to_alert_group: prompt_cell="
                    f"{normalised['prompt_cell']!r} does not match any "
                    f"cell id in this notebook. Available cell ids: "
                    f"{sorted(sibling_cell_ids)}."
                )

        return normalised

    # ── Cell + notebook record validators ──────────────────────────

    @classmethod
    def validate_cell(cls, data: dict, *, position: int = -1) -> dict:
        """Validate + normalise a single cell record. ``position`` is
        used for error context (so the operator sees which cell failed
        when validating a whole notebook).
        """
        loc = f"cell #{position}" if position >= 0 else "cell"
        if not isinstance(data, dict):
            raise ValueError(f"{loc} must be a mapping (dict), got {type(data).__name__}.")

        cell_id = data.get("id", "")
        try:
            cell_id = cls.validate_cell_id(cell_id)
        except ValueError as exc:
            raise ValueError(f"{loc}: {exc}") from exc

        try:
            cell_type = cls.validate_cell_type(data.get("type", ""))
        except ValueError as exc:
            raise ValueError(f"{loc} ({cell_id!r}): {exc}") from exc

        try:
            source = cls.validate_cell_source(data.get("source", ""))
        except ValueError as exc:
            raise ValueError(f"{loc} ({cell_id!r}): {exc}") from exc

        # ``metadata`` is a free-form dict reserved for cell-type-specific
        # config (chart layout, param input options, pipe model defaults).
        # Slice 1 just stores it; slice 5+ defines the per-type schemas.
        # Slice 9 adds promote_to_alert_group with its own strict schema
        # validated below - the catch-all "must be a dict" check stays
        # for every other cell type.
        metadata = data.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError(
                f"{loc} ({cell_id!r}): metadata must be a mapping when "
                f"provided, got {type(metadata).__name__}."
            )

        # promote_to_alert_group metadata is load-bearing - validate
        # eagerly so a malformed config fails at notebook save time,
        # not weeks later when the operator clicks Deploy.
        #
        # For these cells, ``source`` IS the YAML form of the AG config
        # (Monaco-editable in the SPA, hand-editable in the .spqnb
        # file). We parse it, layer onto any explicit ``metadata`` dict
        # supplied by the caller (round-trip path uses metadata
        # directly), and validate the merged shape. We can't check
        # ``prompt_cell`` cross-references here because validate_cell
        # only sees one cell at a time; the cross-check runs in
        # validate_record (whole notebook).
        if cell_type == "promote_to_alert_group":
            from_source: dict = {}
            if source.strip():
                try:
                    parsed = yaml.safe_load(source)
                    if isinstance(parsed, dict):
                        from_source = parsed
                    elif parsed is not None:
                        raise ValueError(
                            f"source must be a YAML mapping, got "
                            f"{type(parsed).__name__}."
                        )
                except yaml.YAMLError as exc:
                    raise ValueError(
                        f"{loc} ({cell_id!r}): source YAML parse error: {exc}"
                    ) from exc
            # Merge precedence: explicit metadata dict wins over source-
            # parsed values (caller-supplied metadata is the
            # programmatic interface; source is the human-edited
            # surface). Round-trip writes BOTH so the merge is a no-op.
            merged = {**from_source, **metadata}
            try:
                metadata = cls.validate_promote_metadata(merged)
            except ValueError as exc:
                raise ValueError(f"{loc} ({cell_id!r}): {exc}") from exc

        record = {
            "id": cell_id,
            "type": cell_type,
            "source": source,
            "metadata": metadata,
        }

        # Reactive-cache fields (optional, populated by slice 3+). Stored
        # in the cell record so the YAML round-trips through git/share
        # without losing cache state. Forward-declared here so slice 3
        # doesn't need to migrate any existing notebooks.
        for field in ("_last_executed_at", "_last_input_hash",
                      "_last_output_hash", "_last_runtime_ms"):
            if field in data and data[field] is not None:
                record[field] = data[field]

        return record

    @classmethod
    def validate_record(cls, data: dict) -> dict:
        """Validate + normalise a complete notebook record. Returns the
        canonical dict.

        Required: ``id``. Defaults applied for everything else.

        Cross-cell rule: cell ids MUST be unique within the notebook
        (otherwise reactive execution can't resolve cell_id references
        unambiguously).
        """
        if not isinstance(data, dict):
            raise ValueError("Notebook record must be a dict.")

        # Schema version - read but tolerate higher-versioned files for
        # forward compat; reject lower (we only ship v1, so anything <1
        # is a malformed file).
        schema_version = data.get("schema_version", cls.CURRENT_SCHEMA_VERSION)
        if not isinstance(schema_version, int):
            raise ValueError(
                f"schema_version must be an integer, got "
                f"{type(schema_version).__name__}."
            )
        if schema_version < 1:
            raise ValueError(
                f"schema_version must be >= 1, got {schema_version}."
            )

        notebook_id = cls.validate_notebook_id(data.get("id", ""))
        name = cls.validate_optional_string(
            data.get("name"), name="name", max_len=cls.MAX_NAME_LEN,
        )
        description = cls.validate_optional_string(
            data.get("description"), name="description",
            max_len=cls.MAX_DESCRIPTION_LEN,
        )
        default_max_cost_usd = cls.validate_default_max_cost_usd(
            data.get("default_max_cost_usd"),
        )

        # ── Cells ──────────────────────────────────────────────────
        raw_cells = data.get("cells", [])
        if raw_cells is None:
            raw_cells = []
        if not isinstance(raw_cells, list):
            raise ValueError(
                f"cells must be a list, got {type(raw_cells).__name__}."
            )
        if len(raw_cells) > cls.MAX_CELLS_PER_NOTEBOOK:
            raise ValueError(
                f"Notebook exceeds maximum cell count "
                f"({cls.MAX_CELLS_PER_NOTEBOOK}); has {len(raw_cells)}."
            )

        seen_ids: set[str] = set()
        cells: list[dict] = []
        for i, raw in enumerate(raw_cells):
            cell = cls.validate_cell(raw, position=i)
            if cell["id"] in seen_ids:
                raise ValueError(
                    f"Duplicate cell id {cell['id']!r} at position {i}. "
                    "Cell ids MUST be unique within a notebook so reactive "
                    "execution can resolve references unambiguously."
                )
            seen_ids.add(cell["id"])
            cells.append(cell)

        # Slice 9: cross-cell validation for promote_to_alert_group
        # cells. The per-cell pass already validated metadata fields
        # in isolation; here we confirm ``prompt_cell`` resolves to a
        # real sibling cell id.
        for i, cell in enumerate(cells):
            if cell.get("type") != "promote_to_alert_group":
                continue
            try:
                cls.validate_promote_metadata(
                    cell.get("metadata") or {},
                    sibling_cell_ids=seen_ids,
                )
            except ValueError as exc:
                raise ValueError(
                    f"cell #{i} ({cell['id']!r}): {exc}"
                ) from exc

        return {
            "id": notebook_id,
            "schema_version": schema_version,
            "name": name,
            "description": description,
            "default_max_cost_usd": default_max_cost_usd,
            "cells": cells,
        }
