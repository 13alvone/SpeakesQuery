"""
Tests for notebook_engine.py + notebook_cache_store.py integration -
Phase 3 / Bet 4 slice 3.

The cache layer wraps the slice-2 execution engine. These tests pin:

  * **Result equivalence** (`reference_result_equivalence_test_pattern`):
    use_cache=True ≡ use_cache=False on output. The fast path must
    produce identical results to the slow path or the cache is wrong.
  * **Cache hit signature**: cache_hit=True, runtime_ms=0 on hit;
    namespace_delta restored without re-execution.
  * **Edit-invalidation cascade**: editing cell N invalidates only
    cell N + downstream cells; cells <N stay cached.
  * **Identical-output preservation**: a re-run of the SAME notebook
    (no edits) hits cache for every cell.
  * **Money-leak canary** (`feedback_money_leak_audit_pattern`):
    cache hits route around `process_query_with_diagnostics` and
    around `exec()`. Patch the production code path; assert
    zero invocations on a cache-hit run.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

import notebook_cache_store
import notebook_engine
from notebook_cache_store import NotebookCacheStore
from notebook_engine import NotebookEngine, STATUS_SUCCESS


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Fresh cache per test, redirected to tmp_path."""
    notebook_cache_store.reset_for_tests()
    db = tmp_path / "notebook_cache.sqlite"
    payloads = tmp_path / "notebook_cache"
    monkeypatch.setattr(notebook_cache_store, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_PAYLOAD_DIR", payloads,
    )
    yield NotebookCacheStore()
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
# 1. Cache hit signature
# ═══════════════════════════════════════════════════════════════════

class TestCacheHitSignature:
    def test_first_run_misses_cache(self, engine, isolated_cache):
        cells = [_cell("a", "python", "x = 5\nx * 2")]
        result = engine.execute_notebook(
            _notebook("nb", cells), cache_store=isolated_cache,
        )
        assert result.cells[0].cache_hit is False
        assert result.cells[0].status == STATUS_SUCCESS
        assert result.cache_hits == 0

    def test_second_run_hits_cache(self, engine, isolated_cache):
        cells = [_cell("a", "python", "x = 5\nx * 2")]
        nb = _notebook("nb", cells)
        first = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert first.cells[0].cache_hit is False
        second = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert second.cells[0].cache_hit is True
        assert second.cells[0].status == STATUS_SUCCESS
        assert second.cells[0].output == 10
        assert second.cache_hits == 1

    def test_cache_hit_runtime_ms_is_zero(self, engine, isolated_cache):
        cells = [_cell("a", "python", "x = 1")]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)
        result = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert result.cells[0].runtime_ms == 0
        assert result.cells[0].cache_hit is True

    def test_cache_hit_restores_namespace(self, engine, isolated_cache):
        # First run defines `compute_result`; second run must re-bind it
        # in the namespace via cache restoration.
        cells = [
            _cell("setup", "python", "compute_result = 42 * 100"),
            _cell("consume", "python", "compute_result + 1"),
        ]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)
        result = engine.execute_notebook(nb, cache_store=isolated_cache)
        # Both cells hit cache
        assert result.cells[0].cache_hit is True
        assert result.cells[1].cache_hit is True
        # Second cell's output proves namespace was restored
        assert result.cells[1].output == 4201

    def test_cache_hit_preserves_output_value(self, engine, isolated_cache):
        cells = [_cell("a", "python", "[1, 2, 3, 'hello']")]
        nb = _notebook("nb", cells)
        first = engine.execute_notebook(nb, cache_store=isolated_cache)
        second = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert first.cells[0].output == [1, 2, 3, "hello"]
        assert second.cells[0].output == [1, 2, 3, "hello"]
        assert second.cells[0].cache_hit is True


# ═══════════════════════════════════════════════════════════════════
# 2. Result-equivalence drift guard (the load-bearing test)
# ═══════════════════════════════════════════════════════════════════

