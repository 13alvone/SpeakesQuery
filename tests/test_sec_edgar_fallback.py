"""
Tests for the SEC EDGAR ``contact`` credential fallback.

The previous behaviour raised ``RuntimeError`` when ``SEC_EDGAR_CONTACT``
was absent, blocking default out-of-box scheduling. The SEC fair-access
policy only requires any valid contact email in the User-Agent - no
authentication. The 5 SEC library scripts now synthesize a default UA
when the credential is empty, while still honoring user-supplied values.

What we cover:
  * All 5 SEC scripts execute with empty ``CREDENTIALS`` using the default UA
  * The default UA is actually sent as the HTTP User-Agent header
  * A user-supplied ``SEC_EDGAR_CONTACT`` overrides the default
  * The library metadata correctly marks SEC_EDGAR_CONTACT as ``contact`` kind
    and keeps ``requires_credentials == []`` so the UI won't nag
"""

from __future__ import annotations

import json
import pathlib
import unittest.mock

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).parent.parent / "script_library" / "scripts"
SEC_SCRIPTS = [
    "sec_company_directory",
    "sec_balance_sheet_screen",
    "sec_major_filings_feed",
    "sec_profitability_screen",
    "sec_revenue_leaders",
]

DEFAULT_UA = "SpeakesQuery EDGAR (noreply@speakesquery.local)"


def _make_response(json_body: dict | list, status: int = 200):
    resp = unittest.mock.MagicMock()
    resp.status_code = status
    resp.json = lambda: json_body
    resp.raise_for_status = unittest.mock.MagicMock()
    return resp


def _capture_headers_router(captured: list, fallback_body=None):
    """Return a requests.get side-effect that records the User-Agent on every call."""
    def _get(url, *args, headers=None, **kwargs):
        captured.append({
            "url": url,
            "user_agent": (headers or {}).get("User-Agent"),
        })
        body = fallback_body if fallback_body is not None else []
        if "company_tickers" in url:
            return _make_response({
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
            })
        if "submissions/CIK" in url:
            return _make_response({
                "cik": "320193",
                "name": "Apple Inc.",
                "tickers": ["AAPL"],
                "filings": {
                    "recent": {
                        "form": ["10-K", "8-K"],
                        "filingDate": ["2026-01-01", "2026-02-01"],
                        "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                        "primaryDocument": ["10k.htm", "8k.htm"],
                    }
                },
            })
        if "/xbrl/frames/" in url:
            return _make_response({
                "data": [
                    {"cik": 320193, "entityName": "Apple Inc.",
                     "val": 400_000_000_000, "filed": "2026-01-15", "form": "10-K"},
                    {"cik": 789019, "entityName": "Microsoft Corp",
                     "val": 250_000_000_000, "filed": "2026-01-20", "form": "10-K"},
                ]
            })
        return _make_response(body)
    return _get


@pytest.mark.parametrize("script_name", SEC_SCRIPTS)
class TestSECContactFallback:
    def test_requires_credentials_is_empty(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        assert data["requires_credentials"] == [], (
            f"{script_name}: contact credentials must be OPTIONAL now - "
            f"requires_credentials should be []"
        )

    def test_credential_kinds_marks_contact(self, script_name):
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        kinds = data.get("credential_kinds", {})
        assert kinds.get("SEC_EDGAR_CONTACT") == "contact", (
            f"{script_name}: SEC_EDGAR_CONTACT must still be labelled "
            f"'contact' kind so the UI renders the right pill."
        )

    def test_default_ua_used_when_creds_empty(self, script_name):
        from scheduled_input_engine.executor import CodeExecutor
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        captured: list[dict] = []

        with unittest.mock.patch(
            "requests.get",
            side_effect=_capture_headers_router(captured),
        ), unittest.mock.patch(
            "time.sleep", lambda *a, **kw: None,  # kill rate-limit pacing
        ):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(extra_globals={"CREDENTIALS": {}})

        assert result["status"] == "pass", (
            f"{script_name} should run without creds; errors: {result['errors']}"
        )
        assert captured, f"{script_name} made no HTTP calls - test is broken"
        # Every request must carry the default UA when no cred was supplied
        for call in captured:
            assert call["user_agent"] == DEFAULT_UA, (
                f"{script_name}: expected default UA {DEFAULT_UA!r}, "
                f"got {call['user_agent']!r} for {call['url']}"
            )

    def test_user_supplied_contact_overrides_default(self, script_name):
        from scheduled_input_engine.executor import CodeExecutor
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        captured: list[dict] = []
        user_ua = "Alice <alice@example.com>"

        with unittest.mock.patch(
            "requests.get",
            side_effect=_capture_headers_router(captured),
        ), unittest.mock.patch(
            "time.sleep", lambda *a, **kw: None,
        ):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            result = executor.execute_test(
                extra_globals={"CREDENTIALS": {"SEC_EDGAR_CONTACT": user_ua}},
            )

        assert result["status"] == "pass", (
            f"{script_name} errors under user-supplied UA: {result['errors']}"
        )
        for call in captured:
            assert call["user_agent"] == user_ua, (
                f"{script_name}: user-supplied UA did not override default"
            )
