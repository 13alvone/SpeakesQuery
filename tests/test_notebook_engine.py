"""
Tests for notebook_engine.py - Phase 3 / Bet 4 slice 2.

The cell-engine layer takes a validated notebook record (slice 1) and
runs every cell top-to-bottom against a shared namespace. Slice 2 has
NO reactive cache, NO persistence write-back, NO UI - just execution
semantics with cell-type dispatch.

Test layout:
  * TestSpqlCell - full SPQL query through process_query_with_diagnostics
  * TestPipeCell - same execution as spql; type tag preserved
  * TestPythonCell - full Python via exec(); IPython-style last-expr
    capture; stdout/stderr capture; namespace mutation
  * TestPythonFullPrivilege - drift guard: NO RestrictedPython invoked,
    full builtins available, dangerous-import paths reachable (admin
    tool by design)
  * TestMarkdownCell - passthrough; no namespace exposure
  * TestChartCell - passthrough; no namespace exposure
  * TestParamCell - YAML-parse spec; expose default at namespace[id]
  * TestCrossCellNamespace - cell_2 sees cell_1's output; Python cell
    can compute on prior; assigned-name visibility downstream
  * TestErrorPropagation - error in one cell doesn't stop run; downstream
    NameError when referencing missing name
  * TestNotebookRunResult - counts + timing + serialisability
  * TestUnknownCellType - defense in depth (validation should catch but
    if a malformed YAML slips through the engine fails clean)
"""

from __future__ import annotations

import importlib
import sys

import pandas as pd
import pytest

import notebook_engine
from notebook_engine import (
    CellResult, NotebookEngine, NotebookRunResult,
    STATUS_ERROR, STATUS_SUCCESS,
)


# ── Shared fixtures ─────────────────────────────────────────────────

@pytest.fixture
def engine():
    return NotebookEngine()


def _cell(cell_id, cell_type, source, **extra):
    """Build a cell dict matching the slice-1 schema shape."""
    rec = {
        "id": cell_id,
        "type": cell_type,
        "source": source,
        "metadata": {},
    }
    rec.update(extra)
    return rec


def _notebook(notebook_id, cells, **extra):
    rec = {
        "id": notebook_id,
        "schema_version": 1,
        "name": "",
        "description": "",
        "default_max_cost_usd": 0.0,
        "cells": cells,
    }
    rec.update(extra)
    return rec


# ═════════════════════════════════════════════════════════════════════
# 1. spql cell
# ═════════════════════════════════════════════════════════════════════

class TestSpqlCell:
    def test_spql_query_against_test_parquet_returns_dataframe(self, engine):
        # Use the deterministic test parquet shipped with the project.
        cell = _cell(
            "results", "spql",
            'index="indexes/default_test/output_parquets/test0.parquet"',
        )
        namespace: dict = {}
        result = engine.execute_cell(cell, namespace)
        assert result.status == STATUS_SUCCESS
        assert isinstance(result.output, pd.DataFrame)
        assert len(result.output) > 0
        assert "DataFrame" in result.output_repr
        # Cell binding exposed in namespace
        assert "results" in namespace
        assert namespace["results"] is result.output
        assert result.exposed_names == ["results"]

    def test_spql_invalid_query_returns_error(self, engine):
        cell = _cell("bad", "spql", "this is not a valid query")
        namespace: dict = {}
        result = engine.execute_cell(cell, namespace)
        assert result.status == STATUS_ERROR
        assert result.error_class != ""
        assert result.error_message != ""
        # Bad cell does NOT pollute the namespace
        assert "bad" not in namespace

    def test_spql_runtime_ms_recorded(self, engine):
        cell = _cell(
            "results", "spql",
            'index="indexes/default_test/output_parquets/test0.parquet"',
        )
        result = engine.execute_cell(cell, {})
        assert result.runtime_ms >= 0  # nonzero on real disk; >=0 is the safe assertion

    def test_spql_executed_at_is_tz_aware(self, engine):
        cell = _cell(
            "x", "spql",
            'index="indexes/default_test/output_parquets/test0.parquet"',
        )
        result = engine.execute_cell(cell, {})
        # ISO 8601 with offset (e.g. "+00:00", "-07:00") - required so
        # JS new Date(iso) parses unambiguously. Same convention as
        # AlertGroupStore / SavedSearchStore (caught 2026-04-27).
        assert result.executed_at != ""
        assert (
            "+" in result.executed_at[10:]
            or "Z" in result.executed_at
            or "-" in result.executed_at[10:]
        )


