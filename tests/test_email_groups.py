"""
Tests for the email-group feature:
  - validation/EmailGroupValidation
  - email_group_store (CRUD + resolution + cycle detection)
  - validation/SavedSearchValidation.validate_email accepts @group refs
  - validation/AlertGroupValidation.validate_email accepts @group refs
  - /api/email-groups/* endpoints
  - End-to-end: a saved search / AG with `email_address` containing
    @group_name resolves correctly through query_engine.Alert.

The store + validation tests use temporary directories so they do not
touch the user's real `email_groups/` dir. The API tests use the Flask
test client.
"""

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

# Ensure project root on sys.path
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from validation.EmailGroupValidation import EmailGroupValidation
from validation.SavedSearchValidation import SavedSearchValidation
from validation.AlertGroupValidation import AlertGroupValidation


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_groups_dir(tmp_path, monkeypatch):
    """Point the EmailGroupStore at a temporary directory."""
    test_dir = tmp_path / "email_groups"
    test_dir.mkdir()
    import email_group_store as egs
    monkeypatch.setattr(egs, "EMAIL_GROUPS_DIR", test_dir)
    egs._reset_shared_store_for_tests()
    yield test_dir
    egs._reset_shared_store_for_tests()


def _make_store(test_dir):
    import email_group_store as egs
    store = egs.EmailGroupStore()
    store._dir = test_dir
    return store


# ──────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────


class TestEmailGroupValidationName:
    """validate_name accepts snake_case ASCII; rejects spaces, '@', other."""

    @pytest.mark.parametrize("name", [
        "team_a", "sales", "ops_team_2026", "team123",
    ])
    def test_valid_names(self, name):
        assert EmailGroupValidation.validate_name(name) == name

    @pytest.mark.parametrize("name,reason", [
        ("", "empty"),
        ("   ", "whitespace only"),
        ("team-a", "hyphen not permitted"),
        ("team a", "space not permitted"),
        ("team@a", "'@' not permitted"),
        ("@team", "leading @ not permitted in name"),
        ("team.a", "dot not permitted"),
    ])
    def test_invalid_names(self, name, reason):
        with pytest.raises(ValueError):
            EmailGroupValidation.validate_name(name)


class TestEmailGroupValidationAddress:
    """validate_email_address accepts emails and @group_name refs."""

    @pytest.mark.parametrize("addr", [
        "user@example.com",
        "alice.smith@work.co.uk",
        "user+tag@domain.io",
        "@team_a",
        "@sales",
        "@team_2026",
    ])
    def test_valid_addresses(self, addr):
        assert EmailGroupValidation.validate_email_address(addr) == addr

    @pytest.mark.parametrize("bad", [
        "",
        "not-an-email",
        "missing@tld",          # no domain dot
        "@",                    # bare @
        "@team-a",              # group ref with hyphen
        "@team a",              # group ref with space
        "@team@x",              # group ref with extra @
    ])
    def test_invalid_addresses(self, bad):
        with pytest.raises(ValueError):
            EmailGroupValidation.validate_email_address(bad)

    def test_validate_email_addresses_dedup(self):
        result = EmailGroupValidation.validate_email_addresses([
            "alice@x.com", "ALICE@x.com", "bob@y.com",
        ])
        # Case-insensitive dedup keeps first form
        assert result == ["alice@x.com", "bob@y.com"]

    def test_validate_email_addresses_empty_rejected(self):
        with pytest.raises(ValueError):
            EmailGroupValidation.validate_email_addresses([])

    def test_validate_email_addresses_nested_groups_ok(self):
        result = EmailGroupValidation.validate_email_addresses([
            "@team_a", "@team_b", "alice@x.com",
        ])
        assert result == ["@team_a", "@team_b", "alice@x.com"]


class TestSplitRawRecipients:
    """split_raw_recipients tolerates string AND list inputs."""

    @pytest.mark.parametrize("raw,expected", [
        ("alice@x.com", ["alice@x.com"]),
        ("alice@x.com, bob@y.com", ["alice@x.com", "bob@y.com"]),
        ("alice@x.com; bob@y.com", ["alice@x.com", "bob@y.com"]),
        ("alice@x.com,@team", ["alice@x.com", "@team"]),
        ("", []),
        (None, []),
        (["alice@x.com", "bob@y.com"], ["alice@x.com", "bob@y.com"]),
        (["alice@x.com", "  ", "bob@y.com"], ["alice@x.com", "bob@y.com"]),
    ])
    def test_split(self, raw, expected):
        assert EmailGroupValidation.split_raw_recipients(raw) == expected


