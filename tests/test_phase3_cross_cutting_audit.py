"""
Phase 3 cross-cutting principles audit (slice 10 - Phase 3 close).

The ROADMAP "Cross-Cutting Principles" section lists 8 invariants that
every phase must satisfy. This file is the drift guard that pins each
one in CI for Phase 3 (Bet 4 - Notebook Mode). Mirrors the slice-8
Phase 2 audit pattern (`tests/test_phase2_cross_cutting_audit.py`).

The 8 principles, mapped to the test classes below:

  1. **Zero green-test regression** - TestPrinciple1ZeroRegression
     (file-existence drift guard for every Phase 3 slice's test file)
  2. **Additive only** - TestPrinciple2AdditiveOnly
     (`.spqnb` schema record fields + `ALLOWED_CELL_TYPES` frozen set
     + cell record field set - frozen snapshots for everything that
     could regress)
  3. **Drift guards from day 1** - TestPrinciple3DriftGuards
     (JS↔Python NB_CELL_TYPES drift guard exists; per-cell-type
     dispatch present; cell-engine handler exists for every type)
  4. **Docs = definition of done** - TestPrinciple4Docs
     (`docs/lang/19_notebooks.md` exists + non-trivial; CHANGELOG.md
     mentions every Phase 3 slice 1-10)
  5. **Each phase ends with a demoable artifact** - TestPhase3DemoableArtifact
     (the OEB-in-a-notebook example: every cell type reachable +
     promote_to_alert_group is the deploy primitive - verifies
     reachability without running the full pipeline)
  6. **Feature-flagged until burn-in** - TestPrinciple6ExplicitOptIn
     (notebooks default to inert: a notebook only runs when explicitly
     opened/executed; nothing fires from cron without explicit
     promote → AG. The default tree ships only `getting_started.spqnb`,
     an inert walk-through.)
  7. **Local-first remains the moat** - TestPrinciple7LocalFirst
     (notebooks run entirely local; Monaco loads from CDN with
     textarea fallback; vega-embed loads from CDN with JSON-pre
     fallback; WeasyPrint optional with graceful 503; no mandatory
     cloud dependency.)
  8. **Money-leak audit pattern applies to every billable surface** -
     TestPrinciple8MoneyLeakAndConfigLeakCanaries
     (slice-9 config-leak canary class exists in
     `tests/test_notebook_slice9_promote.py` + CLAUDE.md "Do Not"
     references the boundary; slice-7 money-leak canary still applies
     to notebook execution since cells can include `| llm` pipes via
     the slice-7 affordance.)

This file is the slice-10 acceptance test for Phase 3 close. If any
test here fails, Phase 3 is not actually done.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


# ═════════════════════════════════════════════════════════════════════
# Principle 1 - Zero green-test regression
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple1ZeroRegression:
    """Phase 3 shipped 9 user-facing slices (1-9). Each has at least
    one dedicated test file. The sweep itself is the enforcement; this
    class catches the case where a future commit silently moves /
    deletes one of those test files.
    """

    PHASE_3_TEST_FILES = (
        "test_notebook_store.py",                # slice 1
        "test_notebook_engine.py",               # slice 2
        "test_notebook_engine_cache.py",         # slice 3
        "test_notebook_cache_store.py",          # slice 3
        "test_notebook_api.py",                  # slice 4
        "test_notebook_slice5_renderers.py",     # slice 5
        "test_notebook_slice6_polish.py",        # slice 6
        "test_notebook_slice7_chart_pipe.py",    # slice 7
        "test_notebook_slice8_export.py",        # slice 8
        "test_notebook_slice9_promote.py",       # slice 9
        "test_phase3_cross_cutting_audit.py",    # this file (slice 10)
    )

    def test_phase3_test_files_exist(self):
        for f in self.PHASE_3_TEST_FILES:
            assert (PROJECT_ROOT / "tests" / f).exists(), (
                f"Phase 3 expects {f} to exist. If a slice's tests "
                "moved, update this list AND every CHANGELOG entry "
                "that referenced the old path."
            )


# ═════════════════════════════════════════════════════════════════════
# Principle 2 - Additive only (no schema field ever removed)
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple2AdditiveOnly:
    """Phase 3 introduced two persistence schemas + a closed enum:

      * `.spqnb` notebook YAML - `validation/NotebookValidation.py`
        (slice 1, frozen v1 - additive only)
      * `notebook_cache.sqlite` - `notebook_cache_store.py` (slice 3,
        content-hash + on-disk pickle payload paths)
      * `ALLOWED_CELL_TYPES` frozen-set enum - slice 1 shipped 6;
        slice 9 added `promote_to_alert_group` for 7 total

    All are now FROZEN. Future slices may grow the schema (add
    optional fields to the cell record, add new cell types) but
    columns / fields / enum members documented here MUST NEVER be
    removed without a formal migration. The cache-tracking fields
    (`_last_*_hash`) were forward-declared in slice 1 specifically
    so slice 3's reactive cache didn't need a YAML migration -
    same additive principle.
    """

    # Frozen as of slice 1; slice 9 did NOT alter these (added a new
    # cell type, not new top-level fields).
    NOTEBOOK_RECORD_FROZEN_FIELDS = frozenset({
        "id", "schema_version", "name", "description",
        "default_max_cost_usd", "cells",
    })

    # Frozen as of slice 1. Cache-tracking fields are optional + only
    # appear when set; the BASE field set is what matters.
    CELL_RECORD_BASE_FROZEN_FIELDS = frozenset({
        "id", "type", "source", "metadata",
    })

    # Frozen as of slice 9. Slice 1 shipped 6; slice 9 added
    # promote_to_alert_group → 7. The additive-only rule means future
    # slices can add but never remove.
    ALLOWED_CELL_TYPES_FROZEN = frozenset({
        "spql", "python", "chart", "markdown", "param", "pipe",
        "promote_to_alert_group",
    })

    def test_notebook_record_field_set(self):
        from validation.NotebookValidation import NotebookValidation
        out = NotebookValidation.validate_record({"id": "audit_test"})
        actual = set(out.keys())
        missing = self.NOTEBOOK_RECORD_FROZEN_FIELDS - actual
        assert not missing, (
            f"Notebook record missing frozen fields: {sorted(missing)}. "
            "Phase 3 schema is additive-only. Removing a field breaks "
            "every existing .spqnb on disk."
        )

    def test_cell_record_base_field_set(self):
        from validation.NotebookValidation import NotebookValidation
        out = NotebookValidation.validate_record({
            "id": "audit_test",
            "cells": [{"id": "c1", "type": "markdown", "source": "x"}],
        })
        cell = out["cells"][0]
        missing = self.CELL_RECORD_BASE_FROZEN_FIELDS - set(cell.keys())
        assert not missing, (
            f"Cell record missing base frozen fields: {sorted(missing)}. "
            "Schema is additive-only - optional fields may be added but "
            "the base field set never shrinks."
        )

    def test_allowed_cell_types_includes_all_frozen(self):
        from validation.NotebookValidation import ALLOWED_CELL_TYPES
        missing = self.ALLOWED_CELL_TYPES_FROZEN - ALLOWED_CELL_TYPES
        assert not missing, (
            f"ALLOWED_CELL_TYPES missing frozen members: "
            f"{sorted(missing)}. Phase 3 enum is additive-only. "
            "Removing a cell type breaks every notebook that uses it."
        )

    def test_cache_tracking_fields_remain_optional(self):
        """Slice-1 forward-declared cache-tracking fields (slice 3
        populates them). They're OPTIONAL - a notebook saved by an
        operator hand-editing YAML doesn't need them. This pins the
        contract so a future slice can't make them required."""
        from validation.NotebookValidation import NotebookValidation
        # Should validate WITHOUT any _last_* fields
        out = NotebookValidation.validate_record({
            "id": "audit_test",
            "cells": [{"id": "c1", "type": "markdown", "source": "x"}],
        })
        cell = out["cells"][0]
        # No cache-tracking fields populated - that's allowed
        for cache_field in ("_last_executed_at", "_last_input_hash",
                            "_last_output_hash", "_last_runtime_ms"):
            assert cache_field not in cell or cell.get(cache_field) is None


