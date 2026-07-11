"""
Tests for alert group robustness features added in Waves B + C:

  * Per-search row cap enforcement (regression - the user reported uncertainty
    about whether max_rows is actually honored)
  * Dispatcher emits an alert_groups log row + writes to alert_group_runs.sqlite
  * Failure-alert email fires when the dispatch ends in error status
  * Failure email respects the ``alert_group_failure_email_enabled`` gate
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from alert_groups.dispatcher import AlertGroupDispatcher
from alert_groups.serializer import ResultSerializer
from alert_groups.models import SerializedResult


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Point log output + alert-group-runs DB at tmp dirs."""
    from global_settings import get_settings
    from functionality import log_writer as lw

    settings = get_settings()
    settings.set("logs_root", str(tmp_path / "logs"))
    settings.set("logs_enabled", True)
    lw.LogWriter.reset_for_tests()

    import alert_group_store
    monkeypatch.setattr(
        alert_group_store, "RUNS_DB", tmp_path / "ag_runs.sqlite",
    )
    monkeypatch.setattr(
        alert_group_store, "LAST_CHANCE_DB", tmp_path / "last_chance.sqlite",
    )
    monkeypatch.setattr(
        alert_group_store, "GROUPS_DIR", tmp_path / "alert_groups",
    )

    yield tmp_path
    lw.LogWriter.reset_for_tests()


class TestRowCap:
    """Regression - verify max_rows is genuinely enforced per search."""

    def test_serializer_truncates_to_max_rows(self):
        big = pd.DataFrame({"x": list(range(500)), "_epoch": [0] * 500})

        serializer = ResultSerializer(max_rows=10)
        with patch.object(
            ResultSerializer,
            "_load_last_result",
            return_value=big,
        ):
            result = serializer.serialize("big_search")

        assert result.row_count == 10

    def test_dispatcher_per_group_max_rows_propagates(self, tmp_paths):
        """Simulate an AG with max_rows=5 against a 100-row search; verify the
        serialized payload row_count matches 5 and the Claude wrapper receives
        content built from only 5 rows."""
        fake_df = pd.DataFrame({"value": list(range(100)), "_epoch": [0] * 100})

        captured = {}

        def fake_call(**kwargs):
            captured["messages"] = kwargs.get("messages")
            captured["model"] = kwargs.get("model")
            resp = MagicMock()
            resp.input_tokens = 10
            resp.output_tokens = 5
            resp.cost_usd = 0.0001
            resp.latency_ms = 10
            resp.request_id = "rid-cap"
            resp.model = kwargs.get("model")
            # AlertGroupDispatcher._extract_response_text consumes .response
            resp.response = MagicMock()
            resp.response.content = [MagicMock(text="ok")]
            return resp

        group = {
            "name": "row_cap_test",
            "disabled": False,
            "max_rows": 5,
            "search_names": ["big_search"],
            "prompt_text": "Analyse the data.",
            "email_address": "",  # skip email send path
        }

        with patch.object(ResultSerializer, "_load_last_result", return_value=fake_df), \
             patch("alert_groups.dispatcher.call_messages_create", side_effect=fake_call):
            d = AlertGroupDispatcher()
            result = d.run(group)

        assert result.status == "success"
        # Messages shape depends on PayloadBuilder; just verify the content body
        # mentions only rows 0..4 - i.e. row_count=5 truncation held.
        body = captured["messages"][-1]["content"]
        # JSON-serialized list will contain "value":0 through "value":4 but not 5
        assert '"value": 0' in body or '"value":0' in body
        assert '"value": 5' not in body and '"value":5' not in body


class TestDispatcherLogAndAudit:
    def test_success_writes_log_and_sqlite_audit(self, tmp_paths):
        fake_df = pd.DataFrame({"value": [1, 2], "_epoch": [0, 0]})

        def fake_call(**kwargs):
            resp = MagicMock()
            resp.input_tokens = 10
            resp.output_tokens = 3
            resp.cost_usd = 0.00015
            resp.latency_ms = 5
            resp.request_id = "rid-log"
            resp.model = kwargs["model"]
            resp.response = MagicMock()
            resp.response.content = [MagicMock(text="done")]
            return resp

        group = {
            "name": "ag_log", "disabled": False, "max_rows": 50,
            "search_names": ["s1"], "prompt_text": "Go.",
            "email_address": "",
        }

        with patch.object(ResultSerializer, "_load_last_result", return_value=fake_df), \
             patch("alert_groups.dispatcher.call_messages_create", side_effect=fake_call):
            d = AlertGroupDispatcher()
            r = d.run(group)

        assert r.status == "success"

        # Parquet log emitted
        from functionality.log_writer import flush_all
        flush_all()
        log_dir = tmp_paths / "logs" / "alert_groups"
        assert log_dir.exists()
        rows = []
        for p in log_dir.glob("*.parquet"):
            rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(r["group_name"] == "ag_log" and r["status"] == "success"
                   for r in rows)

        # SQLite audit row
        import sqlite3
        with sqlite3.connect(tmp_paths / "ag_runs.sqlite") as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM alert_group_runs WHERE group_name = ?",
                ("ag_log",),
            ).fetchone()
        assert row is not None
        assert row["status"] == "success"


