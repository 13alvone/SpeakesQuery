"""
Tests for the Wave 2 (2026-04-25) extension to
``/api/alert-groups/<name>/deploy-feeders``: chained Install → Deploy →
Run-now per task.

Background
----------
The original endpoint only deployed library scripts as scheduled
ingestion tasks. The user then ran Pipeline Check, saw 0 rows for
every feeder (because the cron hadn't fired yet), and assumed the AG
was broken. Wave 2 closes that loop by chaining ``run_task_now`` on
every newly-deployed task plus any feeder that's already deployed but
still has zero parquet (state=``pending``).

Coverage
--------
* Default ``run_after_deploy=true`` runs each newly-deployed task and
  reports per-task results in ``runs[]``.
* ``?run_after_deploy=false`` keeps the old deploy-only behaviour
  (empty ``runs[]``).
* Pre-existing pending tasks (deployed earlier, no data yet) also get
  picked up by the run-now phase.
* Run failures land in ``runs[]`` with ``status=failed`` and a
  human-readable ``error_message`` (don't lose them silently).
* ``max_run_workers`` is clamped to [1, 8].
* The frontend-side cross-tab nav helper has its data-attribute
  contract pinned: an ingestion task row must carry ``data-si-task-id``
  matching what ``navigateToIngestionTask()`` queries.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml as _yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def deploy_run_setup(tmp_path):
    """Spin up the Flask client + isolated AG/SS stores + a stub library
    script, plus the matching saved-search default template.

    Uses pytest's built-in ``tmp_path`` rather than the project's own
    ``tmp_dir`` fixture (defined in tests/test_alert_groups.py and not
    shared via conftest), so this file stays standalone and runnable
    without dragging in the larger test_alert_groups.py state."""
    from scheduled_input_engine import start_engine
    start_engine()

    from desktop_app.server import app, _ag_store, _ss_store

    orig_ag_dir = _ag_store._dir
    orig_ag_db = _ag_store._db
    orig_ag_runs = _ag_store._runs_db
    orig_ss_dir = _ss_store._dir
    orig_ss_defaults = _ss_store._defaults_dir

    _ag_store._dir = tmp_path / "ag"
    _ag_store._db = str(tmp_path / "lc.sqlite")
    _ag_store._runs_db = str(tmp_path / "runs.sqlite")
    _ag_store.initialize()

    _ss_store._dir = tmp_path / "ss"
    _ss_store._defaults_dir = tmp_path / "ss_defaults"
    _ss_store._dir.mkdir(parents=True, exist_ok=True)
    _ss_store._defaults_dir.mkdir(parents=True, exist_ok=True)

    feed_name = "ag_drct_feed_one"
    template = {
        "name": feed_name,
        "query": 'index="indexes/drctest/feed_one/*.parquet" | head 1',
        "cron_schedule": "*/30 * * * *",
        "lookback": "-1h",
        "trigger": "once",
        "email_address": "noreply@speakesquery.local",
        "send_email": "no",
        "disabled": False,
    }
    (_ss_store._defaults_dir / f"{feed_name}.yaml").write_text(
        _yaml.safe_dump(template)
    )

    stub_script = {
        "id": "drctest_feed_one",
        "title": "Deploy-Run Chain test feed",
        "description": "",
        "suggested_cron": "*/30 * * * *",
        "suggested_subdirectory": "drctest/feed_one",
        "suggested_overwrite": False,
        "api_url": "https://example.invalid/test",
        "code": (
            "import pandas as pd\n"
            "GENERATE_RESULTS(pd.DataFrame({'_epoch':[0]}))"
        ),
        "requires_credentials": [],
        "trust_level": "sandboxed",
        "tags": [],
    }

    app.config["TESTING"] = True
    client = app.test_client()

    try:
        yield {
            "client": client,
            "feed_name": feed_name,
            "stub_script": stub_script,
        }
    finally:
        _ag_store._dir = orig_ag_dir
        _ag_store._db = orig_ag_db
        _ag_store._runs_db = orig_ag_runs
        _ag_store.initialize()
        _ss_store._dir = orig_ss_dir
        _ss_store._defaults_dir = orig_ss_defaults
        # Clean up scheduled tasks the test created.
        from scheduled_input_engine import get_engine
        try:
            for t in get_engine().store.list_scheduled_inputs():
                if (t.get("subdirectory") or "").startswith("drctest/"):
                    get_engine().delete_task(t["id"])
        except Exception:
            pass


def _create_ag(client, name, search_names):
    return client.post(
        "/api/alert-groups/create",
        data=json.dumps({
            "name": name,
            "search_names": search_names,
            "prompt_text": "Analyze these results",
        }),
        content_type="application/json",
    )


# ── Default chain (install + deploy + run) ───────────────────────────────
class TestRunAfterDeployDefault:
    def test_runs_array_populated_for_newly_deployed_task(
        self, deploy_run_setup
    ):
        """Default behaviour: every task created by deploy gets a
        run_task_now call, and the result lands in runs[]."""
        ctx = deploy_run_setup
        _create_ag(ctx["client"], "drct_default", [ctx["feed_name"]])

        fake_run = {
            "task_id": 999,
            "status": "success",
            "rows_inserted": 42,
            "runtime": 0.31,
            "error_message": None,
        }
        with patch("desktop_app.server._list_library_scripts",
                   return_value=[ctx["stub_script"]]), \
             patch("desktop_app.server._get_library_script",
                   return_value=ctx["stub_script"]), \
             patch(
                "scheduled_input_engine.engine.ScheduledInputEngine"
                ".run_task_now", return_value=fake_run):
            resp = ctx["client"].post(
                "/api/alert-groups/drct_default/deploy-feeders"
            )
        data = json.loads(resp.data)
        assert resp.status_code == 200, data
        assert data["status"] == "success"
        assert data["ran_after_deploy"] is True
        assert len(data["installed"]) == 1
        assert len(data["deployed"]) == 1
        assert len(data["runs"]) == 1, data
        run_entry = data["runs"][0]
        assert run_entry["search_name"] == ctx["feed_name"]
        assert run_entry["trigger_reason"] == "newly_deployed"
        assert run_entry["run"]["status"] == "success"
        assert run_entry["run"]["rows_inserted"] == 42
        assert run_entry["skipped"] is False

    def test_run_failure_surfaced_in_runs(self, deploy_run_setup):
        """A run that raises during run_task_now must land in runs[]
        with status=failed and the exception class + message preserved
        - never silently dropped."""
        ctx = deploy_run_setup
        _create_ag(ctx["client"], "drct_runfail", [ctx["feed_name"]])

        with patch("desktop_app.server._list_library_scripts",
                   return_value=[ctx["stub_script"]]), \
             patch("desktop_app.server._get_library_script",
                   return_value=ctx["stub_script"]), \
             patch(
                "scheduled_input_engine.engine.ScheduledInputEngine"
                ".run_task_now",
                side_effect=RuntimeError("simulated network blowup")):
            resp = ctx["client"].post(
                "/api/alert-groups/drct_runfail/deploy-feeders"
            )
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert len(data["runs"]) == 1
        run_entry = data["runs"][0]
        assert run_entry["run"]["status"] == "failed"
        assert "RuntimeError" in run_entry["run"]["error_message"]
        assert "simulated network blowup" in run_entry["run"]["error_message"]


# ── Opt-out: ?run_after_deploy=false keeps deploy-only behaviour ─────────
class TestRunAfterDeployOptOut:
    def test_run_after_deploy_false_returns_empty_runs(
        self, deploy_run_setup
    ):
        ctx = deploy_run_setup
        _create_ag(ctx["client"], "drct_optout", [ctx["feed_name"]])

        with patch("desktop_app.server._list_library_scripts",
                   return_value=[ctx["stub_script"]]), \
             patch("desktop_app.server._get_library_script",
                   return_value=ctx["stub_script"]), \
             patch(
                "scheduled_input_engine.engine.ScheduledInputEngine"
                ".run_task_now") as run_mock:
            resp = ctx["client"].post(
                "/api/alert-groups/drct_optout/deploy-feeders"
                "?run_after_deploy=false"
            )
            assert run_mock.call_count == 0, (
                "run_task_now must not be called when "
                "run_after_deploy=false"
            )
        data = json.loads(resp.data)
        assert data["status"] == "success"
        assert data["ran_after_deploy"] is False
        assert data["runs"] == []
        # Deploy still happened
        assert len(data["deployed"]) == 1


# ── max_run_workers clamping ─────────────────────────────────────────────
class TestMaxRunWorkersClamp:
    def test_max_run_workers_above_eight_is_clamped(
        self, deploy_run_setup
    ):
        """max_run_workers=99 must clamp to 8 so a hostile/typo'd query
        param can't spawn an unbounded thread pool."""
        ctx = deploy_run_setup
        _create_ag(ctx["client"], "drct_clamp", [ctx["feed_name"]])

        captured = {}

        def _fake_run(self, _tid):
            return {
                "task_id": _tid, "status": "success",
                "rows_inserted": 0, "runtime": 0.01,
                "error_message": None,
            }

        with patch("desktop_app.server._list_library_scripts",
                   return_value=[ctx["stub_script"]]), \
             patch("desktop_app.server._get_library_script",
                   return_value=ctx["stub_script"]), \
             patch("concurrent.futures.ThreadPoolExecutor") as pool_cls, \
             patch(
                "scheduled_input_engine.engine.ScheduledInputEngine"
                ".run_task_now", autospec=True, side_effect=_fake_run):
            pool_cls.return_value.__enter__.return_value.submit \
                .return_value.result.return_value = {
                    "search_name": ctx["feed_name"],
                    "task_id": 1,
                    "trigger_reason": "newly_deployed",
                    "run": {"status": "success", "rows_inserted": 0,
                            "runtime": 0.01, "error_message": None},
                    "skipped": False,
                }
            # as_completed needs to yield our mock futures
            from unittest.mock import MagicMock
            mock_future = MagicMock()
            mock_future.result.return_value = {
                "search_name": ctx["feed_name"],
                "task_id": 1,
                "trigger_reason": "newly_deployed",
                "run": {"status": "success", "rows_inserted": 0,
                        "runtime": 0.01, "error_message": None},
                "skipped": False,
            }
            pool_cls.return_value.__enter__.return_value.submit \
                .return_value = mock_future
            with patch("concurrent.futures.as_completed",
                       return_value=[mock_future]):
                ctx["client"].post(
                    "/api/alert-groups/drct_clamp/deploy-feeders"
                    "?max_run_workers=99"
                )
            captured["max_workers"] = pool_cls.call_args.kwargs.get(
                "max_workers"
            )
        assert captured["max_workers"] == 8, (
            f"max_run_workers=99 must clamp to 8; got "
            f"{captured['max_workers']}"
        )


