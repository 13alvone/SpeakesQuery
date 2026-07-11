"""
Tests for Wave 5 (2026-04-26): per-AG / per-search ``admin_error_email``
field that splits error/diagnostic email routing from the customer-
facing recipient list.

Coverage
--------
* AlertGroupStore + SavedSearchStore round-trip the new field through
  YAML save/load/update without losing it.
* The validator accepts blank (= use fallback) and a valid `@`-form
  address; rejects malformed input.
* Existing YAMLs that pre-date the field load cleanly (schema is
  additive - no migration required).
* The AG dispatcher's ``_maybe_send_failure_email`` routes to the
  per-AG ``admin_error_email`` when set; falls through to the global
  ``alert_group_failure_email_to`` setting when blank; falls through
  again to ``smtp_from`` as a last resort.
* The customer-facing ``email_address`` (often a paid mailing list)
  NEVER receives a failure email - that's the security-critical
  invariant Wave 5 exists to enforce. Pinned with a regression that
  asserts the failure email's `to_addr` is not the AG's email_address
  when admin_error_email is set.
* Frontend contracts (static text scan):
    - Both AG + saved-search forms have the new input
    - Both forms include the field in the load + save paths
    - Field is autocomplete=off so browsers don't autofill it
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Helpers ───────────────────────────────────────────────────────────
@pytest.fixture
def ag_store(tmp_path):
    """Isolated AlertGroupStore pointing at tmp_path."""
    from alert_group_store import AlertGroupStore
    s = AlertGroupStore()
    s._dir = tmp_path / "ag"
    s._db = str(tmp_path / "lc.sqlite")
    s._runs_db = str(tmp_path / "runs.sqlite")
    s.initialize()
    return s


@pytest.fixture
def ss_store(tmp_path):
    """Isolated SavedSearchStore pointing at tmp_path."""
    from saved_search_store import SavedSearchStore
    s = SavedSearchStore()
    s._dir = tmp_path / "ss"
    s._defaults_dir = tmp_path / "ss_defaults"
    s._db = str(tmp_path / "lc.sqlite")
    s._dir.mkdir(parents=True, exist_ok=True)
    s._defaults_dir.mkdir(parents=True, exist_ok=True)
    s._init_db()
    return s


def _ag_payload(name: str, **overrides) -> dict:
    base = {
        "name": name,
        "search_names": ["fake_feeder"],
        "prompt_text": "Analyze the data.",
        "email_address": "customer@example.com",
    }
    base.update(overrides)
    return base


def _ss_payload(name: str, **overrides) -> dict:
    base = {
        "name": name,
        "purpose": "standalone",
        "query": 'index="indexes/test/foo/*.parquet" | head 1',
        "cron_schedule": "0 * * * *",
        "lookback": "-1h",
        "trigger": "once",
        "email_address": "customer@example.com",
        "send_email": "yes",
    }
    base.update(overrides)
    return base


# ── AlertGroupStore schema ─────────────────────────────────────────────
class TestAlertGroupSchema:
    def test_admin_error_email_round_trips(self, ag_store):
        g = ag_store.save_group(_ag_payload(
            "wave5_ag_round", admin_error_email="admin@example.com",
        ))
        assert g["admin_error_email"] == "admin@example.com"
        reloaded = ag_store.get_group("wave5_ag_round")
        assert reloaded["admin_error_email"] == "admin@example.com"
        # email_address is preserved separately
        assert reloaded["email_address"] == "customer@example.com"

    def test_blank_admin_error_email_accepted(self, ag_store):
        """Blank admin field = use global fallback, must save fine."""
        g = ag_store.save_group(_ag_payload("wave5_ag_blank"))
        assert g["admin_error_email"] == ""

    def test_invalid_admin_error_email_rejected(self, ag_store):
        with pytest.raises(ValueError, match="email"):
            ag_store.save_group(_ag_payload(
                "wave5_ag_bad", admin_error_email="not-an-email",
            ))

    def test_update_can_change_admin_error_email(self, ag_store):
        ag_store.save_group(_ag_payload(
            "wave5_ag_upd", admin_error_email="old@example.com",
        ))
        updated = ag_store.update_group("wave5_ag_upd", {
            "admin_error_email": "new@example.com",
        })
        assert updated["admin_error_email"] == "new@example.com"

    def test_update_can_clear_admin_error_email(self, ag_store):
        ag_store.save_group(_ag_payload(
            "wave5_ag_clear", admin_error_email="will@example.com",
        ))
        updated = ag_store.update_group("wave5_ag_clear", {
            "admin_error_email": "",
        })
        assert updated["admin_error_email"] == ""

    def test_legacy_yaml_without_field_loads_clean(self, ag_store, tmp_path):
        """Existing YAMLs that pre-date Wave 5 must load without error.
        Schema is additive - never block on missing column."""
        legacy_yaml = (tmp_path / "ag" / "legacy.yaml")
        legacy_yaml.parent.mkdir(parents=True, exist_ok=True)
        legacy_yaml.write_text(
            "name: legacy\n"
            "description: ''\n"
            "search_names: ['x']\n"
            "prompt_text: 'analyze'\n"
            "schedule: ''\n"
            "max_rows: 100\n"
            "email_address: 'old@example.com'\n"
            "disabled: false\n"
            "delivery_mode: api\n"
            "created_at: '2026-01-01T00:00:00'\n"
            "updated_at: '2026-01-01T00:00:00'\n",
            encoding="utf-8",
        )
        # Should load fine - admin_error_email defaults to absent / "".
        g = ag_store.get_group("legacy")
        assert g["email_address"] == "old@example.com"
        # Field absent from YAML → key missing OR coerced to "" by reader
        assert g.get("admin_error_email", "") == ""


# ── SavedSearchStore schema ────────────────────────────────────────────
class TestSavedSearchSchema:
    def test_admin_error_email_round_trips(self, ss_store):
        s = ss_store.save_search(_ss_payload(
            "wave5_ss_round", admin_error_email="admin@example.com",
        ))
        assert s["admin_error_email"] == "admin@example.com"
        reloaded = ss_store.get_search("wave5_ss_round")
        assert reloaded["admin_error_email"] == "admin@example.com"

    def test_blank_admin_error_email_accepted(self, ss_store):
        s = ss_store.save_search(_ss_payload("wave5_ss_blank"))
        assert s["admin_error_email"] == ""

    def test_invalid_admin_error_email_rejected(self, ss_store):
        with pytest.raises(ValueError, match="email"):
            ss_store.save_search(_ss_payload(
                "wave5_ss_bad", admin_error_email="not-an-email",
            ))

    def test_update_can_change_admin_error_email(self, ss_store):
        ss_store.save_search(_ss_payload(
            "wave5_ss_upd", admin_error_email="old@example.com",
        ))
        updated = ss_store.update_search("wave5_ss_upd", {
            "admin_error_email": "new@example.com",
        })
        assert updated["admin_error_email"] == "new@example.com"


# ── AG dispatcher routing ─────────────────────────────────────────────
class TestAGFailureRouting:
    """Wave 5's central security invariant: customer-facing email_address
    must never receive a failure notice when admin_error_email is set.
    """

    def _make_result(
        self, group_name: str = "fail_route_test",
    ):
        from alert_groups.models import AlertGroupRunResult
        # Real model (not mocked) so any future field changes flow
        # through this test cleanly.
        return AlertGroupRunResult(
            group_name=group_name,
            status="error",
            error_message="simulated dispatch failure for routing test",
        )

    def test_per_ag_admin_email_wins_over_global(self, ag_store, tmp_path):
        """When admin_error_email is set on the AG, it MUST be the
        recipient - not the global setting, not the customer email."""
        ag_store.save_group(_ag_payload(
            "fail_route_test",
            email_address="customer-list@example.com",
            admin_error_email="ops-on-call@example.com",
        ))

        from alert_groups.dispatcher import AlertGroupDispatcher
        captured = {}

        def _fake_send(subject, body, to_addr):
            captured["to"] = to_addr
            captured["subject"] = subject

        with patch.object(
            AlertGroupDispatcher, "_send_plain_email",
            staticmethod(_fake_send),
        ), patch("alert_group_store.AlertGroupStore") as MockStore:
            MockStore.return_value = ag_store
            with patch("global_settings.get_settings") as gs:
                gs.return_value = {
                    "alert_group_failure_email_enabled": True,
                    "alert_group_failure_email_to": "global-fallback@example.com",
                    "smtp_from": "smtp@example.com",
                    "smtp_user": "smtp@example.com",
                }
                AlertGroupDispatcher._maybe_send_failure_email(
                    self._make_result(),
                )

        assert "to" in captured, "_send_plain_email was never called"
        assert captured["to"] == "ops-on-call@example.com", (
            f"per-AG admin_error_email must win over global fallback; "
            f"got {captured['to']!r}"
        )
        # Customer-facing recipient must NEVER receive failure emails.
        assert captured["to"] != "customer-list@example.com", (
            "REGRESSION: failure email was about to go to the customer "
            "recipient list - this is the central Wave 5 invariant"
        )

    def test_falls_back_to_global_when_per_ag_blank(
        self, ag_store, tmp_path,
    ):
        """Blank per-AG admin → global setting wins, never falls to
        the customer email_address."""
        ag_store.save_group(_ag_payload(
            "fail_route_test",
            email_address="customer-list@example.com",
            admin_error_email="",
        ))

        from alert_groups.dispatcher import AlertGroupDispatcher
        captured = {}

        def _fake_send(subject, body, to_addr):
            captured["to"] = to_addr

        with patch.object(
            AlertGroupDispatcher, "_send_plain_email",
            staticmethod(_fake_send),
        ), patch("alert_group_store.AlertGroupStore") as MockStore:
            MockStore.return_value = ag_store
            with patch("global_settings.get_settings") as gs:
                gs.return_value = {
                    "alert_group_failure_email_enabled": True,
                    "alert_group_failure_email_to": "global-fallback@example.com",
                    "smtp_from": "smtp@example.com",
                    "smtp_user": "smtp@example.com",
                }
                AlertGroupDispatcher._maybe_send_failure_email(
                    self._make_result(),
                )

        assert captured["to"] == "global-fallback@example.com"
        assert captured["to"] != "customer-list@example.com"

    def test_falls_back_to_smtp_from_when_no_admin_anywhere(
        self, ag_store, tmp_path,
    ):
        ag_store.save_group(_ag_payload(
            "fail_route_test",
            email_address="customer-list@example.com",
            admin_error_email="",
        ))

        from alert_groups.dispatcher import AlertGroupDispatcher
        captured = {}

        def _fake_send(subject, body, to_addr):
            captured["to"] = to_addr

        with patch.object(
            AlertGroupDispatcher, "_send_plain_email",
            staticmethod(_fake_send),
        ), patch("alert_group_store.AlertGroupStore") as MockStore:
            MockStore.return_value = ag_store
            with patch("global_settings.get_settings") as gs:
                gs.return_value = {
                    "alert_group_failure_email_enabled": True,
                    "alert_group_failure_email_to": "",
                    "smtp_from": "ops@smtp.example.com",
                    "smtp_user": "",
                }
                AlertGroupDispatcher._maybe_send_failure_email(
                    self._make_result(),
                )

        assert captured["to"] == "ops@smtp.example.com"
        assert captured["to"] != "customer-list@example.com"

    def test_failure_email_disabled_globally_skips(
        self, ag_store,
    ):
        """When ``alert_group_failure_email_enabled=False`` the
        dispatcher must skip the send entirely - even when a per-AG
        admin_error_email is set."""
        ag_store.save_group(_ag_payload(
            "fail_route_test",
            admin_error_email="ops@example.com",
        ))

        from alert_groups.dispatcher import AlertGroupDispatcher
        send_called = []

        def _fake_send(subject, body, to_addr):
            send_called.append(to_addr)

        with patch.object(
            AlertGroupDispatcher, "_send_plain_email",
            staticmethod(_fake_send),
        ):
            with patch("global_settings.get_settings") as gs:
                gs.return_value = {
                    "alert_group_failure_email_enabled": False,
                    "alert_group_failure_email_to": "x@example.com",
                    "smtp_from": "y@example.com",
                }
                AlertGroupDispatcher._maybe_send_failure_email(
                    self._make_result(),
                )
        assert send_called == [], (
            "failure email must respect the global enable/disable flag"
        )


# ── Frontend contract regressions ─────────────────────────────────────
class TestFrontendContracts:
    def _ui(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_ag_form_has_admin_error_email_input(self):
        ui = self._ui()
        assert 'id="ag-admin-error-email"' in ui, (
            "Wave 5 AG form must expose the admin_error_email input."
        )

    def test_ss_form_has_admin_error_email_input(self):
        ui = self._ui()
        assert 'id="ss-admin-error-email"' in ui, (
            "Wave 5 saved-search form must expose the admin_error_email "
            "input."
        )

    def test_ag_save_payload_includes_admin_error_email(self):
        ui = self._ui()
        # Save payload object literal must carry admin_error_email
        assert "admin_error_email" in ui, (
            "AG save payload must include admin_error_email for the "
            "backend route to read."
        )

    def test_ag_load_populates_admin_error_email(self):
        ui = self._ui()
        # Edit-load path must read group.admin_error_email so the field
        # round-trips through Edit.
        assert "group.admin_error_email" in ui, (
            "AG Edit-load must populate admin_error_email from the "
            "group's saved value."
        )

    def test_ss_load_populates_admin_error_email(self):
        ui = self._ui()
        assert "s.admin_error_email" in ui, (
            "Saved-search Edit-load must populate admin_error_email."
        )

    def test_admin_inputs_have_autocomplete_off(self):
        ui = self._ui()
        # Browser autofill on admin email is the kind of footgun the
        # block-autofill memory entry warns about.
        for inp_id in ("ag-admin-error-email", "ss-admin-error-email"):
            # Find the line for this input
            idx = ui.find(f'id="{inp_id}"')
            assert idx != -1
            # Read ~200 chars around the input to find autocomplete attr
            window = ui[max(0, idx - 200): idx + 400]
            assert 'autocomplete="off"' in window, (
                f"{inp_id} input must include autocomplete=\"off\" so "
                f"browsers don't overwrite the operator's value at "
                f"submit time."
            )
