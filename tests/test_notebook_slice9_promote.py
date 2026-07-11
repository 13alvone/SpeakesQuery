"""
Tests for Phase 3 / Bet 4 slice 9 - promote_to_alert_group (the headliner).

The dev → production gap collapses to one cell. This test file covers
the full surface:

  * ``TestPromoteCellTypeAccepted`` - schema/validator accepts the new
    cell type; drift guard for the additive frozenset.
  * ``TestPromoteMetadataValidator`` - required fields, field types,
    AG-validator reuse.
  * ``TestSourceYamlParsing`` - source IS the YAML form of metadata
    (Monaco-editable), parsed at validation time.
  * ``TestExtractAgPayload`` + ``TestBuildPromotePreview`` - pure
    converter functions.
  * ``TestPromoteCellEngine`` - engine dispatch returns the structured
    preview; never throws.
  * ``TestConfigLeakCanary`` - THE LOAD-BEARING TEST. Patches
    ``AlertGroupStore.save_group`` AND ``update_group`` with
    ``AssertionError("CONFIG LEAK")`` and runs a notebook with a
    promote cell. Both must stay zero on the engine path. Same shape
    as ``tests/test_llm_pipe_slice7.py::TestMoneyLeakCanary`` and
    ``tests/test_ag_disabled_money_leak_audit.py``.
  * ``TestPromoteCellToAg`` - explicit deploy path actually creates /
    updates the AG; second deploy is an update.
  * ``TestRoundTrip`` - AG → notebook → AG produces an equivalent AG.
  * ``TestApi*`` - REST endpoints (preview, deploy, round-trip).
  * ``TestUiSurfaceDriftGuards`` - JS NB_CELL_TYPES includes the new
    type; renderer + Deploy button HTML elements present in ui.html.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import notebook_cache_store
import notebook_store


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Per-test isolation for notebook + AG + saved_search stores.

    Every persistent path is redirected under tmp_path so a test can
    create AGs / notebooks freely without touching the user's data
    or polluting the next test.
    """
    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()
    nb_dir = tmp_path / "notebooks"
    nb_dir.mkdir()
    nb_defaults = tmp_path / "default_notebooks"
    nb_defaults.mkdir()
    monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
    monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", nb_defaults)
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_DB_PATH",
        tmp_path / "notebook_cache.sqlite",
    )
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_PAYLOAD_DIR",
        tmp_path / "notebook_cache",
    )

    # Redirect the alert-group store
    import alert_group_store
    monkeypatch.setattr(
        alert_group_store, "GROUPS_DIR", tmp_path / "alert_groups",
    )
    monkeypatch.setattr(
        alert_group_store, "DEFAULTS_DIR", tmp_path / "default_alert_groups",
    )
    monkeypatch.setattr(
        alert_group_store, "LAST_CHANCE_DB", tmp_path / "ag_lc.sqlite",
    )
    monkeypatch.setattr(
        alert_group_store, "RUNS_DB", tmp_path / "ag_runs.sqlite",
    )

    # Redirect the saved-search store so feeder pre-flight resolves
    # against the per-test directory.
    import saved_search_store
    monkeypatch.setattr(
        saved_search_store, "SEARCHES_DIR", tmp_path / "saved_searches",
    )
    monkeypatch.setattr(
        saved_search_store, "DEFAULT_SEARCHES_DIR",
        tmp_path / "default_saved_searches",
    )
    monkeypatch.setattr(
        saved_search_store, "LAST_CHANCE_DB",
        tmp_path / "ss_lc.sqlite",
    )

    yield tmp_path

    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()


@pytest.fixture
def client(isolated_stores):
    """Flask test client with isolated stores."""
    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _make_saved_search(tmp_path: Path, name: str, query: str = "index=test") -> None:
    """Drop a minimal saved search YAML so feeder pre-flight finds it."""
    ss_dir = tmp_path / "saved_searches"
    ss_dir.mkdir(exist_ok=True)
    ss_dir.joinpath(f"{name}.yaml").write_text(yaml.dump({
        "name": name,
        "query": query,
        "cron_schedule": "0 12 * * *",
        "lookback": "-1d",
        "purpose": "alert_group_feeder",
        "send_email": "no",
        "email_address": "noreply@speakesquery.local",
    }))


