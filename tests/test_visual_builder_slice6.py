"""
Tests for Phase 4 / Bet 4 slice 6 - Visual Builder round-trip + reorder.

Slice 6 ships the round-trip primitive:
  * Server-side parser ``lexers/spql_pipeline_split.py`` (covered in
    ``tests/test_spql_pipeline_split.py`` - unit + 100-query lossless).
  * New endpoint ``POST /api/visual-builder/parse``.
  * SPA "Load SPQL into canvas" UI (textarea + Load button).
  * Drag-to-reorder stage cards (HTML5 native draggable).

This file pins the API + UI surfaces. The 100-query lossless round-
trip lives in ``test_spql_pipeline_split.py`` (the parser is the
load-bearing piece).
"""

from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ui_html_text() -> str:
    return (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()


@pytest.fixture
def client():
    """Flask test client. No state isolation needed - the parse
    endpoint is pure (no DB, no FS writes)."""
    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ═══════════════════════════════════════════════════════════════════
# 1. Endpoint behaviour
# ═══════════════════════════════════════════════════════════════════

class TestParseEndpoint:
    def test_basic_index_plus_stages_round_trips(self, client):
        resp = client.post(
            "/api/visual-builder/parse",
            json={"spql": 'index="x.parquet" | head 5 | stats count by host'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["index_clause"] == 'index="x.parquet"'
        assert [s["command"] for s in data["stages"]] == ["head", "stats"]

    def test_empty_string_returns_empty_structure(self, client):
        resp = client.post("/api/visual-builder/parse", json={"spql": ""})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["index_clause"] == ""
        assert data["stages"] == []

    def test_pipe_inside_quoted_string_preserved(self, client):
        resp = client.post(
            "/api/visual-builder/parse",
            json={"spql": 'index="x" | regex msg "(error|warn|info)" | head 5'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["stages"]) == 2
        assert "(error|warn|info)" in data["stages"][0]["kwargs"]
        assert data["stages"][1]["command"] == "head"

    def test_missing_spql_field_returns_400(self, client):
        resp = client.post("/api/visual-builder/parse", json={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error_class"] == "InvalidInput"

    def test_non_string_spql_field_returns_400(self, client):
        resp = client.post(
            "/api/visual-builder/parse", json={"spql": 12345},
        )
        assert resp.status_code == 400

    def test_phase4_pipes_parse_correctly(self, client):
        # Sanity: every Phase 4 meta-pipe parses through the endpoint
        for spql, expected_cmd in (
            ('index="x" | llm_route model="m" prompt="p" escalate_to="m2"',
             "llm_route"),
            ('index="x" | llm_refine drafter_model="d" critic_model="c" '
             'drafter_prompt="x" critic_prompt="y"', "llm_refine"),
            ('index="x" | llm_ensemble models="m1,m2" prompt="x"',
             "llm_ensemble"),
            ('index="x" | llm_until model="m" prompt="x" max_iterations=3',
             "llm_until"),
        ):
            resp = client.post(
                "/api/visual-builder/parse", json={"spql": spql},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["stages"][0]["command"] == expected_cmd, (
                f"Expected first stage command {expected_cmd!r} for "
                f"input {spql!r}; got {data['stages'][0]['command']!r}"
            )


# ═══════════════════════════════════════════════════════════════════
# 2. UI surface drift guards
# ═══════════════════════════════════════════════════════════════════

class TestUiSurfaceDriftGuards:
    """Pin the slice-6 UI additions."""

    def test_load_section_present(self, ui_html_text):
        assert 'id="vb-load-section"' in ui_html_text

    def test_load_textarea_present(self, ui_html_text):
        assert 'id="vb-load-textarea"' in ui_html_text

    def test_load_button_present(self, ui_html_text):
        assert 'id="vb-load-btn"' in ui_html_text

    def test_load_handler_function_defined(self, ui_html_text):
        assert "function _vbLoad(" in ui_html_text or \
               "async function _vbLoad(" in ui_html_text

    def test_load_handler_calls_parse_endpoint(self, ui_html_text):
        # Pin the slice-6 wire: the Load button POSTs to the new
        # /api/visual-builder/parse endpoint
        assert "/api/visual-builder/parse" in ui_html_text

    def test_reorder_function_defined(self, ui_html_text):
        assert "function _vbReorderStage(" in ui_html_text

    def test_reorder_wired_in_canvas_render(self, ui_html_text):
        # Pin: _vbWireStageReorder is called from _vbRenderCanvas so
        # cards become draggable after every render.
        assert "_vbWireStageReorder" in ui_html_text

    def test_reorder_uses_html5_draggable(self, ui_html_text):
        # Per slice-5 design: vanilla JS, no drag-drop library
        assert "setAttribute('draggable', 'true')" in ui_html_text or \
               'setAttribute("draggable", "true")' in ui_html_text

    def test_load_button_wired_in_toolbar(self, ui_html_text):
        # Pin: the toolbar wirer attaches the Load handler
        assert "vb-load-btn" in ui_html_text
        # And there should be a click handler binding (the wireUp in
        # _vbWireToolbar)
        assert "loadBtn.addEventListener('click', _vbLoad)" in ui_html_text

    def test_test_hooks_extended_with_slice_6_methods(self, ui_html_text):
        # _vbTestHooks gained reorderStage + loadFromSpql in slice 6.
        # Pin these so future test files can rely on them.
        assert "reorderStage:" in ui_html_text
        assert "loadFromSpql:" in ui_html_text


# ═══════════════════════════════════════════════════════════════════
# 3. Endpoint registration drift guard
# ═══════════════════════════════════════════════════════════════════

class TestEndpointRegistration:
    def test_route_registered(self):
        # Pin the route exists on the Flask app - a refactor that
        # accidentally moves or renames it would fail loud.
        from desktop_app.server import app
        rules = {str(r) for r in app.url_map.iter_rules()}
        assert "/api/visual-builder/parse" in rules

    def test_route_only_accepts_post(self):
        from desktop_app.server import app
        for r in app.url_map.iter_rules():
            if str(r) == "/api/visual-builder/parse":
                # GET should NOT be in methods (Flask adds OPTIONS + HEAD
                # automatically; we only care about POST being explicit)
                assert "POST" in r.methods
                assert "GET" not in r.methods
                return
        pytest.fail("Route /api/visual-builder/parse not found")


# ═══════════════════════════════════════════════════════════════════
# 4. Round-trip end-to-end (uses the real parser via the endpoint)
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndRoundTrip:
    """End-to-end through the HTTP endpoint: build SPQL via
    join_spql_pipeline → POST to parse endpoint → assert structure
    matches what we built. Same lossless property as the parser tests
    but verified through the wire."""

    @pytest.mark.parametrize("spql", [
        'index="x.parquet"',
        '| head 5',
        'index="x" | head 5 | stats count by host | sort - count',
        'index="x" | nearest "fed" topk=20',
        'index="x" | llm_route model="m" prompt="p" escalate_to="m2"',
        'index="x" | llm_until model="m" prompt="x" max_iterations=3 '
        'converge_when_output_contains="DONE"',
        'index="x" | regex msg "(a|b|c)"',
        'makeresults count=5 | head 3',
    ])
    def test_endpoint_round_trip(self, client, spql):
        from lexers.spql_pipeline_split import join_spql_pipeline

        resp = client.post(
            "/api/visual-builder/parse", json={"spql": spql},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        # Re-join via the canonical Python helper; should equal what
        # the JS-side _vbBuildSpql would produce given the same dict
        rejoined = join_spql_pipeline({
            "index_clause": data["index_clause"],
            "stages": data["stages"],
        })
        # Re-parse the rejoined string; structural equality with the
        # first parse (the lossless property)
        resp2 = client.post(
            "/api/visual-builder/parse", json={"spql": rejoined},
        )
        data2 = resp2.get_json()
        assert data["index_clause"] == data2["index_clause"]
        assert data["stages"] == data2["stages"]