# ═════════════════════════════════════════════════════════════════════
# Principle 3 - Drift guards from day 1
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple3DriftGuards:
    """Every cell type / cell-type-aware surface has a drift guard.
    The most load-bearing one: the JS↔Python `NB_CELL_TYPES` drift
    guard from slice 6 - if the JS list and Python frozen set ever
    drift, the SPA's "+ Cell" picker silently disagrees with the
    schema validator.
    """

    def test_js_python_cell_type_drift_guard_exists(self):
        path = PROJECT_ROOT / "tests" / "test_notebook_slice6_polish.py"
        text = path.read_text()
        # The drift guard test compares NB_CELL_TYPES (JS) to
        # ALLOWED_CELL_TYPES (Python). Pin its presence.
        assert "NB_CELL_TYPES" in text and "ALLOWED_CELL_TYPES" in text, (
            "tests/test_notebook_slice6_polish.py should contain the "
            "JS↔Python NB_CELL_TYPES drift guard. Without it, adding "
            "a cell type to one surface and forgetting the other is "
            "silent until the operator hits the gap."
        )

    def test_every_cell_type_has_engine_dispatch(self):
        """Engine's `execute_cell` should dispatch every cell type.
        If a future slice adds a type to the schema but forgets the
        handler, the engine will fall through to `UnknownCellType` -
        catastrophic UX silently. Pin the dispatch table by source-
        scanning the engine module.
        """
        from validation.NotebookValidation import ALLOWED_CELL_TYPES
        engine_src = (
            PROJECT_ROOT / "notebook_engine.py"
        ).read_text()
        for cell_type in ALLOWED_CELL_TYPES:
            # Each type should appear in a dispatch comparison in the
            # engine. Match either `cell_type == "X"` or `cell_type in
            # ("X", ...)` shapes.
            patterns = (
                f'cell_type == "{cell_type}"',
                f"cell_type == '{cell_type}'",
                f'"{cell_type}",',
                f"'{cell_type}',",
                f'"{cell_type}"',
                f"'{cell_type}'",
            )
            assert any(p in engine_src for p in patterns), (
                f"notebook_engine.py has no dispatch for cell type "
                f"{cell_type!r}. Adding a type to ALLOWED_CELL_TYPES "
                "without an engine handler routes the cell to "
                "UnknownCellType - a silent UX failure."
            )

    def test_promote_cell_grammar_path_via_validator_not_g4(self):
        """Phase 3 cells are NOT new SPQL pipes; they don't add to
        speakesQuery.g4. The drift guard is the validator + cell-type
        enum, not grammar parity. Document the absence so a future
        audit doesn't flag this as a gap.
        """
        # The .g4 file should NOT contain promote_to_alert_group as
        # a token - that would be a category error.
        g4 = (PROJECT_ROOT / "lexers" / "speakesQuery.g4").read_text()
        assert "promote_to_alert_group" not in g4, (
            "promote_to_alert_group is a NOTEBOOK CELL type, not a "
            "SPQL pipe. It must NOT appear in the grammar file."
        )


