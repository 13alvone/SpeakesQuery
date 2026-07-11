"""
Headroom proxy integration tests
────────────────────────────────
Covers the SpeakesQuery × Headroom feature (2026-06-23):

* the tri-state precedence resolver (:mod:`analyzers.headroom`),
* proxy-URL resolution + the HEADROOM_DISABLE kill switch,
* per-call routing + **fail-open** inside
  :func:`analyzers.claude_client.call_messages_create`,
* the per-AG ``use_headroom`` override validation + store round-trip,
* the additive ``headroom_path`` log column.

The fail-open requirement is the load-bearing one: Headroom must never be
able to take down alert analysis. A money-leak-canary-style guard asserts
a genuine Anthropic 4xx does NOT fail over (it would also fail direct, so
a second call just doubles the cost of a doomed request).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analyzers import headroom
from validation.AlertGroupValidation import AlertGroupValidation


# ─────────────────────────────────────────────────────────────────────
# Fake anthropic SDK surface
# ─────────────────────────────────────────────────────────────────────
# The wrapper classifies errors by exception class NAME (it never imports
# the SDK directly), so these stand-ins reproduce the relevant names.


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class BadRequestError(Exception):
    status_code = 400


class _Usage:
    def __init__(self, i=10, o=5):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Resp:
    def __init__(self):
        self.usage = _Usage()
        self.stop_reason = "end_turn"
        self.content = []

    def model_dump(self):
        return {"stop_reason": self.stop_reason}


class _Messages:
    def __init__(self, behavior):
        self._behavior = behavior

    def create(self, **kwargs):
        return self._behavior()


class _Client:
    def __init__(self, behavior):
        self.messages = _Messages(behavior)


class _FactorySpy:
    """A 2-arg client factory that records the base_url it's built with.

    ``base_url`` is non-empty on the headroom client and ``None`` on the
    direct client, so we can assert how each call was routed and how many
    times each underlying client's ``messages.create`` actually fired.
    """

    def __init__(self, headroom_behavior, direct_behavior):
        self._headroom_behavior = headroom_behavior
        self._direct_behavior = direct_behavior
        self.base_urls: list = []
        self.headroom_calls = 0
        self.direct_calls = 0

    def __call__(self, key, base_url=None):
        self.base_urls.append(base_url)
        if base_url:
            return _Client(self._wrap_headroom)
        return _Client(self._wrap_direct)

    def _wrap_headroom(self):
        self.headroom_calls += 1
        return self._headroom_behavior()

    def _wrap_direct(self):
        self.direct_calls += 1
        return self._direct_behavior()


def _ok():
    return _Resp()


def _conn_error():
    raise APIConnectionError("connection refused")


def _timeout():
    raise APITimeoutError("timed out")


def _bad_request():
    raise BadRequestError("400 invalid request")


# ─────────────────────────────────────────────────────────────────────
# Resolver precedence + URL + kill switch
# ─────────────────────────────────────────────────────────────────────


class TestResolverPrecedence:
    """Acceptance §8: alert → group → global default, kill switch wins."""

    def setup_method(self):
        # Ensure the env kill switch is off for the precedence tests.
        import os
        os.environ.pop("HEADROOM_DISABLE", None)

    def test_global_default_true_no_overrides_uses_headroom(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: True)
        assert headroom.resolve_use_headroom() is True

    def test_global_default_false_no_overrides_direct(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: False)
        assert headroom.resolve_use_headroom() is False

    def test_group_no_beats_global_true(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: True)
        assert headroom.resolve_use_headroom(group_override=False) is False

    def test_group_yes_beats_global_false(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: False)
        assert headroom.resolve_use_headroom(group_override=True) is True

    def test_alert_yes_beats_group_no_and_global_false(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: False)
        assert headroom.resolve_use_headroom(
            alert_override=True, group_override=False,
        ) is True

    def test_alert_no_beats_group_yes_and_global_true(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: True)
        assert headroom.resolve_use_headroom(
            alert_override=False, group_override=True,
        ) is False

    def test_inherit_falls_through_each_level(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: True)
        # alert inherit, group inherit → global
        assert headroom.resolve_use_headroom(
            alert_override=None, group_override=None,
        ) is True
        # alert inherit, group no → group
        assert headroom.resolve_use_headroom(
            alert_override=None, group_override=False,
        ) is False

    def test_string_tristate_tokens_accepted(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: True)
        assert headroom.resolve_use_headroom(group_override="no") is False
        assert headroom.resolve_use_headroom(group_override="yes") is True
        assert headroom.resolve_use_headroom(group_override="inherit") is True


class TestKillSwitch:
    def test_env_disable_forces_direct_over_everything(self, monkeypatch):
        monkeypatch.setattr(headroom, "global_default", lambda: True)
        monkeypatch.setenv("HEADROOM_DISABLE", "1")
        assert headroom.is_globally_disabled() is True
        # Even an explicit alert + group "yes" is overridden.
        assert headroom.resolve_use_headroom(
            alert_override=True, group_override=True,
        ) is False

    def test_env_disable_off_by_default(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_DISABLE", raising=False)
        assert headroom.is_globally_disabled() is False


class TestProxyUrlResolution:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_PROXY_URL", raising=False)
        monkeypatch.setattr(
            headroom, "_get_setting",
            lambda key, default: default,
        )
        assert headroom.resolve_proxy_url() == headroom.DEFAULT_HEADROOM_URL

    def test_setting_overrides_default(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_PROXY_URL", raising=False)
        monkeypatch.setattr(
            headroom, "_get_setting",
            lambda key, default: "http://192.0.2.10:1234",
        )
        assert headroom.resolve_proxy_url() == "http://192.0.2.10:1234"

    def test_env_wins_over_setting(self, monkeypatch):
        monkeypatch.setenv("HEADROOM_PROXY_URL", "http://192.0.2.10:9999")
        monkeypatch.setattr(
            headroom, "_get_setting",
            lambda key, default: "http://192.0.2.10:1234",
        )
        assert headroom.resolve_proxy_url() == "http://192.0.2.10:9999"


class TestTristateValidation:
    def test_inherit_forms(self):
        for v in (None, "", "inherit", "default"):
            assert headroom.validate_tristate(v) is None

    def test_true_forms(self):
        for v in (True, "yes", "true", "on", "1"):
            assert headroom.validate_tristate(v) is True

    def test_false_forms(self):
        for v in (False, "no", "false", "off", "0"):
            assert headroom.validate_tristate(v) is False

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            headroom.validate_tristate("maybe")


# ─────────────────────────────────────────────────────────────────────
# call_messages_create routing + fail-open
# ─────────────────────────────────────────────────────────────────────


class TestCallRouting:
    def _call(self, *, spy, use_headroom, monkeypatch):
        from analyzers import claude_client
        # call_messages_create does ``from analyzers import headroom`` and
        # calls ``headroom.resolve_proxy_url()`` - patch the shared module
        # object so the headroom client is built with a deterministic URL.
        monkeypatch.setattr(
            headroom, "resolve_proxy_url", lambda: "http://192.0.2.10:8787",
        )
        return claude_client.call_messages_create(
            source="alert_group",
            api_key_override="sk-ant-test-key",
            client_factory=spy,
            use_headroom=use_headroom,
            model="claude-opus-4-8",
            max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )

    def test_headroom_path_builds_client_with_base_url(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_DISABLE", raising=False)
        spy = _FactorySpy(_ok, _ok)
        result = self._call(spy=spy, use_headroom=True, monkeypatch=monkeypatch)
        assert result.path == "headroom"
        # Direct client built first (fail-open target), then headroom.
        assert None in spy.base_urls
        assert any(b for b in spy.base_urls if b)  # a non-empty base_url
        assert spy.headroom_calls == 1
        assert spy.direct_calls == 0

    def test_direct_when_use_headroom_false(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_DISABLE", raising=False)
        spy = _FactorySpy(_ok, _ok)
        result = self._call(spy=spy, use_headroom=False, monkeypatch=monkeypatch)
        assert result.path == "direct"
        assert spy.base_urls == [None]
        assert spy.direct_calls == 1
        assert spy.headroom_calls == 0

    def test_fail_open_on_connection_error(self, monkeypatch, caplog):
        """Acceptance §8: proxy unreachable → completes via direct fallback."""
        import logging
        monkeypatch.delenv("HEADROOM_DISABLE", raising=False)
        spy = _FactorySpy(_conn_error, _ok)
        with caplog.at_level(logging.WARNING):
            result = self._call(
                spy=spy, use_headroom=True, monkeypatch=monkeypatch,
            )
        assert result.path == "direct-fallback"
        assert spy.headroom_calls == 1
        assert spy.direct_calls == 1
        assert any("failing open" in r.message.lower()
                   or "unreachable" in r.message.lower()
                   for r in caplog.records)

    def test_fail_open_on_timeout(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_DISABLE", raising=False)
        spy = _FactorySpy(_timeout, _ok)
        result = self._call(spy=spy, use_headroom=True, monkeypatch=monkeypatch)
        assert result.path == "direct-fallback"
        assert spy.direct_calls == 1

    def test_no_fail_open_on_4xx(self, monkeypatch):
        """A real Anthropic 4xx must NOT fail over (would also fail direct)."""
        from analyzers.claude_client import ClaudeCallError
        monkeypatch.delenv("HEADROOM_DISABLE", raising=False)
        spy = _FactorySpy(_bad_request, _ok)
        with pytest.raises(ClaudeCallError):
            self._call(spy=spy, use_headroom=True, monkeypatch=monkeypatch)
        assert spy.headroom_calls == 1
        # Direct client was built (it's the fallback target) but NEVER called.
        assert spy.direct_calls == 0

    def test_kill_switch_forces_direct_even_when_caller_asks_headroom(
        self, monkeypatch,
    ):
        monkeypatch.setenv("HEADROOM_DISABLE", "1")
        spy = _FactorySpy(_ok, _ok)
        result = self._call(spy=spy, use_headroom=True, monkeypatch=monkeypatch)
        assert result.path == "direct"
        assert spy.headroom_calls == 0
        assert spy.direct_calls == 1


# ─────────────────────────────────────────────────────────────────────
# Log schema + per-AG override wiring
# ─────────────────────────────────────────────────────────────────────


class TestLogSchema:
    def test_headroom_path_column_present(self):
        from functionality.log_writer import SCHEMAS
        assert "headroom_path" in SCHEMAS["claude_api"]

    def test_log_call_accepts_headroom_path_kwarg(self):
        import inspect
        from functionality.log_writer import log_claude_api_call
        params = inspect.signature(log_claude_api_call).parameters
        assert "headroom_path" in params


class TestAlertGroupValidation:
    def test_validate_use_headroom_tristate(self):
        assert AlertGroupValidation.validate_use_headroom(None) is None
        assert AlertGroupValidation.validate_use_headroom("") is None
        assert AlertGroupValidation.validate_use_headroom(True) is True
        assert AlertGroupValidation.validate_use_headroom("yes") is True
        assert AlertGroupValidation.validate_use_headroom(False) is False
        assert AlertGroupValidation.validate_use_headroom("no") is False

    def test_validate_use_headroom_rejects_garbage(self):
        with pytest.raises(ValueError):
            AlertGroupValidation.validate_use_headroom("sometimes")


@pytest.fixture()
def ag_store(tmp_path):
    from alert_group_store import AlertGroupStore
    empty_defaults = tmp_path / "_empty_default_alert_groups"
    empty_defaults.mkdir()
    store = AlertGroupStore()
    store._dir = tmp_path / "alert_groups"
    store._defaults_dir = empty_defaults
    store._db = str(tmp_path / "last_chance.sqlite")
    store._runs_db = str(tmp_path / "alert_group_runs.sqlite")
    store.initialize()
    return store


class TestAlertGroupStoreRoundTrip:
    def _base(self, **extra):
        data = {
            "name": "hr_test_ag",
            "search_names": ["feeder_one"],
            "prompt_text": "Analyze the data.",
            "schedule": "0 12 * * mon-fri",
            "email_address": "ops@example.com",
        }
        data.update(extra)
        return data

    def test_save_and_read_yes(self, ag_store):
        ag_store.save_group(self._base(use_headroom=True), overwrite=True)
        g = ag_store.get_group("hr_test_ag")
        assert g["use_headroom"] is True

    def test_save_and_read_no(self, ag_store):
        ag_store.save_group(self._base(use_headroom=False), overwrite=True)
        g = ag_store.get_group("hr_test_ag")
        assert g["use_headroom"] is False

    def test_default_is_inherit_none(self, ag_store):
        ag_store.save_group(self._base(), overwrite=True)
        g = ag_store.get_group("hr_test_ag")
        assert g.get("use_headroom") is None

    def test_update_flips_override(self, ag_store):
        ag_store.save_group(self._base(use_headroom=True), overwrite=True)
        ag_store.update_group("hr_test_ag", {"use_headroom": False})
        assert ag_store.get_group("hr_test_ag")["use_headroom"] is False
        # Flip back to inherit.
        ag_store.update_group("hr_test_ag", {"use_headroom": None})
        assert ag_store.get_group("hr_test_ag")["use_headroom"] is None