class TestFetchTasksYamlSource:
    """The 2026-04-20 silent-failure bug: ``QueryEngine.fetch_tasks`` was
    reading the legacy ``saved_searches.db`` SQLite table which has been
    empty since the YAML migration. It returned 0 rows, so the saved-search
    scheduler registered nothing, so alert groups had no cached data to
    serialize, so dispatch never produced a useful email.
    """

    def test_fetch_tasks_reads_yaml_saved_searches(self, tmp_path, monkeypatch):
        import asyncio
        import query_engine.QueryEngine as qe

        # Build a tmp SavedSearchStore with two enabled + one disabled entry
        yaml_dir = tmp_path / "saved_searches"
        yaml_dir.mkdir()
        import saved_search_store
        monkeypatch.setattr(
            saved_search_store, "SEARCHES_DIR", yaml_dir, raising=False,
        )
        monkeypatch.setattr(
            saved_search_store, "LAST_CHANCE_DB", tmp_path / "lc.sqlite",
            raising=False,
        )
        monkeypatch.setattr(
            saved_search_store, "DEFAULT_SEARCHES_DIR", tmp_path / "defaults",
            raising=False,
        )

        store = saved_search_store.SavedSearchStore()
        store._dir = yaml_dir
        store._db = str(tmp_path / "lc.sqlite")
        store._default_dir = tmp_path / "defaults"
        store.initialize()

        store.save_search({
            "name": "search_a",
            "description": "d",
            "query": 'index="x/*.parquet" | head 1',
            "cron_schedule": "0 5 * * *",
            "email_address": "noreply@speakesquery.local",
            "send_email": "no",
            "lookback": "-1h",
        })
        store.save_search({
            "name": "search_b",
            "description": "d",
            "query": 'index="y/*.parquet" | head 1',
            "cron_schedule": "0 6 * * *",
            "email_address": "noreply@speakesquery.local",
            "send_email": "no",
            "lookback": "-1h",
        })
        store.save_search({
            "name": "search_c_disabled",
            "description": "d",
            "query": 'index="z/*.parquet" | head 1',
            "cron_schedule": "0 7 * * *",
            "email_address": "noreply@speakesquery.local",
            "send_email": "no",
            "lookback": "-1h",
        })
        # Disable search_c via direct YAML edit (SavedSearchStore.update only
        # takes specific fields and disabled isn't in the default allowlist;
        # simulate the UI toggling disabled: true)
        import yaml as _yaml
        path_c = yaml_dir / "search_c_disabled.yaml"
        data = _yaml.safe_load(path_c.read_text())
        data["disabled"] = True
        path_c.write_text(_yaml.dump(data))

        # Monkey-patch the fetch_tasks import so it uses our tmp store
        class _FakeStore:
            def initialize(self): pass
            def list_searches(self_):
                return store.list_searches()

        monkeypatch.setattr(
            "saved_search_store.SavedSearchStore", lambda: _FakeStore(),
        )

        tasks = asyncio.run(qe.fetch_tasks())
        names = sorted(t[0] for t in tasks)
        # search_c_disabled must be skipped
        assert names == ["search_a", "search_b"], f"got {names}"
        # Shape: (id, title, query, cron_schedule) - id == title == name for YAML
        first = next(t for t in tasks if t[0] == "search_a")
        assert first[0] == first[1] == "search_a"
        assert "parquet" in first[2]
        assert first[3] == "0 5 * * *"


