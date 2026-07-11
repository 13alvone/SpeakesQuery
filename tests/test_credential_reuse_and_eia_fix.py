"""
Tests for the 2026-04-26 follow-up fixes:
* Credential reuse across scripts (promote-to-global vault method,
  ``GET /api/credentials/<id>?split=true`` endpoint, promote endpoint,
  UI contract).
* EIA Daily Electricity Demand script: endpoint URL fix
  (``region-data`` → ``daily-region-data``) + per-region failure
  logging so silent zero-row results become visible.

Both fixes were filed by the user as separate issues but ship together
because each is small and the credential reuse fix is the first time
the global-vault UI gets integrated into the per-script credentials
form.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Vault: promote_to_global ──────────────────────────────────────────
class TestPromoteToGlobalVault:
    @pytest.fixture
    def vault(self, tmp_path):
        from scheduled_input_engine.credentials import CredentialVault
        v = CredentialVault(
            db_path=tmp_path / "creds.sqlite",
            key_dir=tmp_path / "keys",
        )
        v._init_db()
        return v

    def test_promote_moves_per_task_value_into_global(self, vault):
        vault.store(42, "FRED_API_KEY", "secret-value-123")
        assert "FRED_API_KEY" in vault.list_keys(42, include_global=False)
        assert "FRED_API_KEY" not in vault.list_global_keys()

        vault.promote_to_global(42, "FRED_API_KEY")

        # Per-task entry gone, global entry created with the same value
        assert "FRED_API_KEY" not in vault.list_keys(42, include_global=False)
        assert "FRED_API_KEY" in vault.list_global_keys()
        assert vault.retrieve_global("FRED_API_KEY") == "secret-value-123"

    def test_promote_does_not_emit_plaintext_anywhere(self, vault, caplog):
        """Plaintext value must never appear in logs - promote happens
        entirely server-side via decrypt → store_global → delete."""
        import logging
        caplog.set_level(logging.INFO)
        vault.store(7, "MY_KEY", "plaintext-secret-xyz")
        vault.promote_to_global(7, "MY_KEY")
        # No log line may contain the plaintext value
        for record in caplog.records:
            assert "plaintext-secret-xyz" not in record.getMessage(), (
                "REGRESSION: plaintext credential value leaked to log"
            )

    def test_promote_raises_on_missing_per_task_entry(self, vault):
        with pytest.raises(KeyError):
            vault.promote_to_global(99, "DOES_NOT_EXIST")

    def test_promoted_global_resolves_for_other_scripts(self, vault):
        """The whole point: after promote, OTHER scripts that haven't
        stored their own copy of the key still get the value when they
        decrypt their merged credential map."""
        vault.store(1, "EIA_API_KEY", "eia-secret-456")
        vault.promote_to_global(1, "EIA_API_KEY")
        # Script 2 has no per-task creds at all
        merged = vault.decrypt_for_script(2)
        assert merged is not None
        assert merged.get("EIA_API_KEY") == "eia-secret-456"

    def test_promote_overwrites_existing_global(self, vault):
        """If a global with the same name already exists, promote
        overwrites it with the per-task value (consistent with
        ``store_global``'s upsert semantics)."""
        vault.store_global("SHARED_KEY", "old-global")
        vault.store(5, "SHARED_KEY", "new-from-script")
        vault.promote_to_global(5, "SHARED_KEY")
        assert vault.retrieve_global("SHARED_KEY") == "new-from-script"


# ── Engine + endpoint plumbing ────────────────────────────────────────
@pytest.fixture
def client():
    from scheduled_input_engine import start_engine
    start_engine()
    from desktop_app.server import app
    app.config["TESTING"] = True
    return app.test_client()


class TestSplitListEndpoint:
    def test_split_query_returns_per_script_and_global_arrays(self, client):
        """Default behavior unchanged - adding ``?split=true`` adds the
        new fields without breaking existing callers that read .keys."""
        resp = client.get("/api/credentials/0?split=true")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert "keys" in data            # back-compat (merged)
        assert "per_script" in data      # new
        assert "global" in data          # new
        assert isinstance(data["per_script"], list)
        assert isinstance(data["global"], list)

    def test_default_get_still_returns_only_keys_field(self, client):
        """Without ``?split=true`` the endpoint preserves its 1.x
        response shape - only the merged ``keys`` array."""
        resp = client.get("/api/credentials/0")
        data = resp.get_json()
        assert data["status"] == "success"
        assert "keys" in data
        # No surprise fields for callers that just want the merged list
        assert "per_script" not in data
        assert "global" not in data


class TestPromoteEndpoint:
    def test_404_when_per_script_credential_missing(self, client):
        resp = client.post(
            "/api/credentials/1234567/NOT_THERE_KEY/promote-to-global",
        )
        assert resp.status_code == 404

    def test_promote_succeeds_and_credential_visible_in_global_split(
        self, client,
    ):
        # Use an unlikely script id so we don't collide with the user's
        # actual data on the dev box.
        from scheduled_input_engine import get_engine
        eng = get_engine()
        # Clean slate for the test key
        try:
            eng._vault.delete(0, "WAVE7_TEST_KEY")
        except Exception:
            pass
        try:
            eng._vault.delete_global("WAVE7_TEST_KEY")
        except Exception:
            pass

        eng.store_credential(0, "WAVE7_TEST_KEY", "wave-7-test-secret")
        try:
            resp = client.post(
                "/api/credentials/0/WAVE7_TEST_KEY/promote-to-global",
            )
            assert resp.status_code == 200, resp.get_json()
            data = resp.get_json()
            assert data["status"] == "success"
            assert "global" in data["message"].lower()

            # After promote: not per-script (script_id=0), but in globals
            split = client.get(
                "/api/credentials/0?split=true",
            ).get_json()
            assert "WAVE7_TEST_KEY" not in split["per_script"]
            assert "WAVE7_TEST_KEY" in split["global"]
        finally:
            try:
                eng._vault.delete_global("WAVE7_TEST_KEY")
            except Exception:
                pass


# ── EIA Daily Electricity Demand fix ──────────────────────────────────
class TestEIAElectricityDemandEndpoint:
    """Pin the URL fix: the script must hit ``daily-region-data``, not
    the hourly ``region-data`` route. The hourly endpoint silently
    returned empty data arrays for ``frequency=daily`` requests, which
    is what produced the user's "0 rows even with valid API key"
    failure.
    """

    SCRIPT_PATH = (
        REPO_ROOT
        / "script_library" / "scripts" / "eia_electricity_demand.json"
    )

    def _script(self) -> dict:
        return json.loads(self.SCRIPT_PATH.read_text(encoding="utf-8"))

    def test_api_url_field_uses_daily_endpoint(self):
        s = self._script()
        assert s["api_url"] == (
            "https://api.eia.gov/v2/electricity/rto/daily-region-data/data"
        ), (
            "EIA Daily Electricity Demand must hit the DAILY route. "
            "The hourly /region-data route silently returns empty data "
            "for frequency=daily requests."
        )

    def test_script_body_uses_daily_endpoint(self):
        s = self._script()
        # Defence in depth - the script body's hard-coded URL must also
        # be the daily one. A JSON-only fix is not sufficient because
        # the script's HTTP call doesn't read api_url.
        assert "daily-region-data" in s["code"], (
            "Script body must reference daily-region-data; otherwise "
            "the api_url field is decorative and the request will hit "
            "the broken hourly endpoint."
        )
        assert "rto/region-data/data'," not in s["code"], (
            "REGRESSION: script body still references the hourly route"
        )

    def test_script_captures_per_region_failures(self):
        s = self._script()
        # The 2026-04-26 fix replaced the bare `except Exception: continue`
        # with explicit failure capture into a `failures` list. Make sure
        # the diagnostic stayed (the print() approach didn't survive the
        # RestrictedPython sandbox; capture-into-list is the working
        # pattern).
        assert "failures.append" in s["code"], (
            "Per-region failures must be captured for diagnostics - the "
            "prior silent-except produced 0-row results with no breadcrumb"
        )

    def test_script_emits_sentinel_on_total_failure(self):
        s = self._script()
        # 2026-05-02 redesign (commit d0362b9): the original raise-on-failure
        # path was replaced with a sentinel-row pattern (region='INFO',
        # regime_flag='API_ERROR') so downstream feeders surface the
        # diagnostic via cohort tally instead of the engine logging a
        # bare exception. Pin the new contract:
        #   1. Script does NOT raise - sentinel emit handles all 0-row paths
        #   2. Sentinel row uses regime_flag='API_ERROR' so the downstream
        #      `egib_electricity_demand` SPQL feeder's
        #      `eventstats sum(if_(regime_flag="API_ERROR", 1, 0))` cohort
        #      tally surfaces the failure to the operator
        #   3. Sentinel row has region='INFO' so SPQL filters that drop
        #      'INFO' rows (the standard sentinel pattern) work
        assert "raise RuntimeError" not in s["code"], (
            "Script must NOT raise - sentinel pattern is the design "
            "(commit d0362b9, 2026-05-02). See "
            "reference_oeb_script_sentinel_rows.md."
        )
        assert "'API_ERROR'" in s["code"] or '"API_ERROR"' in s["code"], (
            "Total-failure path must emit a sentinel row with "
            "regime_flag='API_ERROR' so downstream feeders' cohort tally "
            "surfaces the failure."
        )
        assert "'region': 'INFO'" in s["code"] or '"region": "INFO"' in s["code"], (
            "Sentinel row must use region='INFO' so the downstream feeder's "
            "`where region != \"INFO\"` filter drops it from the headline list."
        )


# ── Frontend contract regressions ─────────────────────────────────────
class TestFrontendContracts:
    def _ui(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_credentials_loader_uses_split_endpoint(self):
        ui = self._ui()
        assert "/api/credentials/${scriptId}?split=true" in ui, (
            "loadSiCredentials must request ?split=true so the renderer "
            "can show per-script + global sections distinctly."
        )

    def test_make_global_button_present(self):
        ui = self._ui()
        assert "Make global" in ui, (
            "Per-script credential rows must expose a 'Make global' "
            "button that promotes the entry to the global vault."
        )
        assert "promoteSiCredential" in ui, (
            "promoteSiCredential() helper must exist."
        )
        assert "/promote-to-global" in ui, (
            "Promote button must POST to /promote-to-global."
        )

    def test_global_section_label_present(self):
        ui = self._ui()
        # Loose match - the heading text uses backticks + a count.
        assert "Globally available" in ui, (
            "Credentials box must label the global section so the "
            "operator distinguishes per-script from globally-available."
        )

    def test_global_rows_are_read_only_in_script_view(self):
        ui = self._ui()
        # Globals shouldn't carry a Remove button on the script's view -
        # removing them per-script would silently break every other
        # script. Verify the manage-elsewhere hint is present.
        assert "manage in Settings" in ui, (
            "Global credential rows on the script view must point to "
            "Settings → Global Credentials instead of offering an "
            "in-place Remove that would mislead the operator."
        )
