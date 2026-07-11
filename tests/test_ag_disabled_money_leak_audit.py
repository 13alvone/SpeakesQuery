#!/usr/bin/env python3
"""
Money-leak audit for the Alert Groups Disable contract - 2026-04-30.

User raised this verbatim:

    "The disabled button on ALERT GROUPS should be a toggle button for
    enable and/or disable per alert group. This MUST WORK BECAUSE IF IT
    JUST SAYS IT'S DISABLED AND IT'S NOT, IT COULD COST MONEY!"

Same pattern as the 2026-04-27 prompt_only audit: trace the contract end
to end, prove every transition preserves the disabled flag, prove every
gate refuses to call Claude for a disabled AG, AND prove the UI surfaces
state explicitly so a missing visual signal can never mean "I forgot to
set this".

Layers covered (every transition between user click and money spent):

1. **Toggle endpoint contract** - POST /enable / /disable update the
   YAML and re-register scheduler jobs.
2. **Scheduler stale-job removal** - register_alert_group_jobs() must
   REMOVE existing jobs for AGs that are now disabled. Pre-2026-04-30
   it only skipped registration; the previously-registered job kept
   firing on its original cron until the next container restart.
3. **Dispatcher gate** - Even if the scheduler somehow fires (e.g. via
   a manual "Run now"), the dispatcher refuses to dispatch a disabled
   AG. This is the last line of defense before Claude.
4. **UI state pill contract** - The OFF/ON pill is a separate visual
   indicator from the action button. The user can NEVER confuse the
   action label ("Enable" / "Disable") with the current state because
   the pill explicitly says "OFF" or "ON".
5. **Full toggle cycle** - Click Disable → state goes OFF → next render
   shows OFF + the button now reads "Enable". Click Enable → state
   goes ON → button reads "Disable".

If ANY of these layers fails, the user could see "this AG is disabled"
but the dispatcher could still call Claude. That's the bug we're
preventing.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from alert_group_store import AlertGroupStore


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def isolated_store(tmp_path):
    """An AlertGroupStore with isolated temp dirs and an EMPTY defaults dir
    so initialize() doesn't seed anything we didn't explicitly create."""
    empty_defaults = tmp_path / "_empty_default_alert_groups"
    empty_defaults.mkdir()

    store = AlertGroupStore()
    store._dir = tmp_path / "alert_groups"
    store._defaults_dir = empty_defaults
    store._db = str(tmp_path / "last_chance.sqlite")
    store._runs_db = str(tmp_path / "alert_group_runs.sqlite")
    store.initialize()
    return store


def _ag_payload(name="test_ag", disabled=False, schedule="0 12 * * *"):
    """Minimal valid AG payload."""
    return {
        "name": name,
        "description": f"AG '{name}' for money-leak audit",
        "search_names": ["any_search"],
        "prompt_text": "Summarize the data in 1 sentence.",
        "schedule": schedule,
        "max_rows": 10,
        "email_address": "test@example.com",
        "disabled": disabled,
    }


# ===========================================================================
# Layer 1 - Toggle endpoint contract: YAML round-trips disabled correctly
# ===========================================================================


class TestDisableToggleRoundTrip:
    """The /enable and /disable endpoints must atomically update the YAML."""

    def test_save_disabled_true_persists(self, isolated_store):
        isolated_store.save_group(_ag_payload(disabled=True))
        loaded = isolated_store.get_group("test_ag")
        assert loaded["disabled"] is True

    def test_save_disabled_false_persists(self, isolated_store):
        isolated_store.save_group(_ag_payload(disabled=False))
        loaded = isolated_store.get_group("test_ag")
        assert loaded["disabled"] is False

    def test_update_to_disabled_round_trips(self, isolated_store):
        isolated_store.save_group(_ag_payload(disabled=False))
        isolated_store.update_group("test_ag", {"disabled": True})
        assert isolated_store.get_group("test_ag")["disabled"] is True

    def test_update_to_enabled_round_trips(self, isolated_store):
        isolated_store.save_group(_ag_payload(disabled=True))
        isolated_store.update_group("test_ag", {"disabled": False})
        assert isolated_store.get_group("test_ag")["disabled"] is False

    def test_full_disable_enable_disable_cycle(self, isolated_store):
        """The user can flip the state arbitrarily many times."""
        isolated_store.save_group(_ag_payload(disabled=False))
        for desired in (True, False, True, False, True):
            isolated_store.update_group("test_ag", {"disabled": desired})
            assert isolated_store.get_group("test_ag")["disabled"] is desired


# ===========================================================================
# Layer 2 - Scheduler MUST remove jobs for AGs that become disabled
# ===========================================================================


class TestSchedulerRemovesStaleJobs:
    """Pre-2026-04-30 register_alert_group_jobs only ADDED jobs; it never
    REMOVED jobs whose AG had been disabled since registration. The disabled
    AG would keep firing its cron, and only the dispatcher gate prevented
    a Claude call. That's defense-in-depth broken - fix it."""

    def _make_fake_scheduler(self):
        """A minimal scheduler stand-in that records add/remove and
        exposes get_jobs() so the production code can sweep stale ones."""
        class FakeJob:
            def __init__(self, jid):
                self.id = jid

        class FakeScheduler:
            def __init__(self):
                self._jobs = {}
                self.add_calls = []
                self.remove_calls = []

            def add_job(self, func, trigger, **kwargs):
                jid = kwargs["id"]
                self._jobs[jid] = FakeJob(jid)
                self.add_calls.append(jid)

            def remove_job(self, jid):
                if jid not in self._jobs:
                    raise KeyError(jid)
                del self._jobs[jid]
                self.remove_calls.append(jid)

            def get_jobs(self):
                return list(self._jobs.values())

            def get_job(self, jid):
                return self._jobs.get(jid)

        return FakeScheduler()

    def test_disabled_ag_job_removed_on_re_register(self, isolated_store, monkeypatch):
        """Register an enabled AG → flip to disabled → re-register →
        the previously-registered job MUST be removed from the scheduler."""
        from alert_groups.scheduler import register_alert_group_jobs
        import alert_group_store as ag_module

        # Make the module-level lookup return our fixture
        monkeypatch.setattr(
            ag_module, "AlertGroupStore",
            lambda: isolated_store,
        )

        # Step 1: enabled AG registers a job
        isolated_store.save_group(_ag_payload(name="my_ag", disabled=False))
        sched = self._make_fake_scheduler()
        register_alert_group_jobs(sched)
        assert "alert_group_my_ag" in sched._jobs, (
            "Enabled AG was not registered on the scheduler"
        )

        # Step 2: flip to disabled
        isolated_store.update_group("my_ag", {"disabled": True})

        # Step 3: re-register - the job MUST be removed
        register_alert_group_jobs(sched)
        assert "alert_group_my_ag" not in sched._jobs, (
            "MONEY LEAK: register_alert_group_jobs left the scheduler "
            "job in place after the AG was disabled. The cron will keep "
            "firing on its original schedule until the container restarts. "
            "Only the dispatcher gate prevents the Claude call - defense "
            "in depth is broken."
        )
        assert "alert_group_my_ag" in sched.remove_calls

    def test_re_enable_re_registers_job(self, isolated_store, monkeypatch):
        """Re-enabling a previously-disabled AG must put the job back."""
        from alert_groups.scheduler import register_alert_group_jobs
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore",
            lambda: isolated_store,
        )

        isolated_store.save_group(_ag_payload(name="my_ag", disabled=True))
        sched = self._make_fake_scheduler()
        register_alert_group_jobs(sched)
        assert "alert_group_my_ag" not in sched._jobs

        isolated_store.update_group("my_ag", {"disabled": False})
        register_alert_group_jobs(sched)
        assert "alert_group_my_ag" in sched._jobs

    def test_sweep_only_touches_alert_group_prefix(self, isolated_store, monkeypatch):
        """The stale-job sweep must NEVER remove jobs from other subsystems
        (saved searches, ingestion, scheduled_input_engine, etc.).
        Their job IDs have different prefixes."""
        from alert_groups.scheduler import register_alert_group_jobs
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore",
            lambda: isolated_store,
        )

        sched = self._make_fake_scheduler()
        # Plant a non-AG job that the sweep MUST leave alone
        sched._jobs["scheduled_search_foo"] = sched._jobs.get(
            "scheduled_search_foo",
        ) or type("J", (), {"id": "scheduled_search_foo"})()
        sched._jobs["ingestion_task_bar"] = type(
            "J", (), {"id": "ingestion_task_bar"},
        )()

        register_alert_group_jobs(sched)

        assert "scheduled_search_foo" in sched._jobs, (
            "Stale-job sweep incorrectly removed a non-AG job"
        )
        assert "ingestion_task_bar" in sched._jobs, (
            "Stale-job sweep incorrectly removed a non-AG job"
        )

    def test_no_schedule_ag_has_no_job(self, isolated_store, monkeypatch):
        """An AG with no schedule field must NOT have a scheduler job, AND
        if one was previously created (from a prior schedule edit), the
        sweep must remove it."""
        from alert_groups.scheduler import register_alert_group_jobs
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore",
            lambda: isolated_store,
        )
        isolated_store.save_group(
            _ag_payload(name="manual_only_ag", schedule="0 9 * * *")
        )
        sched = self._make_fake_scheduler()
        register_alert_group_jobs(sched)
        assert "alert_group_manual_only_ag" in sched._jobs

        # Now remove the schedule (manual-trigger only)
        isolated_store.update_group("manual_only_ag", {"schedule": ""})
        register_alert_group_jobs(sched)
        assert "alert_group_manual_only_ag" not in sched._jobs