class TestRunGroupByNameHardening:
    """Regression: the APScheduler callback must never swallow an exception
    silently. Any failure - store load, dispatcher construction, dispatcher
    internals - must produce an ``alert_groups`` log row AND a failure email.
    """

    def test_dispatcher_raises_still_emits_log_and_email(
        self, tmp_paths, monkeypatch,
    ):
        from alert_groups.scheduler import _run_group_by_name
        from alert_groups.dispatcher import AlertGroupDispatcher

        # Set up: group exists; but dispatcher.run explodes mid-flight
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "exploder",
            "description": "d",
            "search_names": ["s1"],
            "prompt_text": "Go.",
            "schedule": "* * * * *",
            "max_rows": 10,
            "email_address": "user@example.com",
            "disabled": False,
        })

        def _boom(self, group, dry_run=False):
            raise RuntimeError("synthetic dispatcher crash")

        sent = {}

        def _fake_plain(subject, body, to_addr):
            sent["subject"] = subject
            sent["to"] = to_addr
            sent["body"] = body

        from global_settings import get_settings
        get_settings().set("alert_group_failure_email_enabled", True)
        get_settings().set("alert_group_failure_email_to", "admin@example.com")

        with patch.object(AlertGroupDispatcher, "run", _boom), \
             patch.object(AlertGroupDispatcher, "_send_plain_email",
                          staticmethod(_fake_plain)):
            _run_group_by_name("exploder")

        # Log row
        from functionality.log_writer import flush_all
        flush_all()
        log_dir = tmp_paths / "logs" / "alert_groups"
        rows = []
        for p in log_dir.glob("*.parquet"):
            rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(
            r["group_name"] == "exploder" and r["status"] == "error"
            and "synthetic dispatcher crash" in (r["error_message"] or "")
            for r in rows
        ), f"Expected exploder error row in alert_groups log; got {rows}"

        # Failure email
        assert sent.get("to") == "admin@example.com", (
            f"Expected failure email to admin; sent={sent}"
        )
        assert "exploder" in sent.get("subject", "")

    def test_store_load_failure_still_emits(self, tmp_paths, monkeypatch):
        """If AlertGroupStore itself can't initialise (corrupt DB, perms),
        we still produce a log row + failure email - the user must see
        SOMETHING in the failure channel."""
        from alert_groups import scheduler as ag_sched
        from alert_groups.dispatcher import AlertGroupDispatcher

        class _BoomStore:
            def initialize(self):
                raise RuntimeError("simulated perms error")

        import alert_group_store
        monkeypatch.setattr(
            alert_group_store, "AlertGroupStore", _BoomStore,
        )

        sent = {}

        def _fake_plain(subject, body, to_addr):
            sent["called"] = True
            sent["to"] = to_addr

        from global_settings import get_settings
        get_settings().set("alert_group_failure_email_enabled", True)
        get_settings().set("alert_group_failure_email_to", "admin@example.com")

        with patch.object(AlertGroupDispatcher, "_send_plain_email",
                          staticmethod(_fake_plain)):
            ag_sched._run_group_by_name("some_group")

        from functionality.log_writer import flush_all
        flush_all()
        log_dir = tmp_paths / "logs" / "alert_groups"
        rows = []
        for p in log_dir.glob("*.parquet"):
            rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(
            r["group_name"] == "some_group" and r["status"] == "error"
            for r in rows
        )
        assert sent.get("called") is True


class TestDispatcherOuterGuard:
    """Regression: ``dispatcher.run()`` must remain crash-free even when
    deeply-nested helpers throw. Without the outer try/except, a payload
    build error skipped the log emit entirely."""

    def test_uncaught_mid_flight_exception_still_logs(
        self, tmp_paths, monkeypatch,
    ):
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.serializer import ResultSerializer
        from alert_groups.builder import PayloadBuilder

        # Serializer returns a normal result - we crash inside PayloadBuilder
        fake_df = pd.DataFrame({"x": [1], "_epoch": [0]})
        monkeypatch.setattr(
            ResultSerializer, "_load_last_result", lambda self, name: fake_df,
        )

        def _blow_up(self, group_name, serialized, prompt_text):
            raise ValueError("prompt template exploded")

        monkeypatch.setattr(PayloadBuilder, "build", _blow_up)

        from global_settings import get_settings
        get_settings().set("alert_group_failure_email_enabled", False)

        group = {
            "name": "mid_flight_boom",
            "disabled": False,
            "max_rows": 10,
            "search_names": ["s1"],
            "prompt_text": "Go.",
            "email_address": "",
        }

        d = AlertGroupDispatcher()
        result = d.run(group)  # must NOT raise

        assert result.status == "error"
        assert "Uncaught dispatcher exception" in (result.error_message or "")

        from functionality.log_writer import flush_all
        flush_all()
        log_dir = tmp_paths / "logs" / "alert_groups"
        rows = []
        for p in log_dir.glob("*.parquet"):
            rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(
            r["group_name"] == "mid_flight_boom" and r["status"] == "error"
            for r in rows
        ), f"Expected uncaught-exception error row; got {rows}"


