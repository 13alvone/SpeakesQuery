"""
Tests for Phase 4 / Bet 4 slice 5 - Visual Builder foundation.

Slice 5 is UI-foundation: the page exists, the palette is wired to
grammar vocab, drag-drop drop-zone is present, generated-SPQL preview
+ Run button hook into the existing /api/query endpoint. Per-command
form templates + round-trip parsing + starter templates land in
slices 6-7.

Test layout:
  * TestUiSurfaceDriftGuards - page element + nav tab + palette host +
    canvas drop zone + run button + clear button + index input + result pane
  * TestJsModuleSurface - _vbTestHooks present; initVisualBuilder
    function exposed on window for the page-switch handler
  * TestPaletteCoverage - every command in grammar vocab maps to a
    palette group OR falls into the "More" catch-all (no command
    silently dropped)
  * TestNavTabRegistration - nav tab present in Develop dropdown
  * TestStyleScoping - CSS classes use the .vb-* prefix to avoid
    collision with the existing pages
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def ui_html_text() -> str:
    return (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()


# ═══════════════════════════════════════════════════════════════════
# 1. UI surface drift guards
# ═══════════════════════════════════════════════════════════════════

class TestUiSurfaceDriftGuards:
    """Pin every load-bearing DOM element. If a future refactor moves
    or renames any of these, the test fails loud at CI."""

    def test_page_div_present(self, ui_html_text):
        assert 'id="page-visual-builder"' in ui_html_text

    def test_nav_tab_present_in_develop_group(self, ui_html_text):
        # Nav tab must (a) exist with the right data-page, AND
        # (b) live inside the Develop dropdown nav group.
        assert (
            'data-page="page-visual-builder" data-group="develop"'
            in ui_html_text
        ), (
            "Visual Builder nav tab missing OR not in Develop group. "
            "Per the 2026-04-27 nav contract (CLAUDE.md 'Do Not'), "
            "leaf .nav-tab buttons must carry both data-page + data-group."
        )

    def test_palette_host_present(self, ui_html_text):
        assert 'id="vb-palette"' in ui_html_text
        assert 'id="vb-palette-body"' in ui_html_text

    def test_canvas_drop_zone_present(self, ui_html_text):
        assert 'id="vb-canvas-stages"' in ui_html_text

    def test_run_button_present(self, ui_html_text):
        assert 'id="vb-run-btn"' in ui_html_text

    def test_clear_button_present(self, ui_html_text):
        assert 'id="vb-clear-btn"' in ui_html_text

    def test_index_input_present(self, ui_html_text):
        assert 'id="vb-index-input"' in ui_html_text

    def test_spql_preview_present(self, ui_html_text):
        assert 'id="vb-spql-preview"' in ui_html_text

    def test_result_pane_present(self, ui_html_text):
        assert 'id="vb-result-pane"' in ui_html_text

    def test_message_area_present(self, ui_html_text):
        assert 'id="vb-message"' in ui_html_text


# ═══════════════════════════════════════════════════════════════════
# 2. JS module surface
# ═══════════════════════════════════════════════════════════════════

class TestJsModuleSurface:
    """Pin the JS function/hook surface that the page-switch handler
    + future tests rely on."""

    def test_init_function_exposed_on_window(self, ui_html_text):
        assert "window.initVisualBuilder = initVisualBuilder" in ui_html_text

    def test_page_switch_hook_invokes_init(self, ui_html_text):
        # The page-switch dispatcher should call initVisualBuilder when
        # the Visual Builder page is opened.
        assert "page === 'page-visual-builder'" in ui_html_text
        assert "initVisualBuilder()" in ui_html_text

    def test_test_hooks_present(self, ui_html_text):
        # _vbTestHooks gives slice 6+ tests a way to inspect internal
        # state (stages list, spql builder) without scraping DOM
        assert "window._vbTestHooks" in ui_html_text
        assert "buildSpql:" in ui_html_text
        assert "addStage:" in ui_html_text
        assert "resetForTests:" in ui_html_text

    def test_uses_existing_query_endpoint(self, ui_html_text):
        # The Run button posts to /api/query (the existing endpoint),
        # not to a hypothetical new /api/visual-builder/* endpoint.
        # This pins the slice-5 design decision: NO new backend
        # endpoints; reuse what's already there.
        assert "fetch('/api/query'" in ui_html_text or 'fetch("/api/query"' in ui_html_text

    def test_uses_grammar_vocab_endpoint(self, ui_html_text):
        assert "fetch('/api/grammar/vocab'" in ui_html_text or \
               'fetch("/api/grammar/vocab"' in ui_html_text


# ═══════════════════════════════════════════════════════════════════
# 3. Palette coverage - every grammar vocab command surfaces somewhere
# ═══════════════════════════════════════════════════════════════════

class TestPaletteCoverage:
    """The palette categories explicitly enumerate the vocab commands
    we care to surface. Anything in vocab but not categorised falls
    into the JS-side "More" catch-all. Pin the contract: every
    grammar command appears in at least one category in the JS source
    OR is implicitly covered by the catch-all (which we verify by
    asserting the JS catch-all branch exists)."""

    def test_palette_categories_cover_phase4_pipes(self, ui_html_text):
        # Phase 1-4 pipes added since the visual builder began design
        # MUST be in the JS palette categories (the catch-all is
        # acceptable for grammar-builtins but new pipes deserve
        # named groupings).
        for pipe in (
            "nearest", "llm", "llm_batch", "llm_route",
            "llm_refine", "llm_ensemble", "llm_until",
        ):
            assert "'" + pipe + "'" in ui_html_text, (
                f"Pipe `| {pipe}` missing from visual-builder palette "
                f"groups. Add it to the appropriate JS group in "
                f"_vbRenderPalette."
            )

    def test_palette_has_catchall_for_uncategorised(self, ui_html_text):
        # The "More" catch-all branch ensures any grammar command
        # not explicitly grouped still appears in the palette.
        # Pin BOTH the leftover-derivation logic AND the visible label.
        assert "leftover" in ui_html_text, (
            "Visual builder palette must compute a `leftover` set "
            "from grammar commands not in any named group."
        )
        assert ">More</div>" in ui_html_text, (
            "Visual builder palette must include a 'More' catch-all "
            "group label so future grammar additions don't silently disappear."
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Style scoping
# ═══════════════════════════════════════════════════════════════════

class TestStyleScoping:
    """All visual-builder CSS classes must use the .vb-* prefix to
    avoid collision with existing pages (.nb-* for notebooks, .nbx-*
    for notebook export, .ss-* for saved searches, etc.)."""

    def test_css_classes_use_vb_prefix(self, ui_html_text):
        # Pin a few key classes - full enumeration would be brittle,
        # but the load-bearing ones must follow the convention.
        for cls in (
            ".vb-layout", ".vb-panel", ".vb-palette-item",
            ".vb-canvas-stages", ".vb-stage", ".vb-stage-type-badge",
            ".vb-stage-kwargs", ".vb-stage-remove",
            ".vb-spql-preview", ".vb-result-pane", ".vb-result-table",
            ".vb-result-error",
        ):
            assert cls in ui_html_text, (
                f"Visual builder CSS class {cls!r} missing - slice-5 "
                "drift guard. If renamed, update both sides."
            )

    def test_no_collision_with_notebook_classes(self, ui_html_text):
        # The visual builder must NOT shadow notebook (.nb-*) classes.
        # We don't enumerate every .nb-* class but confirm that the
        # vb-stage-type-badge has its own scoped definition (not
        # piggybacking on .nb-cell-type-badge).
        assert ".vb-stage-type-badge" in ui_html_text
        # And confirm the nav-tab data-page is unique
        assert ui_html_text.count('data-page="page-visual-builder"') == 1, (
            "The Visual Builder nav tab should appear exactly once "
            "(in the Develop dropdown)."
        )
