"""MEDIUMs batch 3 - M-AN-9, M-AN-10, M-AN-11 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-AN-9** - batch poller retries transient ``retrieve()`` errors once
    with exponential backoff, and iterates from a persistent checkpoint
    so a mid-cycle failure doesn't re-process rows that already landed.
  * **M-AN-10** - every analyzer storage write and every daily-budget
    key uses UTC so budget rollover lines up with the AG scheduler
    (also UTC) and email subject dates.
  * **M-AN-11** - ``AlertGroupDispatcher.run`` now acquires a
    cross-process filesystem lock keyed on the group name. A manual
    "Run Now" racing a cron fire for the same AG returns
    ``status='skipped'`` with a descriptive error_message instead of
    both dispatches mutating ``AlertGroupStore`` concurrently.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ======================================================================
# M-AN-9: batch poller retry + resume
# ======================================================================

class TestBatchPollerRetryAndResume:

    def _fake_batch(self, status="ended"):
        b = MagicMock()
        b.processing_status = status
        return b

    def test_retrieve_retries_once_on_transient(self):
        """_retry_batch_retrieve retries once on a retryable exception class."""
        from analyzers import batch_poller as bp

        class _Transient(Exception):
            pass
        _Transient.__name__ = "APIConnectionError"

        attempts = {"n": 0}

        def flaky_retrieve(batch_id):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _Transient("network hiccup")
            return self._fake_batch()

        client = MagicMock()
        client.messages.batches.retrieve = flaky_retrieve

        with patch("time.sleep", lambda *a, **kw: None):
            batch = bp._retry_batch_retrieve(client, "batch_1")

        assert batch is not None
        assert attempts["n"] == 2

    def test_retrieve_does_not_retry_on_non_transient(self):
        """Non-retryable exception classes abort immediately."""
        from analyzers import batch_poller as bp

        class _Fatal(Exception):
            pass
        _Fatal.__name__ = "AuthenticationError"

        attempts = {"n": 0}

        def failing(batch_id):
            attempts["n"] += 1
            raise _Fatal("bad key")

        client = MagicMock()
        client.messages.batches.retrieve = failing

        batch = bp._retry_batch_retrieve(client, "batch_x")
        assert batch is None
        assert attempts["n"] == 1

    def test_progress_checkpoint_roundtrip(self, tmp_path, monkeypatch):
        """AnalyzerStorage's get/set_batch_progress persist across instances."""
        from analyzers import storage as storage_mod
        monkeypatch.setattr(storage_mod, "_PROJECT_ROOT", tmp_path)
        s1 = storage_mod.AnalyzerStorage()
        assert s1.get_batch_progress("batch_abc") == 0

        s1.set_batch_progress("batch_abc", 5)
        # Fresh instance (simulates next poll cycle) sees the same value.
        s2 = storage_mod.AnalyzerStorage()
        assert s2.get_batch_progress("batch_abc") == 5

        # Re-setting overwrites, doesn't insert a duplicate.
        s2.set_batch_progress("batch_abc", 12)
        assert s2.get_batch_progress("batch_abc") == 12

    def test_iteration_resumes_from_checkpoint(self, tmp_path, monkeypatch):
        """_process_batch skips rows below the stored checkpoint."""
        from analyzers import batch_poller as bp
        from analyzers import storage as storage_mod

        monkeypatch.setattr(storage_mod, "_PROJECT_ROOT", tmp_path)
        storage = storage_mod.AnalyzerStorage()
        storage.set_batch_progress("batch_rsm", 3)

        handled = []

        def fake_handle(result, _storage):
            handled.append(result.custom_id)

        fake_results = [
            MagicMock(custom_id=f"cid_{i}") for i in range(6)
        ]

        client = MagicMock()
        client.messages.batches.retrieve = lambda batch_id: self._fake_batch()
        client.messages.batches.results = lambda batch_id: iter(fake_results)

        with patch.object(bp, "_handle_batch_result", fake_handle):
            processed = bp._process_batch(client, "batch_rsm", storage)

        # Start index was 3 → only rows [3], [4], [5] are handled.
        assert handled == ["cid_3", "cid_4", "cid_5"]
        assert processed == 3
        # Checkpoint advanced to the full length.
        assert storage.get_batch_progress("batch_rsm") == 6


