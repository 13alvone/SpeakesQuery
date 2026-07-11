"""LOWs batch 2 - L-CE-12, L-CE-13, L-MI-13 regressions.

  * **L-CE-12** - the ``.system4.system4.parquet`` doubled suffix is
    load-bearing (27+ references) and must not be renamed. The
    filename-generation site in ``ParquetWriter.write_atomic`` now
    carries a comment explaining the invariant.
  * **L-CE-13** - new ``sync_batch_poller_job(sched=None)`` helper that
    idempotently reconciles the poller job with the current settings.
    Replaces the one-shot startup registration; the
    ``/api/settings`` endpoint (or any future runtime toggle) can call
    it to remove the job when ``claude_analyzer_enable_batch`` flips
    to False.
  * **L-MI-13** - DEFERRED: adding ``requests.Session()`` to sandboxed
    scripts silently bypasses ``BudgetAwareRequests``' per-execution
    budget + allowlist enforcement via ``__getattr__`` passthrough.
    Documented in ``cache.py`` so nobody reaches for it until a
    ``BudgetAwareSession`` wrapper ships.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# L-CE-12: parquet filename invariant documented
# ======================================================================


class TestParquetSuffixInvariantDocumented:
    """The doubled suffix must stay; the source comment explains why."""

    SRC = _PROJECT_ROOT / "scheduled_input_engine" / "parquet_writer.py"

    def test_writer_comment_marks_suffix_as_load_bearing(self):
        text = self.SRC.read_text()
        assert "load-bearing" in text, (
            "parquet_writer.py must document why the ``.system4.system4.parquet`` "
            "suffix is NOT a cleanup target (L-CE-12)."
        )
        assert "system4.system4.parquet" in text

    def test_generated_filename_still_uses_double_suffix(self):
        """Smoke: the default filename includes both ``.system4`` segments."""
        import tempfile
        from scheduled_input_engine.parquet_writer import ParquetWriter
        import pandas as pd

        with tempfile.TemporaryDirectory() as td:
            writer = ParquetWriter(td, target_file_mb=128)
            df = pd.DataFrame({"x": [1], "_epoch": [1]})
            p = writer.write_atomic(df, subdirectory="inv")
            assert p.name.endswith(".system4.system4.parquet"), (
                f"Default filename must preserve the doubled suffix; got {p.name!r}"
            )


# ======================================================================
# L-CE-13: sync_batch_poller_job toggles the scheduled job on settings flip
# ======================================================================


class TestSyncBatchPollerJob:

    def _fake_scheduler(self):
        """Lightweight duck-typed scheduler: get_job / add_job / remove_job."""
        sched = MagicMock()
        sched._jobs = {}

        def get_job(job_id):
            return sched._jobs.get(job_id)

        def add_job(fn, trigger, *, id=None, minutes=None, **kw):
            sched._jobs[id] = MagicMock(id=id, fn=fn, minutes=minutes)

        def remove_job(job_id):
            if job_id in sched._jobs:
                del sched._jobs[job_id]
            else:
                raise Exception("no such job")

        sched.get_job = get_job
        sched.add_job = add_job
        sched.remove_job = remove_job
        return sched

    def test_registers_when_enabled(self, monkeypatch):
        """When both toggles are on, sync adds the batch-poller job."""
        from query_engine import QueryEngine

        class _FakeSettings:
            def __init__(self, d):
                self._d = d

            def get(self, k, *a, **kw):
                return self._d.get(k)

        from global_settings import get_settings
        monkeypatch.setattr(
            "global_settings.get_settings",
            lambda: _FakeSettings({
                "claude_analyzer_enabled": True,
                "claude_analyzer_enable_batch": True,
                "claude_analyzer_batch_poll_interval_minutes": 5,
            }),
        )

        sched = self._fake_scheduler()
        QueryEngine.sync_batch_poller_job(sched)
        assert "batch_poller" in sched._jobs

    def test_removes_when_disabled_after_enabled(self, monkeypatch):
        """Flipping the batch toggle off removes the previously-registered job."""
        from query_engine import QueryEngine

        settings_state = {
            "claude_analyzer_enabled": True,
            "claude_analyzer_enable_batch": True,
            "claude_analyzer_batch_poll_interval_minutes": 5,
        }

        class _FakeSettings:
            def get(self, k, *a, **kw):
                return settings_state.get(k)

        monkeypatch.setattr(
            "global_settings.get_settings", lambda: _FakeSettings(),
        )

        sched = self._fake_scheduler()
        QueryEngine.sync_batch_poller_job(sched)
        assert "batch_poller" in sched._jobs

        # Flip the batch toggle off and re-sync.
        settings_state["claude_analyzer_enable_batch"] = False
        QueryEngine.sync_batch_poller_job(sched)
        assert "batch_poller" not in sched._jobs, (
            "Second sync after disabling batch mode should have removed the "
            "poller job."
        )

    def test_idempotent_on_repeated_calls(self, monkeypatch):
        """Calling sync twice with the same enabled state does not duplicate."""
        from query_engine import QueryEngine

        class _FakeSettings:
            def get(self, k, *a, **kw):
                return {
                    "claude_analyzer_enabled": True,
                    "claude_analyzer_enable_batch": True,
                    "claude_analyzer_batch_poll_interval_minutes": 5,
                }.get(k)

        monkeypatch.setattr(
            "global_settings.get_settings", lambda: _FakeSettings(),
        )

        sched = self._fake_scheduler()
        QueryEngine.sync_batch_poller_job(sched)
        QueryEngine.sync_batch_poller_job(sched)
        # Still one job, not two.
        assert len(sched._jobs) == 1


# ======================================================================
# L-MI-13: deferred-with-comment
# ======================================================================


class TestSessionProxyCarriesWarning:
    """The ``BudgetAwareRequests.__getattr__`` docstring warns about the session trap."""

    SRC = _PROJECT_ROOT / "scheduled_input_engine" / "cache.py"

    def test_getattr_docstring_flags_session_bypass(self):
        text = self.SRC.read_text()
        # The comment must mention the budget-bypass concern so a future
        # maintainer doesn't innocently migrate scripts to Session().
        assert "BYPASSES the budget" in text, (
            "cache.py __getattr__ must carry the L-MI-13 warning about "
            "Session() bypassing budget + allowlist enforcement."
        )
        assert "BudgetAwareSession" in text, (
            "The warning should name the design-level remediation so a "
            "reader can search for its future introduction."
        )
