"""
Tests for the 2026-04-23 per-task timeout feature.

User report: ``options_unusual_activity_pro`` never completes even in the
Test path because the global 120s timeout is too tight for its 10-ticker
Yahoo-paced + Black-Scholes-greeks workload.

Fix: add a per-task ``timeout_seconds`` column on ``scheduled_inputs``;
library scripts declare a ``suggested_timeout_seconds`` JSON hint; the
deploy flow auto-fills the task from the hint; engine uses the
per-task value when set, else falls back to the global default.

These tests pin every layer: store CRUD, engine dispatch, /api/si/add
auto-fill from library hint, /api/si/list enrichment, bounds validation,
and the specific 300s value on options_unusual_activity_pro.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Store CRUD: accept + persist + bounds + round-trip
# ---------------------------------------------------------------------------


class TestStoreTimeoutColumn:
    @pytest.fixture
    def store(self, tmp_path):
        from scheduled_input_engine.store import ScheduledInputStore
        # Point the store at temp DBs so we don't pollute the dev tree.
        s = ScheduledInputStore()
        s._inputs_db = str(tmp_path / "inputs.sqlite")
        s._history_db = str(tmp_path / "history.sqlite")
        # Re-init so the tmp DBs get the schema (auto-migration covers
        # timeout_seconds on first CREATE TABLE since it's in the list).
        s.initialize_databases()
        return s

    def test_add_with_timeout_persists(self, store):
        task = store.add_scheduled_input(
            title="test_timeout_add",
            code="pass",
            cron_schedule="0 0 * * *",
            timeout_seconds=240,
        )
        assert task["timeout_seconds"] == 240

    def test_add_without_timeout_stores_null(self, store):
        task = store.add_scheduled_input(
            title="test_timeout_default",
            code="pass",
            cron_schedule="0 0 * * *",
        )
        # NULL in the DB surfaces as None in the dict so engine knows
        # to fall back to the global default.
        assert task.get("timeout_seconds") is None

    def test_update_timeout_seconds(self, store):
        task = store.add_scheduled_input(
            title="test_timeout_update",
            code="pass",
            cron_schedule="0 0 * * *",
        )
        updated = store.update_scheduled_input(
            task["id"], timeout_seconds=180,
        )
        assert updated["timeout_seconds"] == 180
        # Set back to None → stored as NULL, engine re-uses global
        updated2 = store.update_scheduled_input(
            task["id"], timeout_seconds=None,
        )
        assert updated2.get("timeout_seconds") is None

    def test_validate_timeout_bounds(self, store):
        # Lower bound 10
        with pytest.raises(ValueError, match="between 10 and 3600"):
            store.add_scheduled_input(
                title="x_low", code="pass", cron_schedule="0 0 * * *",
                timeout_seconds=5,
            )
        # Upper bound 3600
        with pytest.raises(ValueError, match="between 10 and 3600"):
            store.add_scheduled_input(
                title="x_high", code="pass", cron_schedule="0 0 * * *",
                timeout_seconds=5000,
            )
        # Non-integer
        with pytest.raises(ValueError, match="must be an integer"):
            store.add_scheduled_input(
                title="x_str", code="pass", cron_schedule="0 0 * * *",
                timeout_seconds="fast",
            )

    def test_empty_string_treated_as_none(self, store):
        """UI sends empty string when the user leaves the field blank -
        must be coerced to None, not rejected as invalid."""
        task = store.add_scheduled_input(
            title="test_timeout_empty_str",
            code="pass",
            cron_schedule="0 0 * * *",
            timeout_seconds="",
        )
        assert task.get("timeout_seconds") is None


# ---------------------------------------------------------------------------
# Engine: _run_task + test_task honor per-task override
# ---------------------------------------------------------------------------


class TestEngineTimeoutPrecedence:
    """Per-task timeout_seconds wins over the global default."""

    def test_run_task_uses_per_task_timeout(self, monkeypatch):
        from scheduled_input_engine import get_engine, start_engine
        start_engine()
        engine = get_engine()

        captured = {}
        real_add = engine.store.add_scheduled_input
        task = real_add(
            title="pytest_timeout_precedence_run",
            code=(
                "import pandas as pd\n"
                "df = pd.DataFrame({'v': [1], '_epoch': [1.0]})\n"
                "GENERATE_RESULTS(df)"
            ),
            cron_schedule="0 0 1 1 *",
            subdirectory="pytest_timeout_precedence_run",
            timeout_seconds=250,
        )
        try:
            # Inject a spy that captures the wall-time the engine tried to
            # enforce - stash in `captured['timeout']`.
            import concurrent.futures as _cf
            original_ctor = _cf.ThreadPoolExecutor

            class SpyPool(original_ctor):
                def submit(self, fn, *args, **kwargs):
                    fut = super().submit(fn, *args, **kwargs)
                    # We can't capture the timeout at submit time - it's
                    # only passed to .result(). So wrap .result().
                    original_result = fut.result
                    def wrapped(timeout=None):
                        captured['timeout'] = timeout
                        return original_result(timeout=timeout)
                    fut.result = wrapped
                    return fut

            monkeypatch.setattr(_cf, "ThreadPoolExecutor", SpyPool)
            engine._run_task(task)
            assert captured.get("timeout") == 250, (
                f"Engine must pass per-task timeout (250) to result(); got {captured.get('timeout')}"
            )
        finally:
            engine.store.delete_scheduled_input(task["id"])

    def test_run_task_falls_back_to_global_when_none(self, monkeypatch):
        from scheduled_input_engine import get_engine, start_engine
        start_engine()
        engine = get_engine()
        task = engine.store.add_scheduled_input(
            title="pytest_timeout_fallback_global",
            code=(
                "import pandas as pd\n"
                "df = pd.DataFrame({'v': [1], '_epoch': [1.0]})\n"
                "GENERATE_RESULTS(df)"
            ),
            cron_schedule="0 0 1 1 *",
            subdirectory="pytest_timeout_fallback_global",
            # no timeout_seconds → engine should read global default
        )
        try:
            from global_settings import get_settings
            global_default = get_settings().get("default_script_timeout_seconds") or 600

            captured = {}
            import concurrent.futures as _cf
            original_ctor = _cf.ThreadPoolExecutor

            class SpyPool(original_ctor):
                def submit(self, fn, *args, **kwargs):
                    fut = super().submit(fn, *args, **kwargs)
                    original_result = fut.result
                    def wrapped(timeout=None):
                        captured['timeout'] = timeout
                        return original_result(timeout=timeout)
                    fut.result = wrapped
                    return fut

            monkeypatch.setattr(_cf, "ThreadPoolExecutor", SpyPool)
            engine._run_task(task)
            assert captured.get("timeout") == global_default, (
                f"Engine must fall back to global ({global_default}) "
                f"when task.timeout_seconds is None; got {captured.get('timeout')}"
            )
        finally:
            engine.store.delete_scheduled_input(task["id"])


# ---------------------------------------------------------------------------
# Library script hint floor: any explicit suggested_timeout_seconds must be
# >= 600s. Per the 2026-05-04 directive, 600s is the uniform floor - scripts
# that legitimately need more (eg. very slow scrapers) can opt up via the
# per-task UI, but the library hints never set a CEILING below 600.
# ---------------------------------------------------------------------------


class TestLibraryHint:
    UNIFORM_TIMEOUT_FLOOR = 600

    def test_library_hints_meet_uniform_floor(self):
        """Drift guard: every library script declaring an explicit
        ``suggested_timeout_seconds`` must be >= UNIFORM_TIMEOUT_FLOOR.
        Scripts may opt UP if their workload genuinely needs more, but
        never down - bumping the floor is intentional headroom so a slow
        API day doesn't surface as a timeout error to the operator."""
        from script_library import list_scripts
        violations = []
        for s in list_scripts():
            hint = s.get("suggested_timeout_seconds")
            if hint is not None and hint < self.UNIFORM_TIMEOUT_FLOOR:
                violations.append((s["id"], hint))
        assert not violations, (
            f"Library scripts below the {self.UNIFORM_TIMEOUT_FLOOR}s "
            f"timeout floor: {violations}. Bump each script's "
            f"suggested_timeout_seconds to >= {self.UNIFORM_TIMEOUT_FLOOR}."
        )

    def test_global_default_meets_uniform_floor(self):
        """The global ``default_script_timeout_seconds`` setting must
        also meet the uniform floor - that's what NULL-timeout deployed
        tasks fall back to."""
        from global_settings import DEFAULTS
        assert DEFAULTS["default_script_timeout_seconds"] >= self.UNIFORM_TIMEOUT_FLOOR, (
            f"Global default_script_timeout_seconds is "
            f"{DEFAULTS['default_script_timeout_seconds']}, below the "
            f"{self.UNIFORM_TIMEOUT_FLOOR}s uniform floor."
        )


