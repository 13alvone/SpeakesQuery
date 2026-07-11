"""Tests for ``tools.smtp_diagnose`` and ``/api/email/diagnose``.

The diagnostic must be runnable from inside the deployed Docker image
(where ``tests/`` is excluded by ``.dockerignore``), so the real code
lives in ``tools/`` and these tests exercise it from outside.

Covered:

* ``tools.smtp_diagnose.run_diagnostic`` reports a structured
  ``DiagnosticReport`` with saved-config shape (no password echo) and a
  per-step list of ``StepResult`` objects.
* ``POST /api/email/diagnose`` returns the same report as JSON.
* When AUTH fails with Gmail's 535 on a password that contains
  whitespace, the hint steers users toward the paste-form fix.
"""
from __future__ import annotations

import smtplib

from tools.smtp_diagnose import DiagnosticReport, run_diagnostic


class _FakeSmtp:
    """Minimal stand-in for ``smtplib.SMTP`` - configurable per step."""

    def __init__(
        self,
        *,
        has_starttls: bool = True,
        starttls_exc: Exception | None = None,
        login_exc: Exception | None = None,
        send_exc: Exception | None = None,
    ):
        self._starttls = has_starttls
        self._starttls_exc = starttls_exc
        self._login_exc = login_exc
        self._send_exc = send_exc
        self.logged_in = None
        self.last_sent = None

    def ehlo(self):
        return (250, b"ok")

    def has_extn(self, name):
        return name == "STARTTLS" and self._starttls

    def starttls(self, context=None):
        if self._starttls_exc:
            raise self._starttls_exc

    def login(self, user, password):
        if self._login_exc:
            raise self._login_exc
        self.logged_in = (user, password)

    def send_message(self, msg):
        """``run_diagnostic`` uses ``EmailMessage`` + ``send_message`` so
        the message body is encoded correctly (the old raw-``sendmail``
        path broke on non-ASCII characters like an em-dash)."""
        if self._send_exc:
            raise self._send_exc
        self.last_sent = (msg["From"], msg["To"], msg.get_content())

    def quit(self):
        pass


def _install_fake_settings(monkeypatch, **overrides):
    """Patch every settings-access path the diagnostic touches.

    ``run_diagnostic`` now resolves config through
    ``query_engine.Alert.load_smtp_config_from_env`` (same as real
    sends), which lazily imports ``global_settings.get_settings``.  The
    diagnostic's own ``_saved_config`` still calls the eagerly-imported
    ``tools.smtp_diagnose.get_settings``.  We patch both.
    """
    import global_settings

    base = {
        "smtp_server": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "u@example.com",
        "smtp_password": "abcdefghijklmnop",
        "smtp_from": "u@example.com",
        "smtp_starttls": True,
    }
    base.update(overrides)

    class _Fake:
        def get_all(self_inner):
            return dict(base)

        def get(self_inner, k):
            return base.get(k)

    fake = _Fake()
    monkeypatch.setattr("tools.smtp_diagnose.get_settings", lambda: fake)
    monkeypatch.setattr(global_settings, "get_settings", lambda *a, **kw: fake)
    monkeypatch.setattr(global_settings, "_instance", None)

    # Strip any SMTP_* env vars that may have leaked from the shell or
    # PyCharm run config so the resolver actually falls through to our
    # fake settings.  Tests that need to set env vars do so explicitly.
    for k in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_SERVER", "SMTP_PORT",
              "SMTP_FROM", "SMTP_STARTTLS"):
        monkeypatch.delenv(k, raising=False)

    # Clear one-shot warn dedupe + placeholder record so tests don't
    # leak state into each other through module-global sets.
    from query_engine import Alert
    Alert._placeholder_warned.clear()
    Alert._placeholders_ignored.clear()


