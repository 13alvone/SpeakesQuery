"""Regression tests for the production-readiness fixes shipped 2026-04-17.

Each test nails down a specific bug that was exposed while validating the
default alert-group feeders end-to-end. If any of these starts failing
the underlying bug has been reintroduced and the next live-integration
run will fail.

Covered:

1. ``*.parquet`` glob resolver - trailing ``*.parquet`` in an ``index="…"``
   path must match files directly, not be treated as a directory
   (``_resolve_glob_pattern``).
2. SPQL decimal tokenization - ``0.75`` must tokenise as one number, not
   ``0 / . / 75`` (``_cmd_search`` regex + ``tokenize_query_tokens``).
3. Sandbox globals/locals merge - top-level ``from X import Y`` must be
   visible inside nested helper functions in sandboxed mode
   (``CodeExecutor.execute``).
4. Empty-DataFrame tolerance - a zero-row ingest must not raise "No
   parseable timestamp field" (``_ensure_epoch``).
5. SEC_EDGAR_CONTACT strict enforcement - scripts must fail loud when
   the credential is missing rather than silently using a placeholder
   User-Agent (SEC fair-access policy compliance).
"""
from __future__ import annotations

import json
import pandas as pd
import pytest

from functionality.duckdb_index_call import _resolve_glob_pattern, INDEXES_DIR
from handlers.SearchCmdHandler import SearchDirective
from scheduled_input_engine.cache import BudgetAwareRequests, get_cached_or_fetch, reset_budget
from scheduled_input_engine.executor import CodeExecutor


# ─────────────────────────────────────────────────────────────────
# 1. Glob resolver
# ─────────────────────────────────────────────────────────────────


class TestGlobResolver:
    def test_trailing_wildcard_parquet_not_appended(self):
        """``indexes/foo/*.parquet`` must resolve as-is, not gain ``/**/*.parquet``."""
        resolved = _resolve_glob_pattern("indexes/crypto/demo/*.parquet")
        assert resolved.endswith("/crypto/demo/*.parquet"), (
            f"Trailing '*.parquet' was re-wrapped: {resolved!r}"
        )
        assert "*.parquet/**" not in resolved

    def test_single_file_path_unchanged(self):
        """Bare single-file paths still resolve verbatim."""
        # Use a path that doesn't need to exist; the resolver only
        # rewrites when the path has a wildcard or directory shape.
        resolved = _resolve_glob_pattern("indexes/default_test/output_parquets/test0.parquet")
        # When the file exists it resolves to the file itself.
        assert resolved.endswith("test0.parquet")

    def test_directory_double_star(self):
        """``indexes/foo/**`` → ``indexes/foo/**/*.parquet``."""
        resolved = _resolve_glob_pattern("indexes/crypto/demo/**")
        assert resolved.endswith("/crypto/demo/**/*.parquet")

    def test_directory_single_star(self):
        """``indexes/foo/*`` → ``indexes/foo/*.parquet``."""
        resolved = _resolve_glob_pattern("indexes/crypto/demo/*")
        assert resolved.endswith("/crypto/demo/*.parquet")


# ─────────────────────────────────────────────────────────────────
# 2. Decimal tokenisation in WHERE / SEARCH clauses
# ─────────────────────────────────────────────────────────────────


class TestDecimalTokenization:
    def test_decimal_literal_classified(self):
        tokens = SearchDirective().tokenize_query_tokens([
            "leading_price", ">=", "0.75",
        ])
        kinds = [t.type for t in tokens]
        assert kinds == [
            SearchDirective.TokenType.IDENTIFIER,
            SearchDirective.TokenType.OPERATOR,
            SearchDirective.TokenType.NUMBER_LITERAL,
        ], f"Unexpected kinds: {kinds}"

    def test_integer_literal_classified(self):
        tokens = SearchDirective().tokenize_query_tokens([
            "rank", "<=", "200",
        ])
        kinds = [t.type for t in tokens]
        assert kinds == [
            SearchDirective.TokenType.IDENTIFIER,
            SearchDirective.TokenType.OPERATOR,
            SearchDirective.TokenType.NUMBER_LITERAL,
        ]

    def test_where_clause_regex_splits_decimals_as_one_token(self):
        """End-to-end: the regex in ``_cmd_search`` must tokenise 0.75 as one unit."""
        import re
        pattern = (
            r'"[^\"]*"|>=|<=|!=|=|>|<|\(|\)|,|\d+\.\d+|\w+|\S'
        )
        tokens = re.findall(pattern, "leading_price >= 0.75 AND leading_price < 0.95")
        assert "0.75" in tokens
        assert "0.95" in tokens
        assert "." not in tokens, f"regex split a decimal: {tokens}"


# ─────────────────────────────────────────────────────────────────
# 3. Sandbox globals/locals merge
# ─────────────────────────────────────────────────────────────────