def _make_notebook_record(
    *, with_promote: bool = True, ag_name: str = "slice9_test_ag",
    search_names: list | None = None,
) -> dict:
    """Build a notebook record with a prompt cell + (optionally) a
    promote_to_alert_group cell."""
    if search_names is None:
        search_names = ["slice9_feeder"]
    cells = [
        {
            "id": "prompt",
            "type": "pipe",
            "source": "Analyse the data.",
            "metadata": {},
        },
    ]
    if with_promote:
        deploy_yaml = yaml.dump({
            "name": ag_name,
            "schedule": "0 12 * * mon-fri",
            "timezone": "UTC",
            "email_address": "ops@example.com",
            "search_names": search_names,
            "prompt_cell": "prompt",
            "max_rows": 200,
            "delivery_mode": "api",
        }, sort_keys=False)
        cells.append({
            "id": "deploy",
            "type": "promote_to_alert_group",
            "source": deploy_yaml,
            "metadata": {},
        })
    return {
        "id": "slice9_test_nb",
        "schema_version": 1,
        "name": "slice 9 test",
        "description": "",
        "default_max_cost_usd": 0.0,
        "cells": cells,
    }


# ═══════════════════════════════════════════════════════════════════
# 1. Schema + validator
# ═══════════════════════════════════════════════════════════════════

class TestPromoteCellTypeAccepted:
    """The cell-type closed enum gained one entry. Drift guard pins
    the size + membership."""

    def test_promote_in_allowed_types(self):
        from validation.NotebookValidation import ALLOWED_CELL_TYPES
        assert "promote_to_alert_group" in ALLOWED_CELL_TYPES

    def test_allowed_types_is_exactly_seven(self):
        # If you add an 8th cell type, update this number deliberately
        # in the same commit. Forces a thoughtful review.
        from validation.NotebookValidation import ALLOWED_CELL_TYPES
        assert len(ALLOWED_CELL_TYPES) == 7, (
            f"ALLOWED_CELL_TYPES grew unexpectedly: {sorted(ALLOWED_CELL_TYPES)}. "
            f"Adding a cell type is a deliberate slice - update this drift "
            f"guard + the JS NB_CELL_TYPES + the docs in lockstep."
        )

    def test_allowed_types_exact_membership(self):
        from validation.NotebookValidation import ALLOWED_CELL_TYPES
        assert ALLOWED_CELL_TYPES == frozenset({
            "spql", "python", "chart", "markdown", "param", "pipe",
            "promote_to_alert_group",
        })