class TestRunDiagnostic:
    def test_happy_path_returns_ok(self, monkeypatch):
        _install_fake_settings(monkeypatch)
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(),
        )
        report = run_diagnostic()
        assert isinstance(report, DiagnosticReport)
        assert report.ok
        step_names = [s.name for s in report.steps]
        assert step_names == ["tcp_reach", "starttls", "auth"]

    def test_reports_send_step_when_send_to_given(self, monkeypatch):
        _install_fake_settings(monkeypatch)
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(),
        )
        report = run_diagnostic(send_to="recipient@example.com")
        assert report.ok
        assert [s.name for s in report.steps] == [
            "tcp_reach", "starttls", "auth", "send",
        ]

    def test_send_step_uses_ascii_safe_body(self, monkeypatch):
        """Regression: the diagnostic body must be 7-bit ASCII-safe.

        The original raw-``sendmail`` path broke on an em dash in the
        body (``'ascii' codec can't encode character``) and masked
        the real delivery outcome.  We now use ``EmailMessage`` +
        ``send_message`` so the body can contain any character and the
        encoding is explicit - but the default body stays pure ASCII
        to keep the pre-MIME handoff debuggable on arbitrary relays.
        """
        _install_fake_settings(monkeypatch)
        fake = _FakeSmtp()
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: fake,
        )
        report = run_diagnostic(send_to="recipient@example.com")
        assert report.ok
        assert fake.last_sent is not None
        _from, _to, body = fake.last_sent
        body.encode("ascii")  # must not raise

    def test_auth_failure_surfaces_gmail_hint(self, monkeypatch):
        _install_fake_settings(monkeypatch)
        exc = smtplib.SMTPAuthenticationError(
            535, b"5.7.8 Username and Password not accepted.",
        )
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(login_exc=exc),
        )
        report = run_diagnostic()
        assert not report.ok
        auth_step = next(s for s in report.steps if s.name == "auth")
        assert not auth_step.ok
        assert "535" in auth_step.message
        assert "App Password" in auth_step.hint

    def test_missing_credentials_surfaces_config_step(self, monkeypatch):
        """Empty creds short-circuit before any SMTP connection is made.

        ``load_smtp_config_from_env`` raises ``RuntimeError`` with a
        "fill in Settings" message; ``run_diagnostic`` catches it and
        surfaces it as a dedicated ``config`` step so users see the
        fix path without a TCP connection even being attempted.
        """
        _install_fake_settings(monkeypatch, smtp_user="", smtp_password="")
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(),
        )
        report = run_diagnostic()
        assert not report.ok
        step_names = [s.name for s in report.steps]
        assert step_names == ["config"]
        config_step = report.steps[0]
        assert "Settings" in config_step.hint

    def test_env_placeholders_surface_in_saved_config(self, monkeypatch):
        """Detected placeholders must appear in ``saved_config`` so a
        remote user can see why AUTH is falling back to settings."""
        _install_fake_settings(monkeypatch)
        monkeypatch.setenv("SMTP_USER", "you@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "your_16_char_app_password")
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(),
        )
        report = run_diagnostic()
        ignored = report.saved_config["env_placeholders_ignored"]
        assert ignored == {
            "SMTP_USER": "you@gmail.com",
            "SMTP_PASSWORD": "your_16_char_app_password",
        }
        # Report still passes AUTH - the resolver correctly falls
        # through to the fake settings we installed.
        assert report.ok

    def test_no_password_echo_anywhere_in_report(self, monkeypatch):
        _install_fake_settings(monkeypatch, smtp_password="supersecret1234567")
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(),
        )
        report = run_diagnostic()
        full = str(report.as_dict())
        assert "supersecret1234567" not in full, (
            "Diagnostic report leaked the password"
        )


class TestEndpoint:
    def test_endpoint_returns_report_json(self, client, monkeypatch):
        _install_fake_settings(monkeypatch)
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: _FakeSmtp(),
        )
        resp = client.post("/api/email/diagnose", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["report"]["ok"]
        names = [s["name"] for s in data["report"]["steps"]]
        assert names == ["tcp_reach", "starttls", "auth"]

    def test_endpoint_passes_send_to(self, client, monkeypatch):
        _install_fake_settings(monkeypatch)
        fake = _FakeSmtp()
        monkeypatch.setattr(
            "tools.smtp_diagnose.smtplib.SMTP",
            lambda *args, **kwargs: fake,
        )
        resp = client.post(
            "/api/email/diagnose",
            json={"send_to": "you@example.com"},
        )
        data = resp.get_json()
        assert data["report"]["ok"]
        send_step = next(s for s in data["report"]["steps"] if s["name"] == "send")
        assert send_step["ok"]
        assert fake.last_sent is not None