# ═════════════════════════════════════════════════════════════════════
# Principle 4 - Docs = definition of done
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple4Docs:
    """`docs/lang/19_notebooks.md` is the dedicated reference;
    CHANGELOG.md tracks every slice."""

    def test_19_notebooks_doc_exists_and_nontrivial(self):
        path = PROJECT_ROOT / "docs" / "lang" / "19_notebooks.md"
        assert path.exists(), "docs/lang/19_notebooks.md is missing"
        text = path.read_text()
        # Must mention every user-facing surface that shipped in Phase 3.
        for token in (
            "promote_to_alert_group",   # slice 9 headliner
            "spql",                     # slice 1+ cell type
            "python",                   # slice 1+ cell type
            "markdown",                 # slice 1+ cell type
            "chart",                    # slice 7
            "param",                    # slice 5
            "pipe",                     # slice 1+
            "reactive cache",           # slice 3 (case-insensitive search below)
            "promote",                  # slice 9 deploy concept
            "round-trip",               # slice 9 round-trip
            ".spqnb",                   # the schema file extension
        ):
            assert token in text or token.title() in text or token.lower() in text.lower(), (
                f"docs/lang/19_notebooks.md should mention {token!r}"
            )
        # Should be at least 200 lines (the slice-9 ship was ~250)
        assert len(text.splitlines()) >= 200, (
            f"19_notebooks.md has {len(text.splitlines())} lines; "
            "expected ≥ 200. Did someone truncate?"
        )

    def test_changelog_has_phase3_slice_entries(self):
        path = PROJECT_ROOT / "CHANGELOG.md"
        text = path.read_text()
        for token in (
            "Phase 3 / Bet 4 slice 1",
            "Phase 3 / Bet 4 slice 2",
            "Phase 3 / Bet 4 slice 3",
            "Phase 3 / Bet 4 slice 4",
            "Phase 3 / Bet 4 slice 5",
            "Phase 3 / Bet 4 slice 6",
            "Phase 3 / Bet 4 slice 7",
            "Phase 3 / Bet 4 slice 8",
            "Phase 3 / Bet 4 slice 9",
            # Slice 10 (this audit) - the close itself
            "Phase 3 / Bet 4 slice 10",
        ):
            assert token in text, (
                f"CHANGELOG.md is missing an entry for {token!r}. "
                "Per the 'Docs = definition of done' principle, every "
                "slice gets a CHANGELOG entry before merge."
            )


