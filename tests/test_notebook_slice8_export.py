"""
Tests for Phase 3 / Bet 4 slice 8 - getting_started default + HTML/PDF export.

Three slice-8 surfaces:
  * `default_notebooks/getting_started.spqnb` ships in git, seeds into
    user's `notebooks/` on first init via slice-1's `_seed_defaults`
    machinery.
  * `POST /api/notebooks/<id>/export/html` returns a self-contained
    HTML page with both human rendering AND a JSON sidecar for AI
    consumers (dual-audience contract).
  * `POST /api/notebooks/<id>/export/pdf` returns PDF bytes via
    WeasyPrint; charts appear as spec text since WeasyPrint doesn't
    run JavaScript.

Test layout:
  * TestGettingStartedShipped - file exists, validates, has the right
    cells (drift guard: future contributors can't accidentally remove
    the canonical onboarding notebook)
  * TestHtmlExport - endpoint shape, structured JSON sidecar,
    cell-type rendering, error paths
  * TestPdfExport - endpoint returns valid PDF bytes (magic prefix);
    body shape on missing notebook
  * TestUiSurfaceDriftGuards - welcome banner element + export buttons
    + handler hookups present in ui.html
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import notebook_cache_store
import notebook_store


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Per-test-isolated notebook + cache stores. The default_notebooks
    directory is left at the real on-disk path so the seeded
    getting_started.spqnb is available for tests that exercise
    seeding."""
    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()
    nb_dir = tmp_path / "notebooks"
    nb_dir.mkdir()
    monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
    # Keep DEFAULTS_DIR pointing at the real shipped-in-git file so
    # _seed_defaults actually copies the canonical file.
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


@pytest.fixture
def client(isolated_stores):
    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ═══════════════════════════════════════════════════════════════════
# 1. getting_started.spqnb file contract
# ═══════════════════════════════════════════════════════════════════

class TestGettingStartedShipped:
    """The default getting-started notebook is the canonical onboarding
    artefact. Removing or breaking it silently is the kind of UX-
    regression that's hard to detect, so pin its existence + shape.
    """

    def _path(self) -> Path:
        return PROJECT_ROOT / "default_notebooks" / "getting_started.spqnb"

    def test_file_exists(self):
        assert self._path().is_file(), (
            "default_notebooks/getting_started.spqnb is the canonical "
            "onboarding artefact. Don't remove it without a replacement."
        )

    def test_yaml_loads(self):
        rec = yaml.safe_load(self._path().read_text())
        assert isinstance(rec, dict)
        assert rec["id"] == "getting_started"

    def test_validates_against_schema(self):
        from validation.NotebookValidation import NotebookValidation
        rec = yaml.safe_load(self._path().read_text())
        out = NotebookValidation.validate_record(rec)
        # Sanity: at least 5 cells covering the key types
        assert len(out["cells"]) >= 5
        types_used = {c["type"] for c in out["cells"]}
        # Drift guard: getting-started should expose the operator to at
        # LEAST these cell types so the walk-through is meaningful.
        for required in ("markdown", "spql", "python", "chart"):
            assert required in types_used, (
                f"getting_started must include at least one {required!r} cell"
            )

    def test_seeds_into_notebooks_on_first_init(self, isolated_stores):
        store = notebook_store.get_store()  # triggers seed
        nb = store.get_notebook("getting_started")
        assert nb is not None
        assert nb["id"] == "getting_started"


# ═══════════════════════════════════════════════════════════════════
# 2. HTML export endpoint
# ═══════════════════════════════════════════════════════════════════