class TestSchedulerWiring:
    """Regression: the Flask entrypoint must wire alert-group cron jobs into
    the ScheduledInputEngine's BackgroundScheduler. Previously ``schedule_tasks``
    in ``query_engine/QueryEngine.py`` owned this, but Docker never started
    that second process - so alert groups silently never auto-fired.
    """

    def test_start_background_scheduling_registers_alert_group_jobs(
        self, tmp_paths, monkeypatch,
    ):
        from unittest.mock import MagicMock
        import query_engine.QueryEngine as qe
        from alert_group_store import AlertGroupStore

        # Seed one scheduled alert group in the YAML store
        store = AlertGroupStore()
        store.initialize()
        store.save_group({
            "name": "wired_test_group",
            "description": "desc",
            "search_names": ["s1"],
            "prompt_text": "Go.",
            "schedule": "*/30 * * * *",
            "max_rows": 10,
            "email_address": "alice@example.com",
            "disabled": False,
        })

        # Keep the asyncio loop thread from actually doing work - patch out
        # the three coroutines it would run.
        async def _noop():
            return None

        monkeypatch.setattr(qe, "initialize_history_db", _noop)
        monkeypatch.setattr(qe, "initialize_saved_searches_db", _noop)
        monkeypatch.setattr(qe, "schedule_tasks", _noop)

        mock_sched = MagicMock()
        mock_sched.get_jobs.return_value = [
            MagicMock(id="alert_group_wired_test_group"),
        ]

        thread = qe.start_background_scheduling(mock_sched)
        assert thread is not None
        # register_alert_group_jobs calls scheduler.add_job; verify that
        # reached our mock at least once with an id starting alert_group_
        assert mock_sched.add_job.call_count >= 1
        first_call_kwargs = mock_sched.add_job.call_args.kwargs
        assert first_call_kwargs.get("id", "").startswith("alert_group_"), (
            f"Expected alert_group_ prefix on job id, got: {first_call_kwargs}"
        )

    def test_start_background_scheduling_warns_when_no_scheduler(
        self, tmp_paths, monkeypatch, caplog,
    ):
        """If the BackgroundScheduler is missing, log loudly and skip AG
        registration rather than crashing the Flask startup."""
        import query_engine.QueryEngine as qe

        async def _noop():
            return None

        monkeypatch.setattr(qe, "initialize_history_db", _noop)
        monkeypatch.setattr(qe, "initialize_saved_searches_db", _noop)
        monkeypatch.setattr(qe, "schedule_tasks", _noop)

        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            qe.start_background_scheduling(None)
        assert any("alert groups will not auto-fire" in rec.message
                   for rec in caplog.records), (
            f"Expected warning; records: {[r.message for r in caplog.records]}"
        )


class TestDispatcherAlwaysLogsEvenWithoutSavedSearchData:
    """The end-to-end smoke test that would have caught the user's complaint:
    if a user clicks Run on an alert group whose saved searches have never
    produced any cached results, the dispatcher must STILL emit a log row
    (status="error", error_message="No results available for any search in
    group") so the Last Run pill + failure email both surface the problem.
    """

    def test_manual_run_with_no_saved_search_data_emits_log(
        self, tmp_paths, monkeypatch,
    ):
        from alert_groups.serializer import ResultSerializer, SearchNotFoundError

        def _no_results(self, search_name):
            raise SearchNotFoundError(
                f'No cached result found for search "{search_name}".'
            )

        monkeypatch.setattr(ResultSerializer, "_load_last_result", _no_results)

        group = {
            "name": "empty_saved_search_group",
            "disabled": False,
            "max_rows": 10,
            "search_names": ["s1", "s2"],
            "prompt_text": "Go.",
            "email_address": "user@example.com",
        }

        # Disable the failure email for this test - we're focused on the log
        # emission, not the SMTP path.
        from alert_groups.dispatcher import AlertGroupDispatcher
        with patch.object(AlertGroupDispatcher, "_send_plain_email"):
            d = AlertGroupDispatcher()
            result = d.run(group)

        assert result.status == "error"
        assert "No results available" in (result.error_message or "")

        # Must have emitted the alert_groups log row
        from functionality.log_writer import flush_all
        flush_all()
        log_dir = tmp_paths / "logs" / "alert_groups"
        assert log_dir.exists(), (
            "alert_groups log subdir was not created - the user will have "
            "no visibility into silent failures"
        )
        rows = []
        for p in log_dir.glob("*.parquet"):
            rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(
            r["group_name"] == "empty_saved_search_group"
            and r["status"] == "error"
            and "No results available" in (r["error_message"] or "")
            for r in rows
        ), f"Expected error row, got: {rows}"