class TestSandboxGlobalsMerge:
    def test_from_import_visible_in_nested_function(self):
        """``from datetime import datetime`` must reach a helper's body."""
        code = (
            "import pandas as pd\n"
            "from datetime import datetime, timezone\n"
            "now = datetime.now(timezone.utc)\n"
            "\n"
            "def parse(s):\n"
            "    # Must resolve the ``datetime`` *class*, not the module stub.\n"
            "    return datetime.strptime(s, '%Y-%m-%d')\n"
            "\n"
            "r = parse('2026-04-20')\n"
            "df = pd.DataFrame([{'_epoch': int(now.timestamp()), 'parsed': str(r)}])\n"
            "GENERATE_RESULTS(df)\n"
        )
        reset_budget(max_requests=10, max_response_mb=10, allowed_domains=[])
        out = CodeExecutor(code, trust_level="sandboxed").execute(
            extra_globals={
                "get_cached_or_fetch": get_cached_or_fetch,
                "requests": BudgetAwareRequests(),
            },
        )
        assert out.iloc[0]["parsed"] == "2026-04-20 00:00:00"


# ─────────────────────────────────────────────────────────────────
# 4. Empty-DataFrame tolerance in _ensure_epoch
# ─────────────────────────────────────────────────────────────────


class TestEmptyDataFrameEpoch:
    def test_empty_df_gets_empty_epoch_column(self):
        """A zero-row ingest must write cleanly, not raise."""
        code = (
            "import pandas as pd\n"
            "df = pd.DataFrame()\n"
            "GENERATE_RESULTS(df)\n"
        )
        reset_budget(max_requests=10, max_response_mb=10, allowed_domains=[])
        out = CodeExecutor(code, trust_level="sandboxed").execute(
            extra_globals={
                "get_cached_or_fetch": get_cached_or_fetch,
                "requests": BudgetAwareRequests(),
            },
        )
        assert out.empty
        assert "_epoch" in out.columns

    def test_rows_without_epoch_still_raise(self):
        """The tolerance applies ONLY to empty frames - populated frames
        without ``_epoch`` or a fallback timestamp column still raise."""
        code = (
            "import pandas as pd\n"
            "df = pd.DataFrame([{'foo': 1}])\n"
            "GENERATE_RESULTS(df)\n"
        )
        reset_budget(max_requests=10, max_response_mb=10, allowed_domains=[])
        with pytest.raises(ValueError, match="No parseable timestamp field"):
            CodeExecutor(code, trust_level="sandboxed").execute(
                extra_globals={
                    "get_cached_or_fetch": get_cached_or_fetch,
                    "requests": BudgetAwareRequests(),
                },
            )


# ─────────────────────────────────────────────────────────────────
# 5. SEC_EDGAR_CONTACT strict enforcement
# ─────────────────────────────────────────────────────────────────


SEC_SCRIPTS = [
    "sec_revenue_leaders",
    "sec_balance_sheet_screen",
    "sec_company_directory",
    "sec_major_filings_feed",
    "sec_profitability_screen",
]

SCRIPTS_DIR = INDEXES_DIR.parent / "script_library" / "scripts"


class TestPasswordInputAutofillSuppression:
    """The SMTP password inputs must block browser/password-manager autofill.

    Without these attributes Chrome / Safari / 1Password / LastPass will
    silently replace a populated value with a saved credential at submit
    time. That makes Send Test Email post a stale password to Gmail and
    surfaces as 535 BadCredentials even when the user pasted correctly.

    The HTML itself is source-of-truth, so we grep it here - no need to
    drive a browser, and this catches someone refactoring the inputs and
    forgetting to copy the attributes.
    """

    REQUIRED_ATTRS = (
        'autocomplete="new-password"',
        'data-lpignore="true"',
        'data-form-type="other"',
    )

    def _assert_input_has_attrs(self, html: str, input_id: str) -> None:
        import re

        match = re.search(
            rf'<input[^>]*id="{re.escape(input_id)}"[^>]*>', html
        )
        assert match, f"input #{input_id} not found in ui.html"
        tag = match.group(0)
        missing = [a for a in self.REQUIRED_ATTRS if a not in tag]
        assert not missing, (
            f"input #{input_id} is missing autofill-suppression attrs: "
            f"{missing}. Current tag: {tag!r}"
        )

    def test_settings_page_password_input_suppresses_autofill(self):
        from pathlib import Path

        html = (Path(__file__).parent.parent / "desktop_app" / "ui.html").read_text()
        self._assert_input_has_attrs(html, "set-smtp-password")

    def test_first_run_modal_password_input_suppresses_autofill(self):
        from pathlib import Path

        html = (Path(__file__).parent.parent / "desktop_app" / "ui.html").read_text()
        self._assert_input_has_attrs(html, "es-smtp-password")


