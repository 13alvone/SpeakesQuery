"""
Tests for Phase 3 / Bet 4 slice 5 - dual-audience rich rendering.

Per ``feedback_dual_audience_ai_and_human``: every output that the SPA
displays as visual rendering also has a STRUCTURED form an AI agent
can introspect without HTML scraping.

Test layout:
  * TestDataFramePreview - output_preview shape + JSON-safe primitives
    + edge cases (empty, large, mixed dtypes, NaN, unicode)
  * TestMarkdownRenderer - output_html via the markdown library +
    graceful fallback when library is absent
  * TestParamSpec - param_spec field on CellResult mirrors the parsed
    YAML; AI agents can read spec.type / spec.options without parsing
  * TestParamOverride - execute_notebook(namespace=...) seed overrides
    the param cell's default; downstream cache invalidates correctly
  * TestParamCellBypassesCache - param cells re-execute every run
    (override semantics demand it); downstream cells still cache
  * TestSlice5CacheRoundTrip - cache writes + reads include the new
    fields; old cache entries (pre-slice-5 shape) load gracefully
  * TestApiResponseShape - /execute response carries new fields
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

import notebook_cache_store
import notebook_engine
import notebook_store
from notebook_engine import (
    CellResult, NotebookEngine, STATUS_SUCCESS,
    _build_dataframe_preview, _coerce_cell_value_for_preview,
    _render_markdown_html,
)


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
# 1. DataFrame preview: structured (AI) + visual (human-renderable)
# ═══════════════════════════════════════════════════════════════════

class TestDataFramePreview:
    def test_basic_shape(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        preview = _build_dataframe_preview(df)
        # Schema markers - AI agents key off these
        assert preview["schema_version"] == 1
        assert preview["kind"] == "dataframe"
        assert preview["total_rows"] == 3
        assert preview["total_cols"] == 2
        # Column metadata for type-aware downstream consumption
        assert preview["columns"] == [
            {"name": "a", "dtype": "int64"},
            {"name": "b", "dtype": "object"},
        ]
        # Head rows are JSON-safe primitives
        assert preview["head_rows"] == [
            {"a": 1, "b": "x"},
            {"a": 2, "b": "y"},
            {"a": 3, "b": "z"},
        ]
        assert preview["head_truncated"] is False

    def test_json_serialisable(self):
        # The whole preview must round-trip through json.dumps. AI
        # agents read it from /api/notebooks/<id>/execute response.
        df = pd.DataFrame({
            "i": [1, 2, 3],
            "f": [1.5, 2.5, 3.5],
            "s": ["a", "b", "c"],
            "b": [True, False, True],
        })
        preview = _build_dataframe_preview(df)
        # No exception → JSON-safe
        s = json.dumps(preview)
        assert "schema_version" in s
        # Parse back, verify primitives preserved
        roundtrip = json.loads(s)
        assert roundtrip["head_rows"][0]["i"] == 1
        assert roundtrip["head_rows"][0]["f"] == 1.5
        assert roundtrip["head_rows"][0]["b"] is True

    def test_head_truncation(self):
        df = pd.DataFrame({"x": list(range(100))})
        preview = _build_dataframe_preview(df)
        assert preview["total_rows"] == 100
        assert len(preview["head_rows"]) == 10  # _PREVIEW_HEAD_ROWS cap
        assert preview["head_truncated"] is True

    def test_empty_dataframe(self):
        df = pd.DataFrame({"x": []})
        preview = _build_dataframe_preview(df)
        assert preview["total_rows"] == 0
        assert preview["head_rows"] == []
        assert preview["head_truncated"] is False

    def test_nan_values_become_none(self):
        df = pd.DataFrame({"x": [1, float("nan"), 3]})
        preview = _build_dataframe_preview(df)
        assert preview["head_rows"][1]["x"] is None

    def test_long_string_truncated(self):
        long_s = "x" * 500
        df = pd.DataFrame({"s": [long_s]})
        preview = _build_dataframe_preview(df)
        cell_value = preview["head_rows"][0]["s"]
        assert len(cell_value) < 500
        assert "…" in cell_value or "+" in cell_value

    def test_complex_object_coerced_to_str(self):
        df = pd.DataFrame({"obj": [[1, 2, 3], {"k": "v"}, None]})
        preview = _build_dataframe_preview(df)
        # Lists / dicts → str; None → None
        assert isinstance(preview["head_rows"][0]["obj"], str)
        assert isinstance(preview["head_rows"][1]["obj"], str)
        assert preview["head_rows"][2]["obj"] is None

    def test_unicode_preserved(self):
        df = pd.DataFrame({"s": ["価格 (price)", "émoji 🎉"]})
        preview = _build_dataframe_preview(df)
        assert preview["head_rows"][0]["s"] == "価格 (price)"
        assert preview["head_rows"][1]["s"] == "émoji 🎉"

    def test_none_input_returns_none(self):
        assert _build_dataframe_preview(None) is None

    def test_non_dataframe_returns_none(self):
        assert _build_dataframe_preview("not a df") is None
        assert _build_dataframe_preview([1, 2, 3]) is None

    def test_value_coercion_helper(self):
        # _coerce_cell_value_for_preview specifically - drift guard
        # for the JSON-safe-primitives contract.
        import numpy as np
        assert _coerce_cell_value_for_preview(None) is None
        assert _coerce_cell_value_for_preview(42) == 42
        assert _coerce_cell_value_for_preview(3.14) == 3.14
        assert _coerce_cell_value_for_preview(True) is True
        assert _coerce_cell_value_for_preview("hello") == "hello"
        # Numpy scalars unwrap to primitives
        assert _coerce_cell_value_for_preview(np.int64(5)) == 5
        assert _coerce_cell_value_for_preview(np.float64(3.14)) == 3.14


# ═══════════════════════════════════════════════════════════════════
# 2. Markdown renderer (output_html) + graceful fallback
# ═══════════════════════════════════════════════════════════════════

class TestMarkdownRenderer:
    def test_basic_markdown_renders(self):
        out = _render_markdown_html("# Title\n\nSome **bold** text.")
        assert "<h1>" in out
        assert "<strong>" in out

    def test_empty_source_returns_empty(self):
        assert _render_markdown_html("") == ""
        assert _render_markdown_html(None) == ""

    def test_fenced_code_block(self):
        out = _render_markdown_html(
            "```python\nx = 1\n```",
        )
        # markdown library wraps fenced code in <pre><code [class=...]>
        assert "<pre>" in out
        assert "<code" in out  # may have language class
        assert "x = 1" in out

    def test_table_renders(self):
        out = _render_markdown_html(
            "| a | b |\n|---|---|\n| 1 | 2 |\n",
        )
        assert "<table>" in out
        assert "<th>" in out

    def test_fallback_when_markdown_lib_missing(self):
        # When the `markdown` library isn't importable, the renderer
        # falls back to <pre>-wrapped HTML-escaped source. Page stays
        # functional during the deploy window.
        with patch.dict("sys.modules", {"markdown": None}):
            # Force ImportError by patching __import__
            import builtins
            orig_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "markdown":
                    raise ImportError("simulated missing")
                return orig_import(name, *args, **kwargs)
            with patch("builtins.__import__", side_effect=fake_import):
                out = _render_markdown_html("# Title\n\n<script>alert('xss')</script>")
        assert "<pre" in out
        # XSS-safe even on fallback path
        assert "&lt;script&gt;" in out
        assert "<script>" not in out

    def test_engine_markdown_cell_includes_output_html(self, engine):
        cell = _cell("notes", "markdown", "## Findings\n\nSee above.")
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert "<h2>" in result.output_html
        # The original source is also preserved (AI audience reads
        # markdown directly, not HTML)
        assert result.output == "## Findings\n\nSee above."

    def test_chart_cell_does_not_render_html(self, engine):
        # Chart cells stay passthrough; rendering is slice 6.
        cell = _cell("plot", "chart", '{"mark": "bar"}')
        result = engine.execute_cell(cell, {})
        assert result.status == STATUS_SUCCESS
        assert result.output_html == ""


# ═══════════════════════════════════════════════════════════════════
# 3. param_spec on CellResult (AI introspection contract)
# ═══════════════════════════════════════════════════════════════════

class TestParamSpec:
    def test_param_cell_exposes_spec(self, engine):
        spec_yaml = (
            "type: select\n"
            "options: [aapl, msft, goog]\n"
            "default: aapl\n"
            "label: \"Ticker\"\n"
        )
        result = engine.execute_cell(_cell("ticker", "param", spec_yaml), {})
        assert result.status == STATUS_SUCCESS
        # Param spec exposed for AI introspection - same dict the YAML
        # parsed into, with type / options / default / label keys.
        assert result.param_spec == {
            "type": "select",
            "options": ["aapl", "msft", "goog"],
            "default": "aapl",
            "label": "Ticker",
        }
        # Output is the resolved value (default in absence of override)
        assert result.output == "aapl"

    def test_param_spec_in_to_dict(self, engine):
        spec_yaml = "type: number\ndefault: 42\n"
        result = engine.execute_cell(_cell("limit", "param", spec_yaml), {})
        d = result.to_dict()
        # AI agent reading the API response gets the spec without
        # re-parsing the YAML source.
        assert d["param_spec"]["type"] == "number"
        assert d["param_spec"]["default"] == 42

    def test_non_param_cell_has_no_param_spec(self, engine):
        cell = _cell("py", "python", "x = 1")
        result = engine.execute_cell(cell, {})
        assert result.param_spec is None


# ═══════════════════════════════════════════════════════════════════
# 4. Param-override semantics + cache correctness
# ═══════════════════════════════════════════════════════════════════

class TestParamOverride:
    def test_override_supplies_value_instead_of_default(self, engine):
        cells = [
            _cell("ticker", "param", "default: AAPL\ntype: text\n"),
            _cell("greet", "python", "f'looking at {ticker}'"),
        ]
        nb = _notebook("nb", cells)
        # Default path: ticker = "AAPL"
        r1 = engine.execute_notebook(nb)
        assert r1.cells[0].output == "AAPL"
        assert r1.cells[1].output == "looking at AAPL"
        # Override path: ticker = "MSFT"
        r2 = engine.execute_notebook(nb, namespace={"ticker": "MSFT"})
        assert r2.cells[0].output == "MSFT"
        assert r2.cells[1].output == "looking at MSFT"

    def test_override_invalidates_downstream_cache(self, engine, isolated_cache):
        cells = [
            _cell("ticker", "param", "default: AAPL\n"),
            _cell("greet", "python", "f'looking at {ticker}'"),
        ]
        nb = _notebook("nb_invalidate", cells)
        # First run with default - populates cache for cell `greet`.
        r1 = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r1.cells[0].cache_hit is False  # param bypasses cache
        assert r1.cells[1].output == "looking at AAPL"
        # Second run with same default - `greet` hits cache.
        r2 = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r2.cells[1].cache_hit is True
        assert r2.cells[1].output == "looking at AAPL"
        # Third run with override - param output_hash changes →
        # `greet` content_hash changes → `greet` cache MISS, re-runs
        # with the new ticker value.
        r3 = engine.execute_notebook(
            nb, cache_store=isolated_cache, namespace={"ticker": "MSFT"},
        )
        assert r3.cells[1].cache_hit is False
        assert r3.cells[1].output == "looking at MSFT"
        # Fourth run reverting to default - should hit cache from r1/r2.
        r4 = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r4.cells[1].cache_hit is True
        assert r4.cells[1].output == "looking at AAPL"


# ═══════════════════════════════════════════════════════════════════
# 5. Param cells bypass cache (correctness > marginal speedup)
# ═══════════════════════════════════════════════════════════════════

class TestParamCellBypassesCache:
    def test_param_cell_never_cache_hit(self, engine, isolated_cache):
        cell = _cell("ticker", "param", "default: AAPL\n")
        nb = _notebook("nb", [cell])
        # Run multiple times - each should re-execute the param cell.
        for _ in range(3):
            r = engine.execute_notebook(nb, cache_store=isolated_cache)
            assert r.cells[0].cache_hit is False
        # Cache for the notebook stays empty (param cells never write)
        assert isolated_cache.count() == 0

    def test_param_cell_skip_does_not_break_downstream_caching(
        self, engine, isolated_cache,
    ):
        # Downstream non-param cells still cache normally.
        cells = [
            _cell("p", "param", "default: 5\ntype: number\n"),
            _cell("c", "python", "p * 2"),
        ]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)
        # Cache has cell `c` but NOT cell `p`
        assert isolated_cache.count() == 1
        # Second run: `p` re-executes; `c` hits cache
        r = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r.cells[0].cache_hit is False
        assert r.cells[1].cache_hit is True


# ═══════════════════════════════════════════════════════════════════
# 6. Cache round-trip with slice-5 fields (additive, backward-compat)
# ═══════════════════════════════════════════════════════════════════

class TestSlice5CacheRoundTrip:
    def test_dataframe_preview_persists_through_cache(
        self, engine, isolated_cache,
    ):
        cells = [_cell(
            "results", "spql",
            'index="indexes/default_test/output_parquets/test0.parquet"',
        )]
        nb = _notebook("nb_pv", cells)
        # First run populates cache
        r1 = engine.execute_notebook(nb, cache_store=isolated_cache)
        first_preview = r1.cells[0].output_preview
        assert first_preview is not None
        assert first_preview["kind"] == "dataframe"
        # Second run hits cache; preview is restored from the cache
        r2 = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r2.cells[0].cache_hit is True
        assert r2.cells[0].output_preview == first_preview

    def test_markdown_html_persists_through_cache(
        self, engine, isolated_cache,
    ):
        cells = [_cell("notes", "markdown", "# Hello")]
        nb = _notebook("nb_md", cells)
        r1 = engine.execute_notebook(nb, cache_store=isolated_cache)
        first_html = r1.cells[0].output_html
        assert "<h1>" in first_html
        r2 = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert r2.cells[0].cache_hit is True
        assert r2.cells[0].output_html == first_html

    def test_pre_slice5_cache_entry_loads_gracefully(self, isolated_cache):
        # Simulate an old-shape cache entry (no output_preview / output_html
        # / param_spec keys). The store should reconstruct CachedEntry
        # with safe defaults, NOT raise.
        old_payload = {
            "namespace_delta": {"x": 42},
            "output": 42,
            "output_repr": "42",
            "stdout": "",
            "stderr": "",
            "exposed_names": ["x"],
            # NO output_preview / output_html / param_spec
        }
        isolated_cache.put(
            content_hash="old", output_hash="oh",
            notebook_id="nb", cell_id="cell_1", cell_type="python",
            payload=old_payload,
            runtime_ms=10, executed_at="2026-05-09T00:00:00+00:00",
        )
        cached = isolated_cache.get("old")
        assert cached is not None
        # Backward-compat defaults
        assert cached.output_preview is None
        assert cached.output_html == ""
        assert cached.param_spec is None
        # Original fields still intact
        assert cached.output == 42
        assert cached.namespace_delta == {"x": 42}


# ═══════════════════════════════════════════════════════════════════
# 7. /api/notebooks/<id>/execute carries new fields
# ═══════════════════════════════════════════════════════════════════

class TestApiResponseShape:
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

    def test_execute_response_includes_slice5_fields(self, client):
        nb = {
            "id": "ai_audience",
            "schema_version": 1,
            "name": "AI test", "description": "", "default_max_cost_usd": 0.0,
            "cells": [
                _cell(
                    "rows", "spql",
                    'index="indexes/default_test/output_parquets/test0.parquet"',
                ),
                _cell("notes", "markdown", "# Hello"),
                _cell("ticker", "param", "default: AAPL\ntype: text\n"),
            ],
        }
        client.post("/api/notebooks", json=nb)
        resp = client.post("/api/notebooks/ai_audience/execute", json={})
        assert resp.status_code == 200
        body = resp.get_json()
        result = body["result"]
        # AI agents key off these structured fields per cell
        cell_results = {c["cell_id"]: c for c in result["cells"]}
        # SPQL cell carries output_preview
        assert cell_results["rows"]["output_preview"] is not None
        assert cell_results["rows"]["output_preview"]["kind"] == "dataframe"
        # Markdown cell carries output_html
        assert "<h1>" in cell_results["notes"]["output_html"]
        # Param cell carries param_spec
        assert cell_results["ticker"]["param_spec"]["default"] == "AAPL"

    def test_execute_accepts_namespace_overrides(self, client):
        nb = {
            "id": "ovr",
            "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [
                _cell("ticker", "param", "default: AAPL\ntype: text\n"),
                _cell("g", "python", "f'using {ticker}'"),
            ],
        }
        client.post("/api/notebooks", json=nb)
        # Default
        d = client.post("/api/notebooks/ovr/execute", json={}).get_json()
        assert d["result"]["cells"][0]["output_repr"] == "param[text] = 'AAPL'"
        # Override
        d = client.post(
            "/api/notebooks/ovr/execute",
            json={"namespace_overrides": {"ticker": "MSFT"}},
        ).get_json()
        assert d["result"]["cells"][0]["output_repr"] == "param[text] = 'MSFT'"

    def test_execute_rejects_non_dict_overrides(self, client):
        nb = {
            "id": "bad_ovr",
            "schema_version": 1,
            "name": "", "description": "", "default_max_cost_usd": 0.0,
            "cells": [_cell("x", "python", "x = 1")],
        }
        client.post("/api/notebooks", json=nb)
        resp = client.post(
            "/api/notebooks/bad_ovr/execute",
            json={"namespace_overrides": "not a dict"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        # Dual-audience structured error: human message + machine fields
        assert body["status"] == "error"
        assert body["error_class"] == "InvalidInput"
        assert body["expected"] == "dict"
        assert body["actual"] == "str"
