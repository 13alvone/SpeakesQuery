"""
Tests for Phase 4 / Bet 4 slice 7 - Visual Builder per-command forms,
starter templates, and onboarding tour.

Slice 7 ships three deliverables on top of slice-5/6 foundation:
  * Per-command form templates: stage cards render structured widgets
    above a free-text fallback, defaulting to form mode when kwargs
    parse cleanly. Toggle (⚙ / ✎) switches modes per stage.
  * Starter templates: a JS const map of 12 preset SPQL pipelines an
    operator can drag-install onto the canvas via the slice-6
    round-trip parse endpoint.
  * Onboarding tour: a new ``visual_builder_intro`` TOURS entry +
    "Take the tour" button on the page header.

This file pins the JS source surface (drift guards) + the load-bearing
ROUND-TRIP property: every starter template's SPQL must round-trip
through ``split_spql_pipeline`` / ``join_spql_pipeline`` losslessly,
matching the slice-6 corpus contract.
"""

from __future__ import annotations

import re
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
    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ─────────────────────────────────────────────────────────────────────
# Helpers - extract starter template SPQL strings from the JS source
# ─────────────────────────────────────────────────────────────────────

# The const map is rendered as a JS array of objects with ``id`` and
# ``spql`` keys. We extract the (id, spql) pairs by walking the array
# block and parsing each ``spql:`` value (which is a JS multi-line
# string concatenation).
_STARTER_BLOCK_RE = re.compile(
    r"const\s+_vbStarterTemplates\s*=\s*\[(.*?)\n  \];",
    re.DOTALL,
)
_TEMPLATE_OBJ_RE = re.compile(
    r"\{\s*id:\s*'([^']+)',(.*?)spql:\s*((?:'[^']*'(?:\s*\+\s*'[^']*')*)),?\s*\}",
    re.DOTALL,
)


def _extract_starter_templates(ui_text: str) -> list[tuple[str, str]]:
    """Return list of (template_id, spql_string) extracted from the JS
    source. Concatenated string literals are merged into a single
    string with their separator (typically ``\\n``)."""
    block_m = _STARTER_BLOCK_RE.search(ui_text)
    if not block_m:
        return []
    block = block_m.group(1)
    out = []
    for m in _TEMPLATE_OBJ_RE.finditer(block):
        tid = m.group(1)
        spql_raw = m.group(3)
        # Join all 'piece' strings: 'a' + 'b' → 'ab'
        pieces = re.findall(r"'((?:[^'\\]|\\.)*)'", spql_raw)
        # Unescape JS string escapes that we care about
        merged = "".join(p for p in pieces)
        merged = (merged
                  .replace("\\n", "\n")
                  .replace("\\\\", "\\")
                  .replace("\\'", "'"))
        out.append((tid, merged))
    return out


# ═══════════════════════════════════════════════════════════════════
# 1. Form template registry - JS source drift guards
# ═══════════════════════════════════════════════════════════════════

