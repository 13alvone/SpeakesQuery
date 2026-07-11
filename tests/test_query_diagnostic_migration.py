"""H-CE-3 regression: /api/query* routes surface diagnostic error reasons.

Before the 2026-04-22 fix, ``run_query`` / ``macros_test`` / ``ss_validate_tokens``
/ ``analyzer_prompts_test`` called ``process_query()`` which swallowed every
exception into ``(None, None)``. The UI then showed a generic "No data
returned from query" or "Query execution failed" with no clue about the
actual cause (SyntaxError? DuckDB InvalidInputException? OOM?).

After the fix every user-facing route calls ``process_query_with_diagnostics``
and reports the exception class + message to the UI when a query crashes.
"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest
from unittest.mock import patch


# ----------------------------------------------------------------------
# Server-code-level invariants: every /api/query* route imports the
# diagnostic variant, not the legacy non-diagnostic one.
# ----------------------------------------------------------------------

class TestServerImportsDiagnosticVariant:

    SERVER = _PROJECT_ROOT / "desktop_app" / "server.py"

    def test_no_bare_process_query_imports_remain(self):
        """Every ``from .. import process_query`` on the user-facing routes has migrated."""
        import re
        text = self.SERVER.read_text()
        # Match any bare ``import process_query`` that is NOT followed by ``_with_diagnostics``.
        bad = re.findall(
            r"import\s+process_query(?!_with_diagnostics)\b",
            text,
        )
        assert bad == [], (
            f"desktop_app/server.py still imports the non-diagnostic "
            f"process_query at {len(bad)} site(s). All user-facing routes "
            f"must use process_query_with_diagnostics after H-CE-3."
        )

    def test_diagnostic_variant_is_imported_at_least_once(self):
        text = self.SERVER.read_text()
        assert "process_query_with_diagnostics" in text, (
            "server.py must import process_query_with_diagnostics."
        )


# ----------------------------------------------------------------------
# /api/query end-to-end: a crashing query must surface the exception
# class in the UI message, not just "No data returned".
# ----------------------------------------------------------------------

class TestApiQueryDiagnosticMessage:

    def _post(self, client, query: str):
        return client.post(
            "/api/query",
            data=_json.dumps({"query": query}),
            content_type="application/json",
        )

    def test_crashing_query_message_names_exception_class(self, client):
        """When the engine raises, the response message includes the exception class name."""
        # Patch process_query_with_diagnostics at the module level the
        # route imports it from - the route does a function-local import
        # so we patch the source module.
        import query_engine.CmdExecutionBackend as cb

        def fake_diag(_q):
            return (None, None, "InvalidInputException: column 'nope' not found")

        with patch.object(cb, "process_query_with_diagnostics", fake_diag):
            resp = self._post(client, 'index="x" | where nope > 0')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "error"
        msg = data["message"]
        assert "InvalidInputException" in msg, (
            f"Response message should cite the exception class; got {msg!r}"
        )
        assert "nope" in msg, (
            f"Response message should preserve the underlying detail; got {msg!r}"
        )

    def test_empty_result_still_gets_friendly_hint(self, client):
        """When diagnostic signals empty (not crashed), preserve the legacy hint."""
        import query_engine.CmdExecutionBackend as cb

        def fake_diag(_q):
            return (None, None, "empty: query produced zero rows")

        with patch.object(cb, "process_query_with_diagnostics", fake_diag):
            resp = self._post(client, 'index="x" | head 0')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "error"
        # Empty-path response should NOT carry the raw "empty:" token - the
        # UI message falls back to the hint about indexes/permissions.
        assert "empty:" not in data["message"], (
            f"Internal 'empty:' prefix leaked to UI: {data['message']!r}"
        )
        assert "No data" in data["message"] or "Indexes" in data["message"]


# ----------------------------------------------------------------------
# /api/macros/test diagnostic surface
# ----------------------------------------------------------------------

class TestApiMacrosTestDiagnostic:

    def test_crashing_query_returns_400_with_diagnostic(self, client):
        import query_engine.CmdExecutionBackend as cb

        def fake_diag(_q):
            return (None, None, "SyntaxError: unexpected token '{' at line 1")

        with patch.object(cb, "process_query_with_diagnostics", fake_diag):
            resp = client.post(
                "/api/macros/test",
                data=_json.dumps({"query": 'index="x" | {bad}'}),
                content_type="application/json",
            )

        assert resp.status_code == 400
        data = resp.get_json()
        assert data["status"] == "error"
        assert "SyntaxError" in data["message"]


# ----------------------------------------------------------------------
# Low-level: the bare-except in CmdExecutionBackend now logs the
# exception class name at ERROR.
# ----------------------------------------------------------------------

class TestBareExceptLogsExceptionClass:

    def test_process_query_logs_class_name_on_exception(self, caplog):
        """The fallback swallow in ``process_query`` now names the exception class."""
        import logging
        from query_engine import CmdExecutionBackend as cb

        def raise_with_class(*_args, **_kwargs):
            raise RuntimeError("simulated pipeline crash")

        with patch.object(cb, "execute_query", raise_with_class), \
             caplog.at_level(logging.ERROR):
            df, job_id = cb.process_query("index=\"x\" | head 1")

        assert df is None and job_id is None
        assert any(
            "RuntimeError while processing query" in rec.getMessage()
            for rec in caplog.records
        ), (
            "Expected ERROR log naming RuntimeError; got: "
            + "\n".join(r.getMessage() for r in caplog.records)
        )