class TestFailureEmail:
    """Claude failure should trigger a plain-text alert email when enabled."""

    def test_failure_email_sent_on_claude_error(self, tmp_paths):
        from global_settings import get_settings
        get_settings().set("alert_group_failure_email_enabled", True)
        get_settings().set("alert_group_failure_email_to", "admin@example.com")

        fake_df = pd.DataFrame({"value": [1], "_epoch": [0]})

        from analyzers.claude_client import ClaudeCallError

        def raise_claude(**kwargs):
            raise ClaudeCallError(
                "network gone", request_id="rid-x",
                error_class="APIConnectionError", attempts=3,
            )

        sent: dict = {}

        def fake_plain(subject, body, to_addr):
            sent["subject"] = subject
            sent["body"] = body
            sent["to"] = to_addr

        group = {
            "name": "ag_fail", "disabled": False, "max_rows": 10,
            "search_names": ["s1"], "prompt_text": "Go.",
            "email_address": "user@example.com",
        }

        with patch.object(ResultSerializer, "_load_last_result", return_value=fake_df), \
             patch("alert_groups.dispatcher.call_messages_create", side_effect=raise_claude), \
             patch.object(AlertGroupDispatcher, "_send_plain_email", staticmethod(fake_plain)):
            d = AlertGroupDispatcher()
            r = d.run(group)

        assert r.status == "error"
        assert sent.get("to") == "admin@example.com"
        assert "ag_fail" in sent["subject"]
        assert "network gone" in sent["body"]

    def test_failure_email_gated_off(self, tmp_paths):
        from global_settings import get_settings
        get_settings().set("alert_group_failure_email_enabled", False)

        fake_df = pd.DataFrame({"value": [1], "_epoch": [0]})

        from analyzers.claude_client import ClaudeCallError

        def raise_claude(**kwargs):
            raise ClaudeCallError(
                "bad key", request_id="rid-x",
                error_class="AuthenticationError", attempts=1,
            )

        sent: dict = {}

        def fake_plain(subject, body, to_addr):
            sent["called"] = True

        group = {
            "name": "ag_silent", "disabled": False, "max_rows": 10,
            "search_names": ["s1"], "prompt_text": "Go.",
            "email_address": "user@example.com",
        }

        with patch.object(ResultSerializer, "_load_last_result", return_value=fake_df), \
             patch("alert_groups.dispatcher.call_messages_create", side_effect=raise_claude), \
             patch.object(AlertGroupDispatcher, "_send_plain_email", staticmethod(fake_plain)):
            d = AlertGroupDispatcher()
            r = d.run(group)

        assert r.status == "error"
        assert "called" not in sent

        # Restore the setting for downstream tests
        get_settings().set("alert_group_failure_email_enabled", True)


# ══════════════════════════════════════════════════════════════════════
# H-AN-4: _trim_to_budget must handle every JSON shape, not just lists
# ══════════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: the budget-trim loop used to
# ``data = data[:new_rows]`` unconditionally and swallowed the TypeError on
# dict / scalar payloads. Untrimmable shapes now log a WARNING and break
# the outer loop so the operator sees budget exceedance instead of a
# silent 10-iteration no-op.

import json as _json_h_an_4  # noqa: E402 (module-scope import at bottom intentional)


def _result(name: str, content: str, fmt: str = "json") -> SerializedResult:
    """Build a SerializedResult with a realistic estimated_tokens count."""
    return SerializedResult(
        search_name=name,
        row_count=max(1, content.count(",") + 1),
        estimated_tokens=ResultSerializer.estimate_tokens(content),
        format=fmt,
        content=content,
    )


