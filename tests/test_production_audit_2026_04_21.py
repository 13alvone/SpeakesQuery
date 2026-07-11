#!/usr/bin/env python3
"""
Regression tests for the 2026-04-21 production audit.

After the two waves of Alert Group dispatcher fixes, a thorough
production-level review surfaced 9+ additional issues across bugs,
inefficiencies, inconsistencies, orphaned code, and doc drift. This
test file pins the fixes so they don't silently regress.

Categories covered:

- **SPQL listener idempotency** (triple-read fix): a guard flag on
  ``exitSpeakesQuery`` prevents the whole pipeline from running twice when
  both the ANTLR native dispatch AND the manual ``exitEveryRule`` hook
  fire for the same ``SpeakesQueryContext``.
- **``global_settings.defaults.yaml`` mirrors Python ``DEFAULTS``**:
  every key + value identical (previously the YAML was 16 keys behind).
- **Atomic writes enforced**: no ``open(path, "w")`` bypassing
  ``functionality/atomic_write.py`` in project source. 3 violations
  fixed (analyzer_prompt_store.py + two in GeneralHandler.py).
- **Dead code removed**: ``SavedSearchValidation`` module-level import
  and instantiation in ``CmdExecutionBackend.py`` (unused).
- **DataFrame memory bomb in logs**: query results are logged by shape,
  not full content.
- **Chatty INFO logs downgraded to DEBUG**: ``SearchCmdHandler`` no
  longer emits 2 INFO lines per pipe per feeder in hot paths.
- **Swallowed exceptions surfaced**: credential audit + global_settings
  config-change logging failures now emit a warning instead of
  passing silently.
- **Claude API key cached with 60s TTL**: avoids re-opening the vault
  on every retry.
- **Secret redaction in Claude history**: ``sk-ant-*`` patterns in
  request/response bodies are regex-redacted to ``[REDACTED]`` before
  hitting ``claude_api_history.sqlite``.
- **SavedSearchStore shared across dispatcher feeder loop**: one
  initialisation per process, not per feeder per AG.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# =====================================================================
# Part 1: SPQL listener idempotency (triple-read fix)
# =====================================================================

class TestListenerIdempotency:

    def test_exitSpeakesQuery_has_idempotency_guard(self):
        """The guard flag ``_exit_speakesquery_ran`` must appear in the
        listener. A refactor that drops it causes the whole pipeline
        (Parquet reads, where, table, sort, head, stats) to run 2-3×
        per query."""
        path = Path(PROJECT_ROOT) / "lexers" / "speakesQueryListener.py"
        text = path.read_text()
        assert "_exit_speakesquery_ran" in text, (
            "Idempotency guard removed from listener. Restore it or "
            "explain in a comment WHY it's no longer needed."
        )
        assert "getattr(self, \"_exit_speakesquery_ran\"" in text, (
            "Idempotency guard must use getattr (safe on fresh "
            "listener without the attribute set)."
        )

    def test_single_query_triggers_at_most_two_index_reads(self):
        """End-to-end: an SPQL query hitting a real parquet file should
        call ``process_index_calls`` at most twice (once from
        exitExpression, once from exitSpeakesQuery). Pre-fix: 3 calls
        due to the duplicate exitSpeakesQuery firing."""
        from query_engine.CmdExecutionBackend import process_query_with_diagnostics

        call_count = {"n": 0}

        def _counting_call(tokens, *args, **kwargs):
            call_count["n"] += 1
            # Return a tiny DataFrame so the pipeline can run.
            return pd.DataFrame({"x": [1, 2, 3], "_epoch": [1, 2, 3]})

        with patch(
            "lexers.speakesQueryListener.process_index_calls",
            side_effect=_counting_call,
        ):
            df, job_id, diag = process_query_with_diagnostics(
                'index="indexes/fake/*.parquet" | head 2',
            )

        # Expect 2 calls (exitExpression + exitSpeakesQuery), NOT 3.
        assert call_count["n"] <= 2, (
            f"Expected at most 2 process_index_calls, got {call_count['n']}. "
            "The idempotency guard on exitSpeakesQuery is failing and the "
            "whole pipeline is running twice per query."
        )


# =====================================================================
# Part 2: Defaults YAML mirrors Python DEFAULTS
# =====================================================================

class TestDefaultsYamlInSync:

    def test_every_python_default_has_yaml_entry(self):
        import yaml
        from global_settings import DEFAULTS
        yaml_path = Path(PROJECT_ROOT) / "global_settings.defaults.yaml"
        loaded = yaml.safe_load(yaml_path.read_text()) or {}
        missing = set(DEFAULTS) - set(loaded)
        assert not missing, (
            f"{yaml_path.name} is missing these keys from Python "
            f"DEFAULTS: {sorted(missing)}. The YAML is the reference "
            "file operators read - keep it in sync."
        )

    def test_no_extra_keys_in_yaml(self):
        import yaml
        from global_settings import DEFAULTS
        yaml_path = Path(PROJECT_ROOT) / "global_settings.defaults.yaml"
        loaded = yaml.safe_load(yaml_path.read_text()) or {}
        extra = set(loaded) - set(DEFAULTS)
        assert not extra, (
            f"{yaml_path.name} has keys NOT in Python DEFAULTS: "
            f"{sorted(extra)}. Either add to DEFAULTS or remove from yaml."
        )

    def test_yaml_values_match_defaults(self):
        import yaml
        from global_settings import DEFAULTS
        yaml_path = Path(PROJECT_ROOT) / "global_settings.defaults.yaml"
        loaded = yaml.safe_load(yaml_path.read_text()) or {}
        mismatches = []
        for key in DEFAULTS:
            if key in loaded and DEFAULTS[key] != loaded[key]:
                mismatches.append((key, DEFAULTS[key], loaded[key]))
        assert not mismatches, (
            "Value mismatches between DEFAULTS and YAML:\n"
            + "\n".join(f"  {k}: py={dv!r} yaml={yv!r}" for k, dv, yv in mismatches)
        )


# =====================================================================
# Part 3: Atomic writes enforced
# =====================================================================

class TestAtomicWritesEnforced:

    def test_no_bare_open_write_in_store_modules(self):
        """Any ``*_store.py`` file writing YAML must route through
        ``write_text_atomic`` (crash-safe rename). A bare
        ``open(path, "w")`` leaves truncated files on SIGKILL."""
        project_root = Path(PROJECT_ROOT)
        offenders = []
        for store in project_root.glob("*_store.py"):
            text = store.read_text()
            # Find any open(path, "w", ...) - skip comments/docstrings.
            if re.search(r'open\s*\([^)]*,\s*["\']w["\']', text):
                # Confirm it's not inside a comment by checking the line
                # doesn't start with #
                for line in text.splitlines():
                    if re.search(r'open\s*\([^)]*,\s*["\']w["\']', line) \
                       and not line.lstrip().startswith("#"):
                        offenders.append(f"{store.name}: {line.strip()}")
        assert not offenders, (
            "Non-atomic open() calls in store modules:\n"
            + "\n".join(offenders)
            + "\nUse functionality.atomic_write.write_text_atomic instead."
        )

    def test_generalhandler_yaml_output_is_atomic(self):
        """``GeneralHandler.execute_outputlookup`` / ``execute_outputnew``
        write YAML via atomic_write, not bare open."""
        path = Path(PROJECT_ROOT) / "handlers" / "GeneralHandler.py"
        text = path.read_text()
        # The combined "open(w) ... yaml.dump" pattern should be absent.
        bad = 'with open(filename, "w", encoding="utf-8") as f:\n                yaml.dump'
        assert bad not in text, (
            "execute_outputlookup still uses bare open(). Use "
            "functionality.atomic_write.write_text_atomic."
        )


# =====================================================================
# Part 4: Dead code removed
# =====================================================================

class TestDeadCodeRemoved:

    def test_cmd_execution_backend_no_unused_validator(self):
        """``validator = SavedSearchValidation()`` was a module-level
        instance with zero usages - dead init on every import."""
        path = Path(PROJECT_ROOT) / "query_engine" / "CmdExecutionBackend.py"
        text = path.read_text()
        # Strip comments so we don't false-positive on the comment
        # explaining the removal.
        code_only = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "validator = SavedSearchValidation()" not in code_only, (
            "The dead ``validator = SavedSearchValidation()`` module "
            "init came back. It has zero usages - remove it."
        )
        assert "from validation.SavedSearchValidation" not in code_only, (
            "The unused SavedSearchValidation import is still there."
        )


# =====================================================================
# Part 5: Log verbosity
# =====================================================================

class TestLogVerbosity:

    def test_query_result_logged_by_shape_not_content(self):
        """``run_query_and_return_results_df`` logs the DF's shape, not
        its full string repr - prevents OOM on million-row results."""
        path = Path(PROJECT_ROOT) / "query_engine" / "CmdExecutionBackend.py"
        text = path.read_text()
        # The old "Query result before processing: {result_df}" pattern
        # (no shape) is forbidden because it stringifies the entire DF.
        assert 'Query result before processing: {result_df}' not in text, (
            "CmdExecutionBackend still logs the full DataFrame. Use "
            "shape logging (row count × col count) instead."
        )
        assert 'Query result shape:' in text, (
            "Shape-based query log line missing. Either add it back "
            "or explain the removal in a comment."
        )

    def test_search_handler_intermediate_logs_are_debug(self):
        """The ``Generated Pandas query`` + ``DataFrame filtered`` lines
        run once per pipe per feeder. With 10 feeders × 5 pipes × 2
        messages = 100 INFO lines per AG dispatch. They're now DEBUG so
        they don't flood the log, but are still available with
        ``--log-level=DEBUG`` when investigating."""
        path = Path(PROJECT_ROOT) / "handlers" / "SearchCmdHandler.py"
        text = path.read_text()
        # These phrases must now be on debug lines, not info.
        for phrase in (
            "Generated Pandas query",
            "Pandas query is 'True'",
            "DataFrame filtered. Rows before",
        ):
            # A simple heuristic: find the line + check logger call
            for line in text.splitlines():
                if phrase in line and "logging." in line:
                    assert ".debug" in line or ".DEBUG" in line, (
                        f"'{phrase}' is still logged at INFO level: {line.strip()}"
                    )


# =====================================================================
# Part 6: Swallowed exceptions surfaced
# =====================================================================

class TestSwallowedExceptionsSurfaced:

    def test_credential_audit_logs_exception(self):
        """``_emit_credential_event`` logs audit-trail writes; prior
        ``except Exception: pass`` hid permission errors in the logs
        tree. Now logs a warning."""
        path = Path(PROJECT_ROOT) / "scheduled_input_engine" / "credentials.py"
        text = path.read_text()
        # After the fix, the except block should call .warning(
        assert "Could not record credential audit event" in text, (
            "Credential audit logging no longer surfaces the reason "
            "on log-writer failure. Bring back the warning so the "
            "operator can investigate."
        )

    def test_global_settings_config_change_logs_exception(self):
        """``_emit_config_change_safely`` - similar pattern."""
        path = Path(PROJECT_ROOT) / "global_settings.py"
        text = path.read_text()
        assert "Config-change audit log failed" in text, (
            "global_settings swallowed config-change logging failures "
            "silently. Bring back the warning."
        )


# =====================================================================
# Part 7: Claude API key cache
# =====================================================================

class TestClaudeApiKeyCache:

    def setup_method(self):
        from analyzers import claude_client
        claude_client._invalidate_api_key_cache()

    def test_get_api_key_caches_within_ttl(self):
        """Two back-to-back calls within the TTL open the vault once,
        not twice."""
        from analyzers import claude_client

        open_count = {"n": 0}

        class _FakeVault:
            def __init__(self, *_a, **_kw):
                open_count["n"] += 1

            def retrieve(self, _id, _key):
                return "sk-ant-api03-test"

        class _FakeSettings:
            def get(self, key, default=None):
                return default

        def _fake_get_settings():
            return _FakeSettings()

        with patch(
            "scheduled_input_engine.credentials.CredentialVault", _FakeVault,
        ):
            with patch(
                "global_settings.get_settings", _fake_get_settings,
            ):
                k1 = claude_client._get_api_key()
                k2 = claude_client._get_api_key()
        assert k1 == k2 == "sk-ant-api03-test"
        assert open_count["n"] == 1, (
            f"Vault was opened {open_count['n']} times, expected 1. "
            "The API key cache is not taking effect."
        )

    def test_invalidate_cache_forces_refetch(self):
        from analyzers import claude_client

        open_count = {"n": 0}

        class _FakeVault:
            def __init__(self, *_a, **_kw):
                open_count["n"] += 1

            def retrieve(self, _id, _key):
                return "sk-ant-api03-test"

        class _FakeSettings:
            def get(self, key, default=None):
                return default

        with patch(
            "scheduled_input_engine.credentials.CredentialVault", _FakeVault,
        ):
            with patch(
                "global_settings.get_settings", lambda: _FakeSettings(),
            ):
                claude_client._get_api_key()
                claude_client._invalidate_api_key_cache()
                claude_client._get_api_key()
        assert open_count["n"] == 2


# =====================================================================
# Part 8: Secret redaction in Claude history
# =====================================================================

class TestSecretRedaction:

    def test_sk_ant_token_is_redacted_in_request_body(self):
        """Even if a user accidentally pastes an Anthropic API key into
        a prompt, it must NOT land in claude_api_history.sqlite."""
        from analyzers.claude_client import _redact_kwargs

        messages = [
            {"role": "user", "content": "Example: my key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 please"},
        ]
        redacted = _redact_kwargs({"messages": messages, "model": "claude-sonnet-4-6"})
        content = redacted["messages"][0]["content"]
        assert "sk-ant-" not in content, (
            f"Anthropic API key leaked into redacted kwargs: {content!r}"
        )
        assert "[REDACTED]" in content

    def test_nested_system_prompt_also_redacted(self):
        from analyzers.claude_client import _redact_kwargs
        kwargs = {
            "system": "You are a helper. Do NOT leak sk-ant-api03-zzzzzzzzzzzzzzzzzzzzzzzzzzz1 ever.",
            "model": "claude-sonnet-4-6",
        }
        redacted = _redact_kwargs(kwargs)
        assert "sk-ant-" not in redacted["system"]

    def test_non_secret_strings_pass_through(self):
        from analyzers.claude_client import _redact_kwargs
        kwargs = {"messages": [{"role": "user", "content": "Nothing sensitive here."}]}
        redacted = _redact_kwargs(kwargs)
        assert redacted["messages"][0]["content"] == "Nothing sensitive here."


# =====================================================================
# Part 9: SavedSearchStore shared across feeder loop
# =====================================================================

class TestSharedSavedSearchStore:

    def test_store_shared_across_multiple_feeder_executions(self):
        """Two feeder executions on the same dispatcher instance must
        reuse the same SavedSearchStore singleton (not re-initialise
        per feeder)."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        AlertGroupDispatcher._reset_ss_store_cache()

        init_count = {"n": 0}

        class _FakeStore:
            def __init__(self):
                init_count["n"] += 1

            def initialize(self):
                return None

            def get_search(self, _name):
                raise FileNotFoundError("no such search in test stub")

        with patch("saved_search_store.SavedSearchStore", _FakeStore):
            # Two feeder lookups from same dispatcher:
            AlertGroupDispatcher._execute_feeder_query_now(
                "feeder_a", group_name="ag_test",
            )
            AlertGroupDispatcher._execute_feeder_query_now(
                "feeder_b", group_name="ag_test",
            )
        # Exactly one init, not two.
        assert init_count["n"] == 1, (
            f"SavedSearchStore was instantiated {init_count['n']} "
            "times for 2 feeders. The shared singleton is not taking "
            "effect."
        )

    def test_reset_helper_forces_reinit(self):
        """``_reset_ss_store_cache`` is the test-only escape hatch -
        verify it re-initialises on next call."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        AlertGroupDispatcher._reset_ss_store_cache()

        init_count = {"n": 0}

        class _FakeStore:
            def __init__(self):
                init_count["n"] += 1

            def initialize(self):
                return None

            def get_search(self, _name):
                raise FileNotFoundError("stub")

        with patch("saved_search_store.SavedSearchStore", _FakeStore):
            AlertGroupDispatcher._execute_feeder_query_now(
                "x", group_name="ag_test",
            )
            AlertGroupDispatcher._reset_ss_store_cache()
            AlertGroupDispatcher._execute_feeder_query_now(
                "y", group_name="ag_test",
            )
        assert init_count["n"] == 2
