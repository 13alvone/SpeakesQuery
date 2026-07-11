"""MEDIUMs batch 4 - M-AN-12, M-AN-13, M-SV-4 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-AN-12** - new ``AlertGroupDispatcher._extract_response_meta``
    sibling helper returns ``(text, meta)`` where meta carries
    ``stop_reason``, ``block_types``, ``block_count``,
    ``text_block_count``. The empty-text fail-fast branch now cites
    block types in ``result.error_message`` so the audit row is
    self-describing.
  * **M-AN-13** - ``_call_batch_api`` wraps the error-path
    ``ClaudeHistoryStore.record_call`` in a try/except so a DB-lock
    contention inside the recorder can't replace the original
    ``batches.create`` exception.
  * **M-SV-4** - ``CredentialVault.decrypt_for_script`` now returns
    ``None`` when the vault has no rows for the script (vs. the
    historical empty ``MappingProxyType({})``), letting the engine
    skip ``CREDENTIALS`` injection entirely for honest "no creds"
    scripts.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# M-AN-12: _extract_response_meta returns structured diagnostics
# ======================================================================

class TestExtractResponseMeta:

    def _resp(self, content=None, stop_reason="end_turn"):
        r = MagicMock()
        r.content = [] if content is None else content
        r.stop_reason = stop_reason
        return r

    def test_well_formed_response_returns_text_and_meta(self):
        from alert_groups.dispatcher import AlertGroupDispatcher

        text_block = MagicMock()
        text_block.text = "analysis text"
        resp = self._resp(content=[text_block], stop_reason="end_turn")

        text, meta = AlertGroupDispatcher._extract_response_meta(resp)
        assert text == "analysis text"
        assert meta["stop_reason"] == "end_turn"
        assert meta["block_count"] == 1
        assert meta["text_block_count"] == 1
        assert meta["block_types"] == [type(text_block).__name__]

    def test_tool_only_response_meta(self):
        from alert_groups.dispatcher import AlertGroupDispatcher

        class _ToolUseBlock:
            pass
        tool_block = _ToolUseBlock()
        resp = self._resp(content=[tool_block], stop_reason="tool_use")

        text, meta = AlertGroupDispatcher._extract_response_meta(resp)
        assert text == ""
        assert meta["stop_reason"] == "tool_use"
        assert meta["block_count"] == 1
        assert meta["text_block_count"] == 0
        assert "_ToolUseBlock" in meta["block_types"]

    def test_none_response_meta(self):
        from alert_groups.dispatcher import AlertGroupDispatcher

        text, meta = AlertGroupDispatcher._extract_response_meta(None)
        assert text == ""
        assert meta["block_count"] == 0
        assert meta["block_types"] == []
        assert meta["text_block_count"] == 0

    def test_legacy_extract_response_text_still_works(self):
        """M-AN-12 must not break the 6+ existing single-valued call sites."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        text_block = MagicMock()
        text_block.text = "hello"
        resp = self._resp(content=[text_block])
        assert AlertGroupDispatcher._extract_response_text(resp) == "hello"

    def test_failfast_error_message_includes_block_types(self):
        """The dispatcher's empty-text branch now cites block types in the audit row."""
        from analyzers.claude_client import ClaudeCallResult
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.serializer import ResultSerializer
        import pandas as pd

        class _ToolUseBlock:
            pass

        tool_response = MagicMock()
        tool_response.content = [_ToolUseBlock()]
        tool_response.stop_reason = "tool_use"

        call_result = ClaudeCallResult(
            response=tool_response,
            request_id="rid-mblock",
            model="claude-sonnet-4-6",
            input_tokens=50,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.0002,
            latency_ms=400,
            attempts=1,
        )

        group = {
            "name": "ag_mblock", "disabled": False, "max_rows": 10,
            "search_names": ["s1"], "prompt_text": "go",
            "email_address": "user@example.com",
        }
        fake_df = pd.DataFrame({"value": [1], "_epoch": [0]})

        with patch.object(ResultSerializer, "_load_last_result", return_value=fake_df), \
             patch("alert_groups.dispatcher.call_messages_create", return_value=call_result), \
             patch.object(AlertGroupDispatcher, "_send_plain_email", staticmethod(lambda *a, **kw: None)), \
             patch.object(AlertGroupDispatcher, "_log_run", lambda self, r: None), \
             patch.object(AlertGroupDispatcher, "_emit_log", lambda self, r, s, dry_run=False: None):
            d = AlertGroupDispatcher()
            result = d.run(group)

        assert result.status == "error"
        msg = result.error_message or ""
        # M-AN-12 guarantees: stop_reason + block_types must be in the row.
        assert "tool_use" in msg, (
            f"Expected stop_reason in error_message; got {msg!r}"
        )
        assert "_ToolUseBlock" in msg, (
            f"Expected block_types in error_message; got {msg!r}"
        )