# ═════════════════════════════════════════════════════════════════════
# 2. pipe cell - same execution as spql; type preserved
# ═════════════════════════════════════════════════════════════════════

class TestPipeCell:
    def test_pipe_routes_through_same_path_as_spql(self, engine):
        cell = _cell(
            "rated", "pipe",
            'index="indexes/default_test/output_parquets/test0.parquet"',
        )
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.cell_type == "pipe"  # type tag preserved
        assert isinstance(result.output, pd.DataFrame)


# ═════════════════════════════════════════════════════════════════════
# 3. python cell - full Python via exec()
# ═════════════════════════════════════════════════════════════════════

class TestPythonCell:
    def test_simple_assignment(self, engine):
        cell = _cell("compute", "python", "x = 42")
        ns: dict = {}
        result = engine.execute_cell(cell, ns)
        assert result.status == STATUS_SUCCESS
        assert ns["x"] == 42
        assert "x" in result.exposed_names

    def test_last_expression_captured_as_output(self, engine):
        # IPython-style: trailing bare expression becomes the cell's value.
        cell = _cell("compute", "python", "x = 5\nx * 2")
        ns: dict = {}
        result = engine.execute_cell(cell, ns)
        assert result.status == STATUS_SUCCESS
        assert result.output == 10
        assert result.output_repr == "10"
        assert ns["x"] == 5

    def test_no_terminal_expression_means_no_output(self, engine):
        cell = _cell("setup", "python", "x = 1\ny = 2")
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output is None
        # output_repr summarises the new bindings instead
        assert "defined: x, y" in result.output_repr

    def test_stdout_captured(self, engine):
        cell = _cell("noisy", "python", 'print("hello")\nprint("world")')
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert "hello" in result.stdout
        assert "world" in result.stdout

    def test_stderr_captured(self, engine):
        cell = _cell("warn", "python", (
            "import sys\n"
            'sys.stderr.write("oops\\n")'
        ))
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert "oops" in result.stderr

    def test_runtime_error_is_caught(self, engine):
        cell = _cell("bad", "python", "1 / 0")
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_ERROR
        assert result.error_class == "ZeroDivisionError"
        assert "division by zero" in result.error_message.lower()

    def test_syntax_error_is_caught(self, engine):
        cell = _cell("bad", "python", "this is not = valid python")
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_ERROR
        assert result.error_class == "SyntaxError"

    def test_namespace_mutates_shared_state(self, engine):
        ns: dict = {"prior_value": 100}
        cell = _cell(
            "compute", "python",
            "doubled = prior_value * 2",
        )
        engine.execute_cell(cell, ns)
        assert ns["doubled"] == 200
        assert ns["prior_value"] == 100  # unchanged

    def test_python_cell_does_not_auto_assign_cell_id(self, engine):
        # If the cell doesn't explicitly assign cell_id, that name is
        # NOT auto-bound. (Spql cells do auto-bind; python is different.)
        cell = _cell("computed", "python", "y = 1\nz = 2")
        ns: dict = {}
        engine.execute_cell(cell, ns)
        assert "computed" not in ns
        assert "y" in ns and "z" in ns

    def test_python_cell_can_explicitly_assign_cell_id(self, engine):
        cell = _cell("computed", "python", "computed = 99")
        ns: dict = {}
        engine.execute_cell(cell, ns)
        assert ns["computed"] == 99

    def test_repr_long_value_truncated(self, engine):
        # 5000-char string repr should land at our 1000-char cap.
        cell = _cell("big", "python", "'x' * 5000")
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert "truncated" in result.output_repr or len(result.output_repr) <= 1100


