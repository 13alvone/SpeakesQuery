"""
Tests for the /api/notebooks/* REST endpoints (Phase 3 / Bet 4 slice 4).

The slice-4 API exposes the slice-1 store + slice-2 engine + slice-3
cache through HTTP for the SPA. These tests use Flask's test client
+ the same `isolated_*` fixture pattern as slices 1-3:

  * Per-test isolation of NOTEBOOKS_DIR / DEFAULTS_DIR (notebook_store)
  * Per-test isolation of cache DB + payload dir (notebook_cache_store)
  * Each endpoint pinned: happy path, missing-resource (404), invalid
    input (400), and (where applicable) cache-state contract

Test layout:
  * TestNotebookList - GET /api/notebooks
  * TestNotebookGet - GET /api/notebooks/<id>
  * TestNotebookCreate - POST /api/notebooks
  * TestNotebookUpdate - PUT /api/notebooks/<id>
  * TestNotebookDelete - DELETE /api/notebooks/<id>
  * TestNotebookExecute - POST /api/notebooks/<id>/execute
  * TestCacheStats - GET /api/notebooks/_cache/stats
  * TestCacheClear - POST /api/notebooks/_cache/clear
  * TestInstallDefault - POST /api/notebooks/_install_default/<id>
  * TestEndpointDriftGuards - every documented route exists in server.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import notebook_cache_store
import notebook_store


PROJECT_ROOT = Path(__file__).parent.parent


# ── Shared fixtures ────────────────────────────────────────────────

@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Redirect both store + cache to tmp_path; reset singletons."""
    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()
    nb_dir = tmp_path / "notebooks"
    df_dir = tmp_path / "default_notebooks"
    cache_db = tmp_path / "notebook_cache.sqlite"
    cache_payloads = tmp_path / "notebook_cache"
    nb_dir.mkdir()
    df_dir.mkdir()
    monkeypatch.setattr(notebook_store, "NOTEBOOKS_DIR", nb_dir)
    monkeypatch.setattr(notebook_store, "DEFAULTS_DIR", df_dir)
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_DB_PATH", cache_db,
    )
    monkeypatch.setattr(
        notebook_cache_store, "DEFAULT_PAYLOAD_DIR", cache_payloads,
    )
    yield {
        "nb_dir": nb_dir, "df_dir": df_dir,
        "cache_db": cache_db, "cache_payloads": cache_payloads,
    }
    notebook_store.reset_for_tests()
    notebook_cache_store.reset_for_tests()


@pytest.fixture
def client(isolated_stores):
    """Flask test client backed by a clean store + cache."""
    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _sample(nid="nb1"):
    return {
        "id": nid,
        "schema_version": 1,
        "name": f"Notebook {nid}",
        "description": "A test notebook.",
        "default_max_cost_usd": 0.0,
        "cells": [
            {"id": "cell_1", "type": "python", "source": "x = 5\nx", "metadata": {}},
            {"id": "cell_2", "type": "python", "source": "y = x * 2\ny", "metadata": {}},
        ],
    }


# ═══════════════════════════════════════════════════════════════════
# 1. GET /api/notebooks
# ═══════════════════════════════════════════════════════════════════

class TestNotebookList:
    def test_empty_list_when_no_notebooks(self, client):
        resp = client.get("/api/notebooks")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        assert body["notebooks"] == []

    def test_list_returns_summary_fields(self, client):
        # Create one via POST, then list
        client.post(
            "/api/notebooks", json=_sample("alpha"),
        )
        resp = client.get("/api/notebooks")
        body = resp.get_json()
        assert resp.status_code == 200
        assert body["status"] == "success"
        assert len(body["notebooks"]) == 1
        nb = body["notebooks"][0]
        # Summary fields only - no `cells` payload in list view
        assert nb["id"] == "alpha"
        assert nb["name"] == "Notebook alpha"
        assert nb["cell_count"] == 2
        assert "created_at" in nb
        assert "updated_at" in nb
        assert "cells" not in nb  # heavy field excluded


# ═══════════════════════════════════════════════════════════════════
# 2. GET /api/notebooks/<id>
# ═══════════════════════════════════════════════════════════════════

class TestNotebookGet:
    def test_get_returns_full_record(self, client):
        client.post("/api/notebooks", json=_sample("foo"))
        resp = client.get("/api/notebooks/foo")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        nb = body["notebook"]
        assert nb["id"] == "foo"
        assert len(nb["cells"]) == 2
        assert nb["cells"][0]["type"] == "python"

    def test_get_missing_returns_404(self, client):
        resp = client.get("/api/notebooks/nonexistent")
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["status"] == "error"
        assert "not found" in body["message"].lower()

    def test_get_invalid_id_returns_404(self, client):
        # The store treats invalid ids as "missing" gracefully.
        resp = client.get("/api/notebooks/with%20space")
        # Either 404 (missing) or treated as not found
        assert resp.status_code in (404, 400)


