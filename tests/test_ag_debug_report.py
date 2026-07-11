#!/usr/bin/env python3
"""
Tests for the AG Debug Report endpoint + UI contract - 2026-04-30.

User asked for a "Debug" button per Alert Group that runs every saved
search referenced by the AG and produces one pasteable report (with a
Claude prompt prefix on top) so the operator can iterate on query
quality without building special diagnostic tooling - the output piggy-
backs the existing query-execution path.

Scope:
- Endpoint contract (`/api/alert-groups/<name>/debug-report`)
- Response shape (`searches`, `summary`, `report_text`)
- Per-search status taxonomy (ok / empty / error / missing)
- Prompt prefix is present and contains the iteration directives
- Result truncation (cap at 50 rows per search)
- UI HTML contract (button + modal IDs + copy-all wiring)

Does NOT test:
- Claude API calls (the endpoint MUST NOT call Claude - pure diagnostic)
- Email sending (same)
- Long-running query timeout behaviour (would require a slow query
  fixture; skipped here, would belong in a perf test pack)
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)


# ===========================================================================
# Backend endpoint tests
# ===========================================================================


@pytest.fixture
def client():
    """Flask test client with a fresh app instance."""
    from desktop_app.server import app
    app.testing = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def isolated_ag_with_search(tmp_path, monkeypatch):
    """Create an isolated AG + saved search pair the test owns. The AG
    references the search by name. Both stores are pointed at temp dirs
    so the test never touches the user's live config."""
    from alert_group_store import AlertGroupStore
    from saved_search_store import SavedSearchStore
    import desktop_app.server as srv

    # ── AG store ─────────────────────────────────────────────────
    empty_ag_defaults = tmp_path / "_empty_default_alert_groups"
    empty_ag_defaults.mkdir()
    ag_store = AlertGroupStore()
    ag_store._dir = tmp_path / "alert_groups"
    ag_store._defaults_dir = empty_ag_defaults
    ag_store._db = str(tmp_path / "last_chance.sqlite")
    ag_store._runs_db = str(tmp_path / "alert_group_runs.sqlite")
    ag_store.initialize()

    # ── SS store ─────────────────────────────────────────────────
    ss_store = SavedSearchStore()
    ss_store._dir = tmp_path / "saved_searches"
    ss_store._defaults_dir = tmp_path / "_empty_default_saved_searches"
    ss_store._defaults_dir.mkdir()
    ss_store._db = str(tmp_path / "ss_last_chance.sqlite")
    ss_store.initialize()

    # Patch the server's module-level singletons so the endpoint sees
    # our isolated stores
    monkeypatch.setattr(srv, "_ag_store", ag_store)
    # The endpoint constructs its own SavedSearchStore() inside the route
    # body - patch the class itself so any new instance reads our temp dirs
    import saved_search_store as ss_mod
    original_ss_class = ss_mod.SavedSearchStore

    class _PatchedSS(original_ss_class):
        def __init__(self_inner):
            super().__init__()
            self_inner._dir = ss_store._dir
            self_inner._defaults_dir = ss_store._defaults_dir
            self_inner._db = ss_store._db

    monkeypatch.setattr(ss_mod, "SavedSearchStore", _PatchedSS)
    return ag_store, ss_store


def _save_search(ss_store, name, query, description=""):
    """Helper: save a saved search via the store API. Uses an alert-group
    feeder purpose so we don't have to satisfy email-validation rules."""
    ss_store.save_search({
        "name": name,
        "description": description or f"Test search {name}",
        "query": query,
        "purpose": "alert_group_feeder",
        "cron_schedule": "0 12 * * *",  # required, must be valid
        "lookback": "-1d",              # required, must match -<n><unit>
        "send_email": "no",
        "email_address": "noreply@speakesquery.local",
        "max_rows_per_alert": 50,
    })


def _save_ag(ag_store, name, search_names):
    """Helper: save an AG referencing the named searches."""
    ag_store.save_group({
        "name": name,
        "description": f"Test AG {name}",
        "search_names": list(search_names),
        "prompt_text": "Summarize the data in 1 sentence.",
        "schedule": "",
        "max_rows": 10,
        "email_address": "test@example.com",
        "disabled": False,
    })


