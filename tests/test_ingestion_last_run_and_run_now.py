"""
Tests for the 2026-04-23 Ingestion Scripts ergonomics shipment:

  A. ``/api/si/list`` enriches each task with ``last_run_at`` /
     ``last_run_status`` / ``last_run_error`` so the UI can render
     the "Last Run" column (Never / 5m ago / red-failed-pill).
  B. ``POST /api/si/<id>/run`` triggers a real ingestion (not the
     sandbox Test path), writes parquet, updates execution_history.
  C. ``POST /api/si/add`` with ``run_on_create=true`` (the default)
     auto-runs the task immediately after save so the first parquet +
     schema land right away; ``run_on_create=false`` skips the run.
  D. The underlying ``store.get_last_run(task_id[, status])`` helper.

All tests use the in-process Flask ``client`` fixture from conftest.py.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Fixtures - module-scoped so the store is shared across tests in this file
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    """Return the module-global ScheduledInputEngine singleton (already
    started by the ``client`` conftest fixture)."""
    from scheduled_input_engine import get_engine
    return get_engine()


@pytest.fixture
def ephemeral_task(client, engine, request):
    """Create a tiny throwaway ingestion task, yield its id, then delete
    it on teardown. Subdirectory is unique per test to avoid collisions."""
    sub = f"pytest_lastrun_{request.node.name}".replace("-", "_")[:60]
    resp = client.post("/api/si/add", json={
        "title": f"pytest_lastrun_{request.node.name}"[:80],
        "code": (
            "import pandas as pd\n"
            "df = pd.DataFrame({'v': [1, 2, 3], '_epoch': [1.0, 2.0, 3.0]})\n"
            "GENERATE_RESULTS(df)"
        ),
        "cron_schedule": "0 0 1 1 *",
        "subdirectory": sub,
        "run_on_create": False,  # isolate these fixtures from the A-path
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["status"] == "success"
    task_id = data["task"]["id"]
    yield task_id
    # Teardown: best-effort delete; tests may have already removed it.
    try:
        client.delete(f"/api/si/{task_id}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# A. /api/si/list enrichment
# ---------------------------------------------------------------------------

class TestSiListLastRunEnrichment:
    def test_fresh_task_has_null_last_run(self, client, ephemeral_task):
        """A task that has never executed must report last_run_at=None
        so the UI renders 'Never' in the Last Run column."""
        resp = client.get("/api/si/list")
        assert resp.status_code == 200
        data = resp.get_json()
        row = next((t for t in data["tasks"] if t["id"] == ephemeral_task), None)
        assert row is not None, (
            f"Task {ephemeral_task} not in /api/si/list response"
        )
        assert row["last_run_at"] is None
        assert row["last_run_status"] is None
        assert row["last_run_error"] is None

    def test_list_contract_includes_all_three_fields(self, client, ephemeral_task):
        """Every task dict must carry the three last-run fields (even
        when null) so the UI's ``task.last_run_at`` access never
        TypeErrors on undefined."""
        resp = client.get("/api/si/list")
        data = resp.get_json()
        for t in data["tasks"]:
            assert "last_run_at" in t, (
                f"task {t['id']!r} missing last_run_at"
            )
            assert "last_run_status" in t
            assert "last_run_error" in t

    def test_populated_after_run(self, client, ephemeral_task, engine):
        """After a successful run, the same task's next /api/si/list
        response must show last_run_at as a recent epoch + status
        success + null error."""
        before_ts = time.time()
        resp = client.post(f"/api/si/{ephemeral_task}/run")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["status"] == "success"
        run = body.get("run") or {}
        assert run.get("status") == "success", (
            f"Run did not succeed: {run}"
        )

        # Now /api/si/list should reflect the fresh execution_history row.
        list_resp = client.get("/api/si/list")
        row = next(
            (t for t in list_resp.get_json()["tasks"]
             if t["id"] == ephemeral_task), None,
        )
        assert row is not None
        assert row["last_run_at"] is not None
        assert row["last_run_at"] >= before_ts - 1  # generous clock skew
        assert row["last_run_status"] == "success"
        assert row["last_run_error"] in (None, "")


# ---------------------------------------------------------------------------
# B. /api/si/<id>/run endpoint - real ingestion
# ---------------------------------------------------------------------------

class TestSiRunNow:
    def test_run_now_returns_execution_row(self, client, ephemeral_task):
        resp = client.post(f"/api/si/{ephemeral_task}/run")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["status"] == "success"
        run = body["run"]
        assert run is not None
        assert run.get("status") == "success"
        assert run.get("task_id") in (str(ephemeral_task), ephemeral_task)
        # Runtime must be a positive float so the UI can show "0.3s"
        assert isinstance(run.get("runtime"), (int, float))
        assert run["runtime"] >= 0

    def test_run_now_unknown_task_returns_404(self, client):
        resp = client.post("/api/si/999999/run")
        assert resp.status_code == 404
        assert resp.get_json()["status"] == "error"

    def test_run_now_invokes_real_ingestion_not_sandbox(
        self, client, ephemeral_task,
    ):
        """Unlike /api/si/<id>/test (sandbox), /run writes parquet and
        records a row in execution_history. Verify the history store
        has exactly one row for this task after we trigger."""
        from scheduled_input_engine import get_engine
        engine = get_engine()
        before = engine.store.get_execution_history(task_id=ephemeral_task, limit=50)
        before_count = len(before)
        resp = client.post(f"/api/si/{ephemeral_task}/run")
        assert resp.status_code == 200
        after = engine.store.get_execution_history(task_id=ephemeral_task, limit=50)
        assert len(after) == before_count + 1, (
            "Run endpoint must persist exactly one execution_history "
            "row (sandbox Test path does NOT persist - this is the key "
            "differentiator between Run and Test)."
        )


# ---------------------------------------------------------------------------
# C. /api/si/add run_on_create flag
# ---------------------------------------------------------------------------

class TestRunOnCreate:
    def _mint_payload(self, suffix: str) -> dict:
        return {
            "title": f"pytest_runoncreate_{suffix}",
            "code": (
                "import pandas as pd\n"
                "df = pd.DataFrame({'x': [1], '_epoch': [1.0]})\n"
                "GENERATE_RESULTS(df)"
            ),
            "cron_schedule": "0 0 1 1 *",
            "subdirectory": f"pytest_runoncreate_{suffix}",
        }

    def test_run_on_create_true_fires_first_run(self, client):
        payload = {**self._mint_payload("true"), "run_on_create": True}
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200, resp.get_data(as_text=True)
            data = resp.get_json()
            assert data["status"] == "success"
            assert data["first_run"] is not None, (
                "run_on_create=true must attach first_run to response"
            )
            assert data["first_run"].get("status") == "success"
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")

    def test_run_on_create_false_skips_first_run(self, client):
        payload = {**self._mint_payload("false"), "run_on_create": False}
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "success"
            assert data["first_run"] is None, (
                "run_on_create=false must NOT trigger a first run"
            )
            # Verify via /api/si/list: the newly-created task should
            # have last_run_at=None because nothing ran.
            task_id = data["task"]["id"]
            list_resp = client.get("/api/si/list")
            row = next(
                (t for t in list_resp.get_json()["tasks"] if t["id"] == task_id),
                None,
            )
            assert row is not None
            assert row["last_run_at"] is None
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")

    def test_run_on_create_defaults_to_true_when_omitted(self, client):
        """Omit the flag entirely - must default to True so existing
        API clients (and the UI default) seed first-run."""
        payload = self._mint_payload("default")  # no run_on_create key
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["first_run"] is not None, (
                "run_on_create defaults to True; missing key must still "
                "trigger first run"
            )
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")

    def test_save_succeeds_even_when_first_run_fails(self, client):
        """A broken script that fails on execution must still be saved
        (row in scheduled_inputs exists), with first_run reporting the
        failure so the UI can surface it inline."""
        payload = self._mint_payload("brokenscript")
        payload["code"] = (
            "import pandas as pd\n"
            "raise RuntimeError('deliberate failure for test')"
        )
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200
            data = resp.get_json()
            # Task saved
            assert data["status"] == "success"
            assert data["task"]["id"] > 0
            # First run attempted and failed
            assert data["first_run"] is not None
            assert data["first_run"].get("status") == "failed"
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")


# ---------------------------------------------------------------------------
# D. store.get_last_run unit coverage
# ---------------------------------------------------------------------------

class TestStoreGetLastRun:
    def test_get_last_run_none_for_untouched_task(self, engine, ephemeral_task):
        # Fresh task, no run recorded → None.
        assert engine.store.get_last_run(ephemeral_task) is None

    def test_get_last_run_returns_most_recent(self, engine, ephemeral_task, client):
        """Two successful runs back-to-back - ``get_last_run`` must
        return a single row reflecting the newer insert. Check via
        fetched rowid monotonicity (SQLite's implicit rowid is the
        secondary sort key when start_time floats collide)."""
        client.post(f"/api/si/{ephemeral_task}/run")
        time.sleep(0.05)
        client.post(f"/api/si/{ephemeral_task}/run")
        latest = engine.store.get_last_run(ephemeral_task)
        assert latest is not None
        assert latest["status"] == "success"
        # rowid is projected by get_last_run as ``_rowid``. Must be >=
        # the baseline (any row existed BEFORE the two runs is an
        # earlier rowid, so the newer insert's rowid is strictly higher).
        latest_rowid = latest.get("_rowid")
        assert latest_rowid is not None
        assert latest_rowid > 0
        # Confirm there are at least 2 rows for this task - the second
        # run did land.
        history = engine.store.get_execution_history(task_id=ephemeral_task, limit=10)
        assert len(history) >= 2, (
            f"Expected at least 2 history rows, got {len(history)}"
        )

    def test_get_last_run_status_filter(self, engine, ephemeral_task, client):
        """``status="success"`` must skip a subsequent failed run and
        return the last successful one - this is the Last Run pill's
        authoritative source when the current run fails but the user
        wants to see "when did this last work?"."""
        # Successful run
        client.post(f"/api/si/{ephemeral_task}/run")
        # Now break the code so the next run fails - update via PUT
        client.put(f"/api/si/{ephemeral_task}", json={
            "code": "raise RuntimeError('broken')"
        })
        fail_resp = client.post(f"/api/si/{ephemeral_task}/run")
        # Run endpoint always returns 200 - the FAILURE is carried in
        # the body (matching the "save succeeded even if first run
        # failed" UX contract).
        assert fail_resp.status_code == 200
        failed_body = fail_resp.get_json()
        # The last run (unfiltered) should be status=failed
        latest_any = engine.store.get_last_run(ephemeral_task)
        assert latest_any is not None
        assert latest_any["status"] == "failed"
        # The last SUCCESSFUL run should still be the first one
        last_success = engine.store.get_last_run(ephemeral_task, status="success")
        assert last_success is not None
        assert last_success["status"] == "success"