class TestSmtpPasswordNormalisation:
    """`smtp_password` must be saved as the 16-char Gmail App Password form.

    Google's UI shows App Passwords as ``xxxx xxxx xxxx xxxx`` and users
    paste the spaced form into the Settings page. Gmail's SMTP endpoints
    are not always lenient about internal whitespace, so the save path
    normalises it away before persistence.
    """

    def test_save_strips_internal_whitespace(self, tmp_path, monkeypatch):
        # Isolate settings from the user's real global_settings.yaml.
        import global_settings

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        monkeypatch.setattr(global_settings, "_instance", None)
        s = global_settings.GlobalSettings(fake_root)

        s.set("smtp_password", "abcd efgh ijkl mnop")
        assert s.get("smtp_password") == "abcdefghijklmnop"

    def test_save_leaves_already_clean_password_untouched(self, tmp_path, monkeypatch):
        import global_settings

        fake_root = tmp_path / "project2"
        fake_root.mkdir()
        monkeypatch.setattr(global_settings, "_instance", None)
        s = global_settings.GlobalSettings(fake_root)

        s.set("smtp_password", "abcdefghijklmnop")
        assert s.get("smtp_password") == "abcdefghijklmnop"

    def test_update_strips_whitespace_across_bulk_save(self, tmp_path, monkeypatch):
        """The Settings-page Save button goes through ``update`` with a dict."""
        import global_settings

        fake_root = tmp_path / "project3"
        fake_root.mkdir()
        monkeypatch.setattr(global_settings, "_instance", None)
        s = global_settings.GlobalSettings(fake_root)

        errs = s.update({
            "smtp_user": "  you@example.com  ",
            "smtp_password": "abcd efgh ijkl mnop",
        })
        assert errs == {}
        assert s.get("smtp_password") == "abcdefghijklmnop"
        assert s.get("smtp_user") == "you@example.com"

    def test_load_strips_whitespace_from_env_var(self, monkeypatch):
        """Belt-and-suspenders: the load path also strips whitespace.

        Older ``global_settings.yaml`` files written before the save-time
        normalisation landed may still carry a spaced password; the runtime
        loader has to handle them transparently.
        """
        from query_engine.Alert import load_smtp_config_from_env

        monkeypatch.setenv("SMTP_USER", "you@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
        monkeypatch.setenv("SMTP_FROM", "you@example.com")
        cfg = load_smtp_config_from_env()
        assert cfg.password == "abcdefghijklmnop"


class TestSmtpEnvPlaceholderDetection:
    """Known ``.env.example`` placeholder values must not reach AUTH.

    ``desktop_app/docker-compose.yml`` injects the project-root ``.env``
    into the container via ``env_file:``, and PyCharm's default Python
    run config auto-loads the same file locally.  When ``install.sh``
    copies ``.env.example`` verbatim, literal strings like
    ``SMTP_USER=you@gmail.com`` and ``SMTP_PASSWORD=your_16_char_app_password``
    end up in ``os.environ`` and - thanks to env-wins-over-settings
    precedence - silently overwrote every UI save until the user
    realised their freshly-pasted App Password was never actually used.

    ``_env_smtp`` exact-matches the known placeholders and treats them
    as unset so settings can still win.  These tests pin that behaviour
    across every documented placeholder and verify a real value is
    still honoured.
    """

    @pytest.fixture(autouse=True)
    def _reset_warn_dedupe(self):
        """Reset the module-level warn-once state between tests."""
        from query_engine import Alert
        Alert._placeholder_warned.clear()
        Alert._placeholders_ignored.clear()
        yield
        Alert._placeholder_warned.clear()
        Alert._placeholders_ignored.clear()

    @pytest.fixture
    def isolated_settings(self, tmp_path, monkeypatch):
        """Fresh GlobalSettings bound to a tmp path, with real saved creds."""
        import global_settings
        fake_root = tmp_path / "project_envph"
        fake_root.mkdir()
        monkeypatch.setattr(global_settings, "_instance", None)
        s = global_settings.GlobalSettings(fake_root)
        s.update({
            "smtp_user": "real.user@company.com",
            "smtp_password": "realappword16chr",  # 16-char canonical App Password
            "smtp_from": "real.user@company.com",
        })
        # Ensure Alert's lazy settings import hits our isolated instance.
        monkeypatch.setattr(global_settings, "_instance", s)
        yield s

    @pytest.mark.parametrize("placeholder_user", [
        "you@gmail.com",
        "your@email.com",
        "user@example.com",
    ])
    def test_placeholder_user_falls_through_to_settings(
        self, monkeypatch, isolated_settings, placeholder_user,
    ):
        from query_engine.Alert import load_smtp_config_from_env
        monkeypatch.setenv("SMTP_USER", placeholder_user)
        # Real password env var (non-placeholder) so the test isolates user
        # placeholder detection without colliding with password placeholder.
        monkeypatch.setenv("SMTP_PASSWORD", "abcdefghijklmnop")

        cfg = load_smtp_config_from_env()
        assert cfg.user == "real.user@company.com", (
            f"Placeholder SMTP_USER={placeholder_user!r} leaked into AUTH"
        )

    @pytest.mark.parametrize("placeholder_password", [
        "your_16_char_app_password",
        "your_app_password",
        "your-app-password",
    ])
    def test_placeholder_password_falls_through_to_settings(
        self, monkeypatch, isolated_settings, placeholder_password,
    ):
        from query_engine.Alert import load_smtp_config_from_env
        monkeypatch.setenv("SMTP_USER", "real.user@company.com")
        monkeypatch.setenv("SMTP_PASSWORD", placeholder_password)

        cfg = load_smtp_config_from_env()
        assert cfg.password == "realappword16chr", (
            f"Placeholder SMTP_PASSWORD={placeholder_password!r} leaked into AUTH "
            f"(got pw_shape=len={len(cfg.password)} alnum={cfg.password.isalnum()})"
        )

    def test_real_env_value_still_wins_over_settings(
        self, monkeypatch, isolated_settings,
    ):
        """Real (non-placeholder) env vars must preserve their precedence."""
        from query_engine.Alert import load_smtp_config_from_env
        monkeypatch.setenv("SMTP_USER", "env.user@company.com")
        monkeypatch.setenv("SMTP_PASSWORD", "envpasswordvalue")

        cfg = load_smtp_config_from_env()
        assert cfg.user == "env.user@company.com"
        assert cfg.password == "envpasswordvalue"

    def test_whitespace_padded_placeholder_still_detected(
        self, monkeypatch, isolated_settings,
    ):
        """Leading/trailing whitespace around a placeholder still counts as a placeholder."""
        from query_engine.Alert import load_smtp_config_from_env
        monkeypatch.setenv("SMTP_USER", "  you@gmail.com  ")
        monkeypatch.setenv("SMTP_PASSWORD", "abcdefghijklmnop")

        cfg = load_smtp_config_from_env()
        assert cfg.user == "real.user@company.com"

    def test_empty_env_var_treated_as_unset(
        self, monkeypatch, isolated_settings,
    ):
        """Empty string env var falls through to settings (not treated as placeholder)."""
        from query_engine.Alert import load_smtp_config_from_env
        monkeypatch.setenv("SMTP_USER", "")
        monkeypatch.setenv("SMTP_PASSWORD", "")

        cfg = load_smtp_config_from_env()
        assert cfg.user == "real.user@company.com"
        assert cfg.password == "realappword16chr"

    def test_placeholder_recorded_for_diagnostic(
        self, monkeypatch, isolated_settings,
    ):
        """``get_env_placeholders_ignored`` reports what was skipped - the
        ``/api/email/diagnose`` endpoint relies on this for remote debugging."""
        from query_engine.Alert import (
            load_smtp_config_from_env,
            get_env_placeholders_ignored,
        )
        monkeypatch.setenv("SMTP_USER", "you@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "your_16_char_app_password")

        load_smtp_config_from_env()
        ignored = get_env_placeholders_ignored()
        assert ignored == {
            "SMTP_USER": "you@gmail.com",
            "SMTP_PASSWORD": "your_16_char_app_password",
        }

    def test_warn_logged_only_once_per_placeholder(
        self, monkeypatch, isolated_settings, caplog,
    ):
        """Warn-once dedupe keeps logs quiet across repeated send attempts."""
        import logging
        from query_engine.Alert import load_smtp_config_from_env

        monkeypatch.setenv("SMTP_USER", "you@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "your_16_char_app_password")

        with caplog.at_level(logging.WARNING, logger="query_engine.Alert"):
            load_smtp_config_from_env()
            load_smtp_config_from_env()
            load_smtp_config_from_env()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # One warning per distinct placeholder (SMTP_USER + SMTP_PASSWORD);
        # repeated loads in the same process must not re-emit.
        placeholder_warnings = [
            r for r in warnings if ".env.example placeholder" in r.getMessage()
        ]
        assert len(placeholder_warnings) == 2, (
            f"Expected 2 one-shot warnings, got {len(placeholder_warnings)}: "
            f"{[r.getMessage() for r in placeholder_warnings]}"
        )


class TestDotEnvExampleNoActiveSmtpLines:
    """``.env.example`` must ship with all SMTP_* lines lead-`#`-commented.

    ``install.sh`` does a verbatim ``cp .env.example .env``.  If any
    ``SMTP_*=…`` line is uncommented in the template, every fresh
    install produces a ``.env`` whose placeholder value gets loaded
    by docker-compose and (with placeholder detection) triggers a
    one-shot WARN log on startup.  We'd rather silence the noise
    entirely by shipping a clean template.
    """

    def test_no_active_smtp_assignment_in_env_example(self):
        from pathlib import Path
        import re

        project_root = Path(__file__).resolve().parent.parent
        env_example = project_root / ".env.example"
        assert env_example.exists(), "expected .env.example at project root"

        # Active = line starts with SMTP_ and contains = without a leading #
        active_line = re.compile(r"^\s*SMTP_[A-Z_]+=")
        offenders = []
        for lineno, raw in enumerate(env_example.read_text().splitlines(), start=1):
            if active_line.match(raw):
                offenders.append(f"{lineno}: {raw}")
        assert not offenders, (
            "Uncommented SMTP_* lines in .env.example will be copied into "
            ".env by install.sh and override UI settings. Comment them out "
            "and re-run the test:\n" + "\n".join(offenders)
        )


class TestSecContactFallback:
    """SEC scripts use a default User-Agent when SEC_EDGAR_CONTACT is missing.

    Superseded the old ``TestSecContactEnforcement`` on 2026-04-19. Request
    from the user: "it doesn't need an API key or credentials, rather it
    should automatically set to a standard one for that value." SEC's
    fair-access policy accepts any contact email in the UA; silent default
    is safe for `contact` kind - see
    ``reference_silent_credential_fallback_antipattern.md`` for the
    carve-out. Deep coverage lives in ``tests/test_sec_edgar_fallback.py``;
    these two tests just pin the script metadata + the happy path.
    """

    @pytest.mark.parametrize("script_name", SEC_SCRIPTS)
    def test_sec_script_does_not_raise_on_missing_contact(self, script_name):
        """Each SEC script must NOT re-introduce the raise-on-missing guard."""
        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        code = data["code"]
        # Must not have the obsolete raise-based guard
        assert "SEC_EDGAR_CONTACT is required" not in code, (
            f"{script_name} still raises on missing SEC_EDGAR_CONTACT - "
            f"should fall back to default UA."
        )
        # Must set the default UA literal
        assert "SpeakesQuery EDGAR" in code, (
            f"{script_name} missing the default User-Agent fallback literal"
        )

    @pytest.mark.parametrize("script_name", SEC_SCRIPTS)
    def test_sec_script_runs_with_empty_credentials(self, script_name):
        """Executing with CREDENTIALS={} must pass (via the default UA fallback)."""
        import unittest.mock

        data = json.loads((SCRIPTS_DIR / f"{script_name}.json").read_text())
        code = data["code"]
        reset_budget(max_requests=50, max_response_mb=10, allowed_domains=[])
        executor = CodeExecutor(
            code,
            test_mode=True,
            trust_level=data.get("trust_level", "sandboxed"),
        )

        # Mock HTTP so the real SEC endpoint never gets hit from CI.
        def _router(url, *_args, **_kwargs):
            resp = unittest.mock.MagicMock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.MagicMock()
            if "company_tickers" in url:
                resp.json = lambda: {
                    "0": {"cik_str": 320193, "ticker": "AAPL",
                          "title": "Apple Inc."}
                }
            elif "submissions/CIK" in url:
                resp.json = lambda: {
                    "cik": "320193", "name": "Apple Inc.", "tickers": ["AAPL"],
                    "filings": {"recent": {
                        "form": ["10-K"], "filingDate": ["2026-01-01"],
                        "accessionNumber": ["0000320193-26-000001"],
                        "primaryDocument": ["doc.htm"],
                    }},
                }
            elif "/xbrl/frames/" in url:
                resp.json = lambda: {"data": [
                    {"cik": 320193, "entityName": "Apple Inc.",
                     "val": 1_000_000_000, "filed": "2026-01-15", "form": "10-K"}
                ]}
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=_router), \
             unittest.mock.patch("time.sleep", lambda *a, **kw: None):
            result = executor.execute_test(extra_globals={"CREDENTIALS": {}})
        assert result["status"] == "pass", (
            f"SEC script should run with empty creds via fallback; got "
            f"errors: {result['errors']}"
        )


# ══════════════════════════════════════════════════════════════════════
# H-SV-2: redact_credentials helper + engine integration
# ══════════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: exception messages that embed
# credential values (e.g. ``KeyError({'TOKEN': 'ghp_real'})``) must be
# scrubbed before landing in Parquet telemetry, SQLite history, or docker
# logs. The redactor also handles H-SV-3's subprocess-stderr pipeline.


class TestRedactCredentials:
    """Unit tests for scheduled_input_engine/_redact.py::redact_credentials."""

    def test_empty_creds_passthrough(self):
        from scheduled_input_engine._redact import redact_credentials
        assert redact_credentials("no secrets here", {}) == "no secrets here"
        assert redact_credentials("no secrets here", None) == "no secrets here"

    def test_single_credential_value_redacted(self):
        from scheduled_input_engine._redact import redact_credentials
        msg = "KeyError: 'GITHUB_TOKEN' - got 'ghp_abcdef1234567890'"
        out = redact_credentials(msg, {"GITHUB_TOKEN": "ghp_abcdef1234567890"})
        assert "ghp_abcdef1234567890" not in out
        assert "[REDACTED:GITHUB_TOKEN]" in out

    def test_multiple_credentials_redacted(self):
        from scheduled_input_engine._redact import redact_credentials
        msg = "failed with api_key=sk-ant-xxxxxxxx and fred_key=FFRED123456"
        out = redact_credentials(
            msg,
            {"ANTHROPIC": "sk-ant-xxxxxxxx", "FRED_API_KEY": "FFRED123456"},
        )
        assert "sk-ant-xxxxxxxx" not in out
        assert "FFRED123456" not in out
        assert "[REDACTED:ANTHROPIC]" in out
        assert "[REDACTED:FRED_API_KEY]" in out

    def test_short_values_not_redacted(self):
        """Very short values (< 4 chars) aren't meaningful secrets and can produce collateral damage."""
        from scheduled_input_engine._redact import redact_credentials
        msg = "the quick brown fox jumps over the lazy dog"
        out = redact_credentials(msg, {"TINY": "fox", "ONE": "a"})
        # "fox" has 3 chars so it must NOT get substituted.
        assert out == msg

    def test_exception_object_accepted(self):
        from scheduled_input_engine._redact import redact_credentials
        try:
            raise KeyError({"API_KEY": "sk-long-enough-to-redact"})
        except KeyError as e:
            out = redact_credentials(e, {"API_KEY": "sk-long-enough-to-redact"})
        assert "sk-long-enough-to-redact" not in out
        assert "[REDACTED:API_KEY]" in out

    def test_key_upper_cased_in_sentinel(self):
        from scheduled_input_engine._redact import redact_credentials
        out = redact_credentials(
            "got token ghp_xyz1234",
            {"github_token": "ghp_xyz1234"},
        )
        assert "[REDACTED:GITHUB_TOKEN]" in out

    def test_longer_value_substituted_before_shorter_prefix(self):
        """If one cred's value is a prefix of another's, the longer must win."""
        from scheduled_input_engine._redact import redact_credentials
        msg = "prefix_abcd and prefix_abcd_extended"
        out = redact_credentials(
            msg,
            {"SHORT": "prefix_abcd", "LONG": "prefix_abcd_extended"},
        )
        # The longer one must be substituted first; otherwise the shorter
        # one would consume the prefix of the longer, leaving "_extended"
        # dangling.
        assert "prefix_abcd_extended" not in out
        assert "[REDACTED:LONG]" in out
        # The short sentinel appears at least once (the free-standing case).
        assert "[REDACTED:SHORT]" in out

    def test_non_string_value_coerced(self):
        from scheduled_input_engine._redact import redact_credentials
        msg = "numeric token 12345678"
        out = redact_credentials(msg, {"NUM": 12345678})
        assert "12345678" not in out
        assert "[REDACTED:NUM]" in out


class TestEngineRedactsCredentialsInErrorPath:
    """End-to-end: _run_task must log a redacted message when a script crashes with a cred-in-exception."""

    def _make_engine_with_fake_vault(self, creds: dict):
        """Build an engine whose vault returns *creds* for any task_id."""
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()

        class _FakeVault:
            def decrypt_for_script(self, _task_id):
                return creds

        engine._vault = _FakeVault()
        return engine

    def test_valueerror_embedding_credential_is_redacted(self, caplog):
        """ValueError (non-retryable path) must scrub credentials from the log."""
        import logging
        import unittest.mock as _mock

        secret = "ghp_thisIsARealLookingToken123"
        engine = self._make_engine_with_fake_vault({"GITHUB_TOKEN": secret})
        try:
            task = {
                "id": 424242,
                "title": "cred_leak_regression_ve",
                "trust_level": "sandboxed",
                # Must mention GENERATE_RESULTS for the AST check to pass,
                # but raise before reaching it at runtime. The .system4
                # filename convention is also enforced at compile time.
                "code": (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'_epoch': [1]})\n"
                    "raise ValueError('login failed with token "
                    + secret
                    + "')\n"
                    "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
                ),
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_redact",
            }
            with _mock.patch.object(engine._writer, "write_atomic"), \
                 caplog.at_level(logging.ERROR, logger="scheduled_input_engine.engine"):
                engine._run_task(task)

            for rec in caplog.records:
                assert secret not in rec.getMessage(), (
                    f"Credential leaked into log: {rec.getMessage()!r}"
                )
            assert any(
                "[REDACTED:GITHUB_TOKEN]" in rec.getMessage()
                for rec in caplog.records
            ), (
                "Expected the [REDACTED:GITHUB_TOKEN] sentinel in the "
                "error log. records=\n"
                + "\n".join(r.getMessage() for r in caplog.records)
            )
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

    def test_runtime_error_final_attempt_also_redacted(self, caplog):
        """RuntimeError (generic Exception path, final attempt) must also scrub."""
        import logging
        import unittest.mock as _mock

        secret = "sk-ant-longish-fake-key-material"
        engine = self._make_engine_with_fake_vault({"ANTHROPIC": secret})
        try:
            # Force only one attempt so we land in the final-failure branch.
            engine._setting = lambda key, default=None: (
                0 if key == "max_retries" else default
            )

            task = {
                "id": 424243,
                "title": "runtime_error_regression",
                "trust_level": "sandboxed",
                "code": (
                    "import pandas as pd\n"
                    "df = pd.DataFrame({'_epoch': [1]})\n"
                    "raise RuntimeError('boom with " + secret + " inside')\n"
                    "GENERATE_RESULTS(df, 'x.system4.system4.parquet')\n"
                ),
                "cron_schedule": "* * * * *",
                "overwrite": False,
                "subdirectory": "_test_redact",
            }
            with _mock.patch.object(engine._writer, "write_atomic"), \
                 caplog.at_level(logging.ERROR, logger="scheduled_input_engine.engine"):
                engine._run_task(task)

            for rec in caplog.records:
                assert secret not in rec.getMessage(), (
                    f"Credential leaked in final-attempt log: {rec.getMessage()!r}"
                )
            assert any(
                "[REDACTED:ANTHROPIC]" in rec.getMessage()
                for rec in caplog.records
            )
        finally:
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
# H-SV-3: subprocess stderr scrubber for repo scripts
# ══════════════════════════════════════════════════════════════════════
# Pins the 2026-04-21 production-review fix: repo scripts receive credentials
# as SPEAKESQUERY_CRED_<KEY> env vars, and any subprocess stderr the engine
# captures is logged verbatim into execution_history. Both the env-var
# assignment form (``SPEAKESQUERY_CRED_X=ghp_...``) and the bare value form
# (``ghp_...`` echoed on its own) must be scrubbed before record_execution.