class TestFormTemplateRegistry:
    """Pin every targeted command has a slice-7 form template
    registered. The JS const ``_vbFormTemplates`` is the source of
    truth; drift here would silently degrade the form-mode UX."""

    def test_form_template_const_present(self, ui_html_text):
        assert "const _vbFormTemplates" in ui_html_text

    @pytest.mark.parametrize("cmd", [
        "head", "limit", "sort", "stats", "eventstats", "streamstats",
        "eval", "where", "search", "fields", "table", "rename",
        "nearest", "dedup_semantic",
        "llm", "llm_batch", "llm_route", "llm_refine",
        "llm_ensemble", "llm_until",
    ])
    def test_command_has_form_template_entry(self, ui_html_text, cmd):
        # Each command has a property in the const map - match the
        # leading whitespace pattern + colon to avoid false-positive
        # substring matches inside string values.
        pat = r"^\s+" + re.escape(cmd) + r":\s*[\{\(_]"
        assert re.search(pat, ui_html_text, re.MULTILINE), (
            f"Form template for `| {cmd}` missing from "
            f"_vbFormTemplates. Add it to ui.html."
        )

    def test_helpers_defined(self, ui_html_text):
        # Helpers used by every form template
        assert "function _vbParseKvPairs(" in ui_html_text
        assert "function _vbSerializeKvPairs(" in ui_html_text

    def test_llm_template_factory_present(self, ui_html_text):
        # Generic LLM-form factory + cross-cutting kwargs
        assert "function _vbLlmTemplate(" in ui_html_text
        assert "_VB_LLM_COMMON_FIELDS" in ui_html_text

    def test_llm_common_fields_include_budget_gate(self, ui_html_text):
        # The user's 10-year compounding mission: every | llm pipe
        # MUST surface max_cost_usd + dry_run as form fields. Pin the
        # presence in the common-fields const.
        # Find the const block + assert both keys are mentioned inside.
        block_m = re.search(
            r"_VB_LLM_COMMON_FIELDS\s*=\s*\[(.*?)\];",
            ui_html_text,
            re.DOTALL,
        )
        assert block_m, "_VB_LLM_COMMON_FIELDS const block missing"
        block = block_m.group(1)
        assert "max_cost_usd" in block, (
            "Slice-7 budget-gate contract: every | llm form must "
            "surface max_cost_usd. See the slice-7 reference memo."
        )
        assert "dry_run" in block, (
            "Slice-7 budget-gate contract: every | llm form must "
            "surface dry_run. See the slice-7 reference memo."
        )


# ═══════════════════════════════════════════════════════════════════
# 2. Form rendering helpers + form-mode toggle
# ═══════════════════════════════════════════════════════════════════

class TestFormRenderingHelpers:
    def test_init_form_mode_helper_present(self, ui_html_text):
        assert "function _vbInitStageFormMode(" in ui_html_text

    def test_has_form_template_helper_present(self, ui_html_text):
        assert "function _vbHasFormTemplate(" in ui_html_text

    def test_render_field_widget_present(self, ui_html_text):
        assert "function _vbRenderFieldWidget(" in ui_html_text

    def test_render_stage_card_present(self, ui_html_text):
        assert "function _vbRenderStageCard(" in ui_html_text

    def test_toggle_form_mode_present(self, ui_html_text):
        assert "function _vbToggleFormMode(" in ui_html_text

    def test_update_form_field_present(self, ui_html_text):
        assert "function _vbUpdateFormField(" in ui_html_text

    def test_render_canvas_dispatches_form_mode(self, ui_html_text):
        # The canvas renderer must use the new card-builder, not the
        # inline slice-5 markup. Pin the dispatch path.
        assert "_vbStages.map(_vbRenderStageCard)" in ui_html_text

    def test_form_mode_default_init_in_add_stage(self, ui_html_text):
        # _vbAddStage must call _vbInitStageFormMode so a fresh stage
        # lands in the right mode.
        # Find the _vbAddStage function body and assert init call
        m = re.search(
            r"function _vbAddStage\(.*?\}\s*\n",
            ui_html_text,
            re.DOTALL,
        )
        assert m, "_vbAddStage function not found"
        body = m.group(0)
        assert "_vbInitStageFormMode" in body, (
            "_vbAddStage must initialise stage.formMode via "
            "_vbInitStageFormMode so new stages default correctly."
        )


# ═══════════════════════════════════════════════════════════════════
# 3. Starter templates - registry presence + categories
# ═══════════════════════════════════════════════════════════════════

