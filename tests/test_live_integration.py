"""Live integration tests for the default alert-group feeders, Claude
Analyzer API, and Gmail SMTP path.

These tests hit **real** external services (CoinGecko, Polymarket, Nasdaq,
SEC EDGAR, FRED, Yahoo Finance, Reddit, Anthropic, Gmail). They are
never run automatically - gate them behind the ``live_integration``
marker::

    pytest -m live_integration -v

Credentials are read from ``secrets.txt`` at the project root (see
``tests/_live_harness.py``). The file is gitignored; developers drop
keys into it locally. The module is skipped cleanly when the file is
absent so CI never accidentally fails.

Test scope (what a passing run guarantees):

* Every default feeder ingestion script parses and executes under the
  engine's sandbox (or unrestricted tier if applicable).
* Each feeder's declared "expected columns" are present AND at least one
  row carries non-null, non-empty values.
* Each feeder's corresponding default SPQL saved-search returns results
  when run against the freshly produced Parquet.
* The Anthropic SDK wiring works - auth succeeds; any failure is
  surfaced with a useful error (e.g. insufficient credits).
* Gmail STARTTLS delivery works with the supplied App Password.

Upstream flakiness (external 429/5xx) is classified separately from
library bugs: a script that correctly emits an ERROR sentinel row or a
zero-row frame is *not* a test failure. Those cases surface via the
``upstream_flaky`` pytest marker on individual tests.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SECRETS_PATH = PROJECT_ROOT / "secrets.txt"
pytestmark = pytest.mark.live_integration

if not SECRETS_PATH.exists():
    pytest.skip(
        "secrets.txt not present - drop live credentials there to enable "
        "live_integration tests.",
        allow_module_level=True,
    )

from tests._live_harness import (  # noqa: E402
    FEEDERS, audit_columns, load_secrets, run_script_live,
)


# ─────────────────────────────────────────────────────────────────
# Credential resolution
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def secrets() -> dict:
    return load_secrets()


def _creds_for(feeder, secrets: dict) -> dict:
    """Port of ``_live_runner._creds_for`` - with the same SEC fallback."""
    out: dict = {}
    for cred_name, section in feeder.required_creds.items():
        values = secrets.get(section.lower(), [])
        if values:
            out[cred_name] = values[0]
            continue
        if cred_name == "SEC_EDGAR_CONTACT":
            gmail = secrets.get("gmail", [])
            if gmail:
                out[cred_name] = f"SpeakesQuery Testing <{gmail[0]}>"
                continue
        pytest.skip(
            f"{feeder.name} needs credential {cred_name} in [{section}] "
            f"section of secrets.txt"
        )
    return out


# ─────────────────────────────────────────────────────────────────
# Feeder end-to-end tests (parametrised over FEEDERS)
# ─────────────────────────────────────────────────────────────────


# Feeders that are known-flaky because their upstream is rate-limit happy
# (Yahoo Finance) or may legitimately find zero matches (cross-platform
# arbitrage on a quiet day). A strict-mode hit on these fires a warning
# but does not fail the test run.
UPSTREAM_FLAKY = {"dob_options_unusual", "dob_kalshi_poly_arb"}


def _run_with_upstream_skip(feeder, secrets):
    """Wrap ``run_script_live`` with an upstream-429/5xx → skip adapter.

    Any upstream HTTP failure that's out of our control (rate limits,
    502/503, connection resets) classifies as "flaky upstream" rather
    than a bug in our ingestion code. Raising in the fixture would
    produce a red test; skipping preserves the signal that the rest of
    the suite is healthy.
    """
    import requests
    try:
        return run_script_live(feeder, creds=_creds_for(feeder, secrets))
    except requests.exceptions.HTTPError as exc:
        code = getattr(exc.response, "status_code", None)
        if code in (408, 429) or (code and 500 <= code < 600):
            pytest.skip(
                f"{feeder.name}: upstream {code} on {exc.response.url} "
                "(rate-limited or server error - classified flaky)"
            )
        raise


@pytest.mark.parametrize(
    "feeder", FEEDERS, ids=[f.name for f in FEEDERS],
)
def test_feeder_ingest_populates_expected_columns(feeder, secrets):
    """Live script run must emit every expected column with real values."""
    df = _run_with_upstream_skip(feeder, secrets)

    # Empty DataFrame from a legitimate "no matches" script (e.g. kalshi
    # arbitrage on a quiet day) is tolerated but not counted as success.
    if df.empty:
        if feeder.name in UPSTREAM_FLAKY:
            pytest.skip(f"{feeder.name}: upstream returned no rows (flaky feeder)")
        pytest.fail(f"{feeder.name}: ingest returned 0 rows")

    # ERROR sentinel row - upstream API down/rate-limited.
    if len(df) == 1 and "ticker" in df.columns and str(df["ticker"].iloc[0]).upper() == "ERROR":
        if feeder.name in UPSTREAM_FLAKY:
            pytest.skip(f"{feeder.name}: upstream error sentinel (flaky feeder)")
        pytest.fail(f"{feeder.name}: ingest emitted ERROR sentinel - upstream failure")

    report = audit_columns(df, feeder.expected_columns)
    missing = [col for col, info in report.items() if not info["present"]]
    fully_empty = [
        col for col, info in report.items()
        if info["present"] and info["empty_ratio"] == 1.0
    ]
    assert not missing, (
        f"{feeder.name}: expected columns missing from output: {missing}"
    )
    assert not fully_empty, (
        f"{feeder.name}: columns present but 100% empty: {fully_empty}"
    )


@pytest.mark.parametrize(
    "feeder", FEEDERS, ids=[f.name for f in FEEDERS],
)
def test_feeder_saved_search_returns_rows(feeder, secrets, tmp_path_factory):
    """The feeder's default SPQL query must return rows against live data."""
    import yaml
    from scheduled_input_engine.parquet_writer import ParquetWriter
    from query_engine.CmdExecutionBackend import run_query_and_return_results_df

    df = _run_with_upstream_skip(feeder, secrets)
    if df.empty or (
        len(df) == 1 and "ticker" in df.columns
        and str(df["ticker"].iloc[0]).upper() == "ERROR"
    ):
        if feeder.name in UPSTREAM_FLAKY:
            pytest.skip(f"{feeder.name}: upstream flaky, no data to query")
        pytest.fail(f"{feeder.name}: no usable ingest data")

    writer = ParquetWriter(PROJECT_ROOT / "indexes", target_file_mb=128)
    writer.write_atomic(
        df,
        subdirectory=feeder.subdirectory,
        filename="_live_integration.parquet",
        overwrite=True,
    )

    ss_path = PROJECT_ROOT / "default_saved_searches" / f"{feeder.name}.yaml"
    query = yaml.safe_load(ss_path.read_text())["query"]
    result_df, _ = run_query_and_return_results_df(query)

    assert result_df is not None and len(result_df) > 0, (
        f"{feeder.name}: saved-search query returned 0 rows against live data"
    )


