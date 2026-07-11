"""
Tests for analyzers/claude_history_store.py + analyzers/claude_client.py.

Covers:
  * Full request/response payload round-trip through gzip
  * retain_payloads=False leaves bodies out but keeps metadata
  * list_calls filtering (source, group_name, status, since/until, limit)
  * stats() aggregates match the underlying rows
  * delete_older_than + vacuum do not corrupt surviving rows
  * call_messages_create retries on transient errors and not on 4xx auth errors
  * All attempts (including failures) land in both the Parquet log stream and the SQLite history
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pandas as pd
import pytest

from analyzers.claude_history_store import ClaudeHistoryStore


@pytest.fixture
def tmp_history(tmp_path):
    db = tmp_path / "claude_api_history.sqlite"
    return ClaudeHistoryStore(db_path=db)


@pytest.fixture
def tmp_logs_dir(tmp_path, monkeypatch):
    """Point the log writer at a tmp dir so call_messages_create logs don't
    collide with the main indexes/ directory."""
    from global_settings import get_settings
    from functionality import log_writer as lw

    settings = get_settings()
    orig_root = settings.get("logs_root")
    orig_enabled = settings.get("logs_enabled")
    settings.set("logs_root", str(tmp_path / "logs"))
    settings.set("logs_enabled", True)
    lw.LogWriter.reset_for_tests()
    yield tmp_path / "logs"
    try:
        settings.set("logs_root", orig_root)
        settings.set("logs_enabled", orig_enabled)
    except Exception:
        pass
    lw.LogWriter.reset_for_tests()


# ── ClaudeHistoryStore ───────────────────────────────────────────────

class TestHistoryStore:
    def test_payload_roundtrip(self, tmp_history):
        req_payload = {"messages": [{"role": "user", "content": "hi"}],
                       "model": "m1", "max_tokens": 16}
        resp_payload = {
            "id": "msg_1",
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 5, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        rid = tmp_history.record_call(
            source="unit_test", model="m1", status="success",
            request_body=req_payload, response_body=resp_payload,
            input_tokens=5, output_tokens=1, cost_usd=0.00002,
            latency_ms=120,
        )
        row = tmp_history.get_call(rid)
        assert row is not None
        assert row["request_body"] == req_payload
        assert row["response_body"] == resp_payload
        assert row["input_tokens"] == 5
        assert row["cost_usd"] == pytest.approx(0.00002)

    def test_retain_payloads_false(self, tmp_history, monkeypatch):
        from global_settings import get_settings
        get_settings().set("claude_history_retain_payloads", False)
        try:
            rid = tmp_history.record_call(
                source="unit_test", model="m1", status="success",
                request_body={"a": 1}, response_body={"b": 2},
                input_tokens=10, output_tokens=5, cost_usd=0.0001,
            )
            row = tmp_history.get_call(rid)
            assert row["request_body"] is None
            assert row["response_body"] is None
            # Metadata still retained
            assert row["input_tokens"] == 10
            assert row["cost_usd"] == pytest.approx(0.0001)
        finally:
            get_settings().set("claude_history_retain_payloads", True)

    def test_list_filters(self, tmp_history):
        now = int(time.time())
        tmp_history.record_call(
            source="alert_group", model="m1", status="success",
            group_name="ga", input_tokens=1,
        )
        tmp_history.record_call(
            source="analyzer", model="m2", status="error",
            group_name="gb", error_message="boom",
        )
        tmp_history.record_call(
            source="alert_group", model="m1", status="success",
            group_name="ga",
        )

        # Filter by source
        rows = tmp_history.list_calls(source="alert_group")
        assert all(r["source"] == "alert_group" for r in rows)
        assert len(rows) == 2

        # Filter by group_name + status
        rows = tmp_history.list_calls(group_name="ga", status="success")
        assert len(rows) == 2
        assert all(r["group_name"] == "ga" for r in rows)

        # Since filter (all rows were just created, so since=now-60 returns all)
        rows = tmp_history.list_calls(since_epoch=now - 60)
        assert len(rows) == 3

    def test_stats_aggregate(self, tmp_history):
        for _ in range(3):
            tmp_history.record_call(
                source="analyzer", model="m1", status="success",
                input_tokens=100, output_tokens=50, cost_usd=0.001,
            )
        tmp_history.record_call(
            source="analyzer", model="m1", status="error",
            error_message="failed",
        )
        stats = tmp_history.stats()
        assert stats["calls"] == 4
        assert stats["success_count"] == 3
        assert stats["error_count"] == 1
        assert stats["input_tokens"] == 300
        assert stats["output_tokens"] == 150
        assert stats["cost_usd"] == pytest.approx(0.003)

    def test_delete_older_than_and_vacuum(self, tmp_history):
        rid_old = tmp_history.record_call(
            source="analyzer", model="m1", status="success",
        )
        # Age the row by rewriting the epoch via direct SQL
        import sqlite3
        with sqlite3.connect(tmp_history._db_path) as conn:
            conn.execute(
                "UPDATE claude_api_calls SET triggered_at_epoch = ? WHERE request_id = ?",
                (1000, rid_old),
            )
            conn.commit()
        rid_new = tmp_history.record_call(
            source="analyzer", model="m1", status="success",
        )
        removed = tmp_history.delete_older_than(cutoff_epoch=int(time.time()) - 60)
        assert removed == 1
        tmp_history.vacuum()
        assert tmp_history.get_call(rid_old) is None
        assert tmp_history.get_call(rid_new) is not None


# ── claude_client.call_messages_create ───────────────────────────────

def _fake_response(text: str = "OK", in_tokens: int = 5, out_tokens: int = 1):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.usage = MagicMock(
        input_tokens=in_tokens, output_tokens=out_tokens,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    resp.stop_reason = "end_turn"
    resp.model_dump = MagicMock(return_value={
        "content": [{"text": text}],
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        "stop_reason": "end_turn",
    })
    return resp


class _FakeClient:
    """Swap-in for anthropic.Anthropic that executes a scripted sequence."""

    def __init__(self, script):
        self._script = list(script)
        self._calls = []
        self.messages = MagicMock()
        self.messages.create = self._create

    def _create(self, **kwargs):
        self._calls.append(kwargs)
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class _MockAPIError(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code
    # Name must match one in the wrapper's retryable set
    __class__name__ = "InternalServerError"


class _Retry500(Exception):
    # Using class name check in wrapper
    pass
_Retry500.__name__ = "InternalServerError"


class _Auth401(Exception):
    status_code = 401
_Auth401.__name__ = "AuthenticationError"


class TestClaudeClient:
    def test_success_path_records_to_history_and_logs(self, tmp_path, tmp_logs_dir):
        import analyzers.claude_client as cc
        from analyzers.claude_history_store import ClaudeHistoryStore
        # Point history DB at a tmp file by constructing a fresh instance.
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )

        scripted = _FakeClient([_fake_response(in_tokens=12, out_tokens=3)])
        result = cc.call_messages_create(
            source="unit_test",
            api_key_override="sk-fake",
            client_factory=lambda key: scripted,
            model="claude-sonnet-4-6",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result.input_tokens == 12
        assert result.output_tokens == 3
        assert result.attempts == 1
        # cost = 12/M * 3.00 + 3/M * 15.00
        assert result.cost_usd == pytest.approx(12/1e6 * 3.0 + 3/1e6 * 15.0)

        # History has the row
        rows = ClaudeHistoryStore.get_instance().list_calls(include_payloads=True)
        assert len(rows) == 1
        assert rows[0]["status"] == "success"
        assert rows[0]["request_body"]["model"] == "claude-sonnet-4-6"

        # Parquet log also has it
        from functionality.log_writer import flush_all
        flush_all()
        log_rows = []
        for p in (tmp_logs_dir / "claude_api").glob("*.parquet"):
            log_rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(r["request_id"] == result.request_id and r["status"] == "success"
                   for r in log_rows)
        ClaudeHistoryStore.reset_for_tests()

    def test_retries_on_transient_then_succeeds(self, tmp_path, tmp_logs_dir):
        import analyzers.claude_client as cc
        from analyzers.claude_history_store import ClaudeHistoryStore
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )

        # Shorten backoff so the test is fast.
        from global_settings import get_settings
        get_settings().set("claude_retry_initial_backoff_seconds", 1)

        scripted = _FakeClient([
            _Retry500("temporary"),
            _fake_response(),
        ])
        result = cc.call_messages_create(
            source="unit_test",
            api_key_override="sk-fake",
            client_factory=lambda key: scripted,
            model="claude-sonnet-4-6",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result.attempts == 2
        rows = ClaudeHistoryStore.get_instance().list_calls(limit=10)
        assert len(rows) == 2  # one error + one success
        statuses = sorted(r["status"] for r in rows)
        assert statuses == ["error", "success"]
        ClaudeHistoryStore.reset_for_tests()

    def test_does_not_retry_on_4xx_auth(self, tmp_path, tmp_logs_dir):
        import analyzers.claude_client as cc
        from analyzers.claude_history_store import ClaudeHistoryStore
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )

        scripted = _FakeClient([_Auth401("bad key"), _fake_response()])
        from analyzers.claude_client import ClaudeCallError
        with pytest.raises(ClaudeCallError):
            cc.call_messages_create(
                source="unit_test",
                api_key_override="sk-fake",
                client_factory=lambda key: scripted,
                model="claude-sonnet-4-6",
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
            )
        # Wrapper should have aborted after the first attempt - success
        # response from the second script slot must NOT have been consumed.
        assert len(scripted._calls) == 1
        ClaudeHistoryStore.reset_for_tests()

    def test_missing_api_key_raises(self, tmp_path, tmp_logs_dir, monkeypatch):
        import analyzers.claude_client as cc
        from analyzers.claude_history_store import ClaudeHistoryStore
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )
        monkeypatch.setattr(cc, "_get_api_key", lambda: "")

        from analyzers.claude_client import ClaudeCallError
        with pytest.raises(ClaudeCallError) as ei:
            cc.call_messages_create(
                source="unit_test",
                model="m1",
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
            )
        assert ei.value.error_class == "MissingCredential"
        ClaudeHistoryStore.reset_for_tests()

    def test_missing_anthropic_sdk_returns_actionable_error(self, tmp_path, tmp_logs_dir):
        """When the anthropic SDK is uninstalled (fresh Docker image, etc.)
        the wrapper must surface a MissingSDK error with pip-install guidance
        - not the raw ``No module named 'anthropic'`` the user reported.
        """
        import builtins
        import sys
        from unittest.mock import patch
        import analyzers.claude_client as cc
        from analyzers.claude_history_store import ClaudeHistoryStore
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )

        sys.modules.pop("anthropic", None)
        orig = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError(f"No module named {name!r}")
            return orig(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked):
            from analyzers.claude_client import ClaudeCallError
            with pytest.raises(ClaudeCallError) as ei:
                cc.call_messages_create(
                    source="test",
                    api_key_override="sk-fake",
                    model="claude-sonnet-4-6",
                    max_tokens=16,
                    messages=[{"role": "user", "content": "hi"}],
                )
        assert ei.value.error_class == "MissingSDK"
        msg = str(ei.value)
        assert "pip install" in msg
        assert "anthropic" in msg
        # The installation hint must be self-contained - a user hitting this
        # shouldn't need to open a doc to know what to type.
        assert "rebuild the image" in msg or "restart" in msg
        ClaudeHistoryStore.reset_for_tests()

    def test_test_connectivity_returns_dict(self, tmp_path, tmp_logs_dir, monkeypatch):
        import analyzers.claude_client as cc
        from analyzers.claude_history_store import ClaudeHistoryStore
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )

        scripted = _FakeClient([_fake_response(in_tokens=4, out_tokens=2)])
        # Patch the default factory so the test doesn't hit the real anthropic SDK
        monkeypatch.setattr(
            cc, "call_messages_create",
            lambda **kw: cc.ClaudeCallResult(
                response=_fake_response(),
                request_id="req-test",
                model=kw["model"],
                input_tokens=4, output_tokens=2,
                cache_read_tokens=0, cache_creation_tokens=0,
                cost_usd=0.0001, latency_ms=50, attempts=1,
            ),
        )
        result = cc.test_connectivity(api_key="sk-ok")
        assert result["ok"] is True
        assert result["input_tokens"] == 4
        assert result["attempts"] == 1
        ClaudeHistoryStore.reset_for_tests()


# ══════════════════════════════════════════════════════════════════════
# H-AN-7: batch-submit request payload must route through the scrubber
# ══════════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: _call_batch_api now imports
# redact_kwargs / scrub_secrets from analyzers/_scrub.py and applies them
# before every ClaudeHistoryStore.record_call on the batch-submit path.
# Parallel to the live-path coverage that already existed for
# call_messages_create.

class TestScrubHelpersShared:
    """The claude_client aliases must reach the shared module."""

    def test_claude_client_aliases_point_at_shared_module(self):
        import analyzers.claude_client as cc
        from analyzers import _scrub

        # The aliases exposed for back-compat (_scrub_secrets /
        # _redact_kwargs) must be the same callables as the public
        # scrub.py helpers. Otherwise a bug fix in _scrub could silently
        # miss one of the two code paths.
        assert cc._scrub_secrets is _scrub.scrub_secrets
        assert cc._redact_kwargs is _scrub.redact_kwargs

    def test_scrub_secrets_redacts_sk_ant_token(self):
        from analyzers._scrub import scrub_secrets
        out = scrub_secrets(
            "operator pasted sk-ant-api03-ABCDEFGH1234567890abcdef into the prompt"
        )
        assert "sk-ant-api03" not in out
        assert "[REDACTED]" in out

    def test_scrub_secrets_walks_nested_messages(self):
        from analyzers._scrub import scrub_secrets
        payload = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {"role": "user", "content": "here is my key sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxx"},
                {"role": "assistant", "content": "ok"},
            ],
        }
        scrubbed = scrub_secrets(payload)
        assert "sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxx" not in str(scrubbed)
        # Non-secret fields preserved.
        assert scrubbed["model"] == "claude-sonnet-4-6"
        assert scrubbed["messages"][1]["content"] == "ok"


class TestBatchSubmitRedacts:
    """End-to-end: _call_batch_api must write a scrubbed payload to history."""

    def test_batch_submit_redacts_sk_ant(self, tmp_path, monkeypatch):
        """Operator-pasted sk-ant token in user_content must NOT reach claude_api_history.sqlite."""
        from analyzers.claude_history_store import ClaudeHistoryStore

        # Point history DB at a tmp file so we can inspect the row.
        ClaudeHistoryStore._instance = ClaudeHistoryStore(
            db_path=tmp_path / "hist.sqlite"
        )

        # Build a minimal ClaudeAnalyzer with enough shape for
        # _call_batch_api. We don't need the full __init__ path - we stub
        # _config and _api_key directly, then invoke _call_batch_api with
        # a patched anthropic client that returns a fake batch.
        from analyzers import claude_analyzer as cam

        class _FakeBatchesAPI:
            def create(self, requests):
                # Mimic anthropic's Batch response shape (only .id used).
                ns = MagicMock()
                ns.id = "batch_fake_abc123"
                return ns

        class _FakeBatchesHolder:
            def __init__(self):
                self.batches = _FakeBatchesAPI()

        class _FakeAnthropic:
            def __init__(self, api_key=None):
                self.messages = _FakeBatchesHolder()

        # Swap the anthropic module lazy-imported inside _call_batch_api.
        monkeypatch.setitem(
            __import__("sys").modules, "anthropic",
            MagicMock(Anthropic=_FakeAnthropic),
        )

        # Build a bare ClaudeAnalyzer. Real constructor does more; we bypass
        # by creating via __new__ and wiring only the attributes the method
        # reads.
        analyzer = cam.ClaudeAnalyzer.__new__(cam.ClaudeAnalyzer)
        analyzer._api_key = "sk-fake-key-not-used-by-mock"
        analyzer._config = MagicMock(max_output_tokens=512)

        secret = "sk-ant-api03-ABCDEFGHijklmnop1234567890"
        leaky_system = f"Example: api_key={secret} is the shape expected"
        leaky_user = f"Hey, my key is {secret}, please use it."

        batch_id = analyzer._call_batch_api(
            model="claude-sonnet-4-6",
            system_prompt=leaky_system,
            user_content=leaky_user,
            custom_id="custom_regression_h_an_7",
        )
        assert batch_id == "batch_fake_abc123"

        # Verify the row landed WITHOUT the raw secret. list_calls does
        # not include payloads by default (expensive for large pages) -
        # explicitly request them for this audit.
        calls = ClaudeHistoryStore.get_instance().list_calls(
            source="batch_submit", limit=10, include_payloads=True,
        )
        assert calls, "Expected a batch_submit row to be recorded"

        # Inspect the stored request_body - must not contain the raw token.
        for row in calls:
            body_str = str(row.get("request_body") or "")
            assert secret not in body_str, (
                f"Raw sk-ant token leaked into claude_api_history: "
                f"{body_str[:500]!r}"
            )
            assert "[REDACTED]" in body_str, (
                "Expected [REDACTED] sentinel in the scrubbed body; "
                f"got {body_str[:500]!r}"
            )

        ClaudeHistoryStore.reset_for_tests()
