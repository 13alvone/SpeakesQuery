"""MEDIUMs batch 7 (final) - M-SV-6, M-SV-7, M-SV-8 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-SV-6** - sandbox escape regression tests covering the classic
    vectors (``import os`` / ``__import__("os")`` /
    ``type([]).__bases__[0].__subclasses__()`` / ``open("/etc/passwd")``
    / ``getattr(obj, "__class__")``). Without these, a future edit to
    ``_build_sandbox_globals`` could regress the boundary silently.

  * **M-SV-7** - doc-drift test that parses the allowlist entry from
    ``docs/lang/09_ingestion_etiquette.md`` and asserts it matches
    ``scheduled_input_engine.executor.ALLOWED_MODULES``. Ensures the
    "allowed imports" line in the docs stays truthful.

  * **M-SV-8** - repo-script env-var injection now detects case-only
    credential collisions (``api_key`` vs ``API_KEY`` both collapse to
    ``SPEAKESQUERY_CRED_API_KEY``) and raises ``ValueError`` with a clear
    operator-facing message instead of silently stomping one value
    with the other.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# M-SV-6: sandbox escape attempts must all fail at test time
# ======================================================================


def _sandbox_run(code: str):
    """Compile + execute code in the sandbox; return (success, errors, captured_df)."""
    from scheduled_input_engine.executor import CodeExecutor

    # Wrap whatever escape attempt we're testing in a minimal harness
    # that still satisfies ``_process_code``'s AST check for
    # ``GENERATE_RESULTS``. If the escape succeeds, the harness runs;
    # if it raises, execute_test reports errors.
    harness = (
        "import pandas as pd\n"
        + code + "\n"
        + "df = pd.DataFrame({'_epoch': [1]})\n"
        + "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
    )
    try:
        executor = CodeExecutor(harness, test_mode=True, trust_level="sandboxed")
    except Exception as exc:
        # Compile-time rejection (RestrictedPython) is the strongest
        # possible block - return a synthetic failure result so the
        # assertion layer reads uniformly.
        return False, [f"compile-time: {type(exc).__name__}: {exc}"]
    try:
        result = executor.execute_test()
    except Exception as exc:
        return False, [f"runtime: {type(exc).__name__}: {exc}"]
    ok = result["status"] == "pass" and not result["errors"]
    return ok, result["errors"]


class TestSandboxEscapeAttempts:
    """Regression coverage for the sandbox boundary.

    This test class is split deliberately: **passing tests** assert what
    IS blocked today (a real regression surface), while **xfail tests**
    document what ISN'T - known gaps that an empirical run surfaced.
    Marking the latter as xfail keeps the suite honest: if a future
    sandbox hardening closes a gap, the xfail unexpectedly PASSES and
    pytest flags it as ``XPASS`` so the test can be promoted to a plain
    assert. Alternatively a regression that reintroduces a previously-
    closed gap re-trips the passing assertion.

    Known gaps surfaced 2026-04-22 (out of scope for M-SV-6 to close -
    M-SV-6 was "write regression tests"; closing the gaps needs a
    follow-up design pass on ``_build_sandbox_globals``):

    * Bare ``import os`` / ``import subprocess`` / ``import sys`` are
      NOT blocked. RestrictedPython's ``compile_restricted`` does not
      transform plain ``import X`` statements into calls through the
      rebound ``__import__``; the IMPORT_NAME opcode uses
      ``__builtins__.__import__`` directly. The ``_safe_import``
      function is called only when code explicitly writes
      ``__import__("os")``.
    * ``open(...)`` is present in ``safe_builtins`` (RestrictedPython
      exposes it intentionally; our override would have to mask it).
    * ``compile`` is likewise in ``safe_builtins``.
    * ``getattr(obj, "__class__")`` returns the real class (``__class__``
      is NOT in ``_BLOCKED_ATTRS`` currently; only the graph-walking
      dunders are).
    """

    # -- What IS blocked today -----------------------------------------

    def test_dunder_import_os_rejected(self):
        """Explicit ``__import__('os')`` goes through ``_safe_import`` - blocked."""
        ok, _errors = _sandbox_run("__import__('os')")
        assert not ok

    def test_subclasses_graph_walk_blocked(self):
        """``type([]).__bases__[0].__subclasses__()`` blocked via _safe_getattr."""
        ok, _errors = _sandbox_run(
            "escaped = type([]).__bases__[0].__subclasses__()"
        )
        assert not ok

    def test_getattr_globals_returns_none(self):
        """``getattr(x, '__globals__', None)`` yields None via _safe_getattr."""
        ok, _errors = _sandbox_run(
            "g = getattr(pd.DataFrame({'a': [1]}), '__globals__', None)\n"
            "assert g is None, 'sandbox should return None for blocked attrs'"
        )
        assert ok, (
            "Expected getattr(..., '__globals__') to return None via the "
            f"sandbox guard; got errors={_errors}"
        )

    def test_eval_function_not_available(self):
        """``eval`` is not in ``safe_builtins``."""
        ok, _errors = _sandbox_run("x = eval('1+1')")
        assert not ok

    def test_exec_function_not_available(self):
        """``exec`` is not in ``safe_builtins``."""
        ok, _errors = _sandbox_run("exec('x = 1')")
        assert not ok

    def test_hasattr_blocked_dunder_returns_false(self):
        """M-CE-9 companion: ``hasattr`` matches ``_safe_getattr``'s allowlist."""
        ok, _errors = _sandbox_run(
            "assert not hasattr(int, '__subclasses__'), 'hasattr leaked dunder existence'"
        )
        assert ok, (
            f"Expected hasattr to return False on blocked dunders; "
            f"errors={_errors}"
        )

    # -- Known gaps, tracked as xfail ---------------------------------
    # These are HONEST documentation of what the sandbox doesn't block
    # today. If a future hardening change closes any of them, pytest
    # will flag the test as XPASS and we should promote it to a plain
    # assertion + file the remediation.

    KNOWN_GAP_REASON = (
        "Known sandbox gap surfaced 2026-04-22 during M-SV-6 - "
        "RestrictedPython's plain ``import X`` bypasses our rebound "
        "``__import__``; ``open`` + ``compile`` come from safe_builtins. "
        "Closing these requires a design pass on _build_sandbox_globals."
    )

    @pytest.mark.xfail(reason=KNOWN_GAP_REASON, strict=False)
    def test_bare_import_os_blocked(self):
        ok, _errors = _sandbox_run("import os")
        assert not ok

    @pytest.mark.xfail(reason=KNOWN_GAP_REASON, strict=False)
    def test_bare_import_subprocess_blocked(self):
        ok, _errors = _sandbox_run("import subprocess")
        assert not ok

    @pytest.mark.xfail(reason=KNOWN_GAP_REASON, strict=False)
    def test_bare_import_sys_blocked(self):
        ok, _errors = _sandbox_run("import sys")
        assert not ok

    @pytest.mark.xfail(reason=KNOWN_GAP_REASON, strict=False)
    def test_open_filesystem_access_blocked(self):
        ok, _errors = _sandbox_run("data = open('/etc/passwd').read()")
        assert not ok

    @pytest.mark.xfail(reason=KNOWN_GAP_REASON, strict=False)
    def test_compile_function_blocked(self):
        ok, _errors = _sandbox_run("c = compile('1+1', 'x', 'eval')")
        assert not ok

    @pytest.mark.xfail(reason=KNOWN_GAP_REASON, strict=False)
    def test_getattr_class_blocked(self):
        ok, _errors = _sandbox_run(
            "cls = getattr(pd.DataFrame({'a': [1]}), '__class__')"
        )
        assert not ok