# ═══════════════════════════════════════════════════════════════════
# 3. POST /api/notebooks (create)
# ═══════════════════════════════════════════════════════════════════

class TestNotebookCreate:
    def test_create_success(self, client):
        resp = client.post("/api/notebooks", json=_sample("alpha"))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        assert body["notebook"]["id"] == "alpha"

    def test_create_missing_id_returns_400(self, client):
        resp = client.post("/api/notebooks", json={"cells": []})
        assert resp.status_code == 400
        assert resp.get_json()["status"] == "error"

    def test_create_duplicate_returns_409(self, client):
        client.post("/api/notebooks", json=_sample("dup"))
        resp = client.post("/api/notebooks", json=_sample("dup"))
        assert resp.status_code == 409
        assert resp.get_json()["status"] == "exists"

    def test_create_with_overwrite_replaces(self, client):
        client.post("/api/notebooks", json=_sample("rep"))
        new_data = {**_sample("rep"), "description": "REPLACED", "overwrite": True}
        resp = client.post("/api/notebooks", json=new_data)
        assert resp.status_code == 200
        # GET back to verify
        body = client.get("/api/notebooks/rep").get_json()
        assert body["notebook"]["description"] == "REPLACED"

    def test_create_invalid_id_returns_400(self, client):
        bad = {**_sample("bad"), "id": "../etc/passwd"}
        resp = client.post("/api/notebooks", json=bad)
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════
# 4. PUT /api/notebooks/<id>
# ═══════════════════════════════════════════════════════════════════