class TestBudgetTrimShapes:
    """All JSON shapes + malformed payloads must be handled deterministically."""

    def test_list_shape_trims_successfully(self, caplog):
        """Top-level list: original behavior - halve rows each iteration until under budget."""
        import logging

        big = _json_h_an_4.dumps([{"id": i, "payload": "x" * 200} for i in range(50)])
        r = _result("big_list", big)

        # Budget forces multiple halving iterations.
        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._trim_to_budget(
                [r], budget=r.estimated_tokens // 4,
            )

        assert len(out) == 1
        parsed = _json_h_an_4.loads(out[0].content)
        assert isinstance(parsed, list)
        assert len(parsed) < 50
        # No warning logs - list shape is fully trimmable.
        for rec in caplog.records:
            assert "cannot trim JSON of shape" not in rec.getMessage()

    def test_dict_records_shape_trims_records_key(self):
        """{"records": [...]} - the list value of 'records' is trimmed."""
        big = _json_h_an_4.dumps({
            "metadata": {"generated_at": "2026-04-21"},
            "records": [{"id": i, "payload": "y" * 200} for i in range(50)],
        })
        r = _result("records_shape", big)

        out = AlertGroupDispatcher._trim_to_budget(
            [r], budget=r.estimated_tokens // 4,
        )

        parsed = _json_h_an_4.loads(out[0].content)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["records"], list)
        assert len(parsed["records"]) < 50
        # Metadata preserved.
        assert parsed["metadata"]["generated_at"] == "2026-04-21"

    def test_dict_rows_shape_trims_rows_key(self):
        """{"rows": [...]} - the list value of 'rows' is trimmed."""
        big = _json_h_an_4.dumps({
            "header": "fine",
            "rows": [{"id": i, "payload": "z" * 200} for i in range(50)],
        })
        r = _result("rows_shape", big)

        out = AlertGroupDispatcher._trim_to_budget(
            [r], budget=r.estimated_tokens // 4,
        )

        parsed = _json_h_an_4.loads(out[0].content)
        assert isinstance(parsed["rows"], list)
        assert len(parsed["rows"]) < 50

    def test_unknown_dict_shape_logs_warning_and_breaks(self, caplog):
        """Dict without 'records' or 'rows' list: warn, break loop, return as-is."""
        import logging

        # A scalar-only dict: no list under any known key → untrimmable.
        scalar_dict = _json_h_an_4.dumps({
            "summary": "all good",
            "error_count": 0,
            "message": "nothing to do",
        })
        # Fake a large estimated_tokens so we enter the trim loop.
        r = SerializedResult(
            search_name="unknown_dict",
            row_count=1,
            estimated_tokens=10_000,
            format="json",
            content=scalar_dict,
        )

        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._trim_to_budget([r], budget=100)

        assert out[0].content == scalar_dict, (
            "Untrimmable content must pass through unchanged."
        )
        # Exactly one warning - the break prevents further iterations.
        warnings = [
            rec for rec in caplog.records
            if "cannot trim JSON of shape" in rec.getMessage()
        ]
        assert len(warnings) == 1, (
            f"Expected 1 warning, got {len(warnings)}: "
            f"{[w.getMessage() for w in warnings]}"
        )
        assert "dict" in warnings[0].getMessage()
        assert "unknown_dict" in warnings[0].getMessage()

    def test_scalar_shape_logs_warning_and_breaks(self, caplog):
        """Top-level scalar (number, string): untrimmable → warn + break."""
        import logging

        r = SerializedResult(
            search_name="scalar_shape",
            row_count=1,
            estimated_tokens=10_000,
            format="json",
            content="42",
        )
        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._trim_to_budget([r], budget=100)

        assert out[0].content == "42"
        warnings = [
            rec for rec in caplog.records
            if "cannot trim JSON of shape" in rec.getMessage()
        ]
        assert len(warnings) == 1
        assert "int" in warnings[0].getMessage()

    def test_malformed_json_logs_and_breaks(self, caplog):
        """Unparseable content: warn + break (don't loop 10× with the same error)."""
        import logging

        r = SerializedResult(
            search_name="malformed",
            row_count=1,
            estimated_tokens=10_000,
            format="json",
            content="{not valid json",
        )
        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._trim_to_budget([r], budget=100)

        assert out[0].content == "{not valid json"
        parse_warnings = [
            rec for rec in caplog.records
            if "cannot parse JSON content" in rec.getMessage()
        ]
        assert len(parse_warnings) == 1

    def test_under_budget_returns_untouched(self):
        """If total is already under budget, results pass through unchanged."""
        small = _json_h_an_4.dumps([{"id": 1}, {"id": 2}])
        r = _result("already_small", small)

        out = AlertGroupDispatcher._trim_to_budget([r], budget=10_000_000)

        assert out[0].content == small
        assert out[0].row_count == r.row_count


# ══════════════════════════════════════════════════════════════════════
# H-AN-2: _extract_response_text empty-text handling
# ══════════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: Claude can return a
# completion with no text blocks (tool-only turn, refusal without prose,
# stop_reason="max_tokens" before any text emitted). Previously the
# dispatcher silently emailed an empty brief with status='success'.
# After the fix, _extract_response_text emits a [!] warning with
# stop_reason + block types, and the dispatcher fails fast with
# status='error' + failure-email + circuit-breaker tick.