class TestPromoteMetadataValidator:
    """Field-by-field validator behaviour for promote_to_alert_group
    metadata. Re-uses AlertGroupValidation under the hood."""

    def _good(self) -> dict:
        return {
            "name": "test_ag",
            "schedule": "0 12 * * mon-fri",
            "email_address": "ops@example.com",
            "search_names": ["feeder1"],
            "prompt_cell": "prompt",
        }

    def test_minimal_metadata_validates(self):
        from validation.NotebookValidation import NotebookValidation
        out = NotebookValidation.validate_promote_metadata(self._good())
        assert out["name"] == "test_ag"
        assert out["schedule"] == "0 12 * * mon-fri"
        assert out["search_names"] == ["feeder1"]
        assert out["prompt_cell"] == "prompt"

    def test_missing_required_field_rejected(self):
        from validation.NotebookValidation import NotebookValidation
        bad = self._good()
        del bad["schedule"]
        with pytest.raises(ValueError, match="schedule"):
            NotebookValidation.validate_promote_metadata(bad)

    def test_empty_required_field_rejected(self):
        from validation.NotebookValidation import NotebookValidation
        bad = self._good()
        bad["search_names"] = []
        with pytest.raises(ValueError, match="search_names"):
            NotebookValidation.validate_promote_metadata(bad)

    def test_unknown_field_rejected(self):
        from validation.NotebookValidation import NotebookValidation
        bad = self._good()
        bad["typoed_field_name"] = 1
        with pytest.raises(ValueError, match="unknown field"):
            NotebookValidation.validate_promote_metadata(bad)

    def test_invalid_cron_rejected(self):
        from validation.NotebookValidation import NotebookValidation
        bad = self._good()
        bad["schedule"] = "not a cron"
        with pytest.raises(ValueError, match="cron"):
            NotebookValidation.validate_promote_metadata(bad)

    def test_invalid_email_rejected(self):
        from validation.NotebookValidation import NotebookValidation
        bad = self._good()
        bad["email_address"] = "not-an-email"
        with pytest.raises(ValueError):
            NotebookValidation.validate_promote_metadata(bad)

    def test_sibling_cell_id_cross_check_passes(self):
        from validation.NotebookValidation import NotebookValidation
        out = NotebookValidation.validate_promote_metadata(
            self._good(), sibling_cell_ids={"prompt", "deploy"},
        )
        assert out["prompt_cell"] == "prompt"

    def test_sibling_cell_id_cross_check_fails(self):
        from validation.NotebookValidation import NotebookValidation
        with pytest.raises(ValueError, match="prompt_cell.*does not match"):
            NotebookValidation.validate_promote_metadata(
                self._good(), sibling_cell_ids={"deploy"},
            )

    def test_optional_pass_through_fields_accepted(self):
        from validation.NotebookValidation import NotebookValidation
        good = self._good()
        good["max_cost_usd_per_run"] = 0.50
        good["max_cost_usd_per_day"] = 5.00
        good["timezone"] = "America/New_York"
        good["delivery_mode"] = "prompt_only"
        out = NotebookValidation.validate_promote_metadata(good)
        assert out["max_cost_usd_per_run"] == 0.50
        assert out["timezone"] == "America/New_York"

    def test_full_record_validation_catches_bad_metadata(self):
        from validation.NotebookValidation import NotebookValidation
        nb = _make_notebook_record(with_promote=True)
        # Corrupt the deploy cell metadata via the source YAML
        deploy = next(c for c in nb["cells"] if c["id"] == "deploy")
        deploy["source"] = yaml.dump({"name": "ok", "schedule": ""}, sort_keys=False)
        with pytest.raises(ValueError):
            NotebookValidation.validate_record(nb)


class TestSourceYamlParsing:
    """promote_to_alert_group cells use ``source`` as the YAML form of
    AG metadata. Validator parses it at save time."""

    def test_source_yaml_populates_metadata(self):
        from validation.NotebookValidation import NotebookValidation
        nb = _make_notebook_record(with_promote=True)
        out = NotebookValidation.validate_record(nb)
        deploy = next(c for c in out["cells"] if c["id"] == "deploy")
        assert deploy["metadata"]["name"] == "slice9_test_ag"
        assert deploy["metadata"]["schedule"] == "0 12 * * mon-fri"

    def test_explicit_metadata_overrides_source(self):
        """When both source-YAML and metadata-dict carry a value, the
        explicit metadata wins (programmatic interface > UI surface)."""
        from validation.NotebookValidation import NotebookValidation
        nb = _make_notebook_record(with_promote=True)
        deploy = next(c for c in nb["cells"] if c["id"] == "deploy")
        # source-YAML sets max_rows=200, explicit overrides to 50
        deploy["metadata"] = {"max_rows": 50}
        out = NotebookValidation.validate_record(nb)
        deploy_out = next(c for c in out["cells"] if c["id"] == "deploy")
        assert deploy_out["metadata"]["max_rows"] == 50

    def test_invalid_yaml_in_source_rejected(self):
        from validation.NotebookValidation import NotebookValidation
        nb = _make_notebook_record(with_promote=True)
        deploy = next(c for c in nb["cells"] if c["id"] == "deploy")
        deploy["source"] = "this: is: not: valid: yaml: at all\n  bad indent"
        with pytest.raises(ValueError, match="YAML"):
            NotebookValidation.validate_record(nb)


# ═══════════════════════════════════════════════════════════════════
# 2. Pure converter functions
# ═══════════════════════════════════════════════════════════════════