# ═════════════════════════════════════════════════════════════════════
# 4. Python full-privilege drift guards
# ═════════════════════════════════════════════════════════════════════

class TestPythonFullPrivilege:
    """Per user direction 2026-05-08 (`feedback_no_restricted_python_outside_ingestion`):
    the notebook's `python` cell type is FULL PYTHON, not RestrictedPython.
    These tests pin that contract so a future contributor can't silently
    re-introduce sandboxing.
    """

    def test_engine_does_not_import_restrictedpython(self):
        """The engine module must not import RestrictedPython, even
        lazily. Fresh-import + check sys.modules + scan source for the
        forbidden import.
        """
        # 1. Reload the engine to ensure no stale state.
        importlib.reload(notebook_engine)
        # 2. After import, RestrictedPython should not be in sys.modules
        # purely because of our engine. (Other tests may have imported it,
        # so we can't assert absence globally - but we CAN check the
        # source for the import statement.)
        import inspect
        src = inspect.getsource(notebook_engine)
        for forbidden in (
            "from RestrictedPython", "import RestrictedPython",
            "from RestrictedPython.Guards", "compile_restricted",
            "safe_builtins",
        ):
            assert forbidden not in src, (
                f"notebook_engine.py contains {forbidden!r} - but per "
                "user direction 2026-05-08, RestrictedPython is reserved "
                "for ingestion-script use only. Notebook python cells "
                "MUST use full Python via exec()."
            )

    def test_dunder_builtins_available(self, engine):
        # Full builtins - operator can call anything they could in a
        # regular Python interpreter on this host.
        cell = _cell("test", "python", (
            "result = (\n"
            "    isinstance(42, int)\n"
            "    and len([1, 2, 3]) == 3\n"
            "    and dict(a=1)['a'] == 1\n"
            ")\n"
            "result"
        ))
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output is True

    def test_underscore_prefix_names_allowed(self, engine):
        # RestrictedPython forbids ``_``-prefix names; full Python allows
        # them. Drift guard.
        cell = _cell("test", "python", (
            "_secret = 'admin only'\n"
            "_secret"
        ))
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output == "admin only"

    def test_tuple_unpacking_in_for_loop_allowed(self, engine):
        # RestrictedPython forbids ``for x, y in pairs``; full Python
        # allows it. Drift guard.
        cell = _cell("test", "python", (
            "pairs = [(1, 2), (3, 4)]\n"
            "totals = []\n"
            "for x, y in pairs:\n"
            "    totals.append(x + y)\n"
            "totals"
        ))
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output == [3, 7]

    def test_dangerous_imports_reachable(self, engine):
        # The point of "admin tool, no sandbox" is that the operator
        # CAN reach modules that RestrictedPython blocks. We don't
        # actually call os.system here - just verify the import works.
        cell = _cell("test", "python", (
            "import os\n"
            "import sys\n"
            "import subprocess\n"
            "type(os).__name__ == 'module' and "
            "type(sys).__name__ == 'module' and "
            "type(subprocess).__name__ == 'module'"
        ))
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output is True


# ═════════════════════════════════════════════════════════════════════
# 5. markdown + chart passthrough
# ═════════════════════════════════════════════════════════════════════

class TestMarkdownCell:
    def test_source_preserved_as_output(self, engine):
        body = "## Summary\n\nThis is some markdown."
        result = engine.execute_cell(
            _cell("notes", "markdown", body), {},
        )
        assert result.status == STATUS_SUCCESS
        assert result.output == body
        assert "markdown" in result.output_repr

    def test_does_not_pollute_namespace(self, engine):
        ns: dict = {}
        engine.execute_cell(
            _cell("notes", "markdown", "## hi"), ns,
        )
        # Markdown is documentation, not data - no namespace exposure
        assert "notes" not in ns


class TestChartCell:
    def test_source_preserved_as_output(self, engine):
        spec = '{"mark": "bar", "encoding": {"x": "score"}}'
        result = engine.execute_cell(
            _cell("plot", "chart", spec), {},
        )
        assert result.status == STATUS_SUCCESS
        assert result.output == spec
        assert "chart" in result.output_repr

    def test_does_not_pollute_namespace(self, engine):
        ns: dict = {}
        engine.execute_cell(_cell("plot", "chart", "{}"), ns)
        assert "plot" not in ns