# ──────────────────────────────────────────────────────────────────
# Store CRUD
# ──────────────────────────────────────────────────────────────────


class TestEmailGroupStoreCRUD:

    def test_save_and_list(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        record = store.save_group({
            "name": "ops_team",
            "description": "Operations distribution list",
            "email_addresses": ["alice@x.com", "bob@y.com"],
        })
        assert record["name"] == "ops_team"
        assert record["email_addresses"] == ["alice@x.com", "bob@y.com"]
        assert "created_at" in record and "updated_at" in record

        groups = store.list_groups()
        assert len(groups) == 1
        assert groups[0]["name"] == "ops_team"

    def test_save_duplicate_without_overwrite_fails(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "team_a",
            "email_addresses": ["alice@x.com"],
        })
        with pytest.raises(FileExistsError):
            store.save_group({
                "name": "team_a",
                "email_addresses": ["bob@y.com"],
            })

    def test_save_overwrite_preserves_created_at(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        first = store.save_group({
            "name": "team_a",
            "email_addresses": ["alice@x.com"],
        })
        second = store.save_group({
            "name": "team_a",
            "email_addresses": ["bob@y.com"],
        }, overwrite=True)
        assert second["created_at"] == first["created_at"]
        assert second["email_addresses"] == ["bob@y.com"]

    def test_get_missing(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        with pytest.raises(FileNotFoundError):
            store.get_group("nonexistent")

    def test_update(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "team_a",
            "description": "v1",
            "email_addresses": ["alice@x.com"],
        })
        updated = store.update_group("team_a", {
            "description": "v2 with notes",
            "email_addresses": ["alice@x.com", "bob@y.com"],
        })
        assert updated["description"] == "v2 with notes"
        assert "bob@y.com" in updated["email_addresses"]

    def test_delete(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "team_a",
            "email_addresses": ["alice@x.com"],
        })
        store.delete_group("team_a")
        assert store.list_groups() == []
        with pytest.raises(FileNotFoundError):
            store.delete_group("team_a")

    def test_save_invalid_email_raises(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        with pytest.raises(ValueError):
            store.save_group({
                "name": "team_a",
                "email_addresses": ["not-an-email"],
            })

    def test_save_invalid_name_raises(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        with pytest.raises(ValueError):
            store.save_group({
                "name": "team a",  # space disallowed
                "email_addresses": ["alice@x.com"],
            })


# ──────────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────────


class TestResolveRecipients:

    def test_pure_literals_passthrough(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        result = store.resolve_recipients("alice@x.com, bob@y.com")
        assert result == ["alice@x.com", "bob@y.com"]

    def test_single_group_expansion(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "ops",
            "email_addresses": ["alice@x.com", "bob@y.com"],
        })
        result = store.resolve_recipients("@ops")
        assert result == ["alice@x.com", "bob@y.com"]

    def test_mixed_literal_and_group(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "ops",
            "email_addresses": ["alice@x.com"],
        })
        result = store.resolve_recipients("@ops, charlie@z.com")
        assert result == ["alice@x.com", "charlie@z.com"]

    def test_dedup_across_groups(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "ops",
            "email_addresses": ["alice@x.com", "bob@y.com"],
        })
        store.save_group({
            "name": "leads",
            "email_addresses": ["alice@x.com", "carol@z.com"],
        })
        result = store.resolve_recipients("@ops, @leads")
        # alice de-duplicated; first-seen order preserved
        assert result == ["alice@x.com", "bob@y.com", "carol@z.com"]

    def test_nested_groups(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        store.save_group({
            "name": "leaders",
            "email_addresses": ["lead@x.com"],
        })
        store.save_group({
            "name": "ops",
            "email_addresses": ["@leaders", "alice@x.com"],
        })
        result = store.resolve_recipients("@ops")
        assert result == ["lead@x.com", "alice@x.com"]

    def test_cycle_detection_breaks_loop(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        # Create a cycle: a -> b -> a
        store.save_group({"name": "a", "email_addresses": ["@b", "a@x.com"]})
        store.save_group({"name": "b", "email_addresses": ["@a", "b@x.com"]})
        # Should not infinite-loop; should not raise; should return both
        # literal addresses (the references are short-circuited)
        result = store.resolve_recipients("@a")
        assert "a@x.com" in result
        assert "b@x.com" in result

    def test_unknown_group_silently_skipped(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        # @missing doesn't exist; literal still resolves
        result = store.resolve_recipients("@missing, alice@x.com")
        assert result == ["alice@x.com"]

    def test_invalid_literal_silently_skipped(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        result = store.resolve_recipients("not-an-email, alice@x.com")
        assert result == ["alice@x.com"]

    def test_empty_input(self, isolated_groups_dir):
        store = _make_store(isolated_groups_dir)
        assert store.resolve_recipients("") == []
        assert store.resolve_recipients(None) == []
        assert store.resolve_recipients([]) == []

    def test_resolve_recipients_for_send_uses_shared_store(self, isolated_groups_dir):
        from email_group_store import resolve_recipients_for_send, get_shared_store
        # Trigger lazy init so it picks up the patched directory
        store = get_shared_store()
        store.save_group({
            "name": "shared_test",
            "email_addresses": ["shared@x.com"],
        })
        result = resolve_recipients_for_send("@shared_test, lit@y.com")
        assert "shared@x.com" in result
        assert "lit@y.com" in result


# ──────────────────────────────────────────────────────────────────
# SavedSearch / AlertGroup validators accept @group_name
# ──────────────────────────────────────────────────────────────────


class TestSavedSearchValidationAcceptsGroupRef:

    def test_literal_email(self):
        assert SavedSearchValidation.validate_email("alice@x.com") == "alice@x.com"

    def test_group_ref_only(self):
        assert SavedSearchValidation.validate_email("@team_a") == "@team_a"

    def test_mixed_literal_and_group(self):
        v = "alice@x.com, @team_a, bob@y.com"
        assert SavedSearchValidation.validate_email(v) == v

    def test_invalid_group_ref_rejected(self):
        with pytest.raises(ValueError):
            SavedSearchValidation.validate_email("@team-a")

    def test_invalid_literal_rejected(self):
        with pytest.raises(ValueError):
            SavedSearchValidation.validate_email("not-an-email")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            SavedSearchValidation.validate_email("")


class TestAlertGroupValidationAcceptsGroupRef:

    def test_literal_email(self):
        assert AlertGroupValidation.validate_email("alice@x.com") == "alice@x.com"

    def test_group_ref_only(self):
        assert AlertGroupValidation.validate_email("@team_a") == "@team_a"

    def test_mixed(self):
        v = "alice@x.com; @team_a"
        assert AlertGroupValidation.validate_email(v) == v

    def test_invalid_group_ref_rejected(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_email("@team a")


# ──────────────────────────────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """Flask test client with email_groups dir patched to tmp."""
    test_dir = tmp_path / "email_groups"
    test_dir.mkdir()
    import email_group_store as egs
    monkeypatch.setattr(egs, "EMAIL_GROUPS_DIR", test_dir)
    egs._reset_shared_store_for_tests()

    # Patch the server-module-level instance too
    from desktop_app.server import app
    import desktop_app.server as server_mod
    new_store = egs.EmailGroupStore()
    new_store._dir = test_dir
    monkeypatch.setattr(server_mod, "_email_group_store", new_store)

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
    egs._reset_shared_store_for_tests()


class TestEmailGroupsAPI:

    def test_list_empty(self, api_client):
        r = api_client.get("/api/email-groups/list")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "success"
        assert body["groups"] == []

    def test_create_and_get(self, api_client):
        r = api_client.post(
            "/api/email-groups/create",
            json={
                "name": "ops_team",
                "description": "Ops list",
                "email_addresses": ["alice@x.com", "bob@y.com"],
            },
        )
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["status"] == "success"
        assert body["group"]["name"] == "ops_team"

        r = api_client.get("/api/email-groups/ops_team")
        assert r.status_code == 200
        body = r.get_json()
        assert body["group"]["email_addresses"] == ["alice@x.com", "bob@y.com"]
        assert body["resolved_recipients"] == ["alice@x.com", "bob@y.com"]

    def test_create_duplicate_without_overwrite_returns_exists(self, api_client):
        api_client.post(
            "/api/email-groups/create",
            json={"name": "team_a", "email_addresses": ["alice@x.com"]},
        )
        r = api_client.post(
            "/api/email-groups/create",
            json={"name": "team_a", "email_addresses": ["bob@y.com"]},
        )
        assert r.get_json()["status"] == "exists"

    def test_create_invalid_returns_400(self, api_client):
        r = api_client.post(
            "/api/email-groups/create",
            json={"name": "team_a", "email_addresses": ["not-an-email"]},
        )
        assert r.status_code == 400

    def test_update(self, api_client):
        api_client.post(
            "/api/email-groups/create",
            json={
                "name": "team_a",
                "description": "v1",
                "email_addresses": ["alice@x.com"],
            },
        )
        r = api_client.put(
            "/api/email-groups/team_a",
            json={
                "description": "v2",
                "email_addresses": ["alice@x.com", "bob@y.com"],
            },
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["group"]["description"] == "v2"
        assert "bob@y.com" in body["group"]["email_addresses"]

    def test_delete(self, api_client):
        api_client.post(
            "/api/email-groups/create",
            json={"name": "team_a", "email_addresses": ["alice@x.com"]},
        )
        r = api_client.delete("/api/email-groups/team_a")
        assert r.status_code == 200
        r = api_client.get("/api/email-groups/team_a")
        assert r.status_code == 404

    def test_preview_resolves_groups(self, api_client):
        api_client.post(
            "/api/email-groups/create",
            json={
                "name": "ops",
                "email_addresses": ["a@x.com", "b@x.com"],
            },
        )
        r = api_client.post(
            "/api/email-groups/preview",
            json={"recipients": "@ops, c@x.com"},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["resolved_recipients"] == ["a@x.com", "b@x.com", "c@x.com"]

    def test_preview_empty_input(self, api_client):
        r = api_client.post("/api/email-groups/preview", json={"recipients": ""})
        assert r.status_code == 200
        assert r.get_json()["resolved_recipients"] == []


# ──────────────────────────────────────────────────────────────────
# End-to-end: query_engine.Alert.resolve_and_normalize_recipients
# ──────────────────────────────────────────────────────────────────


class TestAlertResolveAndNormalize:
    """Verify the public resolver exported from query_engine.Alert
    correctly wires to email_group_store under the hood."""

    def test_literal_string_passthrough(self, isolated_groups_dir):
        from query_engine.Alert import resolve_and_normalize_recipients
        result = resolve_and_normalize_recipients("alice@x.com")
        assert result == ["alice@x.com"]

    def test_comma_split(self, isolated_groups_dir):
        from query_engine.Alert import resolve_and_normalize_recipients
        result = resolve_and_normalize_recipients("alice@x.com, bob@y.com")
        assert result == ["alice@x.com", "bob@y.com"]

    def test_group_ref_expansion(self, isolated_groups_dir):
        from email_group_store import get_shared_store
        from query_engine.Alert import resolve_and_normalize_recipients
        store = get_shared_store()
        store.save_group({
            "name": "team_a",
            "email_addresses": ["lead@x.com", "ops@x.com"],
        })
        result = resolve_and_normalize_recipients("@team_a, ceo@x.com")
        assert "lead@x.com" in result
        assert "ops@x.com" in result
        assert "ceo@x.com" in result

    def test_falls_back_when_resolver_fails(self, monkeypatch):
        """If email_group_store import raises, Alert should fall back to
        legacy _normalize_recipients (best-effort literal handling)."""
        import query_engine.Alert as alert_mod
        # Force the import to raise
        original = alert_mod.resolve_and_normalize_recipients

        def boom_loader(*args, **kwargs):
            raise RuntimeError("simulated import failure")

        # The function imports email_group_store inside; we cause that
        # import to fail by removing the resolve_recipients_for_send
        # symbol temporarily.
        import email_group_store as egs
        original_fn = egs.resolve_recipients_for_send
        monkeypatch.setattr(
            egs, "resolve_recipients_for_send",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        try:
            # Fallback path: legacy normaliser handles list-form input.
            result = alert_mod.resolve_and_normalize_recipients(["alice@x.com"])
            assert result == ["alice@x.com"]
        finally:
            monkeypatch.setattr(egs, "resolve_recipients_for_send", original_fn)