class TestExtractAgPayload:
    def test_extract_returns_canonical_ag_dict(self, isolated_stores):
        from validation.NotebookValidation import NotebookValidation
        from notebook_to_alert_group import extract_ag_payload
        nb = NotebookValidation.validate_record(_make_notebook_record())
        payload = extract_ag_payload(nb, "deploy")
        assert payload["name"] == "slice9_test_ag"
        assert payload["schedule"] == "0 12 * * mon-fri"
        assert payload["search_names"] == ["slice9_feeder"]
        # Resolved from the prompt cell's source
        assert payload["prompt_text"] == "Analyse the data."

    def test_unknown_cell_id_raises(self, isolated_stores):
        from notebook_to_alert_group import extract_ag_payload
        nb = _make_notebook_record()
        with pytest.raises(ValueError, match="not found"):
            extract_ag_payload(nb, "nonexistent_cell")

    def test_wrong_cell_type_raises(self, isolated_stores):
        from notebook_to_alert_group import extract_ag_payload
        nb = _make_notebook_record()
        with pytest.raises(ValueError, match="expected"):
            extract_ag_payload(nb, "prompt")  # this is type=pipe, not promote

    def test_empty_prompt_cell_raises(self, isolated_stores):
        from notebook_to_alert_group import extract_ag_payload
        from validation.NotebookValidation import NotebookValidation
        nb = NotebookValidation.validate_record(_make_notebook_record())
        # Blank out the prompt cell
        prompt_cell = next(c for c in nb["cells"] if c["id"] == "prompt")
        prompt_cell["source"] = "   "
        with pytest.raises(ValueError, match="empty source"):
            extract_ag_payload(nb, "deploy")


class TestBuildPromotePreview:
    def test_preview_decision_create_when_ag_does_not_exist(self, isolated_stores):
        from validation.NotebookValidation import NotebookValidation
        from notebook_to_alert_group import build_promote_preview
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        preview = build_promote_preview(nb, "deploy")
        assert preview["kind"] == "promote_to_alert_group_preview"
        assert preview["decision"] == "create"
        assert preview["target_payload"]["name"] == "slice9_test_ag"
        assert preview["validation"]["errors"] == []
        # Feeder exists (we created it above) - no warning
        assert preview["validation"]["warnings"] == []

    def test_preview_warns_when_feeder_missing(self, isolated_stores):
        from validation.NotebookValidation import NotebookValidation
        from notebook_to_alert_group import build_promote_preview
        # NOTE: no _make_saved_search call - feeder is missing
        nb = NotebookValidation.validate_record(_make_notebook_record())
        preview = build_promote_preview(nb, "deploy")
        assert preview["decision"] == "create"
        # Validation warning surfaces the missing feeder
        warnings = preview["validation"]["warnings"]
        assert any("slice9_feeder" in w for w in warnings)

    def test_preview_decision_update_when_ag_exists_and_differs(self, isolated_stores):
        from validation.NotebookValidation import NotebookValidation
        from notebook_to_alert_group import build_promote_preview
        from alert_group_store import AlertGroupStore
        _make_saved_search(isolated_stores, "slice9_feeder")
        # Pre-create the AG with a different schedule
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "slice9_test_ag",
            "search_names": ["slice9_feeder"],
            "prompt_text": "Old prompt",
            "schedule": "0 6 * * *",
            "email_address": "old@example.com",
            "max_rows": 200,
        })
        nb = NotebookValidation.validate_record(_make_notebook_record())
        preview = build_promote_preview(nb, "deploy")
        assert preview["decision"] == "update"
        assert len(preview["changed_fields"]) > 0
        changed_field_names = {c["field"] for c in preview["changed_fields"]}
        assert "schedule" in changed_field_names

    def test_preview_decision_no_change_when_identical(self, isolated_stores):
        from validation.NotebookValidation import NotebookValidation
        from notebook_to_alert_group import (
            build_promote_preview, promote_cell_to_ag,
        )
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        # Deploy once
        promote_cell_to_ag(nb, "deploy")
        # Re-preview - should be no_change
        preview = build_promote_preview(nb, "deploy")
        assert preview["decision"] == "no_change"
        assert preview["changed_fields"] == []

    def test_preview_blocked_on_invalid_metadata(self, isolated_stores):
        from notebook_to_alert_group import build_promote_preview
        # Build a notebook by hand with an invalid promote cell that
        # bypasses the validator (simulating a partially-edited cell).
        nb = {
            "id": "bad_nb", "schema_version": 1, "cells": [
                {"id": "prompt", "type": "pipe", "source": "x", "metadata": {}},
                {
                    "id": "deploy",
                    "type": "promote_to_alert_group",
                    "source": "",
                    # Missing prompt_cell + others
                    "metadata": {"name": "x"},
                },
            ],
            "default_max_cost_usd": 0.0,
        }
        preview = build_promote_preview(nb, "deploy")
        # Empty/missing prompt_cell short-circuits before AG validation,
        # so decision is blocked (extract_ag_payload raises "missing
        # required metadata.prompt_cell").
        assert preview["decision"] == "blocked"
        assert preview["validation"]["errors"]