# ═════════════════════════════════════════════════════════════════════
# Principle 6 - Feature-flagged until burn-in
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple6ExplicitOptIn:
    """Notebook mode is explicit-opt-in by construction:

      1. A notebook only runs when an operator opens it + clicks Run
         All / per-cell ▶ Run / hits the /execute endpoint.
      2. Nothing in `notebooks/<id>.spqnb` fires from cron - the
         scheduler does NOT touch the notebook tree.
      3. The default `default_notebooks/` ships exactly one notebook
         (`getting_started.spqnb`), and it's an inert walk-through -
         clicking Run All on it executes inline cells against the
         shipped test parquet, producing zero side effects on AG /
         saved-search / persistent state.
      4. `promote_to_alert_group` cells are dry-run-by-default; the
         operator must explicitly click Deploy to actually create
         an AG (slice 9 contract).

    Together: there is no path from "I installed SpeakesQuery" to
    "the notebook subsystem mutated my data" without a deliberate
    sequence of operator actions. This class pins that contract.
    """

    def test_default_notebooks_contains_only_inert_walkthrough(self):
        """Inventory: `default_notebooks/` should contain at most the
        getting_started walk-through. If a future slice ships a
        default notebook that fires LLM cells / writes to disk on
        execution, that's a feature-flag escape and warrants a
        deliberate update to this drift guard.
        """
        defaults_dir = PROJECT_ROOT / "default_notebooks"
        assert defaults_dir.is_dir()
        spqnb_files = sorted(p.name for p in defaults_dir.glob("*.spqnb"))
        assert spqnb_files == ["getting_started.spqnb"], (
            f"default_notebooks/ contents drifted: {spqnb_files}. "
            "If you're shipping a new default, ensure it's INERT "
            "(executes against test data, no LLM calls, no AG "
            "promotion, no writes to user-data). Then update this "
            "drift guard."
        )

    def test_getting_started_does_not_promote(self):
        """The shipped onboarding notebook should NOT contain a
        promote_to_alert_group cell. New operators should see the
        deploy concept introduced via docs / forward-pointer cells,
        but the walk-through itself must not deploy anything when
        clicked Run All.
        """
        import yaml
        path = (
            PROJECT_ROOT / "default_notebooks" / "getting_started.spqnb"
        )
        assert path.exists()
        rec = yaml.safe_load(path.read_text())
        cell_types = {c.get("type") for c in (rec.get("cells") or [])}
        assert "promote_to_alert_group" not in cell_types, (
            "getting_started.spqnb must not contain a promote cell - "
            "an operator following the walk-through should not "
            "accidentally create an AG."
        )

    def test_no_cron_path_touches_notebooks(self):
        """The scheduler's Phase-3 surface area is intentionally zero.
        The query engine's cron registration covers AGs + saved
        searches; notebooks are not in the loop. Pin this by
        scanning the scheduler modules for any notebook references -
        finding one would mean a future slice silently wired
        notebooks into cron.
        """
        for module_path in (
            PROJECT_ROOT / "query_engine" / "QueryEngine.py",
            PROJECT_ROOT / "alert_groups" / "scheduler.py",
            PROJECT_ROOT / "scheduled_input_engine" / "engine.py",
        ):
            if not module_path.exists():
                continue
            text = module_path.read_text()
            # No reference to notebook_store / notebook_engine /
            # NotebookStore / NotebookEngine in the scheduler path.
            for forbidden in (
                "notebook_store", "notebook_engine",
                "NotebookStore", "NotebookEngine",
                ".spqnb",
            ):
                assert forbidden not in text, (
                    f"{module_path.name} references {forbidden!r} - "
                    "Phase 3 contract requires notebooks to be "
                    "operator-opt-in only. The scheduler must not "
                    "touch the notebook tree."
                )