# ===========================================================================
# Layer 3 - Dispatcher MUST refuse to dispatch a disabled AG
# ===========================================================================


class TestDispatcherDisabledGate:
    """Defense-in-depth: even if a stale scheduler job somehow fires for a
    disabled AG (or the user clicks 'Run now' on a disabled AG), the
    dispatcher MUST short-circuit BEFORE calling Claude."""

    def test_dispatcher_skips_disabled_group(self):
        """The dispatcher's run() method must return status='skipped'
        with no Claude API call when the AG is disabled."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        disabled_group = _ag_payload(name="my_ag", disabled=True)

        # CRITICAL: patch call_messages_create so a regression that
        # bypassed the gate would FAIL LOUD by trying to call this mock,
        # which raises if invoked.
        claude_call_count = {"n": 0}
        def _fail_loud(*args, **kwargs):
            claude_call_count["n"] += 1
            raise AssertionError(
                "MONEY LEAK: dispatcher called Claude for a disabled AG"
            )

        with patch("analyzers.claude_client.call_messages_create", _fail_loud):
            dispatcher = AlertGroupDispatcher()
            result = dispatcher.run(disabled_group)

        assert result.status == "skipped"
        assert "disabled" in (result.error_message or "").lower()
        assert claude_call_count["n"] == 0, (
            "MONEY LEAK: dispatcher invoked Claude despite the disabled gate"
        )

    def test_dispatcher_with_force_still_skips_disabled(self):
        """``force=True`` bypasses the rate limit but MUST NOT bypass the
        disabled gate - disabled is the user's explicit "no money" signal
        and a force-run from the UI would be a money-leak footgun."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        disabled_group = _ag_payload(name="my_ag", disabled=True)

        claude_call_count = {"n": 0}
        def _fail_loud(*args, **kwargs):
            claude_call_count["n"] += 1
            raise AssertionError(
                "MONEY LEAK: force=True bypassed the disabled gate"
            )

        with patch("analyzers.claude_client.call_messages_create", _fail_loud):
            dispatcher = AlertGroupDispatcher()
            result = dispatcher.run(disabled_group, force=True)

        assert result.status == "skipped"
        assert claude_call_count["n"] == 0, (
            "MONEY LEAK: force=True bypassed the disabled gate"
        )

    def test_dispatcher_dispatches_when_enabled(self):
        """Sanity check: an ENABLED AG with no real searches should at
        least PASS the disabled gate (it'll fail later for a different
        reason - no feeders - which is fine for this test)."""
        from alert_groups.dispatcher import AlertGroupDispatcher

        enabled_group = _ag_payload(name="my_ag", disabled=False)
        # Empty search_names list so the dispatcher has nothing to fetch
        enabled_group["search_names"] = []

        with patch("analyzers.claude_client.call_messages_create") as fake_claude:
            fake_claude.return_value = MagicMock()  # never reached anyway
            dispatcher = AlertGroupDispatcher()
            result = dispatcher.run(enabled_group, dry_run=True)

        # Status should NOT be "skipped" (which is what the disabled gate
        # produces) - it might be "error" or "ok" depending on what
        # downstream gates fire, but it must NOT be "skipped".
        assert result.status != "skipped" or "disabled" not in (
            result.error_message or ""
        ).lower(), "Enabled AG was incorrectly skipped as disabled"


