"""
Tests for notebook_cache_store.py - Phase 3 / Bet 4 slice 3.

The cache store is the persistence layer for the slice-3 reactive
cache. Slice-3 engine integration is tested separately in
test_notebook_engine_cache.py - this file covers store-only contracts:

  * Hash determinism (compute_content_hash + compute_output_hash)
  * CRUD (put / get / invalidate / invalidate_notebook)
  * LRU eviction at the budget boundary
  * Stats + accounting
  * Singleton + reset_for_tests lifecycle
  * Settings drift (5 places: DEFAULTS, YAML, validator, UI, JS map)
  * User-data drift (gitignore, persistence.py, docker-compose, install.sh)
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

import notebook_cache_store
from notebook_cache_store import (
    CachedEntry, NotebookCacheStore,
    compute_content_hash, compute_output_hash, get_store, reset_for_tests,
)


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the cache to tmp_path; reset the singleton."""
    notebook_cache_store.reset_for_tests()
    db = tmp_path / "notebook_cache.sqlite"
    payloads = tmp_path / "notebook_cache"
    monkeypatch.setattr(notebook_cache_store, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_PAYLOAD_DIR", payloads,
    )
    yield db, payloads
    notebook_cache_store.reset_for_tests()


def _payload(output, namespace_delta=None, **extra):
    p = {
        "namespace_delta": namespace_delta or {},
        "output": output,
        "output_repr": repr(output)[:100],
        "stdout": "",
        "stderr": "",
        "exposed_names": list((namespace_delta or {}).keys()),
    }
    p.update(extra)
    return p


# ═══════════════════════════════════════════════════════════════════
# 1. Hash determinism
# ═══════════════════════════════════════════════════════════════════

class TestContentHash:
    def test_same_inputs_same_hash(self):
        cell = {"type": "spql", "source": 'index="x"'}
        assert (
            compute_content_hash(cell, [])
            == compute_content_hash(cell, [])
        )

    def test_source_change_changes_hash(self):
        a = {"type": "spql", "source": 'index="x"'}
        b = {"type": "spql", "source": 'index="y"'}
        assert compute_content_hash(a, []) != compute_content_hash(b, [])

    def test_type_change_changes_hash(self):
        a = {"type": "spql", "source": "x"}
        b = {"type": "python", "source": "x"}
        assert compute_content_hash(a, []) != compute_content_hash(b, [])

    def test_prior_hash_change_changes_hash(self):
        cell = {"type": "spql", "source": 'index="x"'}
        h1 = compute_content_hash(cell, [])
        h2 = compute_content_hash(cell, ["abc"])
        h3 = compute_content_hash(cell, ["xyz"])
        assert h1 != h2 != h3

    def test_prior_hash_order_matters(self):
        # Reordering upstream cells must change the hash - different
        # data flow means different downstream value even if upstreams
        # produced the same outputs.
        cell = {"type": "python", "source": "x + y"}
        a = compute_content_hash(cell, ["aaa", "bbb"])
        b = compute_content_hash(cell, ["bbb", "aaa"])
        assert a != b

    def test_returns_64_char_hex(self):
        cell = {"type": "spql", "source": "x"}
        h = compute_content_hash(cell, [])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_missing_fields_default_to_empty(self):
        # No type, no source - both default to "" - hash still computed.
        h = compute_content_hash({}, [])
        assert len(h) == 64