# ═══════════════════════════════════════════════════════════════════
# 3. Engine - preview at execution, NEVER mutates AG state
# ═══════════════════════════════════════════════════════════════════

class TestPromoteCellEngine:
    def test_engine_returns_preview_in_output(self, isolated_stores):
        from notebook_engine import NotebookEngine, STATUS_SUCCESS
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        engine = NotebookEngine()
        result = engine.execute_notebook(nb, use_cache=False)
        deploy_result = next(
            c for c in result.cells if c.cell_id == "deploy"
        )
        assert deploy_result.status == STATUS_SUCCESS
        assert deploy_result.output_preview is not None
        assert deploy_result.output_preview["kind"] == \
            "promote_to_alert_group_preview"

    def test_engine_repr_describes_decision(self, isolated_stores):
        from notebook_engine import NotebookEngine
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        engine = NotebookEngine()
        result = engine.execute_notebook(nb, use_cache=False)
        deploy_result = next(
            c for c in result.cells if c.cell_id == "deploy"
        )
        assert "CREATE" in deploy_result.output_repr
        assert "slice9_test_ag" in deploy_result.output_repr

    def test_engine_handles_missing_notebook_gracefully(self, isolated_stores):
        """Calling execute_cell directly on a promote cell without a
        notebook context surfaces a clear error rather than crashing."""
        from notebook_engine import NotebookEngine, STATUS_ERROR
        from validation.NotebookValidation import NotebookValidation
        nb = NotebookValidation.validate_record(_make_notebook_record())
        deploy_cell = next(c for c in nb["cells"] if c["id"] == "deploy")
        engine = NotebookEngine()
        result = engine.execute_cell(deploy_cell, namespace={})
        assert result.status == STATUS_ERROR
        assert result.error_class == "MissingNotebookContext"

    def test_engine_bypasses_cache(self, isolated_stores):
        """Promote cells must not be served from cache (preview embeds
        live AG state). Even with use_cache=True, the cell re-runs."""
        from notebook_engine import NotebookEngine
        from notebook_cache_store import get_store as get_cache
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        engine = NotebookEngine()
        cache = get_cache()
        # First run populates any caches it would
        result1 = engine.execute_notebook(nb, use_cache=True, cache_store=cache)
        deploy1 = next(c for c in result1.cells if c.cell_id == "deploy")
        assert deploy1.cache_hit is False
        # Second run - cache_hit must STILL be False (bypass guard)
        result2 = engine.execute_notebook(nb, use_cache=True, cache_store=cache)
        deploy2 = next(c for c in result2.cells if c.cell_id == "deploy")
        assert deploy2.cache_hit is False, (
            "promote_to_alert_group cells must always re-run; serving "
            "a stale 'no_change' decision after the operator edited the "
            "AG outside the notebook would erode dev→prod trust."
        )