class TestResultEquivalence:
    """Per ``reference_result_equivalence_test_pattern.md``: when
    two paths exist for the same op (here: cached vs uncached), run
    both on the same input and assert identical output. Catches the
    highest-value regression class - silently-wrong cache hits.
    """

    def test_use_cache_true_equals_use_cache_false_on_python(
        self, engine, isolated_cache,
    ):
        cells = [
            _cell("a", "python", "x = 5"),
            _cell("b", "python", "y = x * 2"),
            _cell("c", "python", "x + y"),
        ]
        nb = _notebook("nb", cells)
        # Slow path
        slow = engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=False,
        )
        # Fast path (re-run; second time will hit)
        engine.execute_notebook(nb, cache_store=isolated_cache)  # populate
        fast = engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=True,
        )
        # Per-cell outputs must match
        for slow_cell, fast_cell in zip(slow.cells, fast.cells):
            assert slow_cell.cell_id == fast_cell.cell_id
            assert slow_cell.status == fast_cell.status
            assert slow_cell.output == fast_cell.output
            assert slow_cell.exposed_names == fast_cell.exposed_names

    def test_equivalence_on_mixed_cell_types(self, engine, isolated_cache):
        cells = [
            _cell("ticker", "param", "default: AAPL\n"),
            _cell("greet", "python", 'f"hello {ticker}"'),
            _cell("note", "markdown", "## hi"),
        ]
        nb = _notebook("mixed", cells)
        slow = engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=False,
        )
        engine.execute_notebook(nb, cache_store=isolated_cache)
        fast = engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=True,
        )
        for slow_cell, fast_cell in zip(slow.cells, fast.cells):
            assert slow_cell.output == fast_cell.output
            assert slow_cell.status == fast_cell.status


# ═══════════════════════════════════════════════════════════════════
# 3. Edit-invalidation cascade
# ═══════════════════════════════════════════════════════════════════

class TestEditInvalidation:
    """The load-bearing UX promise: edit cell N → cells <N stay cached,
    cells N+ recompute. This is the headline economics from ROADMAP
    Bet 4.2 ("iterating on a brief becomes free until you choose to
    spend").
    """

    def test_edit_middle_cell_invalidates_only_downstream(
        self, engine, isolated_cache,
    ):
        cells_v1 = [
            _cell("a", "python", "a_val = 1"),
            _cell("b", "python", "b_val = a_val + 1"),
            _cell("c", "python", "c_val = b_val + 1"),
        ]
        nb1 = _notebook("nb", cells_v1)
        engine.execute_notebook(nb1, cache_store=isolated_cache)

        # Edit cell B (middle)
        cells_v2 = [
            _cell("a", "python", "a_val = 1"),                # unchanged
            _cell("b", "python", "b_val = a_val + 100"),      # CHANGED
            _cell("c", "python", "c_val = b_val + 1"),        # unchanged
        ]
        nb2 = _notebook("nb", cells_v2)
        result = engine.execute_notebook(nb2, cache_store=isolated_cache)

        # Cell A: unchanged source + no upstream → hits cache
        assert result.cells[0].cache_hit is True
        # Cell B: source changed → cache miss (re-runs)
        assert result.cells[1].cache_hit is False
        # Cell C: upstream output_hash changed → cache miss (re-runs)
        assert result.cells[2].cache_hit is False

    def test_edit_first_cell_cascades_through_all(
        self, engine, isolated_cache,
    ):
        cells_v1 = [
            _cell("a", "python", "a_val = 1"),
            _cell("b", "python", "b_val = a_val + 1"),
            _cell("c", "python", "c_val = b_val + 1"),
        ]
        engine.execute_notebook(
            _notebook("nb", cells_v1), cache_store=isolated_cache,
        )

        cells_v2 = [
            _cell("a", "python", "a_val = 999"),  # CHANGED
            _cell("b", "python", "b_val = a_val + 1"),
            _cell("c", "python", "c_val = b_val + 1"),
        ]
        result = engine.execute_notebook(
            _notebook("nb", cells_v2), cache_store=isolated_cache,
        )
        # All three cache misses - cascaded invalidation
        assert all(not c.cache_hit for c in result.cells)

    def test_edit_last_cell_keeps_upstream_cached(
        self, engine, isolated_cache,
    ):
        cells_v1 = [
            _cell("a", "python", "x = 1"),
            _cell("b", "python", "y = 2"),
            _cell("c", "python", "z = 3"),
        ]
        engine.execute_notebook(
            _notebook("nb", cells_v1), cache_store=isolated_cache,
        )

        cells_v2 = [
            _cell("a", "python", "x = 1"),
            _cell("b", "python", "y = 2"),
            _cell("c", "python", "z = 999"),  # ONLY this changed
        ]
        result = engine.execute_notebook(
            _notebook("nb", cells_v2), cache_store=isolated_cache,
        )
        # Cells A + B hit cache (no upstream change)
        assert result.cells[0].cache_hit is True
        assert result.cells[1].cache_hit is True
        # Cell C: source changed → miss
        assert result.cells[2].cache_hit is False

    def test_no_edits_full_cache_hit(self, engine, isolated_cache):
        cells = [
            _cell(c_id, "python", f"{c_id}_val = '{c_id}'")
            for c_id in ("alpha", "beta", "gamma", "delta", "epsilon")
        ]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)
        result = engine.execute_notebook(nb, cache_store=isolated_cache)
        # ALL cells hit cache on identical re-run
        assert all(c.cache_hit for c in result.cells)
        assert result.cache_hits == 5