class TestHtmlExport:
    def test_export_returns_html(self, client):
        # Use the auto-seeded getting_started notebook
        resp = client.post(
            "/api/notebooks/getting_started/export/html",
            json={"run_first": False},
        )
        assert resp.status_code == 200
        assert resp.mimetype.startswith("text/html")
        body = resp.get_data(as_text=True)
        # Doctype + html structure
        assert body.startswith("<!DOCTYPE html>")
        assert "</html>" in body

    def test_export_includes_cell_sources(self, client):
        body = client.post(
            "/api/notebooks/getting_started/export/html",
            json={"run_first": False},
        ).get_data(as_text=True)
        # Drift guard: the export shows the operator's source for every
        # cell. Without this, an AI agent ingesting the export sees
        # rendered output but not the underlying logic.
        assert "Welcome to Notebooks" in body  # markdown cell content
        assert "default_test/output_parquets" in body  # spql cell content

    def test_export_carries_json_sidecar(self, client):
        body = client.post(
            "/api/notebooks/getting_started/export/html",
            json={"run_first": False},
        ).get_data(as_text=True)
        # Dual-audience: AI agents read from the JSON sidecar, NOT
        # by parsing the rendered HTML. Pin its presence.
        assert 'id="notebook-data"' in body
        assert 'type="application/json"' in body
        m = re.search(
            r'<script type="application/json" id="notebook-data">(.+?)</script>',
            body,
            re.DOTALL,
        )
        assert m is not None
        # The sidecar is HTML-escaped JSON - unescape and parse
        import html as _html
        sidecar_raw = _html.unescape(m.group(1))
        parsed = json.loads(sidecar_raw)
        assert parsed["kind"] == "notebook_export"
        assert parsed["schema_version"] == 1
        assert parsed["notebook"]["id"] == "getting_started"

    def test_export_with_run_first_includes_results(self, client):
        body = client.post(
            "/api/notebooks/getting_started/export/html",
            json={"run_first": True},
        ).get_data(as_text=True)
        # Cell results landed in the JSON sidecar
        m = re.search(
            r'<script type="application/json" id="notebook-data">(.+?)</script>',
            body,
            re.DOTALL,
        )
        import html as _html
        parsed = json.loads(_html.unescape(m.group(1)))
        assert parsed["run_result"] is not None
        # At least one successful cell from the run
        assert parsed["run_result"]["success_count"] >= 1

    def test_export_chart_cells_embed_spec_for_clientside_render(self, client):
        body = client.post(
            "/api/notebooks/getting_started/export/html",
            json={"run_first": False},
        ).get_data(as_text=True)
        # Chart cells render via Vega-Lite at view time. The export
        # embeds the spec in a data-spec attribute and inlines the
        # vendored renderer bundles so the standalone file renders
        # charts with zero network access (W10, 2026-07-12).
        assert "nbx-chart" in body
        assert "window.vegaEmbed" in body  # the mount script
        assert "cdn.jsdelivr.net" not in body  # exports never touch a CDN
        if "chart" in body:  # chart cells present -> renderer inlined
            assert "vegaEmbed" in body

    def test_export_missing_notebook_returns_404(self, client):
        resp = client.post(
            "/api/notebooks/nonexistent/export/html",
            json={},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error_class"] == "NotFound"


# ═══════════════════════════════════════════════════════════════════
# 3. PDF export endpoint
# ═══════════════════════════════════════════════════════════════════

class TestPdfExport:
    def test_export_returns_pdf_bytes(self, client):
        resp = client.post(
            "/api/notebooks/getting_started/export/pdf",
            json={"run_first": False},
        )
        # WeasyPrint may not be available on every CI host; skip if so.
        if resp.status_code == 503:
            body = resp.get_json()
            if body and body.get("error_class") == "MissingDependency":
                pytest.skip("WeasyPrint not installed in this environment")
        assert resp.status_code == 200
        assert resp.mimetype == "application/pdf"
        # PDF magic prefix: every PDF starts with "%PDF-"
        data = resp.get_data()
        assert data[:5] == b"%PDF-", (
            f"Response not a valid PDF; first bytes: {data[:8]!r}"
        )

    def test_export_pdf_has_attachment_header(self, client):
        resp = client.post(
            "/api/notebooks/getting_started/export/pdf",
            json={},
        )
        if resp.status_code == 503:
            pytest.skip("WeasyPrint not installed")
        assert resp.headers.get("Content-Disposition", "").startswith(
            "attachment"
        )
        assert "getting_started.pdf" in resp.headers.get(
            "Content-Disposition", ""
        )

    def test_export_pdf_missing_notebook_returns_404(self, client):
        resp = client.post(
            "/api/notebooks/nonexistent/export/pdf",
            json={},
        )
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["error_class"] == "NotFound"


# ═══════════════════════════════════════════════════════════════════
# 4. UI surface drift guards
# ═══════════════════════════════════════════════════════════════════

class TestUiSurfaceDriftGuards:
    def _ui(self) -> str:
        return (
            PROJECT_ROOT / "desktop_app" / "ui.html"
        ).read_text(encoding="utf-8")

    def test_welcome_banner_present(self):
        ui = self._ui()
        assert 'id="nb-welcome-banner"' in ui
        # The banner targets the canonical onboarding notebook id
        assert "getting_started" in ui

    def test_export_buttons_present(self):
        ui = self._ui()
        assert 'id="nb-export-html-btn"' in ui
        assert 'id="nb-export-pdf-btn"' in ui

    def test_export_handler_function_wired(self):
        ui = self._ui()
        # The shared export handler + its two wirings
        assert "_exportNotebook" in ui
        assert "_exportNotebook('html')" in ui
        assert "_exportNotebook('pdf')" in ui

    def test_export_handler_uses_blob_download(self):
        ui = self._ui()
        # Drift guard: download via blob URL + <a> click. Replacing
        # this with a plain navigation would break PDF downloads (the
        # browser would render the PDF inline with no save prompt).
        assert "createObjectURL" in ui
        assert "revokeObjectURL" in ui


# ═══════════════════════════════════════════════════════════════════
# 5. Endpoint drift guard
# ═══════════════════════════════════════════════════════════════════

class TestEndpointDriftGuard:
    def test_export_routes_registered(self):
        src = (
            PROJECT_ROOT / "desktop_app" / "server.py"
        ).read_text(encoding="utf-8")
        for route_pattern in (
            r'@app\.route\(\s*["\']/api/notebooks/<notebook_id>/export/html["\']'
            r'[^)]*methods=\[[^\]]*["\']POST["\']',
            r'@app\.route\(\s*["\']/api/notebooks/<notebook_id>/export/pdf["\']'
            r'[^)]*methods=\[[^\]]*["\']POST["\']',
        ):
            assert re.search(route_pattern, src), (
                f"Slice-8 export route not found via pattern: {route_pattern!r}"
            )