# ═════════════════════════════════════════════════════════════════════
# Principle 7 - Local-first remains the moat
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple7LocalFirst:
    """Notebook mode runs entirely local. CDN-loaded UI assets
    (Monaco, vega-embed) must each have a native fallback so the
    page works offline. WeasyPrint is optional (graceful 503).
    """

    def test_monaco_loader_has_textarea_fallback(self):
        ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()
        # Slice 4 Monaco lazy-load fell back to <textarea> on CDN
        # failure. Pin this contract - a regression would silently
        # break the page in a no-internet deployment.
        assert "textarea" in ui.lower()
        # Look for some indicator the cell-source editor has both
        # paths (Monaco + textarea fallback).
        assert "_nbEditors" in ui, (
            "ui.html should track per-cell editors via _nbEditors so "
            "the Monaco / textarea fallback can be unified."
        )

    def test_vega_embed_has_json_pre_fallback(self):
        ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()
        # Slice 7 chart cells fell back to a JSON-pre block when
        # Vega-Lite CDN was unreachable. Pin the fallback.
        assert "nb-chart-fallback" in ui, (
            "ui.html should preserve the slice-7 nb-chart-fallback "
            "class - it's the offline / CDN-blocked degradation path."
        )

    def test_weasyprint_is_optional(self):
        """The PDF export endpoint should return a structured 503 when
        WeasyPrint is missing rather than crashing. Local-first means
        the HTML export still works without optional deps.
        """
        server_src = (
            PROJECT_ROOT / "desktop_app" / "server.py"
        ).read_text()
        # The export endpoint should mention 503 + MissingDependency.
        assert "MissingDependency" in server_src, (
            "PDF export should surface a structured MissingDependency "
            "error when WeasyPrint isn't installed (graceful 503)."
        )

    def test_no_mandatory_cloud_dependency_in_notebook_modules(self):
        """Notebook modules should not unconditionally require any
        cloud service / API key at import time. Pin this by scanning
        for a few obvious anti-patterns.
        """
        for module_path in (
            PROJECT_ROOT / "notebook_store.py",
            PROJECT_ROOT / "notebook_engine.py",
            PROJECT_ROOT / "notebook_cache_store.py",
            PROJECT_ROOT / "notebook_to_alert_group.py",
            PROJECT_ROOT / "validation" / "NotebookValidation.py",
        ):
            text = module_path.read_text()
            # No top-level `os.environ["...API_KEY"]` reads; no
            # top-level `requests.get(...)` to a cloud URL.
            for pattern in (
                "os.environ['ANTHROPIC_API_KEY'",
                'os.environ["ANTHROPIC_API_KEY"',
                "openai.",
                "anthropic.Anthropic(",
            ):
                # A LAZY import + call inside a function is fine
                # (e.g. analyzers/llm_router.py is allowed to use
                # the SDK). Top-level access is the smell.
                lines = text.splitlines()
                for line_no, line in enumerate(lines, 1):
                    stripped = line.strip()
                    # Skip comments + docstrings (rough heuristic)
                    if stripped.startswith("#"):
                        continue
                    if pattern in line and not stripped.startswith("#"):
                        # Pattern present - must be inside a function
                        # body (indented). Top-level (column 0) is a
                        # hard fail.
                        if line.startswith(pattern) or line.startswith(
                            "import " + pattern.split(".")[0]
                        ):
                            pytest.fail(
                                f"{module_path.name}:{line_no} has "
                                f"top-level access to {pattern!r} - "
                                f"violates local-first principle."
                            )