# ═══════════════════════════════════════════════════════════════════
# 4. Money-leak canary - cache hits don't re-execute
# ═══════════════════════════════════════════════════════════════════

class TestMoneyLeakCanaryForCache:
    """Per ``feedback_money_leak_audit_pattern``: cache hits MUST NOT
    invoke the underlying execution path. We patch the production
    code path with a function that raises AssertionError; if any cache
    hit accidentally re-routes through the patched function, the test
    fails loudly. Slice 7 set this precedent for `| llm`-shaped pipes;
    slice 3's cache layer applies the same drift guard.
    """

    def test_cache_hit_does_not_call_process_query(
        self, engine, isolated_cache,
    ):
        # First run: populate cache (real call)
        cells = [_cell("results", "spql",
                       'index="indexes/default_test/output_parquets/test0.parquet"')]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)

        # Second run: every cell SHOULD hit cache. Patch the SPQL path
        # to raise on invocation; if cache routing fails, the test
        # blows up with our clear MONEY LEAK message.
        def boom(*args, **kwargs):
            raise AssertionError(
                "MONEY LEAK: process_query_with_diagnostics was invoked "
                "during a run where every cell should have hit cache. "
                "The slice-3 cache routing is broken."
            )
        with patch(
            "query_engine.CmdExecutionBackend.process_query_with_diagnostics",
            side_effect=boom,
        ):
            result = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert all(c.cache_hit for c in result.cells)
        assert result.cells[0].status == STATUS_SUCCESS

    def test_cache_hit_does_not_exec_python_source(
        self, engine, isolated_cache,
    ):
        # First run: populate
        cells = [_cell("a", "python", "side_effect_var = 1")]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)

        # Second run with cell.source SWAPPED to malicious code.
        # If cache routing is correct, the original source hash is in
        # the cache and the malicious source is never hashed (because
        # we look up by the NEW source's hash → would miss).
        # Wait - that's not the right check. Let me reframe:
        # The real claim is: when cache_hit=True for a python cell, the
        # cached exec is NOT re-run. We verify by patching `exec` to
        # check it isn't called for the cached cell.

        # Actually the easiest verification: count exec calls. We patch
        # the `_execute_python` method which is the entry point.
        called = {"count": 0}
        original = engine._execute_python

        def counting_exec(*args, **kwargs):
            called["count"] += 1
            return original(*args, **kwargs)

        with patch.object(engine, "_execute_python", side_effect=counting_exec):
            result = engine.execute_notebook(nb, cache_store=isolated_cache)

        assert result.cells[0].cache_hit is True
        assert called["count"] == 0, (
            "Python cell that hit cache should NOT route through "
            f"_execute_python. Got {called['count']} invocations."
        )

    def test_use_cache_false_forces_full_re_execution(
        self, engine, isolated_cache,
    ):
        # If the operator explicitly disables caching, every cell
        # re-runs even if a cache entry exists.
        cells = [_cell("a", "python", "x = 1")]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb, cache_store=isolated_cache)
        result = engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=False,
        )
        assert result.cells[0].cache_hit is False