# ======================================================================
# M-AN-10: UTC date standardization
# ======================================================================

class TestUtcDateEverywhere:

    def test_claude_analyzer_last_reset_date_is_utc(self):
        """ClaudeAnalyzer seeds ``last_reset_date`` from the UTC calendar."""
        from analyzers.claude_analyzer import ClaudeAnalyzer
        from analyzers.models import AnalyzerConfig
        from datetime import datetime, timezone

        # Build an analyzer without storage so the seed path uses only the
        # in-memory initialisation.
        config = AnalyzerConfig(daily_budget_cents=100, api_key="sk-fake")
        analyzer = ClaudeAnalyzer(config=config, storage=None)
        assert analyzer._usage.last_reset_date == (
            datetime.now(timezone.utc).date().isoformat()
        )

    def test_claude_client_writes_under_utc_date(self, tmp_path, monkeypatch):
        """_record_daily_budget_usd persists the UTC date key."""
        import analyzers.claude_client as cc
        import analyzers.storage as storage_mod
        from datetime import datetime, timezone

        monkeypatch.setattr(storage_mod, "_PROJECT_ROOT", tmp_path)
        cc._record_daily_budget_usd("claude-sonnet-4-6", 10, 2, 0.001)

        storage = storage_mod.AnalyzerStorage()
        utc_today = datetime.now(timezone.utc).date().isoformat()
        row = storage.load_daily_budget(utc_today)
        assert row["total_calls"] == 1
        assert row["total_input_tokens"] == 10

    def test_storage_writes_use_utc_iso_now(self):
        """``_utc_now_iso`` produces a tz-aware ISO timestamp ending in +00:00."""
        from analyzers.storage import _utc_now_iso
        stamp = _utc_now_iso()
        # tz suffix is one of +00:00 (iso format) - never naive.
        assert stamp.endswith("+00:00"), (
            f"storage._utc_now_iso should be tz-aware; got {stamp!r}"
        )

    def test_no_naive_today_in_analyzers_module(self):
        """Source-invariant sweep: no bare ``date.today()`` in analyzer hot paths."""
        files = [
            _PROJECT_ROOT / "analyzers" / "claude_analyzer.py",
            _PROJECT_ROOT / "analyzers" / "claude_client.py",
            _PROJECT_ROOT / "analyzers" / "batch_poller.py",
        ]
        for f in files:
            text = f.read_text()
            # Strip docstring / comment noise (lines starting with # or
            # inside triple-quoted blocks) before scanning.
            cleaned_lines = []
            in_tq = False
            for line in text.splitlines():
                if '"""' in line:
                    in_tq = not in_tq if line.count('"""') == 1 else in_tq
                    continue
                if in_tq:
                    continue
                cleaned_lines.append(line.split("#", 1)[0])
            cleaned = "\n".join(cleaned_lines)
            # Tolerate the aliased import line but no call sites.
            calls = re.findall(r"\bdate\.today\(\s*\)", cleaned)
            assert calls == [], (
                f"Naive date.today() remains in {f.name}: {calls}. "
                "Use the UTC helper (datetime.now(timezone.utc).date())."
            )


# ======================================================================
# M-AN-11: cross-process AG dispatch lock
# ======================================================================