# ======================================================================
# M-SV-7: ALLOWED_MODULES doc-drift test
# ======================================================================


class TestAllowedModulesDocDrift:
    """The docs must stay in lockstep with ``ALLOWED_MODULES``."""

    ETIQUETTE = _PROJECT_ROOT / "docs" / "lang" / "09_ingestion_etiquette.md"

    def test_doc_allowlist_matches_executor(self):
        """Parse the allowlist from the docs and compare to the executor's dict.

        The doc contains exactly one inline list of the sandboxed-tier
        allowlist in the form ``Allowlist: pandas, requests, json, …``.
        We extract it, normalise whitespace, and assert the set is
        equal to ``set(ALLOWED_MODULES.keys()) - {"pd"}`` (``pd`` is a
        convenience alias for ``pandas``; documenting both would be
        redundant).
        """
        from scheduled_input_engine.executor import ALLOWED_MODULES

        text = self.ETIQUETTE.read_text()
        match = re.search(
            r"Allowlist:\s*`?([a-zA-Z0-9_,\s]+)`?",
            text,
        )
        assert match, (
            "docs/lang/09_ingestion_etiquette.md must contain an "
            "``Allowlist: ...`` entry listing the sandboxed modules. "
            "The drift test cannot proceed without this anchor."
        )
        raw = match.group(1)
        # Strip trailing backticks or stray punctuation from the
        # extracted list.
        raw = raw.replace("`", "").strip().rstrip("|").strip()
        doc_modules = {
            tok.strip() for tok in raw.split(",") if tok.strip()
        }
        # Drop keywords / noise that can sometimes creep into the
        # regex capture.
        doc_modules = {m for m in doc_modules if m.isidentifier()}

        executor_modules = set(ALLOWED_MODULES.keys()) - {"pd"}

        missing_from_docs = executor_modules - doc_modules
        unexpected_in_docs = doc_modules - executor_modules

        assert not missing_from_docs, (
            "Modules exposed to the sandbox but NOT listed in "
            "docs/lang/09_ingestion_etiquette.md: "
            f"{sorted(missing_from_docs)}. "
            "Update the doc allowlist line."
        )
        assert not unexpected_in_docs, (
            "Modules listed in the docs allowlist but NOT in "
            "scheduled_input_engine.executor.ALLOWED_MODULES: "
            f"{sorted(unexpected_in_docs)}. "
            "Either add them to ALLOWED_MODULES or remove them from the docs."
        )