# ======================================================================
# M-AN-13: _call_batch_api error-path records history defensively
# ======================================================================

class TestBatchSubmitErrorPathDefense:

    def test_original_exception_reaches_caller_even_if_recorder_fails(self, tmp_path, monkeypatch):
        """A DB failure inside record_call must not replace the batches.create exception."""
        from analyzers.claude_analyzer import ClaudeAnalyzer
        from analyzers import claude_analyzer as cam

        # Make anthropic.batches.create raise - this is what the operator
        # cares about seeing propagate.
        class _CreateFail(Exception):
            pass
        _CreateFail.__name__ = "APIConnectionError"

        class _FakeBatchesAPI:
            def create(self, requests):
                raise _CreateFail("upstream broke")

        class _FakeBatchesHolder:
            def __init__(self):
                self.batches = _FakeBatchesAPI()

        class _FakeAnthropic:
            def __init__(self, api_key=None):
                self.messages = _FakeBatchesHolder()

        monkeypatch.setitem(
            __import__("sys").modules, "anthropic",
            MagicMock(Anthropic=_FakeAnthropic),
        )

        # Now make ClaudeHistoryStore.record_call ALSO raise - to simulate
        # DB-lock contention inside the recorder.
        from analyzers.claude_history_store import ClaudeHistoryStore
        original_record = ClaudeHistoryStore.record_call

        def boom_record(self, **kw):
            raise RuntimeError("DB locked")

        monkeypatch.setattr(
            ClaudeHistoryStore, "record_call", boom_record,
        )

        analyzer = ClaudeAnalyzer.__new__(ClaudeAnalyzer)
        analyzer._api_key = "sk-fake"
        analyzer._config = MagicMock(max_output_tokens=256)

        # The batches.create exception (APIConnectionError) must propagate
        # unchanged. The recorder failure is logged, not raised.
        with pytest.raises(_CreateFail) as excinfo:
            analyzer._call_batch_api(
                model="claude-sonnet-4-6",
                system_prompt="sys",
                user_content="usr",
                custom_id="cid-1",
            )
        assert "upstream broke" in str(excinfo.value), (
            f"Original batches.create exception was replaced by the "
            f"recorder's error; got {excinfo.value!r}"
        )

        # Restore for cleanliness.
        monkeypatch.setattr(ClaudeHistoryStore, "record_call", original_record)


# ======================================================================
# M-SV-4: decrypt_for_script returns None for empty vault
# ======================================================================