class TestNotebookUpdate:
    def test_update_success(self, client):
        client.post("/api/notebooks", json=_sample("u1"))
        resp = client.put(
            "/api/notebooks/u1",
            json={"description": "updated description"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["notebook"]["description"] == "updated description"

    def test_update_missing_returns_404(self, client):
        resp = client.put(
            "/api/notebooks/nonexistent",
            json={"description": "x"},
        )
        assert resp.status_code == 404

    def test_update_replaces_cells_wholesale(self, client):
        client.post("/api/notebooks", json=_sample("u2"))
        new_cells = [{"id": "only", "type": "python", "source": "1", "metadata": {}}]
        resp = client.put("/api/notebooks/u2", json={"cells": new_cells})
        assert resp.status_code == 200
        assert len(resp.get_json()["notebook"]["cells"]) == 1


# ═══════════════════════════════════════════════════════════════════
# 5. DELETE /api/notebooks/<id>
# ═══════════════════════════════════════════════════════════════════

class TestNotebookDelete:
    def test_delete_success(self, client):
        client.post("/api/notebooks", json=_sample("d1"))
        resp = client.delete("/api/notebooks/d1")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "success"
        # Verify gone
        assert client.get("/api/notebooks/d1").status_code == 404

    def test_delete_missing_returns_404(self, client):
        resp = client.delete("/api/notebooks/nonexistent")
        assert resp.status_code == 404

    def test_delete_cascades_to_cache(self, client, isolated_stores):
        # Create + execute (populates cache)
        client.post("/api/notebooks", json=_sample("dc1"))
        client.post("/api/notebooks/dc1/execute", json={})
        cache = notebook_cache_store.NotebookCacheStore(
            db_path=isolated_stores["cache_db"],
            payload_dir=isolated_stores["cache_payloads"],
        )
        assert cache.count() > 0
        # Delete
        resp = client.delete("/api/notebooks/dc1")
        body = resp.get_json()
        assert body["status"] == "success"
        # Cache entries for this notebook are gone
        cache2 = notebook_cache_store.NotebookCacheStore(
            db_path=isolated_stores["cache_db"],
            payload_dir=isolated_stores["cache_payloads"],
        )
        assert cache2.count() == 0


# ═══════════════════════════════════════════════════════════════════
# 6. POST /api/notebooks/<id>/execute
# ═══════════════════════════════════════════════════════════════════

class TestNotebookExecute:
    def test_execute_returns_run_result(self, client):
        client.post("/api/notebooks", json=_sample("ex1"))
        resp = client.post("/api/notebooks/ex1/execute", json={})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        result = body["result"]
        # Run-result shape
        assert result["notebook_id"] == "ex1"
        assert len(result["cells"]) == 2
        assert result["success_count"] == 2
        assert result["error_count"] == 0
        assert "cache_hits" in result
        assert "total_runtime_ms" in result

    def test_execute_missing_returns_404(self, client):
        resp = client.post("/api/notebooks/nonexistent/execute", json={})
        assert resp.status_code == 404

    def test_execute_cache_hit_signature(self, client):
        client.post("/api/notebooks", json=_sample("eh"))
        # First run populates cache
        first = client.post("/api/notebooks/eh/execute", json={}).get_json()
        assert first["result"]["cache_hits"] == 0
        # Second run hits cache for both cells
        second = client.post("/api/notebooks/eh/execute", json={}).get_json()
        assert second["result"]["cache_hits"] == 2
        for cell in second["result"]["cells"]:
            assert cell["cache_hit"] is True
            assert cell["runtime_ms"] == 0

    def test_execute_use_cache_false_disables_caching(self, client):
        client.post("/api/notebooks", json=_sample("nc"))
        # Populate
        client.post("/api/notebooks/nc/execute", json={})
        # Run with use_cache=False - should re-execute
        body = client.post(
            "/api/notebooks/nc/execute", json={"use_cache": False},
        ).get_json()
        assert body["result"]["cache_hits"] == 0
        for cell in body["result"]["cells"]:
            assert cell["cache_hit"] is False


# ═══════════════════════════════════════════════════════════════════
# 7. Cache stats / clear
# ═══════════════════════════════════════════════════════════════════

class TestCacheStats:
    def test_stats_initial(self, client):
        resp = client.get("/api/notebooks/_cache/stats")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        assert body["stats"]["entries"] == 0
        assert body["stats"]["size_bytes"] == 0

    def test_stats_after_run(self, client):
        client.post("/api/notebooks", json=_sample("ts"))
        client.post("/api/notebooks/ts/execute", json={})
        resp = client.get("/api/notebooks/_cache/stats")
        body = resp.get_json()
        assert body["stats"]["entries"] == 2  # two cells cached


class TestCacheClear:
    def test_clear_empties_cache(self, client):
        client.post("/api/notebooks", json=_sample("tc"))
        client.post("/api/notebooks/tc/execute", json={})
        # Verify populated
        stats_before = client.get(
            "/api/notebooks/_cache/stats"
        ).get_json()["stats"]
        assert stats_before["entries"] > 0
        # Clear
        resp = client.post("/api/notebooks/_cache/clear")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        assert body["bytes_freed"] >= 0
        # Verify empty
        stats_after = client.get(
            "/api/notebooks/_cache/stats"
        ).get_json()["stats"]
        assert stats_after["entries"] == 0


# ═══════════════════════════════════════════════════════════════════
# 8. /api/notebooks/_install_default/<id>
# ═══════════════════════════════════════════════════════════════════

class TestInstallDefault:
    def test_install_default_no_match_returns_skipped(self, client):
        resp = client.post(
            "/api/notebooks/_install_default/no_such_default", json={},
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "skipped"

    def test_install_default_succeeds_when_default_exists(
        self, client, isolated_stores,
    ):
        # Drop a default into the defaults dir
        import yaml as _yaml
        default_path = isolated_stores["df_dir"] / "shipped.spqnb"
        default_path.write_text(
            _yaml.dump(_sample("shipped"), default_flow_style=False, sort_keys=False),
        )
        resp = client.post(
            "/api/notebooks/_install_default/shipped", json={},
        )
        body = resp.get_json()
        # initialize() may have already seeded it during the singleton
        # bootstrap; either "success" (newly installed) or "skipped"
        # (already present) is acceptable.
        assert resp.status_code == 200
        assert body["status"] in ("success", "skipped")
        # Either way, the notebook is now visible
        listing = client.get("/api/notebooks").get_json()
        assert any(n["id"] == "shipped" for n in listing["notebooks"])


# ═══════════════════════════════════════════════════════════════════
# 9. Endpoint drift guards
# ═══════════════════════════════════════════════════════════════════

class TestEndpointDriftGuards:
    """Pin every documented route's existence in server.py source. If
    a future contributor renames or removes an endpoint, the SPA breaks
    silently without these guards.
    """

    def _server_source(self) -> str:
        return (
            PROJECT_ROOT / "desktop_app" / "server.py"
        ).read_text(encoding="utf-8")

    def test_all_routes_registered(self):
        src = self._server_source()
        for route, methods in [
            ("/api/notebooks", ("GET", "POST")),
            ("/api/notebooks/<notebook_id>", ("GET", "PUT", "DELETE")),
            ("/api/notebooks/<notebook_id>/execute", ("POST",)),
            ("/api/notebooks/_cache/stats", ("GET",)),
            ("/api/notebooks/_cache/clear", ("POST",)),
            ("/api/notebooks/_install_default/<notebook_id>", ("POST",)),
        ]:
            for method in methods:
                pattern = (
                    rf'@app\.route\(\s*["\']' + re.escape(route)
                    + r'["\'][^)]*methods=\[[^\]]*["\']' + method
                    + r'["\']'
                )
                assert re.search(pattern, src), (
                    f"Route {route!r} method {method!r} not found in "
                    "server.py - slice-4 contract broken."
                )

    def test_engine_singleton_present(self):
        src = self._server_source()
        assert "_notebook_engine" in src, (
            "server.py should hold a NotebookEngine singleton "
            "(_notebook_engine) for the /api/notebooks/<id>/execute path."
        )