# ── Frontend regression: data-si-task-id attribute is wired ──────────────
class TestNavigationContract:
    """The Wave 2 cross-tab navigation helper depends on the ingestion
    table tagging each row with ``data-si-task-id="<task.id>"``. If
    someone removes that line in the renderer (or the helper's query
    selector), the Pipeline Check "Go to ingestion task →" button
    silently fails to find the row. Pin the contract on both sides.
    """

    def _ui_text(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_ingestion_table_renderer_sets_data_si_task_id(self):
        ui = self._ui_text()
        # Renderer side: sets the attribute via dataset.siTaskId
        assert "tr.dataset.siTaskId = String(task.id)" in ui, (
            "Ingestion table renderer must tag each row with "
            "data-si-task-id; the Wave 2 cross-tab nav from Feeder "
            "Health depends on it."
        )

    def test_navigate_helper_queries_data_si_task_id(self):
        ui = self._ui_text()
        # Helper side: queries via the matching CSS selector
        assert 'tr[data-si-task-id="${taskId}"]' in ui, (
            "navigateToIngestionTask() must select rows by "
            "data-si-task-id; if you change one side, change both."
        )

    def test_zero_row_handler_calls_run_now_endpoint(self):
        ui = self._ui_text()
        assert "/api/si/${encodeURIComponent(taskId)}/run" in ui, (
            "Pipeline Check 'Run ingestion now' button must POST to "
            "/api/si/<id>/run (existing endpoint)."
        )