# ═════════════════════════════════════════════════════════════════════
# Principle 8 - Money-leak / config-leak canaries
# ═════════════════════════════════════════════════════════════════════

class TestPrinciple8MoneyLeakAndConfigLeakCanaries:
    """Two canary classes apply at Phase 3 close:

      1. The slice-7 money-leak canary
         (`tests/test_llm_pipe_slice7.py::TestMoneyLeakCanary`) still
         applies - notebook cells can include `| llm` pipes, so the
         dry-run / budget-gate canary protects notebook execution
         transitively.
      2. The slice-9 config-leak canary
         (`tests/test_notebook_slice9_promote.py::TestConfigLeakCanary`)
         is the Phase 3 generalisation - pin that the notebook
         engine path NEVER mutates AG state.

    Both canary classes must remain present + reachable; CLAUDE.md
    must reference both so future authors find the pattern.
    """

    def test_money_leak_canary_class_still_present(self):
        path = PROJECT_ROOT / "tests" / "test_llm_pipe_slice7.py"
        assert path.exists()
        text = path.read_text()
        assert "class TestMoneyLeakCanary" in text
        assert "MONEY LEAK" in text

    def test_config_leak_canary_class_present(self):
        path = (
            PROJECT_ROOT / "tests" / "test_notebook_slice9_promote.py"
        )
        assert path.exists(), (
            "tests/test_notebook_slice9_promote.py is the Phase 3 "
            "config-leak canary file. Don't move it without updating "
            "this audit."
        )
        text = path.read_text()
        assert "class TestConfigLeakCanary" in text, (
            "tests/test_notebook_slice9_promote.py should still "
            "contain the TestConfigLeakCanary class. Slice 9 introduced "
            "it as the load-bearing test for the engine-path / deploy-"
            "endpoint boundary."
        )
        assert "CONFIG LEAK" in text, (
            "The canary tests should patch save_group + update_group "
            "with functions that raise AssertionError(\"CONFIG LEAK\")."
        )

    def test_claude_md_references_both_canaries(self):
        text = (PROJECT_ROOT / "CLAUDE.md").read_text()
        # Both canary references should be reachable from CLAUDE.md
        # so an operator finding a Do Not entry can locate the test.
        assert "TestMoneyLeakCanary" in text, (
            "CLAUDE.md should reference the slice-7 money-leak canary."
        )
        assert "TestConfigLeakCanary" in text, (
            "CLAUDE.md should reference the slice-9 config-leak canary "
            "(the Phase 3 generalisation pinning the engine-path / "
            "deploy-endpoint boundary for notebook cells)."
        )

    def test_promote_cell_engine_handler_does_not_call_save_group_directly(self):
        """Source-scan: the engine handler `_execute_promote_to_alert_group`
        should ONLY call `build_promote_preview` (read-only). It must
        NOT directly invoke `promote_cell_to_ag` or anything that
        could mutate AG state. The runtime canary verifies the
        invariant via patching; this drift guard verifies it via
        source inspection - two layers of defence.

        We scan for CALL SITES (e.g. ``.save_group(``) rather than
        bare names, so a docstring mentioning the boundary doesn't
        false-positive.
        """
        engine_src = (
            PROJECT_ROOT / "notebook_engine.py"
        ).read_text()
        # The handler exists
        assert "_execute_promote_to_alert_group" in engine_src
        # Locate the handler body (rough heuristic: between def + next def)
        handler_match = re.search(
            r"def _execute_promote_to_alert_group\(.*?\n((?:[^\n]*\n)+?)(?=\n    def |\nclass )",
            engine_src,
            re.DOTALL,
        )
        assert handler_match is not None, (
            "Could not locate _execute_promote_to_alert_group body. "
            "Update this drift guard if the function signature changed."
        )
        body = handler_match.group(1)
        # Strip docstrings + comments so a "we never call X" doc line
        # doesn't false-positive. Then scan for call sites only.
        body_no_docs = re.sub(r'""".*?"""', "", body, flags=re.DOTALL)
        body_no_docs = re.sub(r"^\s*#.*$", "", body_no_docs, flags=re.MULTILINE)
        # Forbidden CALL SITES in the engine handler body. Each must
        # match an actual invocation (`.method(` or `function(`), not
        # a bare token in a docstring or comment.
        for forbidden_call in (
            "promote_cell_to_ag(",       # the mutating function
            ".save_group(",              # AG store mutating method
            ".update_group(",            # AG store mutating method
            "AlertGroupStore(",          # direct store instantiation
        ):
            assert forbidden_call not in body_no_docs, (
                f"_execute_promote_to_alert_group body invokes "
                f"{forbidden_call!r}. The engine handler must be "
                "DRY-RUN ONLY - no direct AG mutation. The deploy "
                "goes through POST /api/notebooks/<id>/promote/<cell> "
                "which calls promote_cell_to_ag explicitly."
            )