# ═════════════════════════════════════════════════════════════════════
# 6. param cell
# ═════════════════════════════════════════════════════════════════════

class TestParamCell:
    def test_default_value_exposed(self, engine):
        spec = (
            "type: select\n"
            "options: [aapl, msft, goog]\n"
            "default: aapl\n"
        )
        ns: dict = {}
        result = engine.execute_cell(_cell("ticker", "param", spec), ns)
        assert result.status == STATUS_SUCCESS
        assert result.output == "aapl"
        assert ns["ticker"] == "aapl"
        assert "ticker" in result.exposed_names

    def test_no_default_resolves_to_none(self, engine):
        spec = "type: text\nlabel: 'Search query'\n"
        ns: dict = {}
        result = engine.execute_cell(_cell("query", "param", spec), ns)
        assert result.status == STATUS_SUCCESS
        assert result.output is None
        assert ns["query"] is None

    def test_invalid_yaml_returns_error(self, engine):
        spec = "this: is\n  not: : valid"
        result = engine.execute_cell(_cell("bad", "param", spec), {})
        assert result.status == STATUS_ERROR
        assert result.error_class == "YAMLParseError"

    def test_non_mapping_yaml_returns_error(self, engine):
        spec = "- not a mapping"  # YAML sequence
        result = engine.execute_cell(_cell("bad", "param", spec), {})
        assert result.status == STATUS_ERROR
        assert result.error_class == "InvalidParamSpec"


# ═════════════════════════════════════════════════════════════════════
# 7. Cross-cell namespace sharing
# ═════════════════════════════════════════════════════════════════════

class TestCrossCellNamespace:
    def test_python_cell_sees_prior_spql_dataframe(self, engine):
        cells = [
            _cell(
                "rows", "spql",
                'index="indexes/default_test/output_parquets/test0.parquet"',
            ),
            _cell(
                "row_count", "python",
                "len(rows)",
            ),
        ]
        nb = _notebook("nb1", cells)
        result = engine.execute_notebook(nb)
        assert result.error_count == 0
        assert result.cells[1].output > 0  # actual integer count

    def test_python_assignment_visible_to_downstream_cells(self, engine):
        cells = [
            _cell("setup", "python", "candidates = [1, 2, 3, 4, 5]"),
            _cell("count", "python", "len(candidates)"),
        ]
        nb = _notebook("nb_chain", cells)
        result = engine.execute_notebook(nb)
        assert result.error_count == 0
        assert result.cells[1].output == 5

    def test_param_value_consumable_in_python_cell(self, engine):
        cells = [
            _cell("ticker", "param", "default: AAPL\n"),
            _cell("greet", "python", "f\"Looking at {ticker}\""),
        ]
        nb = _notebook("nb_param", cells)
        result = engine.execute_notebook(nb)
        assert result.error_count == 0
        assert result.cells[1].output == "Looking at AAPL"

    def test_caller_supplied_namespace_seed(self, engine):
        # The execute_notebook entry takes an optional namespace=
        # so callers can seed initial values (used by slice-3 reactive
        # cache to thread state across runs).
        cells = [
            _cell("greet", "python", "f\"hello {who}\""),
        ]
        nb = _notebook("nb_seeded", cells)
        result = engine.execute_notebook(nb, namespace={"who": "world"})
        assert result.error_count == 0
        assert result.cells[0].output == "hello world"


# ═════════════════════════════════════════════════════════════════════
# 8. Error propagation across cells
# ═════════════════════════════════════════════════════════════════════