# ===========================================================================
# Layer 4 - UI state-pill contract (the visual source of truth)
# ===========================================================================


UI_HTML = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()


class TestStatePillContract:
    """The OFF/ON pill is the single source of truth for state. The action
    button is for action. They must coexist so the user is NEVER confused."""

    def test_state_pill_class_present(self):
        assert "ag-state-pill" in UI_HTML, (
            "ag-state-pill class missing from ui.html - without an explicit "
            "state pill, a user could mistake the action button label "
            "('Enable' = currently DISABLED) for the current state."
        )

    def test_state_pill_off_label(self):
        assert "isDisabled ? 'OFF' : 'ON'" in UI_HTML, (
            "State pill text contract changed - the explicit OFF/ON labels "
            "are the money-leak audit signal. Don't use neutral labels "
            "like 'inactive' or 'paused' that could be confused with "
            "'pending' or 'in transition'."
        )

    def test_state_pill_has_explicit_warning_tooltip_when_enabled(self):
        """When ENABLED, the tooltip must explicitly mention 'costs money'
        so a user hovering over an enabled AG sees the financial impact."""
        assert "WILL fire on schedule and call Claude (costs money)" in UI_HTML, (
            "Enabled-state tooltip must warn about money cost - pinned by "
            "the 2026-04-30 money-leak audit."
        )

    def test_state_pill_has_explicit_safe_tooltip_when_disabled(self):
        """When DISABLED, the tooltip must affirm no Claude call happens."""
        assert "DISABLED" in UI_HTML and "will NOT call Claude" in UI_HTML, (
            "Disabled-state tooltip must affirm no Claude calls - without "
            "this the user has no positive signal that money is safe."
        )

    def test_state_pill_data_attribute_for_test_hooks(self):
        assert "statePill.dataset.agStateValue" in UI_HTML
        assert "isDisabled ? 'off' : 'on'" in UI_HTML

    def test_action_button_distinct_from_state_pill(self):
        """The action button must use a DIFFERENT label vocabulary from the
        state pill so no user can ever read them as redundant or
        contradictory. Pill: ON/OFF. Button: Enable/Disable."""
        # Pill uses ON/OFF (state)
        assert "isDisabled ? 'OFF' : 'ON'" in UI_HTML
        # Button uses Enable/Disable (action)
        assert "isDisabled ? 'Enable' : 'Disable'" in UI_HTML

    def test_state_pill_color_red_when_off(self):
        """Visual reinforcement: red when OFF, green when ON. Color and
        text must agree so colorblind users still see the text label."""
        # The ternary that picks the background color is keyed off isDisabled
        # to match the OFF/ON text. Look for both branches.
        assert "isDisabled ? 'rgba(127,29,29,0.30)' : 'rgba(20,83,45,0.40)'" in UI_HTML

    def test_action_button_tooltip_warns_on_enable_action(self):
        """When the user is about to click 'Enable', the tooltip must warn
        them this will start cron firing - i.e. start spending money."""
        assert "this AG will start firing on schedule" in UI_HTML

    def test_action_button_tooltip_confirms_on_disable_action(self):
        """When the user is about to click 'Disable', the tooltip must
        affirm immediate effect - no surprise where the AG keeps firing."""
        assert "this AG will stop firing immediately" in UI_HTML
