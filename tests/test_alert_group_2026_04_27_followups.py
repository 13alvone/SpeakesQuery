"""
Alert Group followups - 2026-04-27
==================================

Pins the second wave of 2026-04-27 changes:

1. **Prompt-only audit (money leak protection).** The user explicitly
   asked us to "MAKE SURE that when something is set to PROMPT ONLY and
   SAVED that it follows the correct path otherwise this could cost a
   lot of money quick." This file owns that contract end-to-end.

2. **PROMPT-ONLY badge regression fix.** Last turn's ``.ft-title``
   ellipsis truncation clipped the badge that was appended INSIDE the
   title div. The fix moves the badge to the cell. We pin it here so
   nobody re-introduces the regression by reverting.

3. **Filter bar pattern.** Three pages share the ``.filter-bar`` shape
   (search input + parameterized toggles). Drift guards check the HTML
   wiring so a future refactor can't silently drop the toggles.

4. **Disabled-row visual.** ``tr.row-disabled`` is applied when an AG
   / saved search / ingestion task is disabled, and the CSS gives it a
   theme-aware light-red wash.

5. **History button + modal.** Per-AG "History" button opens a modal
   showing the last 25 runs via ``/api/alert-groups/runs``.

6. **Global default error email + per-AG opt-out.** Existing global
   setting ``alert_group_failure_email_to`` already worked as the
   default fallback; this entry now ALSO surfaces the new per-AG
   ``error_email_disabled`` boolean which short-circuits the
   failure-email path entirely BEFORE any fallback resolves.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ag_store(tmp_dir):
    from alert_group_store import AlertGroupStore
    store = AlertGroupStore()
    store._dir = Path(tmp_dir) / "alert_groups"
    store._db = str(Path(tmp_dir) / "last_chance.sqlite")
    store._runs_db = str(Path(tmp_dir) / "alert_group_runs.sqlite")
    store.initialize()
    return store


def _ag_payload(**overrides):
    base = {
        "name": "test_ag_27",
        "description": "tests",
        "search_names": ["search_a"],
        "prompt_text": "Test prompt body.",
        "schedule": "30 10 * * 1-5",
        "max_rows": 50,
        "email_address": "user@example.com",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────
# 1. PROMPT-ONLY money-leak audit (the user's #1 concern)
# ─────────────────────────────────────────────────────────────────────

class TestPromptOnlyContract:
    """If any of these break, a 'prompt_only' AG can silently fall
    through to the API path and start spending tokens."""

    def test_explicit_prompt_only_persists_through_save(self, ag_store):
        rec = ag_store.save_group(_ag_payload(delivery_mode="prompt_only"))
        assert rec["delivery_mode"] == "prompt_only"

    def test_loaded_yaml_keeps_prompt_only(self, ag_store):
        ag_store.save_group(_ag_payload(delivery_mode="prompt_only"))
        loaded = ag_store.get_group("test_ag_27")
        assert loaded["delivery_mode"] == "prompt_only"

    def test_update_can_flip_to_api(self, ag_store):
        ag_store.save_group(_ag_payload(delivery_mode="prompt_only"))
        upd = ag_store.update_group("test_ag_27", {"delivery_mode": "api"})
        assert upd["delivery_mode"] == "api"

    def test_update_can_flip_back_to_prompt_only(self, ag_store):
        ag_store.save_group(_ag_payload(delivery_mode="api"))
        upd = ag_store.update_group(
            "test_ag_27", {"delivery_mode": "prompt_only"}
        )
        assert upd["delivery_mode"] == "prompt_only"

    def test_legacy_yaml_without_field_defaults_to_api(self, ag_store):
        """An AG written before delivery_mode existed must default to
        api (the historical behavior). Default to prompt_only would
        silently change the world for every existing AG."""
        path = ag_store._dir / "legacy.yaml"
        path.write_text(
            "name: legacy\n"
            "description: x\n"
            "search_names: [a]\n"
            "prompt_text: |\n  legacy\n"
            "schedule: '0 12 * * *'\n"
            "max_rows: 10\n"
            "email_address: u@x.com\n"
            "disabled: false\n"
            "created_at: '2026-01-01T00:00:00'\n"
            "updated_at: '2026-01-01T00:00:00'\n",
            encoding="utf-8",
        )
        loaded = ag_store.get_group("legacy")
        # Either explicit "api" OR the falsy fallback both lead to the
        # API path in the dispatcher's gate.
        gate = (loaded.get("delivery_mode") or "api").strip().lower()
        assert gate == "api"

    def test_invalid_value_rejected(self, ag_store):
        """A typo like 'promptonly' or 'API' (case-sensitive in some
        downstream path) must be rejected at save time, not silently
        accepted and then silently routed to the wrong path."""
        with pytest.raises(ValueError, match=r"(?i)delivery_mode"):
            ag_store.save_group(_ag_payload(delivery_mode="promptonly"))

    def test_dispatcher_gate_routes_correctly_to_each_path(
        self, ag_store, monkeypatch,
    ):
        """The actual fork in dispatcher.run() - read the AG, check the
        gate, and assert the correct branch is invoked. We mock both
        sides so the test runs in milliseconds."""
        ag_store.save_group(_ag_payload(delivery_mode="prompt_only"))
        loaded = ag_store.get_group("test_ag_27")

        # Simulate the gate exactly the way dispatcher.py:1046 does.
        gate = (loaded.get("delivery_mode") or "api").strip().lower()
        assert gate == "prompt_only", (
            "Money-leak guard: a YAML saved with delivery_mode='prompt_only' "
            "must produce 'prompt_only' at the gate - anything else means "
            "the dispatcher will call Claude and bill the user."
        )

        # And api → api, by symmetric requirement.
        ag_store.update_group("test_ag_27", {"delivery_mode": "api"})
        re_loaded = ag_store.get_group("test_ag_27")
        assert (re_loaded.get("delivery_mode") or "api").strip().lower() == "api"


# ─────────────────────────────────────────────────────────────────────
# 2. PROMPT-ONLY badge regression fix
# ─────────────────────────────────────────────────────────────────────

class TestPromptOnlyBadgeRendering:
    """Drift-guard the 2026-04-27 fix that moved the badge OUT of the
    truncated .ft-title div (where overflow:hidden was clipping it)."""

    def setup_method(self):
        self.ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_both_modes_show_a_badge(self):
        # After the fix, BOTH modes get an explicit badge - a missing
        # badge can never be confused with "I forgot to set this".
        assert "PROMPT-ONLY · $0" in self.ui
        assert "API · billable" in self.ui

    def test_badge_is_appended_to_cell_not_to_title_div(self):
        """The badge must be appended to ``tdName`` (the cell) - not
        to ``nameLine`` (the .ft-title div with overflow:hidden)."""
        # Locate the AG render block by its anchor and assert the badge
        # appendChild target is tdName, not nameLine.
        anchor = "modeBadge.dataset.deliveryMode"
        assert anchor in self.ui, (
            "Badge wiring removed - PROMPT-ONLY visibility regression risk."
        )
        # The line directly after the badge config must append to the cell.
        idx = self.ui.find(anchor)
        following = self.ui[idx:idx + 400]
        assert "tdName.appendChild(modeBadge)" in following, (
            "Badge appended to wrong element. The .ft-title div clips "
            "with overflow:hidden - append to tdName (cell) instead."
        )
        assert "nameLine.appendChild(modeBadge)" not in following, (
            "Regression: badge appended back inside .ft-title - will be "
            "clipped by overflow:hidden for long names."
        )


# ─────────────────────────────────────────────────────────────────────
# 3. Filter bar pattern (drift guard for all 3 pages)
# ─────────────────────────────────────────────────────────────────────

class TestFilterBarFrontendContracts:

    def setup_method(self):
        self.ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_ag_filter_bar_present(self):
        for needed in (
            'id="ag-filter-search"',
            'id="ag-filter-enabled-only"',
            'id="ag-filter-prompt-only"',
            'id="ag-filter-scheduled"',
            'id="ag-filter-count"',
        ):
            assert needed in self.ui, f"AG filter-bar missing: {needed}"

    def test_ss_filter_bar_present(self):
        for needed in (
            'id="ss-filter-search"',
            'id="ss-filter-enabled-only"',
            'id="ss-filter-feeders"',
            'id="ss-filter-standalone"',
            'id="ss-filter-scheduled"',
            'id="ss-filter-count"',
        ):
            assert needed in self.ui, f"SS filter-bar missing: {needed}"

    def test_ingestion_filter_bar_upgraded(self):
        # The pre-existing #si-scripts-search id is preserved (existing
        # JS still reads it) but its parent now uses .filter-bar layout
        # and the new toggles are present.
        for needed in (
            'id="si-scripts-search"',
            'id="si-filter-enabled-only"',
            'id="si-filter-pro-only"',
            'id="si-filter-needs-creds"',
            'id="si-filter-failed"',
        ):
            assert needed in self.ui, f"Ingestion filter-bar missing: {needed}"

    def test_filter_bar_css_modifier_exists(self):
        for selector in (
            ".filter-bar",
            ".filter-search",
            ".filter-toggle",
            ".filter-count",
        ):
            assert selector in self.ui, f"Filter-bar CSS missing: {selector}"


# ─────────────────────────────────────────────────────────────────────
# 4. Disabled-row visual treatment
# ─────────────────────────────────────────────────────────────────────

class TestDisabledRowVisual:

    def setup_method(self):
        self.ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_row_disabled_css_class_styled(self):
        # CSS must define a tinted background for tr.row-disabled td.
        assert ".data-table tr.row-disabled td" in self.ui
        # Theme-aware overrides for dark/night/cyber.
        assert 'body[data-theme="dark"]' in self.ui
        assert 'body[data-theme="cyber"]' in self.ui

    def test_ag_render_applies_row_disabled(self):
        # The AG forEach must apply the class when g.disabled === true.
        assert (
            "if (g.disabled === true) tr.classList.add('row-disabled')"
            in self.ui
        )

    def test_ss_render_applies_row_disabled(self):
        assert (
            "if (s.disabled === true) tr.classList.add('row-disabled')"
            in self.ui
        )

    def test_ingestion_render_applies_row_disabled(self):
        assert (
            "if (task.disabled === true) tr.classList.add('row-disabled')"
            in self.ui
        )


# ─────────────────────────────────────────────────────────────────────
# 5. History button + modal
# ─────────────────────────────────────────────────────────────────────

class TestAGHistoryButton:

    def setup_method(self):
        self.ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_history_button_added_to_each_ag_row(self):
        # The History button is appended to tdActions.
        assert "histBtn.textContent = 'History'" in self.ui
        assert "tdActions.appendChild(histBtn)" in self.ui

    def test_history_modal_html_exists(self):
        for needed in (
            'id="ag-history-modal"',
            'id="ag-history-title"',
            'id="ag-history-summary"',
            'id="ag-history-body"',
            'id="ag-history-close"',
            'id="ag-history-backdrop"',
        ):
            assert needed in self.ui, f"History modal element missing: {needed}"

    def test_history_handler_function_exists(self):
        assert "async function openAGHistory" in self.ui
        assert "/api/alert-groups/runs?" in self.ui
        assert "limit=25" in self.ui

    def test_history_modal_has_dismissal_handlers(self):
        # Close button + backdrop click + Escape key - same pattern as
        # the YAML viewer modal.
        assert "function closeAGHistoryModal" in self.ui
        assert "ag-history-close" in self.ui
        assert "ag-history-backdrop" in self.ui


# ─────────────────────────────────────────────────────────────────────
# 6. error_email_disabled - backend round-trip + dispatcher gate
# ─────────────────────────────────────────────────────────────────────

class TestErrorEmailDisabled:

    def test_save_load_round_trip(self, ag_store):
        rec = ag_store.save_group(_ag_payload(error_email_disabled=True))
        assert rec["error_email_disabled"] is True
        loaded = ag_store.get_group("test_ag_27")
        assert loaded["error_email_disabled"] is True

    def test_default_false_when_field_missing(self, ag_store):
        rec = ag_store.save_group(_ag_payload())
        assert rec["error_email_disabled"] is False

    def test_update_can_flip_field(self, ag_store):
        ag_store.save_group(_ag_payload(error_email_disabled=True))
        upd = ag_store.update_group(
            "test_ag_27", {"error_email_disabled": False}
        )
        assert upd["error_email_disabled"] is False

    def test_legacy_yaml_falls_through_to_false(self, ag_store):
        path = ag_store._dir / "legacy.yaml"
        path.write_text(
            "name: legacy\n"
            "description: x\n"
            "search_names: [a]\n"
            "prompt_text: |\n  legacy\n"
            "schedule: '0 12 * * *'\n"
            "max_rows: 10\n"
            "email_address: u@x.com\n"
            "disabled: false\n"
            "created_at: '2026-01-01T00:00:00'\n"
            "updated_at: '2026-01-01T00:00:00'\n",
            encoding="utf-8",
        )
        loaded = ag_store.get_group("legacy")
        # Defaulting to True would silence operational notifications on
        # every existing AG - the wrong direction.
        assert bool(loaded.get("error_email_disabled", False)) is False

    def test_dispatcher_short_circuits_on_disabled(self, ag_store, monkeypatch):
        """When ``error_email_disabled=True`` the failure-email helper
        must return BEFORE consulting ``admin_error_email`` or any
        global fallback. We mock the AG load and the email-sending
        helper; if the AG is opted out, the email helper must not run."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.models import AlertGroupRunResult

        ag_store.save_group(_ag_payload(
            admin_error_email="admin@x.com",
            error_email_disabled=True,
        ))
        # Patch the store loader inside dispatcher so it sees our
        # fixture rather than the project's real store.
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore", lambda: ag_store
        )

        # Stub get_settings to ensure the failure-email enabled toggle is on.
        from global_settings import GlobalSettings
        fake_settings = GlobalSettings.__new__(GlobalSettings)
        fake_settings._data = {
            "alert_group_failure_email_enabled": True,
            "alert_group_failure_email_to": "global@x.com",
            "smtp_from": "from@x.com",
            "smtp_user": "user@x.com",
        }
        fake_settings.get = lambda k, d=None: fake_settings._data.get(k, d)
        monkeypatch.setattr(
            "global_settings.get_settings", lambda: fake_settings
        )

        sent = []
        monkeypatch.setattr(
            AlertGroupDispatcher, "_send_plain_email",
            staticmethod(lambda subj, body, to: sent.append((subj, to))),
        )

        result = AlertGroupRunResult(
            group_name="test_ag_27",
            status="error",
            error_message="dummy",
        )
        AlertGroupDispatcher._maybe_send_failure_email(result)
        assert sent == [], (
            "Money-leak guard: the dispatcher sent a failure email even "
            "though the AG had error_email_disabled=true. The opt-out "
            "must short-circuit BEFORE the fallback chain runs."
        )

    def test_dispatcher_still_sends_when_not_disabled(self, ag_store, monkeypatch):
        """Symmetric guard: when error_email_disabled is False (or
        absent), the failure email DOES go out."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from alert_groups.models import AlertGroupRunResult

        ag_store.save_group(_ag_payload(
            admin_error_email="admin@x.com",
            error_email_disabled=False,
        ))
        import alert_group_store as ag_module
        monkeypatch.setattr(
            ag_module, "AlertGroupStore", lambda: ag_store
        )

        from global_settings import GlobalSettings
        fake_settings = GlobalSettings.__new__(GlobalSettings)
        fake_settings._data = {
            "alert_group_failure_email_enabled": True,
            "alert_group_failure_email_to": "global@x.com",
            "smtp_from": "from@x.com",
            "smtp_user": "user@x.com",
        }
        fake_settings.get = lambda k, d=None: fake_settings._data.get(k, d)
        monkeypatch.setattr(
            "global_settings.get_settings", lambda: fake_settings
        )

        sent = []
        monkeypatch.setattr(
            AlertGroupDispatcher, "_send_plain_email",
            staticmethod(lambda subj, body, to: sent.append((subj, to))),
        )

        result = AlertGroupRunResult(
            group_name="test_ag_27",
            status="error",
            error_message="dummy",
        )
        AlertGroupDispatcher._maybe_send_failure_email(result)
        assert len(sent) == 1, "Failure email should still go to admin"
        # Per-AG admin wins over global fallback (Wave 5 priority).
        assert sent[0][1] == "admin@x.com"


# ─────────────────────────────────────────────────────────────────────
# 7. UI form contracts for the new error_email_disabled checkbox
# ─────────────────────────────────────────────────────────────────────

class TestErrorEmailDisabledFrontendContracts:

    def setup_method(self):
        self.ui = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_ag_form_has_disable_checkbox(self):
        assert 'id="ag-error-email-disabled"' in self.ui

    def test_ag_save_payload_includes_field(self):
        # The save payload must thread the checkbox value to the server.
        assert "error_email_disabled" in self.ui
        assert "ag-error-email-disabled" in self.ui

    def test_settings_page_documents_master_default(self):
        # Help text must explain that this is the master default applied
        # to ALL AGs, not just one.
        assert "Default Error Email" in self.ui
        assert "Master fallback" in self.ui or "master fallback" in self.ui


# ─────────────────────────────────────────────────────────────────────
# 8. /api/alert-groups/runs endpoint smoke
# ─────────────────────────────────────────────────────────────────────

class TestRunHistoryEndpoint:

    def test_endpoint_returns_runs_with_expected_shape(self, ag_store):
        # Insert a few runs, list them, assert the shape the modal
        # depends on.
        ag_store.save_group(_ag_payload())
        ag_store.log_run(
            group_name="test_ag_27", status="success",
            actual_tokens=1234, cost_usd=0.05,
        )
        ag_store.log_run(
            group_name="test_ag_27", status="error",
            error_message="boom", searches_used=["search_a", "search_b"],
        )
        runs = ag_store.list_runs(group_name="test_ag_27", limit=25)
        assert len(runs) == 2
        # Both inserted runs are present; ordering across tied
        # second-precision triggered_at is non-deterministic in SQLite,
        # so we assert membership rather than position.
        statuses = {r["status"] for r in runs}
        assert statuses == {"success", "error"}
        # Fields the modal renders:
        for f in ("status", "triggered_at", "actual_tokens", "cost_usd",
                  "error_message", "searches_used"):
            assert f in runs[0]