class TestConfigLeakCanary:
    """THE LOAD-BEARING TEST. Patches AlertGroupStore mutating methods
    to raise ``AssertionError("CONFIG LEAK")`` and runs a notebook with
    a promote cell. Both must stay zero. Same shape as the slice-7
    money-leak canary.

    If this test fails it means the engine path silently mutated AG
    state - which would convert a notebook re-run / cache-miss / cell
    re-render into an unintended AG creation or update. That's the
    kind of failure that erodes operator trust irreparably.
    """

    def test_engine_path_does_not_invoke_save_group(self, isolated_stores):
        from notebook_engine import NotebookEngine
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        engine = NotebookEngine()

        with (
            patch.object(
                __import__("alert_group_store", fromlist=["AlertGroupStore"])
                .AlertGroupStore,
                "save_group",
                side_effect=AssertionError("CONFIG LEAK: save_group called from engine"),
            ),
            patch.object(
                __import__("alert_group_store", fromlist=["AlertGroupStore"])
                .AlertGroupStore,
                "update_group",
                side_effect=AssertionError("CONFIG LEAK: update_group called from engine"),
            ),
        ):
            # Run the notebook end-to-end. Must complete WITHOUT
            # invoking either patched method - the preview path is
            # entirely read-only against AG state.
            result = engine.execute_notebook(nb, use_cache=False)

        deploy = next(c for c in result.cells if c.cell_id == "deploy")
        # The cell ran - preview was produced - but no AG was saved.
        assert deploy.status == "success"
        assert deploy.output_preview is not None

    def test_engine_path_does_not_invoke_save_group_even_with_cache(
        self, isolated_stores,
    ):
        """Re-run with cache enabled - cache hit / miss must not
        trigger an AG save either."""
        from notebook_engine import NotebookEngine
        from validation.NotebookValidation import NotebookValidation
        from notebook_cache_store import get_store as get_cache
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        engine = NotebookEngine()
        cache = get_cache()

        with (
            patch.object(
                __import__("alert_group_store", fromlist=["AlertGroupStore"])
                .AlertGroupStore,
                "save_group",
                side_effect=AssertionError("CONFIG LEAK: cached path leaked"),
            ),
            patch.object(
                __import__("alert_group_store", fromlist=["AlertGroupStore"])
                .AlertGroupStore,
                "update_group",
                side_effect=AssertionError("CONFIG LEAK: cached path leaked"),
            ),
        ):
            engine.execute_notebook(nb, use_cache=True, cache_store=cache)
            engine.execute_notebook(nb, use_cache=True, cache_store=cache)

    def test_only_promote_cell_to_ag_invokes_save_group(self, isolated_stores):
        """Positive control - the explicit deploy path DOES invoke
        save_group. If this fails we've broken the actual deploy path."""
        from notebook_to_alert_group import promote_cell_to_ag
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        record = promote_cell_to_ag(nb, "deploy")
        assert record["name"] == "slice9_test_ag"


# ═══════════════════════════════════════════════════════════════════
# 4. Deploy + round-trip
# ═══════════════════════════════════════════════════════════════════

class TestPromoteCellToAg:
    def test_first_deploy_creates_ag(self, isolated_stores):
        from alert_group_store import AlertGroupStore
        from notebook_to_alert_group import promote_cell_to_ag
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        promote_cell_to_ag(nb, "deploy")
        store = AlertGroupStore()
        store.initialize()
        ag = store.get_group("slice9_test_ag")
        assert ag["schedule"] == "0 12 * * mon-fri"
        assert ag["search_names"] == ["slice9_feeder"]
        assert ag["prompt_text"] == "Analyse the data."

    def test_second_deploy_updates_in_place(self, isolated_stores):
        from alert_group_store import AlertGroupStore
        from notebook_to_alert_group import promote_cell_to_ag
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        promote_cell_to_ag(nb, "deploy")
        # Edit the prompt and re-deploy
        prompt = next(c for c in nb["cells"] if c["id"] == "prompt")
        prompt["source"] = "Updated prompt."
        promote_cell_to_ag(nb, "deploy")
        store = AlertGroupStore()
        store.initialize()
        ag = store.get_group("slice9_test_ag")
        assert ag["prompt_text"] == "Updated prompt."

    def test_overwrite_existing_false_collides(self, isolated_stores):
        from notebook_to_alert_group import promote_cell_to_ag
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "slice9_feeder")
        nb = NotebookValidation.validate_record(_make_notebook_record())
        promote_cell_to_ag(nb, "deploy")
        with pytest.raises(FileExistsError):
            promote_cell_to_ag(nb, "deploy", overwrite_existing=False)