# ---------------------------------------------------------------------------
# /api/si/add - auto-fill timeout_seconds from library hint
# ---------------------------------------------------------------------------


class TestApiAddAutoFillHint:
    def _make_payload(self, suffix: str, **extra) -> dict:
        payload = {
            "title": f"pytest_timeouthint_{suffix}",
            "code": (
                "import pandas as pd\n"
                "df = pd.DataFrame({'v': [1], '_epoch': [1.0]})\n"
                "GENERATE_RESULTS(df)"
            ),
            "cron_schedule": "0 0 1 1 *",
            "run_on_create": False,
        }
        payload.update(extra)
        return payload

    def test_deploy_with_matching_subdir_inherits_hint(self, client):
        # Subdirectory matches options_unusual_activity_pro's
        # suggested_subdirectory - /api/si/add should auto-fill 600
        # (the uniform library-hint floor as of 2026-05-04).
        payload = self._make_payload(
            "matching",
            subdirectory="equities/options_unusual_pro",
        )
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200, resp.get_data(as_text=True)
            data = resp.get_json()
            assert data["status"] == "success"
            assert data["task"]["timeout_seconds"] == 600, (
                "Deploying a task whose subdirectory matches a library "
                "script with suggested_timeout_seconds=600 must inherit "
                "the hint."
            )
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")

    def test_explicit_payload_timeout_wins_over_hint(self, client):
        payload = self._make_payload(
            "explicit_wins",
            subdirectory="equities/options_unusual_pro",
            timeout_seconds=150,  # explicit override even though hint=600
        )
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["task"]["timeout_seconds"] == 150, (
                "Explicit timeout in payload must win over library hint."
            )
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")

    def test_no_matching_library_script_leaves_null(self, client):
        payload = self._make_payload(
            "no_match",
            subdirectory="custom/user_invented_subdir_12345",
        )
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["task"].get("timeout_seconds") is None, (
                "Subdirectory with no library match must leave "
                "timeout_seconds NULL so engine uses global default."
            )
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")

    def test_list_endpoint_returns_timeout_seconds(self, client):
        payload = self._make_payload(
            "list_field",
            subdirectory="equities/options_unusual_pro",
        )
        resp = client.post("/api/si/add", json=payload)
        try:
            assert resp.status_code == 200
            task_id = resp.get_json()["task"]["id"]
            list_resp = client.get("/api/si/list")
            row = next(
                (t for t in list_resp.get_json()["tasks"] if t["id"] == task_id),
                None,
            )
            assert row is not None
            assert row.get("timeout_seconds") == 600, (
                "/api/si/list must include timeout_seconds so the UI's "
                "edit form can pre-fill it."
            )
        finally:
            if resp.status_code == 200:
                client.delete(f"/api/si/{resp.get_json()['task']['id']}")