class TestDispatchLock:

    def test_lock_blocks_concurrent_same_group(self, tmp_path, monkeypatch):
        """A second ``_acquire_dispatch_lock`` for the same group yields False."""
        from alert_groups import dispatcher as disp
        monkeypatch.setattr(disp, "_DISPATCH_LOCK_DIR", tmp_path / "locks")

        with disp._acquire_dispatch_lock("ag_racy") as first:
            assert first is True
            with disp._acquire_dispatch_lock("ag_racy") as second:
                assert second is False

        # After the outer release, a fresh acquire succeeds again.
        with disp._acquire_dispatch_lock("ag_racy") as third:
            assert third is True

    def test_lock_is_per_group(self, tmp_path, monkeypatch):
        """Different groups hold independent locks."""
        from alert_groups import dispatcher as disp
        monkeypatch.setattr(disp, "_DISPATCH_LOCK_DIR", tmp_path / "locks")

        with disp._acquire_dispatch_lock("ag_alpha") as a, \
             disp._acquire_dispatch_lock("ag_beta") as b:
            assert a is True and b is True

    def test_stale_lock_is_removed(self, tmp_path, monkeypatch):
        """A lock file older than the staleness threshold is swept and reacquired."""
        from alert_groups import dispatcher as disp
        lock_dir = tmp_path / "locks"
        monkeypatch.setattr(disp, "_DISPATCH_LOCK_DIR", lock_dir)
        # Very short staleness window so the test doesn't actually wait.
        monkeypatch.setattr(disp, "_DISPATCH_LOCK_STALE_AFTER_SECONDS", 0)

        lock_dir.mkdir(parents=True, exist_ok=True)
        stale = lock_dir / "ag_ag_crashed.lock"
        stale.write_text("pid=99999 started=0.000\n")

        with disp._acquire_dispatch_lock("ag_crashed") as got:
            assert got is True, (
                "Stale lock should have been swept and reacquired."
            )

    def test_run_returns_skipped_when_lock_busy(self, tmp_path, monkeypatch):
        """AlertGroupDispatcher.run yields status='skipped' on a locked AG."""
        from alert_groups import dispatcher as disp
        from alert_groups.dispatcher import AlertGroupDispatcher
        monkeypatch.setattr(disp, "_DISPATCH_LOCK_DIR", tmp_path / "locks")

        # Patch _log_run / _emit_log to no-op so we don't touch real
        # persistence.
        with patch.object(AlertGroupDispatcher, "_log_run", lambda self, r: None), \
             patch.object(AlertGroupDispatcher, "_emit_log", lambda self, r, s, dry_run=False: None):
            d = AlertGroupDispatcher()
            # Hold the lock in a background thread, then call run() on the
            # same group name - it should immediately skip.
            barrier = threading.Event()
            released = threading.Event()

            def holder():
                with disp._acquire_dispatch_lock("ag_busy") as got:
                    assert got is True
                    barrier.set()
                    # Block until main thread releases us so the lock
                    # stays held during the run() call below.
                    released.wait(timeout=5)

            t = threading.Thread(target=holder)
            t.start()
            try:
                barrier.wait(timeout=2)
                result = d.run({"name": "ag_busy", "disabled": False})
                assert result.status == "skipped"
                assert "already in progress" in (result.error_message or "")
            finally:
                released.set()
                t.join(timeout=5)

    def test_run_releases_lock_after_successful_call(self, tmp_path, monkeypatch):
        """After run() returns, the lock file must be gone so the next fire proceeds."""
        from alert_groups import dispatcher as disp
        from alert_groups.dispatcher import AlertGroupDispatcher
        monkeypatch.setattr(disp, "_DISPATCH_LOCK_DIR", tmp_path / "locks")

        # Stub out the entire dispatch body via _run_inner so the run
        # returns a synthesised "success" without touching Claude.
        def fake_run_inner(self, group, dry_run, result, run_started, *, force):
            result.status = "success"
            return result

        with patch.object(AlertGroupDispatcher, "_run_inner", fake_run_inner), \
             patch.object(AlertGroupDispatcher, "_log_run", lambda self, r: None), \
             patch.object(AlertGroupDispatcher, "_emit_log", lambda self, r, s, dry_run=False: None):
            d = AlertGroupDispatcher()
            r = d.run({"name": "ag_clean", "disabled": False})
            assert r.status == "success"

        # Lock dir is empty after release.
        remaining = list((tmp_path / "locks").glob("*.lock"))
        assert remaining == [], (
            f"Dispatch lock files should be removed after run; found: {remaining}"
        )
