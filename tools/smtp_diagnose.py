"""SMTP diagnostic - pinpoint why the "Send Test Email" button fails.

Two ways to run:

**CLI** (inside a deployed container or locally)::

    python -m tools.smtp_diagnose                              # just run the auth + reach checks
    python -m tools.smtp_diagnose --send-to you@example.com    # also attempt a real delivery
    python -m tools.smtp_diagnose --strip-password             # retry with whitespace stripped

**HTTP** (no shell access)::

    curl -s -X POST http://<host>:5111/api/email/diagnose \\
         -H 'Content-Type: application/json' \\
         -d '{"send_to":"you@example.com"}' | jq

The two paths share the core logic in :func:`run_diagnostic` so their
outputs are guaranteed to agree.

The diagnostic never echoes the password. It reports:

1. What credentials are currently saved in ``global_settings.yaml``
   (user, password length, whether the saved value has whitespace -
   which new installs normalise away but older installs may still carry).
2. Whether ``SMTP_*`` environment variables would override those settings.
3. Whether the saved server / port / STARTTLS combination is reachable.
4. Whether the SMTP AUTH handshake succeeds (the step that fails most
   often: wrong password, App Password revoked, account locked).
5. If ``send_to`` is supplied, whether a real message delivers.

Each step is reported separately so a network failure is distinguishable
from an auth failure from a delivery failure - the UI's all-or-nothing
error message cannot distinguish these.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import socket
import ssl
import sys
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from global_settings import get_settings  # noqa: E402


# ─────────────────────────────────────────────────────────────────
# Result dataclasses - serialised to JSON for the HTTP endpoint
# ─────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    name: str
    ok: bool
    message: str
    hint: str = ""


@dataclass
class DiagnosticReport:
    ok: bool
    saved_config: dict
    steps: list[StepResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "saved_config": self.saved_config,
            "steps": [asdict(s) for s in self.steps],
        }


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _mask_password_shape(pw: str) -> str:
    if not pw:
        return "(empty)"
    has_ws = any(c.isspace() for c in pw)
    return (
        f"{len(pw)} chars"
        f"{', has internal whitespace' if has_ws else ', no whitespace'}"
        f"{', alnum-only' if pw.replace(' ', '').isalnum() else ', non-alnum present'}"
    )


def _saved_config() -> dict:
    # Import lazily so the diagnostic can still run (with an empty
    # placeholder map) even if Alert.py fails to import - which would be
    # itself a diagnostic signal worth surfacing rather than crashing on.
    try:
        from query_engine.Alert import get_env_placeholders_ignored
        placeholders_ignored = get_env_placeholders_ignored()
    except Exception:
        placeholders_ignored = {}

    s = get_settings().get_all()
    cfg = {
        "smtp_server": s.get("smtp_server") or "",
        "smtp_port": int(s.get("smtp_port") or 587),
        "smtp_user": s.get("smtp_user") or "",
        "smtp_password": s.get("smtp_password") or "",
        "smtp_from": s.get("smtp_from") or "",
        "smtp_starttls": bool(s.get("smtp_starttls")),
    }
    env_overrides = [k for k in (
        "SMTP_SERVER", "SMTP_PORT", "SMTP_USER",
        "SMTP_PASSWORD", "SMTP_FROM", "SMTP_STARTTLS",
    ) if os.environ.get(k)]
    return {
        "server": cfg["smtp_server"],
        "port": cfg["smtp_port"],
        "starttls": cfg["smtp_starttls"],
        "user": cfg["smtp_user"],
        "from": cfg["smtp_from"],
        "password_shape": _mask_password_shape(cfg["smtp_password"]),
        "env_overrides": env_overrides,
        # Env-var names whose values were exact-matched against known
        # ``.env.example`` placeholders and therefore ignored at
        # credential-resolution time.  Populated by
        # ``query_engine.Alert._env_smtp`` when ``load_smtp_config_from_env``
        # runs - the diagnostic calls that function downstream, so the
        # map is authoritative by the time the report is rendered.
        "env_placeholders_ignored": placeholders_ignored,
    }


# ─────────────────────────────────────────────────────────────────
# Core diagnostic
# ─────────────────────────────────────────────────────────────────


def run_diagnostic(
    *,
    send_to: str | None = None,
    strip_password: bool = False,
) -> DiagnosticReport:
    """Run the SMTP diagnostic against the values the real send path uses.

    Resolution goes through ``query_engine.Alert.load_smtp_config_from_env``
    - the same function every alert / test-email call uses - so the
    diagnostic can never pass while the real send fails.  As a side
    effect, loading the config triggers placeholder detection, so
    ``saved_config["env_placeholders_ignored"]`` is authoritative by
    the time the report is built.

    Returns a structured report that both the CLI and HTTP endpoint
    render. Never raises on auth / network failures - those are
    captured as non-OK steps.
    """
    from query_engine.Alert import load_smtp_config_from_env
    try:
        cfg = load_smtp_config_from_env()
    except RuntimeError as exc:
        # Credentials missing or structurally invalid - surface as a
        # dedicated ``config`` step rather than crashing mid-report.
        report = DiagnosticReport(ok=False, saved_config=_saved_config())
        report.steps.append(StepResult(
            name="config", ok=False,
            message=str(exc),
            hint=(
                "Open Settings → Email, fill in username and App Password, "
                "click Save, then retry.  Or set SMTP_USER + SMTP_PASSWORD "
                "in .env with real values (not the shipped placeholders)."
            ),
        ))
        return report

    server = cfg.server
    port = cfg.port
    user = cfg.user
    password = cfg.password
    from_addr = cfg.from_addr
    start_tls = cfg.start_tls

    # ``strip_password`` is retained for backwards-compat with the
    # ``--strip-password`` CLI flag but is now a no-op: the resolver
    # already normalises Gmail App Password whitespace before returning.
    if strip_password:
        password = "".join(password.split())

    report = DiagnosticReport(ok=True, saved_config=_saved_config())

    # Step 1 - TCP reach
    smtp: smtplib.SMTP | None = None
    try:
        smtp = smtplib.SMTP(server, port, timeout=15)
        report.steps.append(StepResult(
            name="tcp_reach", ok=True,
            message=f"connected to {server}:{port}",
        ))
    except socket.timeout:
        report.steps.append(StepResult(
            name="tcp_reach", ok=False,
            message=f"timeout connecting to {server}:{port}",
            hint="firewall or container egress is blocking outbound SMTP",
        ))
        report.ok = False
        return report
    except OSError as exc:
        report.steps.append(StepResult(
            name="tcp_reach", ok=False,
            message=f"{type(exc).__name__}: {exc}",
            hint="DNS / network failure from the container",
        ))
        report.ok = False
        return report

    try:
        # Step 2 - STARTTLS
        if start_tls:
            try:
                smtp.ehlo()
                if not smtp.has_extn("STARTTLS"):
                    report.steps.append(StepResult(
                        name="starttls", ok=False,
                        message="server does not advertise STARTTLS",
                    ))
                    report.ok = False
                    return report
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                report.steps.append(StepResult(
                    name="starttls", ok=True, message="TLS handshake succeeded",
                ))
            except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
                report.steps.append(StepResult(
                    name="starttls", ok=False,
                    message=f"{type(exc).__name__}: {exc}",
                ))
                report.ok = False
                return report

        # Step 3 - AUTH.  ``load_smtp_config_from_env`` already guarantees
        # user + password are non-empty (it raises otherwise, handled at
        # the top of ``run_diagnostic`` as a ``config`` step) and that
        # the password is whitespace-normalised.  So the old "missing
        # creds" and "spaced-password" hints that used to live here are
        # now unreachable - every value passed to ``smtp.login`` is the
        # same one the real send path uses.
        try:
            smtp.login(user, password)
            report.steps.append(StepResult(
                name="auth", ok=True, message="AUTH accepted",
            ))
        except smtplib.SMTPAuthenticationError as exc:
            hint = (
                "Gmail 535 usually means the App Password is wrong, was "
                "revoked, or 2-Step Verification is off. Check "
                "https://myaccount.google.com/apppasswords and paste a "
                "fresh one into Settings → Email."
            )
            report.steps.append(StepResult(
                name="auth", ok=False,
                message=f"{exc.smtp_code} {exc.smtp_error!r}",
                hint=hint,
            ))
            report.ok = False
            return report
        except smtplib.SMTPException as exc:
            report.steps.append(StepResult(
                name="auth", ok=False,
                message=f"{type(exc).__name__}: {exc}",
            ))
            report.ok = False
            return report

        # Step 4 - Optional real delivery
        if send_to:
            # Keep the body 7-bit ASCII.  ``smtplib.sendmail`` encodes
            # the raw message with ``ascii`` by default; a stray em-dash
            # in the body raises ``'ascii' codec can't encode character``
            # and masks the actual delivery outcome.  Use ``EmailMessage``
            # + ``send_message`` so encoding is handled explicitly.
            msg = EmailMessage()
            msg["From"] = from_addr
            msg["To"] = send_to
            msg["Subject"] = "SpeakesQuery SMTP diagnostic"
            msg.set_content(
                "Diagnostic CLI test - if you see this, delivery is healthy."
            )
            try:
                smtp.send_message(msg)
                report.steps.append(StepResult(
                    name="send", ok=True,
                    message=f"message handed to {server} for {send_to}",
                ))
            except smtplib.SMTPException as exc:
                report.steps.append(StepResult(
                    name="send", ok=False,
                    message=f"{type(exc).__name__}: {exc}",
                ))
                report.ok = False
                return report
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    return report


# ─────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────


def _format_cli(report: DiagnosticReport) -> str:
    out = ["── Saved settings (from global_settings.yaml) ──"]
    cfg = report.saved_config
    out.append(f"  server           : {cfg['server']!r}")
    out.append(f"  port             : {cfg['port']}")
    out.append(f"  start_tls        : {cfg['starttls']}")
    out.append(f"  user             : {cfg['user']!r}")
    out.append(f"  from             : {cfg['from']!r}")
    out.append(f"  password shape   : {cfg['password_shape']}")
    if cfg["env_overrides"]:
        out.append(f"  env overrides    : {cfg['env_overrides']}")
    ignored = cfg.get("env_placeholders_ignored") or {}
    if ignored:
        out.append("  env placeholders : (.env.example defaults - ignored)")
        for name, value in ignored.items():
            out.append(f"    {name}={value!r}")
    for step in report.steps:
        marker = "OK" if step.ok else "FAIL"
        out.append(f"\n── {step.name}: {marker} ──")
        out.append(f"  {step.message}")
        if step.hint:
            out.append(f"  hint: {step.hint}")
    out.append("\nAll checks passed." if report.ok else "\nDiagnostic found a problem - see the failing step above.")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="SpeakesQuery SMTP diagnostic")
    parser.add_argument("--send-to", help="Address to send a real test message to")
    parser.add_argument(
        "--strip-password", action="store_true",
        help="Retry AUTH with all whitespace stripped from the password",
    )
    args = parser.parse_args(argv[1:])
    report = run_diagnostic(
        send_to=args.send_to,
        strip_password=args.strip_password,
    )
    print(_format_cli(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