# ═════════════════════════════════════════════════════════════════════
# Phase 3 demoable artifact - informational
# ═════════════════════════════════════════════════════════════════════

class TestPhase3DemoableArtifact:
    """Per ROADMAP: Phase 3 closes with a demoable artifact - a
    notebook that rebuilds OEB itself from scratch - 10 feeders +
    the brief - and ships it live with one cell.

    All seven cell types are reachable; promote_to_alert_group is
    the deploy primitive. This class doesn't run the OEB pipeline
    (that requires real feeder data + Claude API); instead it
    verifies every primitive is reachable from the validator +
    engine surface.
    """

    def test_all_seven_cell_types_reachable_via_validator(self):
        from validation.NotebookValidation import (
            NotebookValidation, ALLOWED_CELL_TYPES,
        )
        # Each cell type validates as a single-cell notebook (with the
        # promote-cell case getting its YAML scaffold so the cross-cell
        # check passes).
        for cell_type in sorted(ALLOWED_CELL_TYPES):
            if cell_type == "promote_to_alert_group":
                # Needs sibling prompt cell + valid YAML metadata
                cells = [
                    {"id": "prompt", "type": "pipe", "source": "X", "metadata": {}},
                    {
                        "id": "deploy",
                        "type": "promote_to_alert_group",
                        "source": (
                            'name: demo_ag\n'
                            'schedule: "0 12 * * mon-fri"\n'
                            'email_address: ops@example.com\n'
                            'search_names: [demo_feeder]\n'
                            'prompt_cell: prompt\n'
                        ),
                        "metadata": {},
                    },
                ]
            else:
                cells = [{
                    "id": "c1", "type": cell_type, "source": "x",
                    "metadata": {},
                }]
            out = NotebookValidation.validate_record({
                "id": f"demo_{cell_type}",
                "cells": cells,
            })
            assert out["cells"], (
                f"validator dropped a {cell_type!r} cell - Phase 3 "
                "demoable artifact requires all seven types reachable."
            )

    def test_promote_cell_engine_dispatch_present(self):
        engine_src = (PROJECT_ROOT / "notebook_engine.py").read_text()
        assert "_execute_promote_to_alert_group" in engine_src, (
            "Engine should dispatch promote_to_alert_group cells. The "
            "headliner cell is unreachable without it."
        )

    def test_promote_endpoints_registered(self):
        """The three Phase-3-slice-9 endpoints should be wired into
        the Flask app. Drift guard: scan the server source for the
        route decorators.
        """
        server_src = (PROJECT_ROOT / "desktop_app" / "server.py").read_text()
        for route in (
            "/api/notebooks/<notebook_id>/promote/<cell_id>/preview",
            "/api/notebooks/<notebook_id>/promote/<cell_id>",
            "/api/alert-groups/<ag_name>/as-notebook",
        ):
            assert route in server_src, (
                f"server.py is missing route {route!r}. The Phase 3 "
                "headliner deploy flow needs all three endpoints "
                "registered."
            )
