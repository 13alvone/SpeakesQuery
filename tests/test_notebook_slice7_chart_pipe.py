"""
Tests for Phase 3 / Bet 4 slice 7 - chart cell rendering + pipe cell
LLM affordances.

Slice 7 wires two surfaces (UI-only for chart; UI + new API for pipe):
  * Chart cells: Vega-Lite spec → SVG/canvas via lazy-loaded CDN
    library, with JSON-pre-block fallback when the CDN is unreachable
    or the spec is invalid JSON.
  * Pipe cells: model-picker affordance row above the editor that
    lists registered models from a new GET /api/models endpoint;
    selecting a model inserts ``model="<id>"`` into the editor source.

Tests focus on what we CAN test in headless / non-Playwright runs:
  * /api/models endpoint contract (listing + structured fields for
    AI agents per the slice-5 dual-audience principle)
  * Chart cell source persists through the cache (slice-5 contract)
  * Drift guards on the UI surface (renderer + affordance hooks
    present in ui.html source) - Playwright-style render checks live
    in the manual test plan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import notebook_cache_store
import notebook_engine
import notebook_store
import model_store


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Slice-7 needs both notebook stores AND the model store
    isolated per test (so /api/models returns a deterministic set).
    """
    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()
    model_store.reset_for_tests()
    nb_dir = tmp_path / "notebooks"
    df_dir = tmp_path / "default_notebooks"
    models_dir = tmp_path / "models"
    nb_dir.mkdir()
    df_dir.mkdir()
    monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
    monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", df_dir)
    monkeypatch.setattr(model_store, "MODELS_DIR", models_dir)
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_DB_PATH",
        tmp_path / "notebook_cache.sqlite",
    )
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_PAYLOAD_DIR",
        tmp_path / "notebook_cache",
    )
    yield
    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()
    model_store.reset_for_tests()


@pytest.fixture
def client(isolated_stores):
    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ═══════════════════════════════════════════════════════════════════
# 1. /api/models endpoint contract
# ═══════════════════════════════════════════════════════════════════

class TestModelsEndpoint:
    def test_returns_seeded_default_models(self, client):
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        # Default registry seeds at least claude-haiku and ollama-llama3
        ids = {m["id"] for m in body["models"]}
        assert "claude-haiku-4-5-20251001" in ids
        assert "ollama-llama3-1-8b" in ids

    def test_each_record_has_structured_dual_audience_fields(self, client):
        body = client.get("/api/models").get_json()
        assert body["status"] == "success"
        # AI-side contract: every record has the same field set so
        # AI agents can reason about cost / capabilities without
        # parsing free-form descriptions.
        required = {
            "id", "provider", "model_name", "description", "endpoint",
            "cost_per_input_million_usd", "cost_per_output_million_usd",
            "max_output_tokens", "default_timeout_seconds",
        }
        for m in body["models"]:
            missing = required - set(m.keys())
            assert not missing, (
                f"Model {m.get('id')!r} missing fields: {missing}"
            )

    def test_costs_are_typed_floats_not_strings(self, client):
        # Per ``reference_numpy_scalar_unwrap_for_json`` + dual-audience
        # principle: numeric fields are real numbers, not stringified.
        body = client.get("/api/models").get_json()
        for m in body["models"]:
            assert isinstance(m["cost_per_input_million_usd"], (int, float))
            assert isinstance(m["cost_per_output_million_usd"], (int, float))
            assert isinstance(m["max_output_tokens"], int)
            assert isinstance(m["default_timeout_seconds"], int)


# ═══════════════════════════════════════════════════════════════════
# 2. Chart cell behavior in the engine
# ═══════════════════════════════════════════════════════════════════

class TestChartCellBehavior:
    """Chart cells stay engine-side passthrough (the spec IS the
    input + output; rendering is a UI concern). Confirm slice-7
    didn't change that contract.
    """

    def test_chart_cell_returns_source_unchanged(self):
        engine = notebook_engine.NotebookEngine()
        spec = json.dumps({
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": [{"x": 1, "y": 2}]},
            "mark": "point",
            "encoding": {
                "x": {"field": "x", "type": "quantitative"},
                "y": {"field": "y", "type": "quantitative"},
            },
        })
        cell = {"id": "plot", "type": "chart", "source": spec, "metadata": {}}
        result = engine.execute_cell(cell, {})
        assert result.status == "success"
        assert result.output == spec
        # Chart cells don't expose to namespace
        assert result.exposed_names == []

    def test_chart_cell_does_not_set_output_html(self):
        # Chart rendering happens UI-side via Vega-Lite; engine MUST
        # NOT set output_html (that's reserved for slice-5 markdown).
        engine = notebook_engine.NotebookEngine()
        cell = {"id": "p", "type": "chart", "source": '{"mark": "bar"}', "metadata": {}}
        result = engine.execute_cell(cell, {})
        assert result.output_html == ""

    def test_chart_cell_source_round_trips_through_cache(
        self, isolated_stores,
    ):
        cache = notebook_cache_store.NotebookCacheStore()
        engine = notebook_engine.NotebookEngine()
        spec = '{"mark": "bar", "data": {"values": [{"a": 1}]}}'
        nb = {
            "id": "chart_nb", "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [{
                "id": "plot", "type": "chart", "source": spec, "metadata": {},
            }],
        }
        # First run populates cache
        r1 = engine.execute_notebook(nb, cache_store=cache)
        assert r1.cells[0].output == spec
        # Second run hits cache; source preserved exactly
        r2 = engine.execute_notebook(nb, cache_store=cache)
        assert r2.cells[0].cache_hit is True
        assert r2.cells[0].output == spec