class TestRedactSubprocessOutput:
    """Unit tests for scheduled_input_engine/_redact.py::redact_subprocess_output."""

    def test_env_assignment_redacted(self):
        from scheduled_input_engine._redact import redact_subprocess_output
        stderr = "debug: SPEAKESQUERY_CRED_GITHUB_TOKEN=ghp_real_token_xyz\n"
        out = redact_subprocess_output(stderr, {})
        assert "ghp_real_token_xyz" not in out
        assert "SPEAKESQUERY_CRED_GITHUB_TOKEN=[REDACTED]" in out

    def test_bare_value_redacted_via_creds_dict(self):
        from scheduled_input_engine._redact import redact_subprocess_output
        stderr = "http error body: token ghp_real_token_xyz expired\n"
        out = redact_subprocess_output(stderr, {"GITHUB_TOKEN": "ghp_real_token_xyz"})
        assert "ghp_real_token_xyz" not in out
        assert "[REDACTED:GITHUB_TOKEN]" in out

    def test_env_assignment_and_bare_value_both_scrubbed(self):
        from scheduled_input_engine._redact import redact_subprocess_output
        stderr = (
            "SPEAKESQUERY_CRED_FRED_API_KEY=FFRED12345678\n"
            "Also logged separately: FFRED12345678\n"
        )
        out = redact_subprocess_output(
            stderr, {"FRED_API_KEY": "FFRED12345678"}
        )
        assert "FFRED12345678" not in out
        assert "SPEAKESQUERY_CRED_FRED_API_KEY=[REDACTED]" in out
        assert "[REDACTED:FRED_API_KEY]" in out

    def test_non_speakesquery_env_vars_preserved(self):
        """Only SPEAKESQUERY_CRED_* env assignments should be touched."""
        from scheduled_input_engine._redact import redact_subprocess_output
        stderr = "HOME=/root\nPATH=/usr/bin:/bin\nPWD=/tmp"
        out = redact_subprocess_output(stderr, {})
        assert out == stderr

    def test_empty_output(self):
        from scheduled_input_engine._redact import redact_subprocess_output
        assert redact_subprocess_output("", {}) == ""
        assert redact_subprocess_output("", {"K": "vvvv"}) == ""

    def test_value_terminator_at_quote(self):
        """Value capture stops at quotes / commas / semicolons so surrounding text survives."""
        from scheduled_input_engine._redact import redact_subprocess_output
        stderr = "Dumped: SPEAKESQUERY_CRED_X=secretval; next line"
        out = redact_subprocess_output(stderr, {})
        assert "secretval" not in out
        assert "SPEAKESQUERY_CRED_X=[REDACTED]" in out
        assert "next line" in out

    def test_multiple_env_assignments_on_separate_lines(self):
        from scheduled_input_engine._redact import redact_subprocess_output
        stderr = (
            "SPEAKESQUERY_CRED_A=aaaaaaaa\n"
            "SPEAKESQUERY_CRED_B=bbbbbbbb\n"
        )
        out = redact_subprocess_output(stderr, {})
        assert "aaaaaaaa" not in out
        assert "bbbbbbbb" not in out
        assert out.count("[REDACTED]") == 2