class TestErrorPropagation:
    def test_cell_failure_does_not_stop_subsequent_cells(self, engine):
        cells = [
            _cell("ok1", "python", "x = 1"),
            _cell("boom", "python", "1 / 0"),
            _cell("ok2", "python", "y = 2"),
        ]
        nb = _notebook("nb_err", cells)
        result = engine.execute_notebook(nb)
        # All three cells ran (one errored, two succeeded)
        assert len(result.cells) == 3
        assert result.success_count == 2
        assert result.error_count == 1
        assert result.cells[0].status == STATUS_SUCCESS
        assert result.cells[1].status == STATUS_ERROR
        assert result.cells[2].status == STATUS_SUCCESS

    def test_downstream_name_error_when_upstream_fails(self, engine):
        cells = [
            _cell("upstream", "python", "1 / 0"),  # fails
            _cell("downstream", "python", "upstream + 1"),  # fails - upstream undefined
        ]
        nb = _notebook("nb_chain_err", cells)
        result = engine.execute_notebook(nb)
        assert result.cells[0].status == STATUS_ERROR
        assert result.cells[1].status == STATUS_ERROR
        assert result.cells[1].error_class == "NameError"

    def test_failed_spql_does_not_pollute_namespace(self, engine):
        cells = [
            _cell("bad_query", "spql", "garbage_not_spql"),
            _cell("check", "python", "'bad_query' in dir() or 'bad_query' in globals()"),
        ]
        nb = _notebook("nb_bad_spql", cells)
        result = engine.execute_notebook(nb)
        # First cell failed; second cell saw no `bad_query` binding
        assert result.cells[0].status == STATUS_ERROR
        assert result.cells[1].status == STATUS_SUCCESS
        assert result.cells[1].output is False


# ═════════════════════════════════════════════════════════════════════
# 9. Notebook-run aggregation + serialisation
# ═════════════════════════════════════════════════════════════════════

class TestNotebookRunResult:
    def test_counts_aggregate_correctly(self, engine):
        cells = [
            _cell("a", "python", "x = 1"),
            _cell("b", "python", "1 / 0"),
            _cell("c", "python", "y = 2"),
        ]
        result = engine.execute_notebook(_notebook("nb_x", cells))
        assert result.success_count == 2
        assert result.error_count == 1
        assert result.skipped_count == 0
        assert len(result.cells) == 3

    def test_started_finished_total_runtime(self, engine):
        cells = [_cell("a", "python", "x = 1")]
        result = engine.execute_notebook(_notebook("nb_y", cells))
        assert result.started_at != ""
        assert result.finished_at != ""
        assert result.total_runtime_ms >= 0

    def test_to_dict_serialisable(self, engine):
        cells = [
            _cell("a", "python", "x = 1"),
            _cell("b", "markdown", "## hi"),
        ]
        result = engine.execute_notebook(_notebook("nb_ser", cells))
        d = result.to_dict()
        # Must round-trip through json.dumps - every value JSON-safe
        import json
        json.dumps(d)
        assert d["notebook_id"] == "nb_ser"
        assert len(d["cells"]) == 2
        # Each cell's to_dict drops the heavy `output` field
        assert "output_repr" in d["cells"][0]
        assert "output" not in d["cells"][0]

    def test_empty_notebook_runs_cleanly(self, engine):
        result = engine.execute_notebook(_notebook("empty", []))
        assert result.success_count == 0
        assert result.error_count == 0
        assert result.cells == []

    def test_namespace_default_is_fresh_dict_per_call(self, engine):
        # Two consecutive runs with no caller-supplied namespace each
        # start fresh. State doesn't leak across runs.
        cells = [_cell("a", "python", "x = x + 1 if 'x' in dir() else 1\nx")]
        nb = _notebook("nb_isolation", cells)
        r1 = engine.execute_notebook(nb)
        r2 = engine.execute_notebook(nb)
        assert r1.cells[0].output == 1
        assert r2.cells[0].output == 1


# ═════════════════════════════════════════════════════════════════════
# 10. Unknown cell type - defense in depth
# ═════════════════════════════════════════════════════════════════════

class TestUnknownCellType:
    def test_unknown_type_returns_error_not_crash(self, engine):
        # Schema validation should block this before reaching the
        # engine, but if a malformed YAML slips through we want a
        # clean error rather than an AttributeError or KeyError.
        cell = {"id": "weird", "type": "rust", "source": "fn main() {}"}
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_ERROR
        assert result.error_class == "UnknownCellType"