class TestExtractResponseTextEmptyWarning:
    """Unit tests for the warning emitted by _extract_response_text."""

    def _fake_response(self, *, content, stop_reason="end_turn"):
        """Build a minimal duck-typed response object."""
        resp = MagicMock()
        resp.content = content
        resp.stop_reason = stop_reason
        return resp

    def test_tool_only_response_emits_warning(self, caplog):
        """Response whose only blocks are tool_use → empty string + warning."""
        import logging

        class _ToolUseBlock:
            pass  # no 'text' attribute

        tool_block = _ToolUseBlock()
        resp = self._fake_response(content=[tool_block], stop_reason="tool_use")

        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            text = AlertGroupDispatcher._extract_response_text(resp)

        assert text == ""
        warnings = [
            r for r in caplog.records
            if "no text blocks" in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"Expected one 'no text blocks' warning; got "
            f"{[w.getMessage() for w in caplog.records]}"
        )
        msg = warnings[0].getMessage()
        assert "stop_reason=tool_use" in msg
        assert "_ToolUseBlock" in msg, (
            f"Warning should include block type names; got {msg!r}"
        )

    def test_text_block_no_warning(self, caplog):
        """Response with a real text block → no warning, text returned."""
        import logging

        text_block = MagicMock()
        text_block.text = "analysis result"
        resp = self._fake_response(content=[text_block])

        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._extract_response_text(resp)

        assert out == "analysis result"
        warnings = [
            r for r in caplog.records
            if "no text blocks" in r.getMessage()
            or "response is empty" in r.getMessage()
        ]
        assert warnings == [], (
            f"Unexpected warnings for a well-formed response: "
            f"{[w.getMessage() for w in warnings]}"
        )

    def test_empty_text_block_treated_as_no_text(self, caplog):
        """A block with an empty-string .text is not a usable text block."""
        import logging

        blank_block = MagicMock()
        blank_block.text = ""
        resp = self._fake_response(content=[blank_block], stop_reason="end_turn")

        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._extract_response_text(resp)

        assert out == ""
        assert any(
            "no text blocks" in r.getMessage() for r in caplog.records
        )

    def test_none_response_emits_empty_warning(self, caplog):
        """A missing response → ``response is empty`` warning."""
        import logging

        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._extract_response_text(None)

        assert out == ""
        assert any(
            "response is empty" in r.getMessage() for r in caplog.records
        )


class TestDispatcherEmptyTextFailsFast:
    """Integration: empty response_text must route through the failure-email path."""

    def test_tool_only_response_marks_failure(self, tmp_paths):
        """Dispatcher run with a tool-only Claude response ends in status='error'."""
        from analyzers.claude_client import ClaudeCallResult
        from global_settings import get_settings

        get_settings().set("alert_group_failure_email_enabled", True)
        get_settings().set("alert_group_failure_email_to", "admin@example.com")

        fake_df = pd.DataFrame({"value": [1], "_epoch": [0]})

        class _ToolUseBlock:
            pass

        tool_response = MagicMock()
        tool_response.content = [_ToolUseBlock()]
        tool_response.stop_reason = "tool_use"

        call_result = ClaudeCallResult(
            response=tool_response,
            request_id="rid-empty-text",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.0005,
            latency_ms=1500,
            attempts=1,
        )

        sent: dict = {}

        def fake_plain(subject, body, to_addr):
            sent["subject"] = subject
            sent["body"] = body
            sent["to"] = to_addr

        group = {
            "name": "ag_empty_text", "disabled": False, "max_rows": 10,
            "search_names": ["s1"], "prompt_text": "Go.",
            "email_address": "user@example.com",
        }

        with patch.object(ResultSerializer, "_load_last_result", return_value=fake_df), \
             patch(
                 "alert_groups.dispatcher.call_messages_create",
                 return_value=call_result,
             ), \
             patch.object(AlertGroupDispatcher, "_send_plain_email", staticmethod(fake_plain)):
            d = AlertGroupDispatcher()
            r = d.run(group)

        assert r.status == "error", (
            f"Tool-only response must mark the dispatch as error; got {r.status!r} "
            f"with error_message={getattr(r, 'error_message', None)!r}"
        )
        assert "no text" in (r.error_message or "").lower(), (
            f"error_message should cite the missing text; got {r.error_message!r}"
        )
        # Failure email fires because the flag is enabled.
        assert "subject" in sent, (
            "Failure email was not sent; sent dict is: " + repr(sent)
        )


# ══════════════════════════════════════════════════════════════════════
# H-AN-5: estimate_prompt_tokens on the fully-built prompt
# ══════════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: the pre-trim + in-loop
# budget gate used ``sum(r.estimated_tokens for r in serialized)``, which
# only counts the per-feeder serialized content. PayloadBuilder.build()
# adds section headers ("## Search: <name> (<n> rows, CSV)"), code-fence
# lines, an Alert-Group/Timestamp/Searches metadata block, and ``---``
# separators - easily several hundred tokens of extra overhead on a
# 10-feeder AG. After the fix the gate uses ``estimate_prompt_tokens`` on
# the full built user_content.