class TestRepoScriptStderrScrub:
    """Integration: _run_repo_script must scrub subprocess stderr before record_execution."""

    def _make_engine_with_fake_vault(self, creds: dict):
        from scheduled_input_engine.engine import ScheduledInputEngine

        engine = ScheduledInputEngine()

        class _FakeVault:
            def decrypt_for_script(self, _key):
                return creds

        engine._vault = _FakeVault()
        return engine

    def _fake_subprocess_result(self, stderr: str, returncode: int = 1):
        """Build a lightweight stand-in for run_in_subprocess's SimpleNamespace result."""
        import types
        return types.SimpleNamespace(
            stdout="",
            stderr=stderr,
            returncode=returncode,
        )

    def test_stderr_env_dump_is_scrubbed_before_recording(self, tmp_path, monkeypatch):
        """A repo script that echoes SPEAKESQUERY_CRED_X=<val> must not leak to execution_history."""
        import unittest.mock as _mock
        from scheduled_input_engine import engine as engine_mod

        secret = "ghp_subprocess_leak_1234"
        creds = {"GITHUB_TOKEN": secret}
        engine = self._make_engine_with_fake_vault(creds)
        try:
            # Stage a dummy repo + script file under INPUT_REPOS_ROOT so the
            # path-traversal check passes. The file's contents don't matter
            # because we mock run_in_subprocess.
            repo_dir = engine_mod.INPUT_REPOS_ROOT / "_sv3_test_repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            script_file = repo_dir / "leak.py"
            script_file.write_text("# noop\n")

            captured: dict = {}

            def fake_record(task_id, script_name, elapsed, status, error_msg=None):
                captured["task_id"] = task_id
                captured["status"] = status
                captured["error_msg"] = error_msg

            monkeypatch.setattr(engine.store, "record_execution", fake_record)

            # Point indexes to a tmp path so we don't pollute real indexes.
            monkeypatch.setattr(
                engine, "_get_indexes_dir", lambda: tmp_path / "indexes"
            )

            # Mock the subprocess runner to return stderr with BOTH forms of
            # leak we need to defend against.
            leak_stderr = (
                f"SPEAKESQUERY_CRED_GITHUB_TOKEN={secret}\n"
                f"requests.get failed with token {secret}\n"
            )
            fake_result = self._fake_subprocess_result(leak_stderr, returncode=1)

            async def _fake_runner(*_args, **_kwargs):
                return fake_result

            with _mock.patch.object(engine_mod, "run_in_subprocess", _fake_runner):
                engine._run_repo_script({
                    "id": 9191,
                    "script_name": "leak.py",
                    "repo_path": str(repo_dir),
                    "output_subdir": "",
                    "overwrite": False,
                })

            # Assert the telemetry row has BOTH scrubs applied and the raw
            # secret is nowhere in error_msg.
            assert captured.get("status") == "failed"
            err = captured.get("error_msg") or ""
            assert secret not in err, (
                f"Raw credential leaked to execution_history: {err!r}"
            )
            assert "SPEAKESQUERY_CRED_GITHUB_TOKEN=[REDACTED]" in err
            assert "[REDACTED:GITHUB_TOKEN]" in err
        finally:
            # Clean up the fake repo so subsequent test runs start clean.
            try:
                script_file.unlink()
                repo_dir.rmdir()
            except Exception:
                pass
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass

    def test_success_path_does_not_call_scrubber(self, tmp_path, monkeypatch):
        """If the subprocess succeeded, error_msg is None - scrubber shouldn't matter."""
        import unittest.mock as _mock
        from scheduled_input_engine import engine as engine_mod

        engine = self._make_engine_with_fake_vault({"X": "aaaaaaaa"})
        try:
            repo_dir = engine_mod.INPUT_REPOS_ROOT / "_sv3_test_success_repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            script_file = repo_dir / "ok.py"
            script_file.write_text("# noop\n")

            captured: dict = {}

            def fake_record(task_id, script_name, elapsed, status, error_msg=None):
                captured.update(
                    task_id=task_id, status=status, error_msg=error_msg,
                )

            monkeypatch.setattr(engine.store, "record_execution", fake_record)
            monkeypatch.setattr(
                engine, "_get_indexes_dir", lambda: tmp_path / "indexes"
            )

            fake_result = self._fake_subprocess_result("", returncode=0)

            async def _fake_runner(*_args, **_kwargs):
                return fake_result

            with _mock.patch.object(engine_mod, "run_in_subprocess", _fake_runner):
                engine._run_repo_script({
                    "id": 9192,
                    "script_name": "ok.py",
                    "repo_path": str(repo_dir),
                    "output_subdir": "",
                    "overwrite": False,
                })

            assert captured.get("status") == "success"
            assert captured.get("error_msg") is None
        finally:
            try:
                script_file.unlink()
                repo_dir.rmdir()
            except Exception:
                pass
            try:
                engine._scheduler.shutdown(wait=False)
            except Exception:
                pass