class TestRoundTrip:
    def test_ag_to_notebook_synthesises_editable_record(self, isolated_stores):
        from alert_group_store import AlertGroupStore
        from notebook_to_alert_group import alert_group_to_notebook
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "round_trip_ag",
            "search_names": ["fX"],
            "prompt_text": "Original prompt body.",
            "schedule": "0 9 * * mon-fri",
            "email_address": "ops@example.com",
            "max_rows": 100,
        })
        ag = store.get_group("round_trip_ag")
        nb = alert_group_to_notebook(ag)
        # Notebook id derived from AG name
        assert nb["id"] == "from_ag_round_trip_ag"
        # Has a deploy cell with all the right metadata
        deploy = next(c for c in nb["cells"] if c["type"] == "promote_to_alert_group")
        assert deploy["metadata"]["name"] == "round_trip_ag"
        assert deploy["metadata"]["schedule"] == "0 9 * * mon-fri"
        assert deploy["metadata"]["prompt_cell"] == "prompt"
        # Has a prompt cell carrying the AG's prompt
        prompt = next(c for c in nb["cells"] if c["id"] == "prompt")
        assert prompt["source"].strip() == "Original prompt body."

    def test_round_trip_preserves_canonical_ag_payload(self, isolated_stores):
        """AG → notebook → AG produces the same canonical payload (modulo
        timestamps + computed fields)."""
        from alert_group_store import AlertGroupStore
        from notebook_to_alert_group import (
            alert_group_to_notebook, extract_ag_payload,
        )
        from validation.NotebookValidation import NotebookValidation
        _make_saved_search(isolated_stores, "fX", query="index=test1")
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "round_trip_ag",
            "search_names": ["fX"],
            "prompt_text": "Original prompt body.",
            "schedule": "0 9 * * mon-fri",
            "email_address": "ops@example.com",
            "max_rows": 100,
        })
        ag = store.get_group("round_trip_ag")
        synth = alert_group_to_notebook(ag)
        # Re-validate (simulates saving + reloading the synthesised
        # notebook)
        synth_valid = NotebookValidation.validate_record(synth)
        replay = extract_ag_payload(synth_valid, "deploy")
        # Field-by-field equivalence (the load-bearing fields)
        for field in ("name", "schedule", "search_names", "max_rows",
                      "email_address", "prompt_text"):
            assert replay[field] == ag[field], (
                f"Round-trip lost field {field!r}: "
                f"original={ag[field]!r}, replayed={replay[field]!r}"
            )

    def test_round_trip_handles_missing_saved_search(self, isolated_stores):
        """If a feeder doesn't exist the round-trip still produces a
        valid notebook - the missing feeder cell carries a placeholder
        comment so the operator can fix it."""
        from alert_group_store import AlertGroupStore
        from notebook_to_alert_group import alert_group_to_notebook
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "missing_feeder_ag",
            "search_names": ["does_not_exist"],
            "prompt_text": "X",
            "schedule": "0 9 * * *",
            "email_address": "x@example.com",
            "max_rows": 100,
        })
        ag = store.get_group("missing_feeder_ag")
        nb = alert_group_to_notebook(ag)
        feeder = next(c for c in nb["cells"] if c["id"] == "feeder_1")
        assert "does_not_exist" in feeder["source"]


# ═══════════════════════════════════════════════════════════════════
# 5. API endpoints
# ═══════════════════════════════════════════════════════════════════