class TestStarterTemplatesRegistry:
    def test_starter_templates_const_present(self, ui_html_text):
        assert "const _vbStarterTemplates" in ui_html_text

    def test_at_least_10_templates_registered(self, ui_html_text):
        templates = _extract_starter_templates(ui_html_text)
        assert len(templates) >= 10, (
            f"Slice 7 ROADMAP target: 10-20 starter templates. "
            f"Extracted {len(templates)} from the JS source."
        )

    def test_template_ids_unique(self, ui_html_text):
        templates = _extract_starter_templates(ui_html_text)
        ids = [t[0] for t in templates]
        assert len(set(ids)) == len(ids), (
            "Starter template ids must be unique. Duplicate ids "
            "would silently break _vbApplyTemplate lookup."
        )

    def test_phase4_meta_pipes_have_starter_templates(self, ui_html_text):
        # Each Phase 4 meta-pipe is a Phase 4 highlight. Pin one
        # starter template per meta-pipe so the cost-cascade /
        # iterative / ensemble / convergence patterns are demoable.
        templates = _extract_starter_templates(ui_html_text)
        all_spql = "\n".join(t[1] for t in templates)
        for pipe in ("llm_route", "llm_refine", "llm_ensemble",
                     "llm_until"):
            assert "| " + pipe in all_spql, (
                f"No starter template demonstrates `| {pipe}`. Phase "
                "4 meta-pipes are Phase 4 highlights - at least one "
                "starter must exercise each."
            )

    def test_phase1_pipes_have_starter_templates(self, ui_html_text):
        templates = _extract_starter_templates(ui_html_text)
        all_spql = "\n".join(t[1] for t in templates)
        assert "| nearest " in all_spql, (
            "No starter template demonstrates `| nearest`."
        )
        assert "| dedup_semantic " in all_spql, (
            "No starter template demonstrates `| dedup_semantic`."
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Starter templates - round-trip lossless property
# ═══════════════════════════════════════════════════════════════════

class TestStarterTemplatesRoundTrip:
    """The load-bearing slice-7 property: every starter template's
    SPQL must round-trip cleanly through the slice-6 parser/joiner.

    Operators clicking a template card invoke ``_vbApplyTemplate``
    which calls ``_vbLoadFromString`` which POSTs to
    ``/api/visual-builder/parse``. The parsed result populates stage
    cards. Re-joining via ``_vbBuildSpql`` should produce a string
    that re-parses to the same structure - the slice-6 lossless
    contract.
    """

    def test_every_starter_template_round_trips(self, ui_html_text):
        from lexers.spql_pipeline_split import (
            join_spql_pipeline,
            split_spql_pipeline,
        )
        templates = _extract_starter_templates(ui_html_text)
        assert templates, "No starter templates extracted from JS"
        failures = []
        for tid, spql in templates:
            first = split_spql_pipeline(spql)
            rejoined = join_spql_pipeline(first)
            second = split_spql_pipeline(rejoined)
            if first != second:
                failures.append((tid, first, second))
        assert not failures, (
            "Starter templates failed lossless round-trip:\n" +
            "\n".join(
                f"  - {tid}: first={first}, second={second}"
                for tid, first, second in failures
            )
        )

    def test_every_starter_template_parses_via_endpoint(
        self, ui_html_text, client,
    ):
        # End-to-end: each starter template's SPQL must parse cleanly
        # via the slice-6 endpoint (the path the SPA actually uses).
        templates = _extract_starter_templates(ui_html_text)
        assert templates, "No starter templates extracted"
        for tid, spql in templates:
            resp = client.post(
                "/api/visual-builder/parse", json={"spql": spql},
            )
            assert resp.status_code == 200, (
                f"Template {tid!r} returned HTTP {resp.status_code}"
            )
            data = resp.get_json()
            assert data["status"] == "success", (
                f"Template {tid!r} parse failed: {data}"
            )

    def test_every_starter_template_has_at_least_one_stage(
        self, ui_html_text,
    ):
        # Sanity: every starter template should produce at least one
        # stage card (otherwise it's a no-op pipeline).
        from lexers.spql_pipeline_split import split_spql_pipeline
        templates = _extract_starter_templates(ui_html_text)
        for tid, spql in templates:
            parsed = split_spql_pipeline(spql)
            assert parsed["stages"], (
                f"Template {tid!r} parses to zero stages - likely "
                "malformed SPQL or missing pipe segments."
            )


# ═══════════════════════════════════════════════════════════════════
# 5. UI surface drift guards - slice 7 elements
# ═══════════════════════════════════════════════════════════════════

class TestUiSurfaceSlice7:
    def test_take_tour_button_present(self, ui_html_text):
        assert 'id="vb-tour-btn"' in ui_html_text

    def test_take_tour_button_invokes_start_tour(self, ui_html_text):
        # Pin the wiring: the button calls window.startTour with the
        # slice-7 tour id.
        assert "window.startTour('visual_builder_intro')" in ui_html_text

    def test_templates_section_present(self, ui_html_text):
        assert 'id="vb-templates-section"' in ui_html_text

    def test_templates_list_host_present(self, ui_html_text):
        assert 'id="vb-templates-list"' in ui_html_text

    def test_templates_renderer_function_present(self, ui_html_text):
        assert "function _vbRenderStarterTemplates(" in ui_html_text

    def test_apply_template_function_present(self, ui_html_text):
        assert "function _vbApplyTemplate(" in ui_html_text

    def test_load_from_string_function_present(self, ui_html_text):
        # Slice-7 refactor extracts _vbLoadFromString so starter
        # templates can reuse the parse + populate path.
        assert "_vbLoadFromString" in ui_html_text


# ═══════════════════════════════════════════════════════════════════
# 6. CSS scoping
# ═══════════════════════════════════════════════════════════════════

class TestCssScoping:
    """Slice 7 added CSS classes for form mode + templates panel +
    tour button. All must use the .vb-* prefix per the slice-5
    convention. Pin a few load-bearing classes."""

    @pytest.mark.parametrize("cls", [
        ".vb-stage-form-mode",
        ".vb-stage-header",
        ".vb-stage-form-fields",
        ".vb-form-field",
        ".vb-form-input",
        ".vb-form-textarea",
        ".vb-stage-mode-toggle",
        ".vb-templates-list",
        ".vb-template-card",
        ".vb-template-card-title",
        ".vb-template-card-desc",
        ".vb-template-card-cat",
        ".vb-tour-btn",
    ])
    def test_slice_7_css_class_present(self, ui_html_text, cls):
        assert cls in ui_html_text, (
            f"Slice-7 CSS class {cls!r} missing - drift guard. "
            "If renamed, update the corresponding JS render path."
        )


# ═══════════════════════════════════════════════════════════════════
# 7. Tour registration
# ═══════════════════════════════════════════════════════════════════

class TestTourRegistration:
    def test_visual_builder_tour_in_TOURS(self, ui_html_text):
        # Pin the TOURS entry exists.
        assert "visual_builder_intro:" in ui_html_text

    def test_tour_has_at_least_5_steps(self, ui_html_text):
        # Extract the visual_builder_intro array and count step objects
        m = re.search(
            r"visual_builder_intro:\s*\[(.*?)\],\s*\n\s*\};",
            ui_html_text,
            re.DOTALL,
        )
        assert m, "visual_builder_intro tour not found in TOURS"
        block = m.group(1)
        # Each step is a `{` block (top-level inside the array)
        # Count via the `target:` keyword which every step has
        steps = re.findall(r"target:\s*", block)
        assert len(steps) >= 5, (
            f"Tour has {len(steps)} steps; need at least 5 for a "
            "meaningful walkthrough."
        )

    def test_tour_targets_real_dom_elements(self, ui_html_text):
        # Steps that target a `#some-id` element should target an id
        # that actually exists in the page. Loose match - ids are
        # quoted in the tour entry; ensure the corresponding `id="..."`
        # attribute is in the source.
        m = re.search(
            r"visual_builder_intro:\s*\[(.*?)\],\s*\n\s*\};",
            ui_html_text,
            re.DOTALL,
        )
        assert m
        block = m.group(1)
        ids = re.findall(r"target:\s*'#([\w-]+)'", block)
        # Filter to vb-prefixed ids only (other tour steps might point
        # at other pages' elements; we only verify our own).
        for tid in ids:
            if tid.startswith("vb-"):
                assert f'id="{tid}"' in ui_html_text, (
                    f"Tour step targets #{tid} but no element with "
                    f'id="{tid}" exists in ui.html.'
                )


# ═══════════════════════════════════════════════════════════════════
# 8. _vbTestHooks extensions - slice 7 surface
# ═══════════════════════════════════════════════════════════════════

class TestTestHooksExtensions:
    """Slice 7 extended _vbTestHooks with form-mode helpers + starter
    template helpers. Pin them - future slices will rely on these."""

    @pytest.mark.parametrize("hook", [
        "toggleFormMode:",
        "updateFormField:",
        "hasFormTemplate:",
        "parseTemplate:",
        "serializeTemplate:",
        "listFormTemplates:",
        "listStarterTemplates:",
        "applyTemplate:",
        "loadFromString:",
    ])
    def test_hook_exposed(self, ui_html_text, hook):
        assert hook in ui_html_text, (
            f"_vbTestHooks missing slice-7 extension {hook!r}. "
            "Add it to the window._vbTestHooks export."
        )


# ═══════════════════════════════════════════════════════════════════
# 9. No new backend endpoints (UI-only slice)
# ═══════════════════════════════════════════════════════════════════

class TestNoNewBackendEndpoints:
    """Slice 7 is a pure UI slice - no new server-side routes. The
    starter templates load via the existing slice-6
    /api/visual-builder/parse endpoint; the tour reuses the existing
    tour engine. Pin that no extra route was added (catches scope
    creep)."""

    def test_only_slice_6_endpoint_for_visual_builder(self):
        from desktop_app.server import app
        rules = [str(r) for r in app.url_map.iter_rules()
                 if "/api/visual-builder/" in str(r)]
        # Slice 6 added /api/visual-builder/parse - that's the only
        # visual-builder route slice 7 should leave behind.
        assert rules == ["/api/visual-builder/parse"], (
            f"Unexpected /api/visual-builder/* routes: {rules}. "
            "Slice 7 is UI-only; no new server-side routes."
        )

    def test_run_button_still_uses_query_endpoint(self, ui_html_text):
        # The slice-5 design decision: Run posts to /api/query.
        # Slice 7 must not introduce a separate execution endpoint.
        assert ("fetch('/api/query'" in ui_html_text or
                'fetch("/api/query"' in ui_html_text)


# ═══════════════════════════════════════════════════════════════════
# 10. Round-trip property: form-mode preserves slice-6 lossless
# ═══════════════════════════════════════════════════════════════════

class TestFormModePreservesLossless:
    """The form-mode contract: serialize(parse(kwargs)) must produce a
    kwargs string that re-parses to the same {key:value} object,
    modulo whitespace normalisation. Without this property, toggling
    a stage card from raw → form → raw could silently mangle the
    pipeline.

    We can't run JS in the test, so we extract the slice-6 corpus and
    assert each query still round-trips through the slice-6 parser.
    Slice 7 doesn't change the slice-6 parser - but a regression here
    would mean form-mode changes broke the underlying split/join."""

    def test_slice_6_corpus_still_round_trips(self):
        # Sanity: slice 7 must not regress slice 6's lossless property.
        # Re-run a small slice of the slice-6 corpus to verify.
        from lexers.spql_pipeline_split import (
            join_spql_pipeline,
            split_spql_pipeline,
        )
        sample = [
            'index="x.parquet"',
            'index="x" | head 5',
            'index="x" | stats count by host | sort - count',
            'index="x" | nearest "fed" topk=20',
            'index="x" | llm_route model="m" prompt="p" escalate_to="m2"',
            'index="x" | llm_until model="m" prompt="x" max_iterations=3',
            'index="x" | regex msg "(a|b|c)"',
        ]
        for spql in sample:
            first = split_spql_pipeline(spql)
            rejoined = join_spql_pipeline(first)
            second = split_spql_pipeline(rejoined)
            assert first == second, (
                f"Slice-7 regression: slice-6 corpus query failed "
                f"round-trip - {spql!r}\nfirst={first}\nsecond={second}"
            )


# ═══════════════════════════════════════════════════════════════════
# 11. Author's note: HTML script tag balance (slice-7 added forms +
#     starter templates + tour entries; verify nothing accidentally
#     broke a <script> boundary)
# ═══════════════════════════════════════════════════════════════════

class TestHtmlIntegrity:
    def test_script_tag_balance(self, ui_html_text):
        opens = ui_html_text.count("\n<script>")
        closes = ui_html_text.count("\n</script>")
        # The reference_html_script_tag_balance_in_spa memo: every
        # opening <script> has a matching </script>. Slice 7 didn't
        # add new script blocks - but if a future edit drifts, this
        # canary fires.
        assert opens == closes, (
            f"Script tag imbalance: {opens} <script> vs {closes} "
            "</script>. JS-style // comments outside script blocks "
            "render as page text. Use <!-- --> for inter-script "
            "descriptions."
        )