# ═══════════════════════════════════════════════════════════════════
# 5. Errored cells are not cached
# ═══════════════════════════════════════════════════════════════════

class TestErroredCellsNotCached:
    """Errors are intentionally NOT memoised - re-running with a fixed
    upstream re-attempts downstream cells. Otherwise a single transient
    failure would poison the cache forever.
    """

    def test_errored_cell_runs_fresh_each_time(self, engine, isolated_cache):
        cells = [_cell("a", "python", "1 / 0")]
        nb = _notebook("nb", cells)
        first = engine.execute_notebook(nb, cache_store=isolated_cache)
        second = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert first.cells[0].status == "error"
        assert second.cells[0].status == "error"
        # Errored cell does NOT get cached
        assert second.cells[0].cache_hit is False

    def test_fixing_upstream_recovers_downstream(
        self, engine, isolated_cache,
    ):
        # Cell B depends on cell A; if A fails, B fails. Fixing A
        # should let B re-run successfully - NOT serve from cache
        # of the prior failed run.
        cells_v1 = [
            _cell("a", "python", "a_val = undefined_name"),  # errors
            _cell("b", "python", "a_val + 1"),
        ]
        first = engine.execute_notebook(
            _notebook("nb", cells_v1), cache_store=isolated_cache,
        )
        assert first.cells[0].status == "error"
        assert first.cells[1].status == "error"

        # Fix cell A; cell B's source unchanged
        cells_v2 = [
            _cell("a", "python", "a_val = 100"),  # FIXED
            _cell("b", "python", "a_val + 1"),
        ]
        second = engine.execute_notebook(
            _notebook("nb", cells_v2), cache_store=isolated_cache,
        )
        assert second.cells[0].status == STATUS_SUCCESS
        assert second.cells[0].output is None  # no terminal expr
        assert second.cells[1].status == STATUS_SUCCESS
        assert second.cells[1].output == 101


# ═══════════════════════════════════════════════════════════════════
# 6. Cache disabled paths
# ═══════════════════════════════════════════════════════════════════

class TestCacheDisabledPaths:
    """Engine cache-disable contract:
      * ``use_cache=False`` → no reads, no writes (full re-execution)
      * ``cache_store=None`` (default) + ``use_cache=True`` → use singleton
      * ``cache_store=<custom>`` → use the custom store regardless
    """

    def test_use_cache_false_skips_reads_and_writes(
        self, engine, isolated_cache,
    ):
        cells = [_cell("a", "python", "x = 1")]
        nb = _notebook("nb", cells)
        engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=False,
        )
        # use_cache=False = neither read nor write; nothing in cache
        assert isolated_cache.count() == 0

    def test_default_args_use_singleton_via_isolated_fixture(
        self, engine, isolated_cache,
    ):
        # The isolated_cache fixture monkeypatches DEFAULT_DB_PATH/
        # DEFAULT_PAYLOAD_DIR, so the singleton resolved by the engine
        # IS the isolated_cache. Default args (cache_store=None,
        # use_cache=True) → write happens.
        cells = [_cell("a", "python", "x = 1")]
        nb = _notebook("nb", cells)
        engine.execute_notebook(nb)
        # The singleton lives in tmp_path; isolated_cache is the same
        # tmp_path (different NotebookCacheStore instance pointing at
        # the same DB). count() reads the DB, so it sees the write.
        assert isolated_cache.count() == 1

    def test_writes_resume_on_use_cache_true(
        self, engine, isolated_cache,
    ):
        # First run: use_cache=False → no cache state
        cells = [_cell("a", "python", "x = 1")]
        nb = _notebook("nb", cells)
        engine.execute_notebook(
            nb, cache_store=isolated_cache, use_cache=False,
        )
        assert isolated_cache.count() == 0
        # Second run: use_cache=True → writes
        engine.execute_notebook(nb, cache_store=isolated_cache)
        assert isolated_cache.count() == 1
        # Third run: hits the now-populated cache
        result = engine.execute_notebook(nb, cache_store=isolated_cache)
        assert result.cells[0].cache_hit is True