class TestApiPreviewEndpoint:
    def _seed_notebook(self, client):
        nb = _make_notebook_record()
        resp = client.post("/api/notebooks", json=nb)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_preview_returns_structured_dict(self, client, isolated_stores):
        _make_saved_search(isolated_stores, "slice9_feeder")
        self._seed_notebook(client)
        resp = client.get(
            "/api/notebooks/slice9_test_nb/promote/deploy/preview"
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        preview = data["preview"]
        assert preview["kind"] == "promote_to_alert_group_preview"
        assert preview["decision"] == "create"

    def test_preview_404_on_missing_notebook(self, client):
        resp = client.get("/api/notebooks/nope/promote/deploy/preview")
        assert resp.status_code == 404

    def test_preview_404_on_missing_cell(self, client, isolated_stores):
        self._seed_notebook(client)
        resp = client.get(
            "/api/notebooks/slice9_test_nb/promote/missing_cell/preview"
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error_class"] == "UnknownCellId"
        assert "valid_cell_ids" in body

    def test_preview_400_on_wrong_cell_type(self, client, isolated_stores):
        self._seed_notebook(client)
        # 'prompt' is type=pipe, not promote
        resp = client.get(
            "/api/notebooks/slice9_test_nb/promote/prompt/preview"
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error_class"] == "WrongCellType"


class TestApiDeployEndpoint:
    def _seed(self, client, isolated_stores):
        _make_saved_search(isolated_stores, "slice9_feeder")
        client.post("/api/notebooks", json=_make_notebook_record())

    def test_deploy_creates_ag(self, client, isolated_stores):
        self._seed(client, isolated_stores)
        resp = client.post(
            "/api/notebooks/slice9_test_nb/promote/deploy",
            json={"overwrite_existing": True},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["status"] == "success"
        assert body["ag"]["name"] == "slice9_test_ag"
        assert body["deploy_record"]["ag_name"] == "slice9_test_ag"

    def test_deploy_404_on_missing_notebook(self, client):
        resp = client.post(
            "/api/notebooks/nope/promote/deploy",
            json={},
        )
        assert resp.status_code == 404

    def test_deploy_409_on_collision_when_overwrite_false(
        self, client, isolated_stores,
    ):
        self._seed(client, isolated_stores)
        # First deploy
        client.post(
            "/api/notebooks/slice9_test_nb/promote/deploy", json={},
        )
        # Second deploy with overwrite=false collides
        resp = client.post(
            "/api/notebooks/slice9_test_nb/promote/deploy",
            json={"overwrite_existing": False},
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error_class"] == "AlertGroupExists"


class TestApiAlertGroupAsNotebook:
    def test_round_trip_endpoint_returns_notebook_record(
        self, client, isolated_stores,
    ):
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "roundtrip_test",
            "search_names": ["fX"],
            "prompt_text": "Round trip body.",
            "schedule": "0 9 * * *",
            "email_address": "x@example.com",
            "max_rows": 100,
        })
        resp = client.get("/api/alert-groups/roundtrip_test/as-notebook")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        nb = body["notebook"]
        assert nb["id"] == "from_ag_roundtrip_test"
        deploy = next(
            c for c in nb["cells"]
            if c["type"] == "promote_to_alert_group"
        )
        assert deploy["metadata"]["name"] == "roundtrip_test"

    def test_round_trip_endpoint_404_on_missing_ag(self, client):
        resp = client.get("/api/alert-groups/nope/as-notebook")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════
# 6. UI surface drift guards
# ═══════════════════════════════════════════════════════════════════

class TestUiSurfaceDriftGuards:
    """Pin the JS surfaces so a future refactor doesn't silently drop
    the new cell type from the SPA."""

    def _ui_text(self) -> str:
        return (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()

    def test_nb_cell_types_includes_promote(self):
        ui = self._ui_text()
        m = re.search(r"const NB_CELL_TYPES = \[([^\]]+)\];", ui)
        assert m is not None
        listed = [t.strip().strip("'\"") for t in m.group(1).split(",")]
        assert "promote_to_alert_group" in listed

    def test_render_promote_preview_function_present(self):
        ui = self._ui_text()
        assert "function renderPromotePreview" in ui

    def test_deploy_button_class_present(self):
        ui = self._ui_text()
        assert "nb-promote-deploy-btn" in ui

    def test_promote_pane_css_class_present(self):
        ui = self._ui_text()
        assert ".nb-promote-pane" in ui
        assert ".nb-promote-decision" in ui

    def test_hydration_helper_wired_into_render_editor(self):
        ui = self._ui_text()
        # Pinned: renderEditor's post-loop calls the hydration helper.
        assert "_wirePromotePreviewHydration" in ui

    def test_monaco_lang_map_includes_promote(self):
        ui = self._ui_text()
        m = re.search(r"const CELL_LANG_BY_TYPE = \{(.+?)\};", ui, re.DOTALL)
        assert m is not None
        assert "promote_to_alert_group" in m.group(1)


# ═══════════════════════════════════════════════════════════════════
# 7. Schema additivity drift guard
# ═══════════════════════════════════════════════════════════════════

class TestSchemaAdditive:
    """Slice-1's schema is forward-compatible; slice 9 added one cell
    type. The field set on the NOTEBOOK record (id, schema_version,
    name, description, default_max_cost_usd, cells) MUST NOT shrink.
    """

    def test_notebook_record_field_set_unchanged(self):
        from validation.NotebookValidation import NotebookValidation
        nb = NotebookValidation.validate_record({"id": "x"})
        # Frozen field set as of slice 9 (no removals from slice 1)
        assert set(nb.keys()) == {
            "id", "schema_version", "name", "description",
            "default_max_cost_usd", "cells",
        }

    def test_cell_record_field_set_unchanged(self):
        from validation.NotebookValidation import NotebookValidation
        nb = NotebookValidation.validate_record({
            "id": "x",
            "cells": [{"id": "c1", "type": "markdown", "source": ""}],
        })
        cell = nb["cells"][0]
        # The base cell record fields. Cache-tracking fields are
        # optional + only present when set.
        assert set(cell.keys()) >= {"id", "type", "source", "metadata"}