class TestEstimatePromptTokensCoversWrappers:
    """Unit tests for the new serializer helper + dispatcher gate."""

    def test_estimate_prompt_tokens_matches_heuristic(self):
        """Simple sanity: ~3.5 chars per token, floor at 1."""
        assert ResultSerializer.estimate_prompt_tokens("") == 1
        s = "a" * 350
        # 350 / 3.5 = 100
        assert ResultSerializer.estimate_prompt_tokens(s) == 100

    def test_built_prompt_estimate_exceeds_raw_content_sum(self):
        """Built prompt (headers + fences + metadata) must have more tokens than raw content sum."""
        from alert_groups.builder import PayloadBuilder

        # Build 3 non-trivial feeders.
        results = []
        for i in range(3):
            content = ",".join(f"col{c}" for c in range(5)) + "\n" + (
                "\n".join(",".join(f"v{r}{c}" for c in range(5)) for r in range(10))
            )
            results.append(SerializedResult(
                search_name=f"feeder_{i}",
                row_count=10,
                estimated_tokens=ResultSerializer.estimate_tokens(content),
                format="csv",
                content=content,
            ))

        per_block_sum = sum(r.estimated_tokens for r in results)

        builder = PayloadBuilder()
        msgs = builder.build("test_group", results, "Analyze the data please.")
        built = msgs[0]["content"]
        built_estimate = ResultSerializer.estimate_prompt_tokens(built)

        assert built_estimate > per_block_sum, (
            f"Built prompt ({built_estimate} tokens) must exceed per-block "
            f"sum ({per_block_sum}) - the wrappers carry real overhead."
        )
        # Sanity: overhead is meaningful (>50 tokens) on a 3-feeder prompt.
        assert built_estimate - per_block_sum > 50, (
            f"Overhead ({built_estimate - per_block_sum}) unexpectedly small. "
            f"built={built_estimate}, blocks={per_block_sum}"
        )


class TestTrimToBudgetUsesBuiltPromptEstimate:
    """_trim_to_budget must measure the full built prompt when given a callable."""

    def test_build_prompt_fn_engages_when_provided(self):
        """With a builder, trim measures post-wrapper tokens each iteration."""
        import json as _j

        # Single large JSON list feeder.
        big = _j.dumps([{"id": i, "payload": "x" * 200} for i in range(50)])
        r = SerializedResult(
            search_name="big_one",
            row_count=50,
            estimated_tokens=ResultSerializer.estimate_tokens(big),
            format="json",
            content=big,
        )

        # Closure that returns a prompt with a fixed, measurable wrapper
        # overhead so the estimate is deterministic.
        WRAPPER = "META:" + "w" * 350  # 355 chars → ~101 tokens
        def build_fn(rows):
            return WRAPPER + "\n" + rows[0].content

        # Budget just a bit below (raw + wrapper). Forces at least one trim.
        raw_tokens = ResultSerializer.estimate_tokens(big)
        wrapper_tokens = ResultSerializer.estimate_prompt_tokens(WRAPPER)
        target_budget = (raw_tokens + wrapper_tokens) // 2
        out = AlertGroupDispatcher._trim_to_budget(
            [r], target_budget, build_prompt_fn=build_fn,
        )

        built_after = build_fn(out)
        assert ResultSerializer.estimate_prompt_tokens(built_after) <= target_budget, (
            f"Trim with build_prompt_fn failed to bring built prompt under "
            f"budget {target_budget}; got {ResultSerializer.estimate_prompt_tokens(built_after)}"
        )

    def test_build_prompt_fn_falls_back_on_builder_error(self, caplog):
        """If the build fn raises, trim warns and falls back to per-block sum."""
        import json as _j
        import logging

        big = _j.dumps([{"id": i} for i in range(100)])
        r = SerializedResult(
            search_name="fallback_feeder",
            row_count=100,
            estimated_tokens=ResultSerializer.estimate_tokens(big),
            format="json",
            content=big,
        )

        def broken_build(_rows):
            raise RuntimeError("builder unavailable")

        with caplog.at_level(logging.WARNING, logger="alert_groups.dispatcher"):
            out = AlertGroupDispatcher._trim_to_budget(
                [r], budget=r.estimated_tokens // 4, build_prompt_fn=broken_build,
            )

        # Fallback path still trims via per-block sum.
        assert sum(x.estimated_tokens for x in out) <= r.estimated_tokens // 4
        # And the fallback warning fires at least once.
        assert any(
            "build_prompt_fn failed" in rec.getMessage()
            for rec in caplog.records
        ), (
            "Expected the 'build_prompt_fn failed' fallback warning. "
            f"records={[r.getMessage() for r in caplog.records]}"
        )