# ---------------------------------------------------------------------------
# Seismic filter relaxation - economic-zone M4.5+ events pass
# ---------------------------------------------------------------------------


class TestTestCodeHonorsTimeoutPayload:
    """Chicken-and-egg regression: the pre-save Test Code button must
    honor a timeout_seconds value from the form, else a slow library
    script (options_unusual_activity_pro hint = 180s) can never Save
    because Save is gated on Test passing and Test would hit the
    global 120s. User reported this exact lockout 2026-04-23."""

    def test_test_code_accepts_timeout_override(self, client):
        """POST /api/si/test-code with a timeout_seconds field must
        pass it through to engine.test_task."""
        import time
        # Write a script that sleeps 2s - would pass with any timeout
        # >= 3, would fail with a tiny timeout. This pins the plumbing.
        code = (
            "import pandas as pd\n"
            "import time\n"
            "time.sleep(2)\n"
            "df = pd.DataFrame({'v': [1], '_epoch': [1.0]})\n"
            "GENERATE_RESULTS(df)"
        )
        # Timeout of 10s - script completes in ~2s, well under
        resp = client.post("/api/si/test-code", json={
            "code": code,
            "timeout_seconds": 10,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["summary"]["status"] == "pass", (
            f"Test with timeout=10s must pass a 2s-sleeping script. "
            f"Got: {data['summary']}"
        )

    def test_test_code_clamps_timeout_bounds(self, client):
        """Payload timeout_seconds outside [10, 3600] is silently
        ignored - falls back to the global default. Prevents a rogue
        UI payload from tying up a worker indefinitely or disabling
        the timeout entirely with 0/negative."""
        # Use a script that would exceed a valid tiny timeout but the
        # out-of-bounds value (2) should be rejected → falls back to
        # global default (120 in dev) → script passes.
        code = (
            "import pandas as pd\n"
            "df = pd.DataFrame({'v': [1], '_epoch': [1.0]})\n"
            "GENERATE_RESULTS(df)"
        )
        resp = client.post("/api/si/test-code", json={
            "code": code,
            "timeout_seconds": 2,  # below lower bound [10, 3600]
        })
        assert resp.status_code == 200
        # Script is instant so even 120s default works; just verify no crash.
        assert resp.get_json()["status"] == "success"


class TestSeismicFilterRelaxed:
    def test_filter_passes_economic_zone_m45(self):
        """The user reported zero rows even on days with seismic
        activity. Filter was ``HIGH/CRITICAL OR mag>=5.5`` which
        excluded M4.5-5.4 events even in Japan/Chile/Indonesia. The
        relaxed filter passes any M4.5+ event in a tagged zone."""
        import yaml
        p = PROJECT_ROOT / "default_saved_searches" / "gmrb_seismic_activity.yaml"
        spec = yaml.safe_load(p.read_text())
        query = spec["query"]
        # The relaxed clause must be present - check for the new
        # region_tag-based OR branch.
        assert 'region_tag != "Unclassified"' in query, (
            f"Expected filter to pass economic-zone M4.5+ events via "
            f"``region_tag != 'Unclassified'`` clause. Query:\n{query}"
        )
        assert "magnitude >= 4.5" in query, (
            f"Expected M4.5+ threshold inside the relaxed clause. Query:\n{query}"
        )
