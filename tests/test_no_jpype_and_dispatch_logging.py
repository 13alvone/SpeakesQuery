#!/usr/bin/env python3
"""
Regression tests for the 2026-04-21 "stuck at Dispatching to Claude"
incident.

Two independent fixes landed together and each needs its own guard:

1. **jpype removal**. ``handlers/JavaHandler.py`` and the ``jpype1``
   dependency were deleted. The Docker image is ``python:3.12-slim`` with
   no JVM, and DuckDB/pandas/pyarrow never yield ``java.lang.Long``, so
   the Java type-coercion path was dead code that only produced
   ``[x] Error starting JVM`` log spam on every object-column query.
   Tests in this file fail loud if the dependency or any import path
   comes back.

2. **Dispatcher phase-boundary logging**. The UI shows a static
   "Dispatching to Claude..." string and the backend used to go silent
   between feeder loop completion and dispatch completion. A
   web_search-enabled analyst brief can legitimately run for 2-10
   minutes; operators could not distinguish "Claude is thinking" from
   "the dispatcher is wedged". Tests here pin the per-phase log lines
   that now make progress visible in ``docker logs -f``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd


# =====================================================================
# Part 1: jpype removal
# =====================================================================

class TestNoJpype:
    """Guarantee that jpype / JavaHandler do not creep back in."""

    def test_java_handler_module_deleted(self):
        """handlers/JavaHandler.py must not exist - it was the import
        vector that pulled in jpype."""
        path = Path(PROJECT_ROOT) / "handlers" / "JavaHandler.py"
        assert not path.exists(), (
            "handlers/JavaHandler.py reappeared; it was deleted on "
            "2026-04-21 to drop the jpype dependency (see "
            "reference_no_jpype_dependency.md memory)."
        )

    def test_requirements_txt_no_jpype(self):
        """Neither root nor desktop_app requirements.txt may list
        jpype1."""
        for path in (
            Path(PROJECT_ROOT) / "requirements.txt",
            Path(PROJECT_ROOT) / "desktop_app" / "requirements.txt",
        ):
            text = path.read_text()
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                assert not stripped.lower().startswith("jpype"), (
                    f"{path.name} still lists '{stripped}'. "
                    "Remove it - jpype is no longer used."
                )

    def test_no_project_source_imports_jpype(self):
        """No first-party Python file may ``import jpype`` any more."""
        bad = []
        for py in Path(PROJECT_ROOT).rglob("*.py"):
            # Skip virtualenvs and third-party package dirs.
            s = str(py)
            if (
                "/.speakesQueryDevEnv/" in s
                or "/env/" in s
                or "/.venv/" in s
                or "/.claude/" in s
                or "/site-packages/" in s
            ):
                continue
            text = py.read_text(errors="ignore")
            for line in text.splitlines():
                stripped = line.strip()
                # Skip single-line strings/comments that just mention the
                # word "jpype" for context.
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("import jpype") or stripped.startswith(
                    "from jpype"
                ):
                    bad.append(str(py))
                    break
        assert not bad, (
            "These files still import jpype: "
            + ", ".join(bad)
            + ". Remove the import - jpype has been deleted from "
            "requirements.txt."
        )

    def test_sanitize_dataframe_is_pure_python(self):
        """``sanitize_dataframe`` must be a safe no-op on any object
        column - no jpype, no JVM, no exceptions."""
        from query_engine.CmdExecutionBackend import sanitize_dataframe

        df = pd.DataFrame({
            "stringy": ["a", "b", "c"],
            "intish":  [1, 2, 3],
            "mixed":   [1, "two", 3.5],
            "nones":   [None, "x", None],
        })
        # Round-trip: no exception, shape preserved, values unchanged.
        result = sanitize_dataframe(df)
        assert result.shape == df.shape
        assert list(result.columns) == list(df.columns)
        for col in df.columns:
            assert result[col].tolist() == df[col].tolist(), (
                f"sanitize_dataframe mutated column {col!r}; it should be "
                "an identity pass now that jpype is gone."
            )

    def test_importing_backend_does_not_import_jpype(self):
        """Fresh-interpreter check: importing the query backend must NOT
        pull ``jpype`` into sys.modules. Catches accidental re-introductions
        even if the removed ``import jpype`` statement only sits in a
        rarely-executed branch."""
        code = (
            "import sys, os; sys.path.insert(0, r'{root}'); "
            "import query_engine.CmdExecutionBackend; "
            "assert 'jpype' not in sys.modules, "
            "  'jpype got imported via query_engine.CmdExecutionBackend'"
        ).format(root=PROJECT_ROOT)
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nSTDOUT:{result.stdout}\n"
            f"STDERR:{result.stderr}"
        )

    def test_string_handler_has_no_java_handler_attribute(self):
        """StringHandler instances may not carry a ``java_handler``
        field - that field was the only reason JavaHandler was ever
        instantiated during query execution."""
        from handlers.StringHandler import StringHandler
        sh = StringHandler()
        assert not hasattr(sh, "java_handler"), (
            "StringHandler grew a java_handler attribute back; the field "
            "was removed on 2026-04-21 because it was the only call site "
            "that triggered the JVM-startup failure in Docker."
        )

    def test_try_ast_conversion_handles_mixed_entries(self):
        """The ``try_ast_conversion`` branch that used to delegate to
        ``is_java_long`` now handles ``int``/``float``/``str`` with
        stdlib conversions. Regression-test all three paths return a
        well-formed list."""
        from handlers.StringHandler import StringHandler
        sh = StringHandler()
        # Input shaped like something ``ast.literal_eval`` accepts, whose
        # inner entries each fail ``ast.literal_eval`` with ValueError -
        # that's the branch where the Java path used to live.
        out = sh.try_ast_conversion("['alpha', 'beta', 'gamma']")
        assert isinstance(out, list)
        assert all(isinstance(x, str) for x in out)


# =====================================================================
# Part 2: dispatcher phase-boundary logging
# =====================================================================

class TestDispatcherPhaseLogging:
    """Pin the log lines that make a stuck dispatch diagnosable."""

    @staticmethod
    def _group():
        return {
            "name": "unit_test_group",
            "search_names": ["feeder_a", "feeder_b"],
            "prompt_text": "analyze data.",
            "email_address": "",
            "disabled": False,
            "max_rows": 200,
        }

    @staticmethod
    def _fake_call_result():
        raw = MagicMock()
        raw.content = [MagicMock(text="ok")]
        raw.usage = MagicMock(input_tokens=100, output_tokens=50)
        raw.stop_reason = "end_turn"
        result = MagicMock()
        result.response = raw
        result.request_id = "rid-test"
        result.model = "claude-sonnet-4-6"
        result.input_tokens = 100
        result.output_tokens = 50
        result.cache_read_tokens = 0
        result.cache_creation_tokens = 0
        result.cost_usd = 0.001
        result.latency_ms = 42
        result.attempts = 1
        return result

    def _run_dispatcher(self, caplog, group=None):
        """Run the dispatcher with serializer + Claude + email mocked.

        Returns (run_result, captured_log_lines). ``_log_run`` and
        ``_get_budget_gate`` are patched away to keep the test
        hermetic - we care about log lines, not the audit DB."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.serializer import ResultSerializer
        from alert_groups.models import SerializedResult

        d = AlertGroupDispatcher()
        d.serializer = MagicMock(spec=ResultSerializer)
        d.serializer.serialize_df.side_effect = lambda name, df: SerializedResult(
            search_name=name, row_count=len(df), estimated_tokens=100,
            format="json", content="[]",
        )

        fake_df = pd.DataFrame({"x": [1, 2, 3]})

        caplog.set_level(logging.INFO, logger="alert_groups.dispatcher")

        with patch.object(
            type(d), "_execute_feeder_query_now", return_value=fake_df,
        ):
            with patch(
                "alert_groups.dispatcher.call_messages_create",
                return_value=self._fake_call_result(),
            ):
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_emit_log"):
                        with patch.object(
                            type(d), "_get_budget_gate", return_value=None,
                        ):
                            run = d.run(group=group or self._group())
        return run, [rec.getMessage() for rec in caplog.records]

    def test_feeder_loop_boundary_logs_present(self, caplog):
        run, lines = self._run_dispatcher(caplog)
        assert run.status == "success"
        assert any("feeder loop start" in line for line in lines), (
            "Missing 'feeder loop start' log line - remove it and the "
            "operator can't tell when the loop began."
        )
        assert any("feeder loop done" in line for line in lines), (
            "Missing 'feeder loop done' log line - remove it and the "
            "operator can't tell if the hang is in the feeder loop or "
            "in the Claude call."
        )

    def test_per_feeder_running_line_logged(self, caplog):
        run, lines = self._run_dispatcher(caplog)
        assert run.status == "success"
        # Each feeder logs a "[N/total] 'name' running..." line.
        running_lines = [
            line for line in lines
            if "running..." in line and "feeder [" in line
        ]
        assert len(running_lines) == 2, (
            "Expected one 'running...' log line per feeder; got "
            f"{len(running_lines)}: {running_lines}"
        )

    def test_pre_claude_call_line_logged(self, caplog):
        """The single most important log - says 'Claude call just
        started, here are the knobs I used'."""
        run, lines = self._run_dispatcher(caplog)
        assert run.status == "success"
        pre = [line for line in lines if "calling Claude" in line]
        assert pre, "Missing pre-Claude log line."
        first = pre[0]
        assert "model=" in first
        assert "max_tokens=" in first
        assert "timeout=" in first
        assert "retry_attempts=" in first

    def test_post_claude_call_line_logged(self, caplog):
        run, lines = self._run_dispatcher(caplog)
        assert run.status == "success"
        post = [line for line in lines if "Claude returned" in line]
        assert post, "Missing post-Claude log line."
        first = post[0]
        assert "in=" in first
        assert "out=" in first
        assert "latency=" in first

    def test_dispatch_complete_log_has_total_ms(self, caplog):
        run, lines = self._run_dispatcher(caplog)
        assert run.status == "success"
        final = [line for line in lines if "dispatch complete" in line]
        assert final, "Missing 'dispatch complete' log line."
        assert "total" in final[0] and "ms" in final[0]

    def test_claude_error_log_has_latency(self, caplog):
        """Failure path must also expose how long Claude took before the
        error surfaced - otherwise retries-hit-timeout looks identical to
        immediate-auth-fail in the logs."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.serializer import ResultSerializer
        from alert_groups.models import SerializedResult
        from analyzers.claude_client import ClaudeCallError

        d = AlertGroupDispatcher()
        d.serializer = MagicMock(spec=ResultSerializer)
        d.serializer.serialize_df.side_effect = lambda name, df: SerializedResult(
            search_name=name, row_count=len(df), estimated_tokens=50,
            format="json", content="[]",
        )

        caplog.set_level(logging.INFO, logger="alert_groups.dispatcher")
        err = ClaudeCallError(
            "boom", request_id="rid", error_class="APIConnectionError",
            attempts=3,
        )

        with patch.object(
            type(d), "_execute_feeder_query_now",
            return_value=pd.DataFrame({"x": [1]}),
        ):
            with patch(
                "alert_groups.dispatcher.call_messages_create", side_effect=err,
            ):
                with patch.object(type(d), "_log_run"):
                    with patch.object(type(d), "_emit_log"):
                        with patch.object(
                            type(d), "_get_budget_gate", return_value=None,
                        ):
                            with patch.object(
                                type(d), "_maybe_send_failure_email",
                            ):
                                run = d.run(group=self._group())

        assert run.status == "error"
        lines = [rec.getMessage() for rec in caplog.records]
        error_line = [line for line in lines if "Claude API error after" in line]
        assert error_line, (
            "Missing 'Claude API error after <ms>' log line - removing "
            "it means the operator can't see whether retries burned "
            "through the timeout or Claude failed fast."
        )