# ═══════════════════════════════════════════════════════════════════
# 3. UI drift guards (Vega-Lite renderer + pipe affordance present)
# ═══════════════════════════════════════════════════════════════════

class TestUIDriftGuards:
    """Slice-7 ships JS-side renderers + affordances. We can't run
    them headlessly without Playwright, but we CAN verify the surface
    is wired by scanning ui.html for the load-bearing identifiers.
    Drift = a future contributor renaming or removing one of these
    breaks the slice-7 UX silently. The drift guards catch that at CI.
    """

    def _ui(self) -> str:
        return (
            PROJECT_ROOT / "desktop_app" / "ui.html"
        ).read_text(encoding="utf-8")

    def test_vega_loader_present(self):
        ui = self._ui()
        # CDN-load pattern + the three vega scripts
        assert "ensureVega" in ui
        assert "vega-embed@" in ui
        assert "vega-lite@" in ui
        assert "vega@" in ui

    def test_vega_loader_is_lazy(self):
        # Vega is heavy (~700KB); it must NOT load on page open.
        # The loader is wrapped in a Promise that only fires on
        # ensureVega() invocation.
        ui = self._ui()
        # Drift guard: the function exists and uses a memoized promise.
        assert "let _vegaLoadPromise = null" in ui
        assert "if (window.vegaEmbed)" in ui  # short-circuit on already-loaded

    def test_chart_cell_dispatches_to_chart_renderer(self):
        ui = self._ui()
        # The renderCellCard dispatch includes a chart branch, AND
        # the post-render mount loop calls _mountChart for chart cells.
        assert "renderChartSpec(cell.source" in ui
        assert "_mountChart(cell.id, cell.source)" in ui

    def test_chart_renderer_has_fallback(self):
        ui = self._ui()
        # When CDN fails OR JSON parse fails, fall back to <pre> block.
        # Slice-4 / slice-5 fallback pattern.
        assert "nb-chart-fallback" in ui
        # Match either copy of the user-facing fallback message.
        assert (
            "Chart renderer unavailable" in ui
            or "Chart source is not valid JSON" in ui
        )

    def test_pipe_affordance_present(self):
        ui = self._ui()
        assert "renderPipeAffordance" in ui
        assert "nb-pipe-model-picker" in ui
        assert "nb-pipe-affordance" in ui

    def test_pipe_affordance_only_on_pipe_cells(self):
        ui = self._ui()
        # Drift guard: the affordance is gated by ``cellType === 'pipe'``.
        # If a future change broadens this without intent, other cell
        # types get an unexpected dropdown. Gate must remain explicit.
        assert "(cellType === 'pipe')" in ui or "cellType === \"pipe\"" in ui

    def test_pipe_affordance_uses_models_endpoint(self):
        ui = self._ui()
        # The picker fetches from the new /api/models endpoint.
        assert "/api/models" in ui
        assert "loadModelsForPipeAffordance" in ui

    def test_pipe_affordance_inserts_model_kwarg(self):
        ui = self._ui()
        # On change, insert ``model="<id>"`` (the SPQL kwarg shape).
        # Drift guard: the snippet template stays consistent.
        assert "snippet = 'model=\"' + modelId + '\"'" in ui


# ═══════════════════════════════════════════════════════════════════
# 4. Endpoint drift guard for /api/models
# ═══════════════════════════════════════════════════════════════════

class TestEndpointDriftGuard:
    """Drift guard on the new endpoint - slice-4 set the precedent;
    every documented route stays surfaced via a regex check on
    server.py source.
    """

    def test_models_route_registered(self):
        src = (
            PROJECT_ROOT / "desktop_app" / "server.py"
        ).read_text(encoding="utf-8")
        pattern = (
            r'@app\.route\(\s*["\']/api/models["\']'
            r'[^)]*methods=\[[^\]]*["\']GET["\']'
        )
        assert re.search(pattern, src), (
            "/api/models GET route missing from server.py - slice-7 "
            "contract broken."
        )