class TestEndpointContract:
    """The endpoint exists, returns the expected shape, handles common errors."""

    def test_404_for_missing_ag(self, client, isolated_ag_with_search):
        resp = client.post("/api/alert-groups/nonexistent_ag/debug-report")
        assert resp.status_code == 404

    def test_400_for_ag_without_searches(self, client, isolated_ag_with_search):
        """If an AG yaml somehow has no search_names (e.g. legacy YAML edited
        on disk, or future schema migration), the endpoint must reject the
        request with 400 - not crash and not return an empty success."""
        ag_store, _ = isolated_ag_with_search
        # The AG validator rejects empty search_names, so we can't construct
        # this state via save_group. Write the YAML directly to bypass.
        import yaml
        yaml_path = ag_store._dir / "empty_ag.yaml"
        yaml_path.write_text(yaml.dump({
            "name": "empty_ag",
            "description": "Pretend a legacy YAML had no searches",
            "search_names": [],
            "prompt_text": "summarize.",
            "schedule": "",
            "max_rows": 10,
            "email_address": "test@example.com",
            "disabled": False,
        }))

        resp = client.post("/api/alert-groups/empty_ag/debug-report")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "no saved searches" in (data.get("message") or "").lower()

    def test_response_shape(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        _save_search(
            ss_store, "test_search_ok",
            'index="archive/system_logs/system4.parquet" | head 5',
        )
        _save_ag(ag_store, "test_ag", ["test_search_ok"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        assert resp.status_code == 200
        data = resp.get_json()
        # Required top-level keys
        for key in ("status", "ag_name", "generated_at", "summary",
                    "searches", "report_text"):
            assert key in data, f"Missing top-level key: {key}"
        assert data["status"] == "success"
        assert data["ag_name"] == "test_ag"
        # Summary required keys
        for key in ("total_searches", "ok", "empty", "error",
                    "missing", "total_rows"):
            assert key in data["summary"]
        assert data["summary"]["total_searches"] == 1


class TestPerSearchStatusTaxonomy:
    """Each saved search yields one of: ok / empty / error / missing."""

    def test_ok_status_for_query_with_results(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        _save_search(
            ss_store, "ok_search",
            'index="archive/system_logs/system4.parquet" | head 5',
        )
        _save_ag(ag_store, "test_ag", ["ok_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        assert data["summary"]["ok"] == 1
        assert data["searches"][0]["status"] == "ok"
        assert data["searches"][0]["row_count"] == 5
        assert len(data["searches"][0]["columns"]) > 0
        assert len(data["searches"][0]["sample_rows"]) == 5

    def test_empty_status_for_zero_row_query(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        # Filter that matches nothing
        _save_search(
            ss_store, "empty_search",
            'index="archive/system_logs/system4.parquet" status="this_status_never_exists"',
        )
        _save_ag(ag_store, "test_ag", ["empty_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        assert data["summary"]["empty"] == 1
        assert data["searches"][0]["status"] == "empty"
        assert data["searches"][0]["row_count"] == 0

    def test_error_status_for_garbage_query(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        # Garbage earliest value - TimeBoundParseError will surface
        _save_search(
            ss_store, "error_search",
            'index="archive/system_logs/system4.parquet" earliest="garbge"',
        )
        _save_ag(ag_store, "test_ag", ["error_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        assert data["summary"]["error"] == 1
        assert data["searches"][0]["status"] == "error"
        assert "garbge" in (data["searches"][0]["error"] or "")

    def test_missing_status_for_unreferenced_search(self, client, isolated_ag_with_search):
        ag_store, _ = isolated_ag_with_search
        # AG references a search that doesn't exist
        _save_ag(ag_store, "test_ag", ["search_that_does_not_exist"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        assert data["summary"]["missing"] == 1
        assert data["searches"][0]["status"] == "missing"

    def test_mixed_status_counts(self, client, isolated_ag_with_search):
        """Realistic case: an AG with some OK, some empty, some error."""
        ag_store, ss_store = isolated_ag_with_search
        _save_search(
            ss_store, "ok_one",
            'index="archive/system_logs/system4.parquet" | head 3',
        )
        _save_search(
            ss_store, "empty_one",
            'index="archive/system_logs/system4.parquet" status="never"',
        )
        _save_search(
            ss_store, "error_one",
            'index="archive/system_logs/system4.parquet" earliest="garbge"',
        )
        _save_ag(ag_store, "mixed_ag", [
            "ok_one", "empty_one", "error_one", "missing_one",
        ])

        resp = client.post("/api/alert-groups/mixed_ag/debug-report")
        data = resp.get_json()
        s = data["summary"]
        assert s["ok"] == 1
        assert s["empty"] == 1
        assert s["error"] == 1
        assert s["missing"] == 1
        assert s["total_searches"] == 4


class TestReportTextContract:
    """The report_text the operator copies must contain the prompt prefix
    AND every search's name + SPQL + results section."""

    def test_prompt_prefix_present(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        _save_search(
            ss_store, "any_search",
            'index="archive/system_logs/system4.parquet" | head 1',
        )
        _save_ag(ag_store, "test_ag", ["any_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        report = data["report_text"]
        # Every iteration directive must be present so the user can
        # paste the report straight to Claude without editing it
        for required in (
            "## Prompt for Claude",
            "decision-relevant data",
            "aggregation",
            "time bounds",
            "concrete SPQL improvements",
            "## Debug Data",
        ):
            assert required in report, f"Missing prompt directive: {required!r}"

    def test_each_search_has_its_section(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        for ss_name in ("alpha_search", "beta_search", "gamma_search"):
            _save_search(
                ss_store, ss_name,
                'index="archive/system_logs/system4.parquet" | head 1',
            )
        _save_ag(
            ag_store, "test_ag",
            ["alpha_search", "beta_search", "gamma_search"],
        )

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        report = data["report_text"]
        for ss_name in ("alpha_search", "beta_search", "gamma_search"):
            assert f" - {ss_name}" in report, (
                f"Search {ss_name} not found in report header line"
            )

    def test_spql_logic_present_in_report(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        unique_marker = "TEST_QUERY_MARKER_FOR_REPORT_VERIFICATION"
        _save_search(
            ss_store, "marked_search",
            f'index="archive/system_logs/system4.parquet" '
            f'| eval marker="{unique_marker}" | head 1',
        )
        _save_ag(ag_store, "test_ag", ["marked_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        assert unique_marker in data["report_text"], (
            "The saved search's SPQL is not present in the report text - "
            "operators won't be able to see the query they're iterating on"
        )

    def test_report_includes_summary_line(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        _save_search(
            ss_store, "any_search",
            'index="archive/system_logs/system4.parquet" | head 1',
        )
        _save_ag(ag_store, "test_ag", ["any_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        report = data["report_text"]
        assert "# Summary:" in report
        assert "ok," in report  # the "ok," prefix from the summary line


class TestResultTruncation:
    """Big result sets must be capped to keep reports pasteable."""

    def test_results_capped_at_50_rows_per_search(self, client, isolated_ag_with_search):
        ag_store, ss_store = isolated_ag_with_search
        # system4.parquet has 1000 rows; head 200 returns 200 rows
        _save_search(
            ss_store, "big_search",
            'index="archive/system_logs/system4.parquet" | head 200',
        )
        _save_ag(ag_store, "test_ag", ["big_search"])

        resp = client.post("/api/alert-groups/test_ag/debug-report")
        data = resp.get_json()
        item = data["searches"][0]
        assert item["row_count"] == 200, "Total row count must reflect REAL count"
        assert len(item["sample_rows"]) == 50, (
            "Sample rows MUST be capped at 50 to keep the pasted report "
            "tractable for Claude"
        )
        assert item["truncated"] is True


class TestNoMoneySpent:
    """The endpoint MUST NOT call Claude. It's a pure diagnostic - same
    money-leak audit principle as the disabled-gate canary."""

    def test_endpoint_does_not_call_claude(self, client, isolated_ag_with_search):
        """Patch the billable client; assert it's NEVER invoked by the
        debug-report flow."""
        ag_store, ss_store = isolated_ag_with_search
        _save_search(
            ss_store, "any_search",
            'index="archive/system_logs/system4.parquet" | head 5',
        )
        _save_ag(ag_store, "test_ag", ["any_search"])

        def _fail_loud(*args, **kwargs):
            raise AssertionError(
                "MONEY LEAK: debug-report endpoint called Claude. "
                "It is a PURE DIAGNOSTIC and must never invoke the API."
            )

        with patch("analyzers.claude_client.call_messages_create", _fail_loud):
            resp = client.post("/api/alert-groups/test_ag/debug-report")
        # If the patch had been called, the with-block would have raised.
        assert resp.status_code == 200


# ===========================================================================
# UI HTML contract tests
# ===========================================================================


UI_HTML = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()


class TestDebugButtonAndModalContract:
    """The Debug button + modal IDs + copy-all wiring must stay stable -
    the cross-tab tests + future Selenium runs depend on these contracts."""

    def test_debug_button_present_in_ag_row_render(self):
        assert "debugBtn.textContent = 'Debug'" in UI_HTML, (
            "Debug button text contract changed - operators look for "
            "literally 'Debug' in the AG row action area"
        )
        assert "openAgDebug(g.name)" in UI_HTML

    def test_debug_button_data_attr_for_test_hooks(self):
        assert "debugBtn.dataset.agName" in UI_HTML

    def test_debug_modal_present(self):
        for required_id in (
            "ag-debug-modal",
            "ag-debug-backdrop",
            "ag-debug-title",
            "ag-debug-summary",
            "ag-debug-body",
            "ag-debug-copy",
            "ag-debug-close",
            "ag-debug-copy-feedback",
        ):
            assert f'id="{required_id}"' in UI_HTML, (
                f"Modal element id={required_id!r} missing - UI wiring broken"
            )

    def test_open_handler_defined(self):
        assert "async function openAgDebug(" in UI_HTML

    def test_close_handlers_wired(self):
        assert "function closeAgDebugModal()" in UI_HTML
        # Wired to both the close button and the backdrop click
        assert (
            "document.getElementById('ag-debug-close')\n"
            "    ?.addEventListener('click', closeAgDebugModal)" in UI_HTML
        ) or "ag-debug-close')\n    ?.addEventListener('click', closeAgDebugModal)" in UI_HTML

    def test_copy_button_uses_clipboard_api_with_fallback(self):
        # Modern path: navigator.clipboard.writeText
        assert "navigator.clipboard.writeText" in UI_HTML
        # Fallback path: textarea + execCommand for older browsers
        assert "execCommand('copy')" in UI_HTML

    def test_modal_does_not_render_data_outside_pre_tag(self):
        """The report data lands inside a <pre> so monospace whitespace is
        preserved AND no HTML in the data is interpreted (XSS guard)."""
        assert 'id="ag-debug-body"' in UI_HTML
        # The <pre> with that ID must exist
        assert '<pre id="ag-debug-body"' in UI_HTML