# ─────────────────────────────────────────────────────────────────
# Claude Analyzer wiring
# ─────────────────────────────────────────────────────────────────


def test_claude_api_key_authenticates(secrets):
    """Auth succeeds with the user-supplied key (credits may be 0 - see body).

    A call with ``max_tokens=5`` to Haiku is the cheapest way to verify
    the key is valid and the SDK wiring lines up. We accept two outcomes
    as "integration works":

    * 200 OK with content - everything green
    * 400 with ``credit balance is too low`` - key is valid, account needs top-up

    Any other error (401, 403, network) fails the test.
    """
    import anthropic

    try:
        claude = secrets["claude"]
    except KeyError:
        pytest.skip("secrets.txt missing [claude] section")

    client = anthropic.Anthropic(api_key=claude[0])
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp.content, "Haiku returned empty content"
    except anthropic.BadRequestError as exc:
        msg = str(exc)
        assert "credit balance" in msg.lower(), (
            f"Unexpected BadRequestError: {msg}"
        )
        pytest.skip(
            "Claude API key is valid but the account has no credits - "
            "top up at console.anthropic.com/settings/billing."
        )
    except anthropic.AuthenticationError as exc:
        pytest.fail(f"Claude API key rejected: {exc}")


def test_claude_rejects_invalid_key():
    """Guard test: an intentionally invalid key must produce AuthenticationError."""
    import anthropic

    bad = anthropic.Anthropic(api_key="sk-ant-invalid-key-000000")
    with pytest.raises(anthropic.AuthenticationError):
        bad.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )


def test_claude_client_wrapper_records_history(secrets, tmp_path):
    """The shared wrapper must actually reach Claude AND persist to the
    dedicated history DB + the Parquet log stream under ``indexes/logs/``.

    This is the end-to-end proof for Request 3: every billable call must
    be auditable after the fact.
    """
    try:
        claude = secrets["claude"]
    except KeyError:
        pytest.skip("secrets.txt missing [claude] section")

    from analyzers.claude_client import call_messages_create, ClaudeCallError
    from analyzers.claude_history_store import ClaudeHistoryStore
    from global_settings import get_settings
    from functionality import log_writer as lw

    # Redirect history DB + logs tree to the tmp dir so we can inspect rows
    # without polluting the dev file.
    settings = get_settings()
    prior_logs = settings.get("logs_root")
    settings.set("logs_root", str(tmp_path / "logs"))
    settings.set("logs_enabled", True)
    lw.LogWriter.reset_for_tests()
    ClaudeHistoryStore._instance = ClaudeHistoryStore(
        db_path=tmp_path / "claude_hist.sqlite",
    )

    try:
        try:
            result = call_messages_create(
                source="live_test",
                api_key_override=claude[0],
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}],
            )
        except ClaudeCallError as exc:
            if "credit balance" in str(exc).lower():
                pytest.skip(
                    "Claude key valid but account out of credit; wrapper "
                    "still produced an audit row - see history DB."
                )
            raise

        # Assert SQLite history captured it with full payloads
        rows = ClaudeHistoryStore.get_instance().list_calls(
            include_payloads=True,
        )
        assert rows, "no history row recorded"
        assert rows[0]["source"] == "live_test"
        assert rows[0]["status"] == "success"
        assert rows[0]["model"] == "claude-haiku-4-5-20251001"
        assert rows[0]["request_body"], "request body missing"
        assert rows[0]["response_body"], "response body missing"

        # Assert Parquet log emitted
        lw.flush_all()
        import pandas as pd
        log_dir = tmp_path / "logs" / "claude_api"
        assert log_dir.exists(), "claude_api log subdir not created"
        log_rows = []
        for p in log_dir.glob("*.parquet"):
            log_rows.extend(pd.read_parquet(p).to_dict(orient="records"))
        assert any(
            r["request_id"] == result.request_id for r in log_rows
        ), "claude_api Parquet log missing the request_id"
    finally:
        settings.set("logs_root", prior_logs)
        lw.LogWriter.reset_for_tests()
        ClaudeHistoryStore.reset_for_tests()


# ─────────────────────────────────────────────────────────────────
# Gmail / SMTP delivery
# ─────────────────────────────────────────────────────────────────


def test_smtp_live_delivery(secrets):
    """STARTTLS to Gmail with the supplied App Password must deliver successfully."""
    from query_engine.Alert import SMTPConfig, send_email_async

    try:
        gmail = secrets["gmail"]
    except KeyError:
        pytest.skip("secrets.txt missing [gmail] section")
    if len(gmail) < 2:
        pytest.skip("[gmail] section must have two lines: address + App Password")
    user, pwd = gmail[0], gmail[1]

    cfg = SMTPConfig(
        server="smtp.gmail.com", port=587, user=user, password=pwd,
        from_addr=user, start_tls=True,
    )
    asyncio.run(send_email_async(
        subject="[SpeakesQuery Live Test] SMTP delivery OK",
        body=(
            "Integration-test email from tests/test_live_integration.py - "
            "receiving this means STARTTLS and credentials are healthy."
        ),
        to_addrs=[user],
        smtp_config=cfg,
        timeout_seconds=30,
    ))