class TestVaultNoneOnEmpty:

    def test_none_for_unknown_script(self, tmp_path, monkeypatch):
        """Zero-row lookup returns None (not an empty MappingProxyType)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from scheduled_input_engine.credentials import CredentialVault

        # Point the vault at an isolated db_path so other tests don't pollute.
        vault = CredentialVault(
            db_path=str(tmp_path / "creds.sqlite"),
            key_dir=str(tmp_path / "keys"),
        )
        result = vault.decrypt_for_script(999)
        assert result is None

    def test_populated_script_still_returns_mapping(self, tmp_path, monkeypatch):
        """When creds exist, the return is a MappingProxyType (back-compat)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from scheduled_input_engine.credentials import CredentialVault

        vault = CredentialVault(
            db_path=str(tmp_path / "creds2.sqlite"),
            key_dir=str(tmp_path / "keys2"),
        )
        vault.store(42, "API_KEY", "ghp_sample_1234")
        result = vault.decrypt_for_script(42)
        assert isinstance(result, MappingProxyType)
        assert dict(result) == {"API_KEY": "ghp_sample_1234"}

    def test_engine_skips_credentials_injection_when_vault_returns_none(self, tmp_path, monkeypatch):
        """_run_task must not add CREDENTIALS to the sandbox when there are no creds."""
        from scheduled_input_engine.engine import ScheduledInputEngine
        from scheduled_input_engine import engine as engine_mod

        engine = ScheduledInputEngine()

        class _NoneVault:
            def decrypt_for_script(self, _task_id):
                return None

        engine._vault = _NoneVault()

        captured = {}

        def fake_execute(self, extra_globals=None):
            captured["extra"] = dict(extra_globals or {})
            import pandas as _pd
            return _pd.DataFrame({"x": [1], "_epoch": [1]})

        monkeypatch.setattr(engine_mod.CodeExecutor, "execute", fake_execute)
        monkeypatch.setattr(
            engine_mod.ParquetWriter, "write_atomic",
            lambda self, *a, **kw: Path("/tmp/x.parquet"),
        )

        try:
            task = {
                "id": 777,
                "title": "no_creds_task",
                "trust_level": "sandboxed",
                "code": (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'_epoch': [1]})\n"
                    "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
                ),
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_msv4",
            }
            engine._run_task(task)
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

        assert "CREDENTIALS" not in captured["extra"], (
            "When the vault returns None, the engine must NOT inject a "
            "CREDENTIALS key (old behaviour injected an empty dict, "
            "which masked 'no creds registered' as 'empty creds dict')."
        )

    def test_engine_still_injects_credentials_when_mapping_non_empty(self, tmp_path, monkeypatch):
        """A populated MappingProxyType IS injected (back-compat)."""
        from scheduled_input_engine.engine import ScheduledInputEngine
        from scheduled_input_engine import engine as engine_mod

        engine = ScheduledInputEngine()

        class _RealVault:
            def decrypt_for_script(self, _task_id):
                return MappingProxyType({"API_KEY": "sk-fake"})

        engine._vault = _RealVault()

        captured = {}

        def fake_execute(self, extra_globals=None):
            captured["extra"] = dict(extra_globals or {})
            import pandas as _pd
            return _pd.DataFrame({"x": [1], "_epoch": [1]})

        monkeypatch.setattr(engine_mod.CodeExecutor, "execute", fake_execute)
        monkeypatch.setattr(
            engine_mod.ParquetWriter, "write_atomic",
            lambda self, *a, **kw: Path("/tmp/y.parquet"),
        )

        try:
            task = {
                "id": 778,
                "title": "with_creds_task",
                "trust_level": "sandboxed",
                "code": (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'_epoch': [1]})\n"
                    "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
                ),
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_msv4_real",
            }
            engine._run_task(task)
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

        # extra.pop("CREDENTIALS", None) runs in the finally from M-CE-7,
        # so by the time we capture `extra` via fake_execute, CREDENTIALS
        # is still there because fake_execute captures BEFORE the finally
        # pops (captured["extra"] was snapshotted inside execute()).
        assert captured["extra"].get("CREDENTIALS") == {"API_KEY": "sk-fake"}, (
            f"Expected CREDENTIALS injected when vault has rows; "
            f"got {captured['extra'].get('CREDENTIALS')!r}"
        )
