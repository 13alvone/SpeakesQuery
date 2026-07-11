"""
Tests for Phase 3 / Bet 4 slice 6 - manual-test polish.

Slice-6 ships three UX-driven additions from slice-5 manual feedback:
  * stop_at_cell_id on execute_notebook (per-cell Run button)
  * Python cells with DataFrame output build output_preview (so they
    render via the slice-5 table renderer)
  * /api/notebooks/<id>/execute accepts stop_at_cell_id with structured
    400 on unknown cell ids (UI: cell-type dropdown changes the cell's
    type post-creation - verified manually + via shape pin in this file)

Test layout:
  * TestStopAtCellId - engine slices the cell list correctly; cells
    past the target are not in the result; LookupError on unknown id
  * TestPythonDataFramePreview - Python cells whose last expression
    or sole binding is a DataFrame produce output_preview
  * TestExecuteApiStopAtCellId - API endpoint accepts the new field;
    structured 400 on unknown cell id with valid_cell_ids list
  * TestCellTypeChangeContract - UI + schema drift guards: the
    cell-type dropdown surface in ui.html is wired; the closed enum
    matches the slice-1 schema
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

import notebook_cache_store
import notebook_engine
import notebook_store
from notebook_engine import NotebookEngine, STATUS_SUCCESS


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    notebook_cache_store.reset_for_tests()
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_DB_PATH",
        tmp_path / "notebook_cache.sqlite",
    )
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_PAYLOAD_DIR",
        tmp_path / "notebook_cache",
    )
    yield notebook_cache_store.NotebookCacheStore()
    notebook_cache_store.reset_for_tests()


@pytest.fixture
def engine():
    return NotebookEngine()


def _cell(cell_id, cell_type, source):
    return {
        "id": cell_id, "type": cell_type, "source": source, "metadata": {},
    }


def _notebook(notebook_id, cells):
    return {
        "id": notebook_id, "schema_version": 1,
        "name": "", "description": "", "default_max_cost_usd": 0.0,
        "cells": cells,
    }


# ═══════════════════════════════════════════════════════════════════
# 1. Engine: stop_at_cell_id slices the cell list
# ═══════════════════════════════════════════════════════════════════

class TestStopAtCellId:
    def test_runs_only_cells_up_to_target(self, engine):
        cells = [
            _cell("a", "python", "x = 1"),
            _cell("b", "python", "y = x + 1"),
            _cell("c", "python", "z = y + 1"),
            _cell("d", "python", "w = z + 1"),
        ]
        nb = _notebook("nb", cells)
        result = engine.execute_notebook(nb, stop_at_cell_id="b")
        # Only cells a + b are in the result; c + d not run
        assert len(result.cells) == 2
        cell_ids = [c.cell_id for c in result.cells]
        assert cell_ids == ["a", "b"]

    def test_stop_at_first_cell_runs_only_that_cell(self, engine):
        cells = [
            _cell("only", "python", "x = 5"),
            _cell("downstream", "python", "1 / 0"),  # would error
        ]
        nb = _notebook("nb", cells)
        result = engine.execute_notebook(nb, stop_at_cell_id="only")
        assert len(result.cells) == 1
        assert result.cells[0].status == STATUS_SUCCESS
        # Downstream errored cell never ran
        assert result.error_count == 0

    def test_stop_at_last_cell_runs_everything(self, engine):
        cells = [
            _cell("a", "python", "x = 1"),
            _cell("b", "python", "y = 2"),
        ]
        nb = _notebook("nb", cells)
        result = engine.execute_notebook(nb, stop_at_cell_id="b")
        assert len(result.cells) == 2

    def test_stop_at_unknown_cell_id_raises_lookup_error(self, engine):
        cells = [_cell("a", "python", "x = 1")]
        nb = _notebook("nb", cells)
        with pytest.raises(LookupError, match="not found"):
            engine.execute_notebook(nb, stop_at_cell_id="ghost")

    def test_stop_at_none_runs_all_cells(self, engine):
        # Backward-compat: omitting the kwarg behaves exactly as before.
        cells = [
            _cell("a", "python", "x = 1"),
            _cell("b", "python", "y = 2"),
            _cell("c", "python", "z = 3"),
        ]
        nb = _notebook("nb", cells)
        r1 = engine.execute_notebook(nb)
        r2 = engine.execute_notebook(nb, stop_at_cell_id=None)
        assert len(r1.cells) == 3
        assert len(r2.cells) == 3
        for a, b in zip(r1.cells, r2.cells):
            assert a.cell_id == b.cell_id

    def test_per_cell_run_uses_cache_for_upstream(
        self, engine, isolated_cache,
    ):
        cells = [
            _cell("a", "python", "a_val = 1"),
            _cell("b", "python", "b_val = a_val + 1"),
            _cell("c", "python", "c_val = b_val + 1"),
        ]
        nb = _notebook("nb_cache", cells)
        # First full run populates cache for a, b, c.
        engine.execute_notebook(nb, cache_store=isolated_cache)
        # Per-cell Run on b: a + b in result, both cache hits, c not run.
        result = engine.execute_notebook(
            nb, cache_store=isolated_cache, stop_at_cell_id="b",
        )
        assert len(result.cells) == 2
        assert result.cells[0].cache_hit is True   # cell a cached
        assert result.cells[1].cache_hit is True   # cell b cached
        assert result.cache_hits == 2

    def test_per_cell_run_after_edit_re_executes_target(
        self, engine, isolated_cache,
    ):
        cells_v1 = [
            _cell("a", "python", "a_val = 1"),
            _cell("b", "python", "b_val = a_val + 1"),
        ]
        engine.execute_notebook(
            _notebook("nb", cells_v1), cache_store=isolated_cache,
        )
        # Edit cell b's source; per-cell Run on b should re-execute b
        # while a stays cached.
        cells_v2 = [
            _cell("a", "python", "a_val = 1"),  # unchanged
            _cell("b", "python", "b_val = a_val + 999"),  # CHANGED
        ]
        result = engine.execute_notebook(
            _notebook("nb", cells_v2), cache_store=isolated_cache,
            stop_at_cell_id="b",
        )
        assert result.cells[0].cache_hit is True   # cell a
        assert result.cells[1].cache_hit is False  # cell b re-ran


# ═══════════════════════════════════════════════════════════════════
# 2. Python cells produce output_preview when last-expr is DataFrame
# ═══════════════════════════════════════════════════════════════════

class TestPythonDataFramePreview:
    def test_terminal_dataframe_expression_builds_preview(self, engine):
        cell = _cell(
            "py",
            "python",
            "import pandas as pd\n"
            "pd.DataFrame({'a': [1, 2, 3], 'b': ['x', 'y', 'z']})\n",
        )
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output_preview is not None
        assert result.output_preview["kind"] == "dataframe"
        assert result.output_preview["total_rows"] == 3
        assert result.output_preview["total_cols"] == 2
        # output_repr also shifts to DataFrame summary instead of repr
        assert "DataFrame" in result.output_repr
        assert "rows" in result.output_repr

    def test_sole_binding_dataframe_builds_preview(self, engine):
        # Cell has no terminal expression but assigns a single name to
        # a DataFrame - preview comes from the binding so the operator
        # sees the data without typing the name on a final line.
        cell = _cell(
            "py",
            "python",
            "import pandas as pd\n"
            "df = pd.DataFrame({'a': [1, 2]})\n",
        )
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output_preview is not None
        assert result.output_preview["total_rows"] == 2

    def test_non_dataframe_output_no_preview(self, engine):
        cell = _cell("py", "python", "x = 5\nx * 2\n")
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output_preview is None
        assert result.output == 10

    def test_multiple_dataframe_bindings_picks_last(self, engine):
        # Two DataFrame bindings + no terminal expression → preview
        # is the LAST DataFrame (Jupyter convention: most-recent
        # assignment is the "result"). Module bindings (``pd``) skipped.
        cell = _cell(
            "py",
            "python",
            "import pandas as pd\n"
            "df1 = pd.DataFrame({'a': [1, 2, 3]})\n"
            "df2 = pd.DataFrame({'b': [10, 20]})\n",
        )
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output_preview is not None
        # Last DataFrame is df2 with column 'b' and 2 rows
        assert result.output_preview["total_rows"] == 2
        assert result.output_preview["columns"][0]["name"] == "b"

    def test_terminal_dataframe_overrides_repr_truncation(self, engine):
        # Without slice 6, a wide DataFrame's repr() would crowd
        # output_repr. With slice 6, output_repr is the schema summary.
        cell = _cell(
            "py",
            "python",
            "import pandas as pd\n"
            "pd.DataFrame({c: list(range(20)) for c in 'abcdefghij'})\n",
        )
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        # 20 rows × 10 cols
        assert "20 rows" in result.output_repr
        assert "10 cols" in result.output_repr


# ═══════════════════════════════════════════════════════════════════
# 3. /api/notebooks/<id>/execute carries stop_at_cell_id
# ═══════════════════════════════════════════════════════════════════

class TestExecuteApiStopAtCellId:
    @pytest.fixture
    def isolated_stores(self, tmp_path, monkeypatch):
        notebook_store.reset_for_tests()
        notebook_cache_store.reset_for_tests()
        nb_dir = tmp_path / "notebooks"
        df_dir = tmp_path / "default_notebooks"
        nb_dir.mkdir()
        df_dir.mkdir()
        monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
        monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", df_dir)
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
    def client(self, isolated_stores):
        from desktop_app.server import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_stop_at_cell_id_runs_only_through_target(self, client):
        nb = {
            "id": "stp", "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [
                _cell("a", "python", "x = 1"),
                _cell("b", "python", "y = 2"),
                _cell("c", "python", "z = 3"),
            ],
        }
        client.post("/api/notebooks", json=nb)
        body = client.post(
            "/api/notebooks/stp/execute",
            json={"stop_at_cell_id": "b"},
        ).get_json()
        assert body["status"] == "success"
        assert len(body["result"]["cells"]) == 2
        assert [c["cell_id"] for c in body["result"]["cells"]] == ["a", "b"]

    def test_unknown_cell_id_returns_structured_400(self, client):
        nb = {
            "id": "stp_err", "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [_cell("a", "python", "x = 1")],
        }
        client.post("/api/notebooks", json=nb)
        resp = client.post(
            "/api/notebooks/stp_err/execute",
            json={"stop_at_cell_id": "ghost"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        # Dual-audience structured error per slice 5
        assert body["status"] == "error"
        assert body["error_class"] == "UnknownCellId"
        assert body["stop_at_cell_id"] == "ghost"
        assert body["valid_cell_ids"] == ["a"]

    def test_non_string_stop_at_cell_id_returns_400(self, client):
        nb = {
            "id": "stp_t", "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [_cell("a", "python", "x = 1")],
        }
        client.post("/api/notebooks", json=nb)
        resp = client.post(
            "/api/notebooks/stp_t/execute",
            json={"stop_at_cell_id": 42},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error_class"] == "InvalidInput"
        assert body["expected"] == "str"

    def test_python_dataframe_preview_in_api_response(self, client):
        nb = {
            "id": "pydf", "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [
                _cell(
                    "py",
                    "python",
                    "import pandas as pd\n"
                    "pd.DataFrame({'a': [10, 20, 30]})\n",
                ),
            ],
        }
        client.post("/api/notebooks", json=nb)
        body = client.post("/api/notebooks/pydf/execute", json={}).get_json()
        cell_result = body["result"]["cells"][0]
        # AI-side contract: structured preview for DataFrame outputs
        # regardless of cell type.
        assert cell_result["output_preview"] is not None
        assert cell_result["output_preview"]["kind"] == "dataframe"
        assert cell_result["output_preview"]["total_rows"] == 3


# ═══════════════════════════════════════════════════════════════════
# 4. UI surface drift guards (cell-type dropdown + per-cell Run)
# ═══════════════════════════════════════════════════════════════════

class TestCellTypeChangeContract:
    def _ui(self) -> str:
        return (
            PROJECT_ROOT / "desktop_app" / "ui.html"
        ).read_text(encoding="utf-8")

    def test_ui_renders_cell_type_dropdown(self):
        ui = self._ui()
        # Dropdown class + change handler + every closed-enum cell type
        assert "nb-cell-type-select" in ui
        for cell_type in ("spql", "pipe", "python", "markdown", "chart", "param"):
            # Each type appears as a value in the select options
            assert (
                f"value=\"{cell_type}\"" in ui
                or f"'{cell_type}'" in ui
            ), f"cell type {cell_type!r} not surfaced in UI"

    def test_ui_renders_per_cell_run_button(self):
        ui = self._ui()
        # Per-cell Run button class + handler hook
        assert "nb-cell-run" in ui
        assert "runCellsUntil" in ui

    def test_nb_cell_types_constant_matches_schema_enum(self):
        # The closed enum on the JS side must match the slice-1
        # schema's ALLOWED_CELL_TYPES. If a future slice adds a cell
        # type, both surfaces must update.
        ui = self._ui()
        # Pull the JS const declaration and compare against the schema
        from validation.NotebookValidation import ALLOWED_CELL_TYPES
        m = re.search(
            r"const NB_CELL_TYPES = \[([^\]]+)\];", ui,
        )
        assert m is not None, "NB_CELL_TYPES JS constant missing"
        # Extract quoted strings from the array literal
        js_types = set(re.findall(r"'([^']+)'", m.group(1)))
        assert js_types == ALLOWED_CELL_TYPES, (
            f"NB_CELL_TYPES (JS) {js_types} != ALLOWED_CELL_TYPES (Python) "
            f"{ALLOWED_CELL_TYPES}. Both surfaces must agree on the enum."
        )


# ═══════════════════════════════════════════════════════════════════
# 5. Python DataFrame preview persists through cache
# ═══════════════════════════════════════════════════════════════════

class TestPythonDataFrameCacheRoundTrip:
    def test_preview_survives_cache_round_trip(
        self, engine, isolated_cache,
    ):
        cells = [_cell(
            "py",
            "python",
            "import pandas as pd\n"
            "pd.DataFrame({'a': [1, 2]})\n",
        )]
        nb = _notebook("nb_pydf", cells)
        r1 = engine.execute_notebook(nb, cache_store=isolated_cache)
        first_preview = r1.cells[0].output_preview
        assert first_preview is not None
        # Second run hits cache; the preview is restored intact
        r2 = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r2.cells[0].cache_hit is True
        assert r2.cells[0].output_preview == first_preview