class TestOutputHash:
    def test_same_payload_same_hash(self):
        a = _payload(output=42, namespace_delta={"x": 1})
        b = _payload(output=42, namespace_delta={"x": 1})
        assert compute_output_hash(a) == compute_output_hash(b)

    def test_different_output_different_hash(self):
        a = _payload(output=1)
        b = _payload(output=2)
        assert compute_output_hash(a) != compute_output_hash(b)

    def test_different_namespace_delta_different_hash(self):
        a = _payload(output=1, namespace_delta={"x": 1})
        b = _payload(output=1, namespace_delta={"x": 2})
        assert compute_output_hash(a) != compute_output_hash(b)

    def test_dataframe_payload_hashes_to_some_value(self):
        # DataFrames pickle to bytes (not byte-deterministic across
        # construction calls - pandas BlockManager state varies - but
        # the SAME object instance pickles identically each call).
        # The slice-3 cache stores the upstream's output_hash and
        # forwards it; downstream cache-hit chains rely on the
        # CACHE ENTRY's stored hash, not on re-pickling the value.
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        payload = _payload(output=df, namespace_delta={"r": df})
        h1 = compute_output_hash(payload)
        h2 = compute_output_hash(payload)
        assert h1 == h2  # same dict → same bytes
        assert len(h1) == 64

    def test_jsonable_payload_is_byte_deterministic(self):
        # Pure-JSON values (int / str / list / dict of primitives)
        # DO pickle deterministically. The whole DAG-cache scheme
        # relies on this for non-DataFrame outputs.
        a = _payload(output={"x": 1, "y": [2, 3]}, namespace_delta={"v": 42})
        b = _payload(output={"x": 1, "y": [2, 3]}, namespace_delta={"v": 42})
        assert compute_output_hash(a) == compute_output_hash(b)


# ═══════════════════════════════════════════════════════════════════
# 2. Store CRUD
# ═══════════════════════════════════════════════════════════════════