# ======================================================================
# M-SV-8: repo-script env-var case-collision detection
# ======================================================================


class TestRepoScriptEnvCaseCollision:

    def _make_engine(self, creds):
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()

        class _FakeVault:
            def decrypt_for_script(self, _key):
                return creds

        engine._vault = _FakeVault()
        return engine

    def test_case_collision_raises_valueerror(self, tmp_path, monkeypatch):
        """``api_key`` + ``API_KEY`` must raise ValueError at injection time."""
        from scheduled_input_engine import engine as engine_mod

        creds = {"api_key": "sk-one", "API_KEY": "sk-two"}
        engine = self._make_engine(creds)

        # Stage a fake repo + script so the path-traversal check passes.
        repo_dir = engine_mod.INPUT_REPOS_ROOT / "_msv8_collision"
        repo_dir.mkdir(parents=True, exist_ok=True)
        script_file = repo_dir / "noop.py"
        script_file.write_text("# noop\n")

        captured: dict = {}

        def fake_record(task_id, script_name, elapsed, status, error_msg=None):
            captured.update(
                task_id=task_id, status=status, error_msg=error_msg,
            )

        monkeypatch.setattr(engine.store, "record_execution", fake_record)
        monkeypatch.setattr(
            engine, "_get_indexes_dir", lambda: tmp_path / "indexes",
        )

        try:
            engine._run_repo_script({
                "id": 8101,
                "script_name": "noop.py",
                "repo_path": str(repo_dir),
                "output_subdir": "",
                "overwrite": False,
            })
        finally:
            try:
                script_file.unlink()
                repo_dir.rmdir()
            except Exception:
                pass
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

        # The ValueError is caught by the outer ``except Exception`` in
        # _run_repo_script which records status='failed' with the error
        # message. Verify the recorded message names the offending keys.
        assert captured.get("status") == "failed", (
            f"Expected failed status; got {captured!r}"
        )
        err = captured.get("error_msg") or ""
        assert "collision" in err.lower() and "case" in err.lower(), (
            f"error_msg should cite the case collision; got {err!r}"
        )
        # Both offending names should appear in the message.
        assert "api_key" in err and "API_KEY" in err, (
            f"error_msg should name both colliding keys; got {err!r}"
        )

    def test_no_collision_with_distinct_keys(self, tmp_path, monkeypatch):
        """Distinct (case-normalised) keys inject cleanly, no error."""
        from scheduled_input_engine import engine as engine_mod
        import unittest.mock as _mock
        import types

        creds = {"API_KEY": "sk-one", "FRED_API_KEY": "fred-two"}
        engine = self._make_engine(creds)
        repo_dir = engine_mod.INPUT_REPOS_ROOT / "_msv8_ok"
        repo_dir.mkdir(parents=True, exist_ok=True)
        script_file = repo_dir / "noop.py"
        script_file.write_text("# noop\n")

        captured_env = {}

        async def fake_runner(script_path, timeout, env):
            captured_env.update(env)
            return types.SimpleNamespace(
                stdout="", stderr="", returncode=0,
            )

        monkeypatch.setattr(engine.store, "record_execution",
                            lambda *a, **kw: None)
        monkeypatch.setattr(
            engine, "_get_indexes_dir", lambda: tmp_path / "indexes",
        )

        try:
            with _mock.patch.object(engine_mod, "run_in_subprocess", fake_runner):
                engine._run_repo_script({
                    "id": 8102,
                    "script_name": "noop.py",
                    "repo_path": str(repo_dir),
                    "output_subdir": "",
                    "overwrite": False,
                })
        finally:
            try:
                script_file.unlink()
                repo_dir.rmdir()
            except Exception:
                pass
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

        assert captured_env.get("SPEAKESQUERY_CRED_API_KEY") == "sk-one"
        assert captured_env.get("SPEAKESQUERY_CRED_FRED_API_KEY") == "fred-two"
