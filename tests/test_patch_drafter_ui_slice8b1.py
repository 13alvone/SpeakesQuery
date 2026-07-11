"""
Tests for Phase 4 / Bet 4 slice 8b-1 - Visual Builder of failed-task
patch-suggestion review.

Slice 8a (commit ``1ca8c15``) shipped the patch drafter module + the
``patch_suggestions`` log + opt-in engine wiring. The diff lived only
in SPQL queries against the log.

Slice 8b-1 surfaces the diff inline on the Ingestions page: each
failed-task row (where ``task.last_run_status === 'failed'``) gets a
secondary row beneath it containing a ``<details>`` disclosure. On
expand, JS fires the SPQL query
``index="indexes/logs/patch_suggestions/*" | where task_id="X" |
sort -_epoch | head 1`` against the existing ``/api/query`` endpoint
and renders the most recent suggestion - diff in a syntax-aware
``<pre>`` block, plus model / cost / latency / explanation metadata.

This file pins:
  * The CSS classes for the disclosure + diff coloring exist
  * The JS helpers (lazy-load, diff colorer, suggestion renderer,
    secondary-row builder) are defined
  * The wiring point in ``_siRender`` calls the row builder
  * The data-si-suggestion-for attribute is DISTINCT from the
    pre-existing data-si-task-id attribute (per CLAUDE.md "Do Not"
    on the data-si-task-id contract)
  * No new backend endpoint was added (slice-5 design principle)
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
# 1. CSS scoping - slice 8b-1 styling drift guards
# ═══════════════════════════════════════════════════════════════════

class TestCssScoping:
    """Pin the CSS classes that the slice-8b-1 inline disclosure
    + diff renderer rely on. All use the ``.si-suggestion-*`` and
    ``.diff-line-*`` prefixes - distinct from existing ``.si-toggle``
    and ``.diff-*`` namespaces; no collision."""

    @pytest.mark.parametrize("cls", [
        ".si-suggestion-row",
        ".si-suggestion-details",
        ".si-suggestion-summary",
        ".si-suggestion-content",
        ".si-suggestion-meta",
        ".si-suggestion-meta-pill",
        ".si-suggestion-diff-label",
        ".si-suggestion-explanation-label",
        ".si-suggestion-diff",
        ".si-suggestion-explanation",
        ".si-suggestion-loading",
        ".si-suggestion-empty",
    ])
    def test_slice_8b1_class_present(self, ui_html_text, cls):
        assert cls in ui_html_text, (
            f"Slice-8b-1 CSS class {cls!r} missing - drift guard. "
            "If renamed, update both the JS render path and this test."
        )

    @pytest.mark.parametrize("cls", [
        ".diff-line-add",
        ".diff-line-remove",
        ".diff-line-meta",
        ".diff-line-context",
    ])
    def test_diff_line_color_classes_present(self, ui_html_text, cls):
        assert cls in ui_html_text, (
            f"Slice-8b-1 diff-line color class {cls!r} missing. "
            "_siRenderDiffWithColoring relies on these classes."
        )

    def test_meta_pill_status_variants_present(self, ui_html_text):
        # The meta pill colors itself based on data-status. Pin all 5
        # status variants used by the patch_drafter return shape.
        for status in (
            "success", "dry_run", "skipped_budget",
            "skipped_no_key", "error",
        ):
            assert f'data-status="{status}"' in ui_html_text, (
                f"Slice-8b-1 meta-pill missing color rule for status "
                f"{status!r}. Add a "
                f'.si-suggestion-meta-pill[data-status="{status}"] '
                "selector to the CSS."
            )


# ═══════════════════════════════════════════════════════════════════
# 2. JS helpers - function presence drift guards
# ═══════════════════════════════════════════════════════════════════

class TestJsHelpersPresent:
    @pytest.mark.parametrize("name", [
        "_siRenderDiffWithColoring",
        "_siRenderSuggestion",
        "_siLoadPatchSuggestion",
        "_siBuildSuggestionRow",
    ])
    def test_function_defined(self, ui_html_text, name):
        # Match either `function name(` or `async function name(`
        pat = r"\b(?:async\s+)?function\s+" + re.escape(name) + r"\("
        assert re.search(pat, ui_html_text), (
            f"Slice-8b-1 helper {name!r} not defined. JS rendering "
            "path will fail at runtime."
        )

    def test_diff_colorer_classifies_all_four_line_kinds(
        self, ui_html_text,
    ):
        # The colorer must check for all 4 prefix kinds: meta (`---`,
        # `+++`, `@@`, `diff `, `index `), add (`+`), remove (`-`),
        # default (context). Pin the prefix checks.
        m = re.search(
            r"function\s+_siRenderDiffWithColoring\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m, "_siRenderDiffWithColoring body not found"
        body = m.group(1)
        # Check the prefix tests are all present
        for prefix in (
            "'+++'", "'---'", "'@@'", "'diff '", "'index '",
        ):
            assert (
                ".startsWith(" + prefix + ")" in body
                or ".startsWith( " + prefix + ")" in body
            ), (
                f"Diff colorer missing meta-prefix check for {prefix!r}. "
                "Lines starting with this prefix would be miscategorised "
                "as add/remove/context."
            )


# ═══════════════════════════════════════════════════════════════════
# 3. Wiring - _siRender appends a secondary row for failed tasks
# ═══════════════════════════════════════════════════════════════════

class TestRenderWiring:
    def test_si_render_calls_row_builder(self, ui_html_text):
        # Pin the call site: _siRender must invoke
        # _siBuildSuggestionRow once inside its forEach loop.
        assert "_siBuildSuggestionRow(tbody, task," in ui_html_text, (
            "_siRender must call _siBuildSuggestionRow inside its "
            "task-iteration loop. Without this call, failed-task "
            "rows show no suggestion disclosure."
        )

    def test_row_builder_short_circuits_for_non_failed_tasks(
        self, ui_html_text,
    ):
        # The row builder MUST early-return when last_run_status is
        # not 'failed' - otherwise every row gets a secondary row
        # cluttering the table.
        m = re.search(
            r"function\s+_siBuildSuggestionRow\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m, "_siBuildSuggestionRow body not found"
        body = m.group(1)
        # The very first guard should be a last_run_status check
        # against 'failed'.
        assert (
            "task.last_run_status" in body and "'failed'" in body
        ), (
            "_siBuildSuggestionRow must guard against non-failed "
            "tasks early. Without this check, every Ingestions row "
            "gets a redundant secondary row."
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Data-attribute boundary (CLAUDE.md "Do Not" pin)
# ═══════════════════════════════════════════════════════════════════

class TestDataAttributeBoundary:
    """The CLAUDE.md "Do Not" entry pins the
    ``tr[data-si-task-id="X"]`` selector contract - Pipeline Check
    cross-tab nav uses it to scroll-highlight the matching row.
    Slice 8b-1's secondary row must NOT carry the same attribute,
    otherwise the cross-tab nav could highlight the wrong row.
    """

    def test_secondary_row_uses_distinct_attribute(self, ui_html_text):
        # The secondary row uses dataset.siSuggestionFor (which the
        # browser exposes as data-si-suggestion-for) instead of
        # dataset.siTaskId. Pin both halves.
        assert (
            "tr.dataset.siSuggestionFor = String(task.id)"
            in ui_html_text
        ), (
            "Slice 8b-1 secondary row must use a distinct data "
            "attribute (siSuggestionFor) - NOT siTaskId - so the "
            "Pipeline Check cross-tab nav scroll target stays "
            "unambiguous. See CLAUDE.md 'Do Not' on data-si-task-id."
        )

    def test_secondary_row_does_not_set_si_task_id(self, ui_html_text):
        # Find the body of _siBuildSuggestionRow and assert the
        # secondary row never sets data-si-task-id.
        m = re.search(
            r"function\s+_siBuildSuggestionRow\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        assert "siTaskId" not in body, (
            "Slice 8b-1 secondary-row builder set siTaskId - that "
            "would put two rows under the same data-si-task-id "
            "attribute and break the cross-tab nav scroll target."
        )


# ═══════════════════════════════════════════════════════════════════
# 5. SPQL query shape - slice 7 reuse-existing-endpoint principle
# ═══════════════════════════════════════════════════════════════════

class TestQueryShape:
    """Per ``reference_reuse_existing_endpoint_for_ui_surface.md`` -
    slice 8b-1 routes the lazy-load through the EXISTING /api/query
    endpoint. The SPQL string is built client-side; no new backend
    route. Pin the SPQL shape so the slice-8a log columns + slice-6
    parser keep matching."""

    def test_spql_targets_patch_suggestions_log(self, ui_html_text):
        m = re.search(
            r"async\s+function\s+_siLoadPatchSuggestion\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m, "_siLoadPatchSuggestion body not found"
        body = m.group(1)
        assert 'indexes/logs/patch_suggestions/' in body, (
            "Slice 8b-1 must query the slice-8a log path. Drift here "
            "would silently return empty results forever."
        )

    def test_spql_uses_where_task_id_clause(self, ui_html_text):
        m = re.search(
            r"async\s+function\s+_siLoadPatchSuggestion\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        assert 'where task_id=' in body, (
            "SPQL must filter by task_id; otherwise the disclosure "
            "would render the most-recent suggestion ACROSS ALL "
            "tasks, which would be misleading."
        )

    def test_spql_sorts_by_recency_and_takes_one(self, ui_html_text):
        m = re.search(
            r"async\s+function\s+_siLoadPatchSuggestion\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        assert "sort -_epoch" in body, (
            "Slice 8b-1 must sort by descending _epoch to surface "
            "the MOST RECENT suggestion, not the oldest."
        )
        assert "head 1" in body, (
            "Slice 8b-1 must limit to one row - the disclosure renders "
            "a single most-recent suggestion."
        )

    def test_uses_existing_query_endpoint(self, ui_html_text):
        m = re.search(
            r"async\s+function\s+_siLoadPatchSuggestion\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        # Must call the existing /api/query endpoint, NOT a new one
        assert "'/api/query'" in body or '"/api/query"' in body, (
            "Slice 8b-1 must POST to /api/query (the existing "
            "endpoint), per the slice-7 reuse-endpoint principle."
        )


# ═══════════════════════════════════════════════════════════════════
# 6. No new backend endpoints (slice 8b-1 is pure UI)
# ═══════════════════════════════════════════════════════════════════

class TestNoNewBackendEndpoints:
    """Slice 8b-1 is a pure UI slice - no new server-side routes.
    The slice-7 starter-templates pattern (use existing endpoints
    for new UI surfaces) applies here too."""

    def test_no_patch_suggestion_routes(self):
        from desktop_app.server import app
        # Word-boundary matching so we don't false-positive on
        # 'dispatch' (which contains 'patch' as a substring).
        pat = re.compile(
            r"\b(patch[-_](drafter|suggestion)|suggestion)\b",
            re.IGNORECASE,
        )
        rules = [str(r) for r in app.url_map.iter_rules()
                 if pat.search(str(r))]
        # Slice 8a + 8b-1 should leave NO server-side patch-drafter,
        # patch-suggestion, or suggestion routes. The slice-8a drafter
        # is invoked from the engine; the slice-8b-1 UI reads via
        # SPQL. Slice 8b-2 may add /api/patch-drafter/create-pr -
        # explicitly out of scope for 8b-1.
        assert rules == [], (
            f"Unexpected patch-drafter/patch-suggestion/suggestion "
            f"routes: {rules}. Slice 8b-1 is UI-only; no new "
            "server-side routes."
        )


# ═══════════════════════════════════════════════════════════════════
# 7. Disclosure UX - lazy-load on first expand, cached afterwards
# ═══════════════════════════════════════════════════════════════════

class TestLazyLoadBehavior:
    def test_load_only_on_first_open(self, ui_html_text):
        m = re.search(
            r"function\s+_siBuildSuggestionRow\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        # The toggle handler must check details.dataset.siSuggestionLoaded
        # to avoid re-fetching on every collapse/expand cycle.
        assert "siSuggestionLoaded" in body, (
            "Slice 8b-1 must cache the lazy-load result via a dataset "
            "marker so collapsing + re-expanding doesn't re-hit "
            "/api/query."
        )

    def test_load_only_when_expanded(self, ui_html_text):
        m = re.search(
            r"function\s+_siBuildSuggestionRow\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        # The toggle handler should bail when details.open is false
        # (the collapse case fires the same event).
        assert "if (!details.open)" in body or "details.open" in body, (
            "Slice 8b-1 toggle handler must guard against firing on "
            "collapse. Without this, every expand+collapse triggers "
            "a redundant fetch."
        )


# ═══════════════════════════════════════════════════════════════════
# 8. HTML escape - operator-controlled task_id can't break the SPQL
# ═══════════════════════════════════════════════════════════════════

class TestSpqlInjectionGuard:
    """The SPQL query is built client-side with the task_id
    interpolated. Task ids are server-issued integers in normal
    operation, but defense-in-depth: the JS should escape any
    embedded double quotes so a malformed id can't break out of the
    SPQL string literal. (The grammar's where-clause syntax uses
    double quotes for string values.)"""

    def test_double_quote_escape_in_task_id(self, ui_html_text):
        m = re.search(
            r"async\s+function\s+_siLoadPatchSuggestion\s*\([^)]*\)\s*\{(.*?)\n  \}",
            ui_html_text, re.DOTALL,
        )
        assert m
        body = m.group(1)
        assert "replace(" in body and '"' in body, (
            "Slice 8b-1 must escape embedded double quotes in the "
            "task_id before building the SPQL where-clause string. "
            "Defense-in-depth against malformed ids."
        )


# ═══════════════════════════════════════════════════════════════════
# 9. HTML integrity (slice-7 pattern)
# ═══════════════════════════════════════════════════════════════════

class TestHtmlIntegrity:
    def test_script_tag_balance(self, ui_html_text):
        opens = ui_html_text.count("\n<script>")
        closes = ui_html_text.count("\n</script>")
        assert opens == closes, (
            f"Script tag imbalance: {opens} <script> vs {closes} "
            "</script>. JS-style // comments outside script blocks "
            "render as page text. Use <!-- --> for inter-script "
            "descriptions."
        )