class TestStoreCrud:
    def test_put_then_get(self, isolated_cache):
        store = NotebookCacheStore()
        size = store.put(
            content_hash="abc",
            output_hash="def",
            notebook_id="nb1",
            cell_id="cell_1",
            cell_type="spql",
            payload=_payload(output=42, namespace_delta={"cell_1": 42}),
            runtime_ms=123,
            executed_at="2026-05-09T00:00:00+00:00",
        )
        assert size > 0
        cached = store.get("abc")
        assert cached is not None
        assert cached.content_hash == "abc"
        assert cached.output_hash == "def"
        assert cached.notebook_id == "nb1"
        assert cached.cell_id == "cell_1"
        assert cached.cell_type == "spql"
        assert cached.output == 42
        assert cached.namespace_delta == {"cell_1": 42}
        assert cached.runtime_ms == 123
        assert cached.exposed_names == ["cell_1"]

    def test_get_miss_returns_none(self, isolated_cache):
        store = NotebookCacheStore()
        assert store.get("nonexistent") is None

    def test_put_replaces_existing(self, isolated_cache):
        store = NotebookCacheStore()
        store.put(
            content_hash="key", output_hash="h1",
            notebook_id="nb", cell_id="c", cell_type="python",
            payload=_payload(output="v1"),
            runtime_ms=100, executed_at="2026-05-09T00:00:00+00:00",
        )
        store.put(
            content_hash="key", output_hash="h2",
            notebook_id="nb", cell_id="c", cell_type="python",
            payload=_payload(output="v2"),
            runtime_ms=200, executed_at="2026-05-09T00:00:00+00:00",
        )
        cached = store.get("key")
        assert cached.output == "v2"
        assert cached.output_hash == "h2"
        # And only one row exists
        assert store.count() == 1

    def test_get_hit_increments_hit_count(self, isolated_cache):
        store = NotebookCacheStore()
        store.put(
            content_hash="hk", output_hash="oh",
            notebook_id="nb", cell_id="c", cell_type="markdown",
            payload=_payload(output="hello"),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        a = store.get("hk")
        b = store.get("hk")
        c = store.get("hk")
        assert a.hit_count == 1
        assert b.hit_count == 2
        assert c.hit_count == 3

    def test_get_hit_updates_last_accessed_at(self, isolated_cache):
        store = NotebookCacheStore()
        store.put(
            content_hash="hk", output_hash="oh",
            notebook_id="nb", cell_id="c", cell_type="markdown",
            payload=_payload(output="x"),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        first = store.get("hk").last_accessed_at
        time.sleep(0.01)
        second = store.get("hk").last_accessed_at
        assert second >= first

    def test_invalidate_removes_entry_and_payload(self, isolated_cache):
        _, payloads_dir = isolated_cache
        store = NotebookCacheStore()
        store.put(
            content_hash="x", output_hash="o",
            notebook_id="nb", cell_id="c", cell_type="python",
            payload=_payload(output=1),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        # Payload file exists
        assert (payloads_dir / "x.pkl").exists()
        assert store.invalidate("x") is True
        assert store.get("x") is None
        assert not (payloads_dir / "x.pkl").exists()

    def test_invalidate_missing_returns_false(self, isolated_cache):
        store = NotebookCacheStore()
        assert store.invalidate("nope") is False

    def test_invalidate_notebook_drops_all_for_id(self, isolated_cache):
        store = NotebookCacheStore()
        for i in range(5):
            store.put(
                content_hash=f"k{i}", output_hash=f"o{i}",
                notebook_id="target", cell_id=f"c{i}",
                cell_type="python",
                payload=_payload(output=i),
                runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
            )
        # And one for a different notebook (should survive)
        store.put(
            content_hash="other", output_hash="oh",
            notebook_id="elsewhere", cell_id="x", cell_type="python",
            payload=_payload(output=99),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        deleted = store.invalidate_notebook("target")
        assert deleted == 5
        assert store.count() == 1
        # The other notebook's entry survives
        assert store.get("other") is not None

    def test_get_with_missing_payload_self_heals(self, isolated_cache):
        _, payloads_dir = isolated_cache
        store = NotebookCacheStore()
        store.put(
            content_hash="orphan", output_hash="o",
            notebook_id="nb", cell_id="c", cell_type="markdown",
            payload=_payload(output="x"),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        # Manually delete the payload file (simulate disk corruption)
        os.unlink(payloads_dir / "orphan.pkl")
        # get() should clean up the orphan row + return None
        assert store.get("orphan") is None
        assert store.count() == 0


# ═══════════════════════════════════════════════════════════════════
# 3. LRU eviction + budget
# ═══════════════════════════════════════════════════════════════════

class TestLRUEviction:
    def test_evict_to_budget_drops_oldest_first(self, isolated_cache):
        store = NotebookCacheStore()
        # Insert 3 entries with increasing timestamps
        for i, key in enumerate(["oldest", "middle", "newest"]):
            store.put(
                content_hash=key, output_hash="o",
                notebook_id="nb", cell_id=f"c_{i}", cell_type="python",
                payload=_payload(output="x" * 1024),  # ~1KB each (a bit more after pickle overhead)
                runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
            )
            time.sleep(0.005)  # ensure distinct timestamps
        sizes_before = store.total_size_bytes()
        # Evict to half the current size
        freed = store.evict_to_budget(sizes_before // 2)
        assert freed > 0
        # The oldest should be gone, the newest preserved
        assert store.get("oldest") is None
        assert store.get("newest") is not None

    def test_evict_to_zero_clears_everything(self, isolated_cache):
        store = NotebookCacheStore()
        for i in range(3):
            store.put(
                content_hash=f"k{i}", output_hash="o",
                notebook_id="nb", cell_id=f"c{i}", cell_type="python",
                payload=_payload(output=f"v{i}"),
                runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
            )
        freed = store.evict_to_budget(-1)
        assert freed > 0
        assert store.count() == 0
        assert store.total_size_bytes() == 0

    def test_recent_access_reorders_lru(self, isolated_cache):
        store = NotebookCacheStore()
        store.put(
            content_hash="a", output_hash="o",
            notebook_id="nb", cell_id="a", cell_type="python",
            payload=_payload(output="aaa"),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        time.sleep(0.005)
        store.put(
            content_hash="b", output_hash="o",
            notebook_id="nb", cell_id="b", cell_type="python",
            payload=_payload(output="bbb"),
            runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
        )
        time.sleep(0.005)
        # Touch "a" - moves it to head of LRU list
        store.get("a")
        time.sleep(0.005)
        # Now evict half - "b" (the actual LRU now) should go first
        store.evict_to_budget(store.total_size_bytes() // 2)
        assert store.get("a") is not None
        assert store.get("b") is None

    def test_clear_empties_cache(self, isolated_cache):
        store = NotebookCacheStore()
        for i in range(2):
            store.put(
                content_hash=f"k{i}", output_hash="o",
                notebook_id="nb", cell_id=f"c{i}", cell_type="python",
                payload=_payload(output=f"v{i}"),
                runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
            )
        assert store.clear() > 0
        assert store.count() == 0


# ═══════════════════════════════════════════════════════════════════
# 4. Stats + accounting
# ═══════════════════════════════════════════════════════════════════

class TestStats:
    def test_stats_empty(self, isolated_cache):
        store = NotebookCacheStore()
        s = store.stats()
        assert s["entries"] == 0
        assert s["size_bytes"] == 0
        assert s["size_gb"] == 0.0

    def test_stats_after_writes(self, isolated_cache):
        store = NotebookCacheStore()
        for i in range(3):
            store.put(
                content_hash=f"k{i}", output_hash="o",
                notebook_id="nb", cell_id=f"c{i}", cell_type="python",
                payload=_payload(output="x" * 1024),
                runtime_ms=1, executed_at="2026-05-09T00:00:00+00:00",
            )
        store.get("k0")
        store.get("k0")
        store.get("k1")
        s = store.stats()
        assert s["entries"] == 3
        assert s["size_bytes"] > 0
        assert s["total_hits"] == 3


# ═══════════════════════════════════════════════════════════════════
# 5. Singleton lifecycle
# ═══════════════════════════════════════════════════════════════════

class TestSingleton:
    def test_get_store_returns_same_instance(self, isolated_cache):
        a = get_store()
        b = get_store()
        assert a is b

    def test_reset_for_tests_clears_singleton(self, isolated_cache):
        a = get_store()
        reset_for_tests()
        b = get_store()
        assert a is not b


# ═══════════════════════════════════════════════════════════════════
# 6. Settings 5-layer drift guards
# ═══════════════════════════════════════════════════════════════════

class TestCacheSettingsDrift:
    """Per ``reference_setting_drift_five_layers``: every new global
    setting lives in 5 places. Slice 3 added two settings; pin all
    five layers per setting.
    """

    def test_defaults_dict(self):
        from global_settings import DEFAULTS
        assert "notebook_cache_enabled" in DEFAULTS
        assert "max_notebook_cache_gb" in DEFAULTS
        assert DEFAULTS["notebook_cache_enabled"] is True
        assert DEFAULTS["max_notebook_cache_gb"] == 1.0

    def test_yaml_mirrors_defaults(self):
        import yaml
        from global_settings import DEFAULTS
        path = PROJECT_ROOT / "global_settings.defaults.yaml"
        loaded = yaml.safe_load(path.read_text()) or {}
        for key in ("notebook_cache_enabled", "max_notebook_cache_gb"):
            assert key in loaded, f"{key} missing from defaults.yaml"
            assert loaded[key] == DEFAULTS[key]

    def test_validator_bool_setting(self):
        from global_settings import _validate_key, DEFAULTS
        # Bool - string rejected
        err = _validate_key("notebook_cache_enabled", "true", DEFAULTS)
        assert err is not None and "true or false" in err
        # Actual booleans pass
        assert _validate_key("notebook_cache_enabled", True, DEFAULTS) is None
        assert _validate_key("notebook_cache_enabled", False, DEFAULTS) is None

    def test_validator_float_setting_bounds(self):
        from global_settings import _validate_key, DEFAULTS
        # Below floor (0.1) rejected
        err = _validate_key("max_notebook_cache_gb", 0.05, DEFAULTS)
        assert err is not None and "0.1" in err
        # Above ceiling (100) rejected
        err = _validate_key("max_notebook_cache_gb", 200.0, DEFAULTS)
        assert err is not None and "100" in err
        # Bool rejected
        err = _validate_key("max_notebook_cache_gb", True, DEFAULTS)
        assert err is not None
        # In-range OK
        assert _validate_key("max_notebook_cache_gb", 1.0, DEFAULTS) is None
        assert _validate_key("max_notebook_cache_gb", 0.5, DEFAULTS) is None
        assert _validate_key("max_notebook_cache_gb", 50.0, DEFAULTS) is None

    def test_ui_inputs_present(self):
        ui_html = (
            PROJECT_ROOT / "desktop_app" / "ui.html"
        ).read_text()
        assert "set-notebook-cache-enabled" in ui_html
        assert "set-max-notebook-cache-gb" in ui_html

    def test_js_settings_map_wires_both_keys(self):
        ui_html = (
            PROJECT_ROOT / "desktop_app" / "ui.html"
        ).read_text()
        assert "'notebook_cache_enabled'" in ui_html
        assert "'max_notebook_cache_gb'" in ui_html


# ═══════════════════════════════════════════════════════════════════
# 7. User-data drift guards (4-layer for the cache tree + sqlite)
# ═══════════════════════════════════════════════════════════════════

class TestCacheUserDataDriftGuards:
    """The cache tree is regenerable, so it doesn't go in default
    backups (DIR_TARGETS_HASHED). It IS in DIR_TARGETS_SUMMARIZED so
    backup-tool stats + the install.sh drift guard work.

    All four layers (gitignore / persistence / docker-compose /
    install.sh) must agree - same five-place principle scaled for a
    regenerable-cache tree.
    """

    def test_gitignore_excludes_cache_tree(self):
        gi = (PROJECT_ROOT / ".gitignore").read_text()
        assert "/notebook_cache/" in gi, (
            ".gitignore must exclude the cache tree."
        )

    def test_persistence_targets_summarized_for_cache(self):
        # Cache tree is regenerable; SUMMARIZED is correct (the same
        # bucket as `indexes`, `jobs`, etc.). NOT in DIR_TARGETS_HASHED
        # (no per-file backup of pickle payloads).
        from tools.persistence import (
            DIR_TARGETS_HASHED, DIR_TARGETS_SUMMARIZED,
        )
        assert "notebook_cache" in DIR_TARGETS_SUMMARIZED
        assert "notebook_cache" not in DIR_TARGETS_HASHED

    def test_docker_compose_bind_mounts_cache_dir(self):
        compose = (
            PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
        ).read_text()
        assert "../notebook_cache:/app/notebook_cache" in compose, (
            "docker-compose.yml must bind-mount the cache dir so "
            "container rebuilds preserve iteration savings."
        )

    def test_docker_compose_bind_mounts_sqlite(self):
        compose = (
            PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
        ).read_text()
        assert (
            "../notebook_cache.sqlite:/app/notebook_cache.sqlite"
            in compose
        ), (
            "docker-compose.yml must bind-mount the cache index DB."
        )

    def test_install_sh_mkdir_creates_cache_dir(self):
        install = (PROJECT_ROOT / "install.sh").read_text()
        assert '"$PROJECT_ROOT/notebook_cache"' in install, (
            "install.sh `mkdir -p` block must create notebook_cache/."
        )

    def test_install_sh_touch_creates_sqlite(self):
        install = (PROJECT_ROOT / "install.sh").read_text()
        assert '"$PROJECT_ROOT/notebook_cache.sqlite"' in install, (
            "install.sh must touch notebook_cache.sqlite before docker compose."
        )

    def test_user_data_yaml_actually_ignored_by_git(self):
        # Realistic check: ask git directly whether the cache dir is ignored.
        sentinel = PROJECT_ROOT / "notebook_cache" / "_drift_guard_sentinel.pkl"
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"sentinel")
            try:
                proc = subprocess.run(
                    ["git", "check-ignore", str(sentinel)],
                    cwd=PROJECT_ROOT, capture_output=True, text=True,
                )
            except FileNotFoundError:
                pytest.skip("git CLI not available")
            assert proc.returncode == 0, (
                "notebook_cache/ contents must be gitignored. "
                f"check-ignore returned rc={proc.returncode}."
            )
        finally:
            if sentinel.exists():
                sentinel.unlink()
