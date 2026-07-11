"""
Tests for the per-execution domain enforcement in scheduled_input_engine.cache.

Covers the fix for the production-readiness audit (2026-04-16) which found:
  - `is_allowed_api_url()` had no producer for its env-var domain source,
    so all sandbox HTTP went denied-by-default.
  - `BudgetAwareRequests` proxy never validated URLs at all, letting scripts
    bypass the allowlist with direct `requests.get(...)`.

The fix wires `reset_budget(allowed_domains=...)` into a thread-local that
both helpers consult, and makes `BudgetAwareRequests._guarded_call()` reject
URLs that are not on the allowlist before any HTTP call leaves the process.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from scheduled_input_engine import cache


@pytest.fixture(autouse=True)
def _reset_budget_state():
    """Reset thread-local budget + clear ALLOWED_API_DOMAINS env between tests."""
    original_env = os.environ.pop("ALLOWED_API_DOMAINS", None)
    cache.reset_budget(max_requests=100, max_response_mb=10, allowed_domains=None)
    yield
    cache.reset_budget(max_requests=100, max_response_mb=10, allowed_domains=None)
    if original_env is not None:
        os.environ["ALLOWED_API_DOMAINS"] = original_env


# ---------------------------------------------------------------------------
# is_allowed_api_url - resolution order + edge cases
# ---------------------------------------------------------------------------


class TestIsAllowedApiUrl:
    def test_denies_when_no_source_configured(self):
        assert cache.is_allowed_api_url("https://example.com/x") is False

    def test_thread_local_allows_match(self):
        cache.reset_budget(allowed_domains=[r"^example\.com$"])
        assert cache.is_allowed_api_url("https://example.com/x") is True

    def test_thread_local_denies_non_match(self):
        cache.reset_budget(allowed_domains=[r"^example\.com$"])
        assert cache.is_allowed_api_url("https://attacker.com/x") is False

    def test_env_var_fallback(self):
        os.environ["ALLOWED_API_DOMAINS"] = r"^api\.example\.com$"
        # No thread-local override
        cache.reset_budget(allowed_domains=None)
        assert cache.is_allowed_api_url("https://api.example.com/x") is True
        assert cache.is_allowed_api_url("https://example.com/x") is False

    def test_thread_local_overrides_env_var(self):
        os.environ["ALLOWED_API_DOMAINS"] = r"^should-be-ignored\.com$"
        cache.reset_budget(allowed_domains=[r"^real\.com$"])
        assert cache.is_allowed_api_url("https://real.com/x") is True
        assert cache.is_allowed_api_url("https://should-be-ignored.com/x") is False

    def test_rejects_non_http_schemes(self):
        cache.reset_budget(allowed_domains=[r".*"])
        assert cache.is_allowed_api_url("file:///etc/passwd") is False
        assert cache.is_allowed_api_url("ftp://example.com/data") is False
        assert cache.is_allowed_api_url("javascript:alert(1)") is False

    def test_hostname_lowercased_and_dot_stripped(self):
        cache.reset_budget(allowed_domains=[r"^example\.com$"])
        # Trailing dot + uppercase still matches
        assert cache.is_allowed_api_url("https://Example.COM./x") is True

    def test_anchored_regex_prevents_suffix_bypass(self):
        cache.reset_budget(allowed_domains=[r"^example\.com$"])
        # An attacker-controlled subdomain trick must not match
        assert cache.is_allowed_api_url("https://example.com.attacker.com/x") is False
        assert cache.is_allowed_api_url("https://attackerexample.com/x") is False

    def test_invalid_regex_logged_not_raised(self, caplog):
        cache.reset_budget(allowed_domains=[r"[invalid("])
        with caplog.at_level("ERROR"):
            assert cache.is_allowed_api_url("https://example.com/x") is False
        assert any("Invalid regex" in rec.message for rec in caplog.records)

    def test_empty_or_non_string_inputs(self):
        cache.reset_budget(allowed_domains=[r".*"])
        assert cache.is_allowed_api_url("") is False
        assert cache.is_allowed_api_url("   ") is False
        assert cache.is_allowed_api_url(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BudgetAwareRequests - URL validation in _guarded_call
# ---------------------------------------------------------------------------


class TestBudgetAwareRequestsAllowlist:
    def _proxy(self):
        proxy = cache.BudgetAwareRequests()
        proxy._real = MagicMock()
        # Fake a successful response with no body
        proxy._real.get.return_value = MagicMock(content=b"")
        proxy._real.post.return_value = MagicMock(content=b"")
        proxy._real.put.return_value = MagicMock(content=b"")
        proxy._real.patch.return_value = MagicMock(content=b"")
        proxy._real.delete.return_value = MagicMock(content=b"")
        proxy._real.head.return_value = MagicMock(content=b"")
        return proxy

    def test_get_allowed_url_passes(self):
        cache.reset_budget(allowed_domains=[r"^api\.allowed\.com$"])
        proxy = self._proxy()
        proxy.get("https://api.allowed.com/v1/data")
        proxy._real.get.assert_called_once_with("https://api.allowed.com/v1/data")

    def test_get_disallowed_url_raises_before_call(self):
        cache.reset_budget(allowed_domains=[r"^api\.allowed\.com$"])
        proxy = self._proxy()
        with pytest.raises(ValueError, match="Domain not in allowed_api_domains"):
            proxy.get("https://attacker.com/exfil")
        proxy._real.get.assert_not_called()

    @pytest.mark.parametrize("verb", ["post", "put", "patch", "delete", "head"])
    def test_all_verbs_validate_url(self, verb):
        cache.reset_budget(allowed_domains=[r"^api\.allowed\.com$"])
        proxy = self._proxy()
        method = getattr(proxy, verb)
        with pytest.raises(ValueError, match="Domain not in allowed_api_domains"):
            method("https://attacker.com/x")
        getattr(proxy._real, verb).assert_not_called()

    def test_url_kwarg_also_validated(self):
        cache.reset_budget(allowed_domains=[r"^api\.allowed\.com$"])
        proxy = self._proxy()
        with pytest.raises(ValueError, match="Domain not in allowed_api_domains"):
            proxy.get(url="https://attacker.com/x")
        proxy._real.get.assert_not_called()

    def test_missing_url_raises(self):
        cache.reset_budget(allowed_domains=[r".*"])
        proxy = self._proxy()
        with pytest.raises(ValueError, match="requires a URL"):
            proxy.get()  # No args, no kwargs

    def test_empty_allowlist_blocks_everything(self):
        # The buggy default before this fix
        cache.reset_budget(allowed_domains=None)
        proxy = self._proxy()
        with pytest.raises(ValueError, match="Domain not in allowed_api_domains"):
            proxy.get("https://example.com/x")
        proxy._real.get.assert_not_called()

    def test_request_count_increments_only_on_allowed(self):
        cache.reset_budget(
            max_requests=5, allowed_domains=[r"^api\.allowed\.com$"]
        )
        proxy = self._proxy()
        proxy.get("https://api.allowed.com/x")
        assert cache._budget.request_count == 1
        # Disallowed call must not consume budget
        with pytest.raises(ValueError):
            proxy.get("https://attacker.com/x")
        assert cache._budget.request_count == 1


# ---------------------------------------------------------------------------
# get_cached_or_fetch - sanity check the existing path still validates
# ---------------------------------------------------------------------------


class TestGetCachedOrFetchAllowlist:
    def test_disallowed_url_raises(self):
        cache.reset_budget(allowed_domains=[r"^api\.allowed\.com$"])
        with pytest.raises(ValueError, match="Domain not allowed"):
            cache.get_cached_or_fetch("https://attacker.com/x", ttl=60)

    def test_allowed_url_uses_cache_or_fetches(self, tmp_path, monkeypatch):
        cache_db = tmp_path / "cache.db"
        monkeypatch.setattr(cache, "CACHE_DB", cache_db)
        cache.reset_budget(allowed_domains=[r"^api\.allowed\.com$"])

        fake_resp = MagicMock(content=b"hello")
        fake_resp.raise_for_status = MagicMock()
        with patch.object(cache.requests, "get", return_value=fake_resp) as mock_get:
            data = cache.get_cached_or_fetch("https://api.allowed.com/x", ttl=60)
            assert data == b"hello"
            mock_get.assert_called_once()
