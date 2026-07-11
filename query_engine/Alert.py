#!/usr/bin/env python3
"""
query_engine/Alert.py

Design goals:
- NEVER fail at import-time due to missing SMTP env vars (setup / db init imports must be safe).
- Validate SMTP configuration only at send-time.
- Provide async + safe sync wrappers.
- Maintain backwards compatibility for legacy callers expecting email_results().
- Use logging (no print) with required prefixes.
"""

from __future__ import annotations

import os
import ssl
import asyncio
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import List, Optional, Sequence, Any

import aiosmtplib

logger = logging.getLogger(__name__)

# Max size for a single email attachment.  Most SMTP relays (Gmail, O365,
# corporate gateways) cap individual messages at 25 MB *after* MIME encoding,
# which inflates raw bytes by ~37%.  We cap raw attachment bytes at 18 MB so
# the encoded payload stays comfortably under the typical limit.  Larger
# result sets should be filtered in SPQL before emailing, or written to disk
# and linked instead.
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024


@dataclass(frozen=True)
class SMTPConfig:
    server: str
    port: int
    user: str
    password: str
    start_tls: bool
    from_addr: str


def _env_bool(val: Optional[str], default: bool) -> bool:
    if val is None:
        return default
    v = val.strip().lower()
    if v in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _env_int(val: Optional[str], default: int) -> int:
    if val is None:
        return default
    try:
        return int(val.strip())
    except Exception:
        return default


def _normalize_recipients(to_addrs: Sequence[str] | str) -> List[str]:
    if isinstance(to_addrs, str):
        raw = [to_addrs]
    else:
        raw = list(to_addrs)

    cleaned: List[str] = []
    for item in raw:
        if item is None:
            continue
        s = str(item).strip()
        if not s:
            continue
        cleaned.append(s)

    # De-dupe while preserving order
    seen = set()
    out: List[str] = []
    for addr in cleaned:
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def resolve_and_normalize_recipients(raw) -> List[str]:
    """Public resolver entry point used by every send path.

    Expands ``@group_name`` references (defined in ``email_groups/<name>.yaml``),
    splits comma/semicolon-delimited strings, validates each literal as
    an email, and returns a flat de-duplicated list.

    Inputs accepted (any of):
      - ``"alice@x.com"`` (single literal)
      - ``"alice@x.com, @sales_team, bob@y.com"`` (mixed, comma-delimited)
      - ``["alice@x.com", "@team"]`` (list)

    Behaviour:
      - Unknown ``@group_name`` references are skipped with a WARNING
        log - never raises so a typo cannot block a send that has at
        least some valid recipients.
      - On any unexpected error the function falls back to the legacy
        :func:`_normalize_recipients` behaviour (best-effort).
    """
    try:
        from email_group_store import resolve_recipients_for_send
        return resolve_recipients_for_send(raw)
    except Exception:
        # Fallback to the legacy normaliser so a bug in the resolver
        # never blocks an in-flight send.
        return _normalize_recipients(raw)


def _settings_value(key: str) -> str:
    """Read a single value from global_settings (best-effort, never raises)."""
    try:
        from global_settings import get_settings  # lazy to avoid circular imports
        return str(get_settings().get(key) or "")
    except Exception:
        return ""


# ── Env-var placeholder detection ────────────────────────────────
#
# ``.env.example`` historically shipped with literal placeholder values
# (``SMTP_USER=you@gmail.com``, ``SMTP_PASSWORD=your_16_char_app_password``)
# that ``install.sh`` copies verbatim into ``.env``.  Because
# ``desktop_app/docker-compose.yml`` loads ``../.env`` via ``env_file:``
# and PyCharm's Python run config also auto-loads the project-root
# ``.env``, those placeholders landed in ``os.environ`` and - thanks to
# env-wins-over-settings precedence - silently overrode anything the
# user saved through the UI.  Gmail returned ``535 5.7.8 BadCredentials``
# every send because AUTH was using the literal string
# ``your_16_char_app_password`` (25 chars, non-alnum) instead of the
# real 16-char App Password stored in ``global_settings.yaml``.
#
# The placeholder set below is **exact-match** - real credentials never
# collide with these literals (``you@gmail.com`` is a documentation
# placeholder Gmail does not assign; ``your_16_char_app_password``
# cannot be an App Password because they are 16 alnum chars).  We
# intentionally keep the set short and conservative; broadening it would
# risk discarding a user's real credential.
_SMTP_ENV_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "SMTP_USER": frozenset({"you@gmail.com", "your@email.com", "user@example.com"}),
    "SMTP_PASSWORD": frozenset({
        "your_16_char_app_password",
        "your_app_password",
        "your-app-password",
    }),
    "SMTP_FROM": frozenset({"you@gmail.com", "your@email.com", "user@example.com"}),
    "SMTP_SERVER": frozenset(),  # .env.example ships real default here
}

# Names for which ``[!] .env.example placeholder detected`` has already
# been logged this process.  Dedupe at module scope so every send path
# shares the same state - no floods of identical warnings.
_placeholder_warned: set[str] = set()

# Public view of placeholders ignored during the current process, keyed
# by env-var name → the placeholder string that was seen.  The
# ``/api/email/diagnose`` endpoint surfaces this so a remote user
# debugging a 535 can see *why* AUTH is falling through to settings
# without shell access to the container.
_placeholders_ignored: dict[str, str] = {}


def _env_smtp(name: str) -> str | None:
    """Return the stripped env var value, or ``None`` if unset or a known placeholder.

    Emits a one-shot ``[!]`` WARN log the first time each placeholder is
    seen so a fresh install whose ``.env`` still has ``.env.example``
    defaults produces a loud, actionable breadcrumb - not a silent 535.
    The placeholder string itself is logged (it is documentation, not a
    secret); the real saved password is never touched.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value in _SMTP_ENV_PLACEHOLDERS.get(name, frozenset()):
        _placeholders_ignored[name] = value
        if name not in _placeholder_warned:
            logger.warning(
                "[!] %s=%r is the shipped .env.example placeholder - ignoring and "
                "falling back to the value saved on the Settings page. "
                "Edit .env (comment out or replace the line) to silence this warning.",
                name, value,
            )
            _placeholder_warned.add(name)
        return None
    return value


def get_env_placeholders_ignored() -> dict[str, str]:
    """Return a copy of env vars whose values were ignored as placeholders.

    Used by the diagnostic endpoint so users can see, without shell
    access, which ``.env`` lines are shadowing their UI-saved settings.
    """
    return dict(_placeholders_ignored)


def load_smtp_config_from_env() -> SMTPConfig:
    """
    Loads SMTP config with the following precedence:
      1. Environment variables  (SMTP_USER, SMTP_PASSWORD, etc.) - except
         when the value is a known ``.env.example`` placeholder, in which
         case it is treated as unset and a one-time ``[!]`` WARN is logged.
      2. Global settings        (Settings page / global_settings.yaml)
      3. Built-in defaults      (smtp.gmail.com:587)

    Required at send-time: user + password (from either source).
    """
    # -- server / port / tls  (env > settings > defaults) --
    server = _env_smtp("SMTP_SERVER") or ""
    if not server:
        server = _settings_value("smtp_server") or "smtp.gmail.com"

    raw_port = os.environ.get("SMTP_PORT")
    if raw_port is not None:
        port = _env_int(raw_port, 587)
    else:
        port = int(_settings_value("smtp_port") or 587)

    raw_tls = os.environ.get("SMTP_STARTTLS")
    if raw_tls is not None:
        start_tls = _env_bool(raw_tls, True)
    else:
        tls_str = _settings_value("smtp_starttls").lower()
        start_tls = tls_str not in ("0", "false", "f", "no", "n", "off") if tls_str else True

    # -- credentials  (env > settings, with placeholder detection) --
    user = _env_smtp("SMTP_USER") or ""
    if not user:
        user = _settings_value("smtp_user").strip()

    password = _env_smtp("SMTP_PASSWORD") or ""
    if not password:
        password = _settings_value("smtp_password") or ""
    # Gmail App Passwords are 16 alphanumeric chars; the Google UI renders
    # them as ``xxxx xxxx xxxx xxxx`` and paste brings the spaces along.
    # Strip *all* whitespace, not just the ends, so both forms succeed.
    password = "".join(password.split())

    from_addr = _env_smtp("SMTP_FROM") or ""
    if not from_addr:
        from_addr = _settings_value("smtp_from").strip() or user

    if not user or not password:
        missing = []
        if not user:
            missing.append("SMTP username")
        if not password:
            missing.append("SMTP password")
        detail = " and ".join(missing)
        logger.error("[x] SMTP credentials incomplete - missing: %s", detail)
        raise RuntimeError(
            f"SMTP credentials incomplete - missing: {detail}. "
            "Fill in both fields on the Settings page (Email section) and click Save, "
            "or set SMTP_USER / SMTP_PASSWORD environment variables."
        )

    if not server:
        logger.error("[x] SMTP_SERVER resolved to empty string.")
        raise RuntimeError("SMTP_SERVER must be set (or left unset to use the default).")

    if port <= 0 or port > 65535:
        logger.error("[x] SMTP_PORT is invalid: %s", port)
        raise RuntimeError("SMTP_PORT is invalid.")

    if not from_addr:
        logger.error("[x] SMTP_FROM resolved to empty string.")
        raise RuntimeError("SMTP_FROM must be set (or SMTP_USER must be set).")

    return SMTPConfig(
        server=server,
        port=port,
        user=user,
        password=password,
        start_tls=start_tls,
        from_addr=from_addr,
    )


def build_email_message(
    *,
    subject: str,
    body: str,
    to_addrs: Sequence[str] | str,
    from_addr: str,
    csv_bytes: Optional[bytes] = None,
    csv_filename: str = "results.csv",
) -> EmailMessage:
    recipients = resolve_and_normalize_recipients(to_addrs)
    if not recipients:
        logger.error("[x] No valid recipient addresses provided.")
        raise ValueError("No valid recipient addresses provided.")

    msg = EmailMessage()
    msg["Subject"] = subject.strip()
    msg["From"] = from_addr.strip()
    msg["To"] = ", ".join(recipients)

    msg.set_content(body if body is not None else "")

    if csv_bytes is not None:
        if len(csv_bytes) > MAX_ATTACHMENT_BYTES:
            actual_mb = len(csv_bytes) / (1024 * 1024)
            limit_mb = MAX_ATTACHMENT_BYTES / (1024 * 1024)
            logger.error(
                "[x] Email attachment %s is %.1f MB, exceeds %.0f MB cap.",
                csv_filename, actual_mb, limit_mb,
            )
            raise ValueError(
                f"Email attachment {csv_filename!r} is {actual_mb:.1f} MB; "
                f"max allowed is {limit_mb:.0f} MB. Filter the result set "
                f"with `| head N` or `| where ...` to reduce its size."
            )
        msg.add_attachment(
            csv_bytes,
            maintype="text",
            subtype="csv",
            filename=csv_filename,
        )

    return msg


async def send_email_async(
    *,
    subject: str,
    body: str,
    to_addrs: Sequence[str] | str,
    smtp_config: Optional[SMTPConfig] = None,
    timeout_seconds: int = 30,
    csv_bytes: Optional[bytes] = None,
    csv_filename: str = "results.csv",
) -> None:
    """
    Async email sender. Validates config at call time.
    """
    cfg = smtp_config or load_smtp_config_from_env()
    msg = build_email_message(
        subject=subject, body=body, to_addrs=to_addrs, from_addr=cfg.from_addr,
        csv_bytes=csv_bytes, csv_filename=csv_filename,
    )

    # Include a non-revealing shape of the password in the send log so a
    # subsequent 535 tells us whether the stored value was the expected
    # 16-char App Password or something stale/corrupted. The value itself
    # is never logged.
    _pw = cfg.password or ""
    _pw_shape = (
        f"len={len(_pw)}"
        f" ws={'y' if any(c.isspace() for c in _pw) else 'n'}"
        f" alnum={'y' if _pw.isalnum() else 'n'}"
    ) if _pw else "len=0"
    logger.info(
        "[i] Sending email to %s via %s:%s (start_tls=%s, user=%s, pw_shape=%s)",
        msg["To"], cfg.server, cfg.port, cfg.start_tls, cfg.user, _pw_shape,
    )

    # Use certifi's CA bundle when available - Python's default cafile is often
    # missing on macOS (CERTIFICATE_VERIFY_FAILED).  Falls back to the system
    # default if certifi is not installed.
    try:
        import certifi
        tls_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        tls_context = ssl.create_default_context()

    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg.server,
            port=cfg.port,
            username=cfg.user,
            password=cfg.password,
            start_tls=cfg.start_tls,
            tls_context=tls_context if cfg.start_tls else None,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        logger.error(
            "[x] Failed to send email: %s (user=%s pw_shape=%s)",
            exc, cfg.user, _pw_shape,
        )
        raise

    logger.info("[i] Email sent successfully.")


def send_email(
    *,
    subject: str,
    body: str,
    to_addrs: Sequence[str] | str,
    smtp_config: Optional[SMTPConfig] = None,
    timeout_seconds: int = 30,
    csv_bytes: Optional[bytes] = None,
    csv_filename: str = "results.csv",
) -> None:
    """
    Safe synchronous wrapper around send_email_async.
    """
    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            logger.error("[x] send_email() called while an event loop is running; use send_email_async() instead.")
            raise RuntimeError("send_email() cannot be called from a running event loop. Use send_email_async().")
    except RuntimeError:
        # No running loop; proceed.
        pass

    asyncio.run(
        send_email_async(
            subject=subject,
            body=body,
            to_addrs=to_addrs,
            smtp_config=smtp_config,
            timeout_seconds=timeout_seconds,
            csv_bytes=csv_bytes,
            csv_filename=csv_filename,
        )
    )


# ---------------------------------------------------------------------
# Body template rendering with $variable$ substitution
# ---------------------------------------------------------------------

def _truncate_multivalue(values: list, limit: int = 3) -> str:
    """
    Format a multivalue (list) field.  If the list exceeds *limit* entries,
    show the first *limit* items and append a concise truncation note.

    Example (limit=3, 50 entries):
        "entry1, entry2, entry3, ... [+] Truncated 47 Entries"
    """
    if not values:
        return ""
    if len(values) <= limit:
        return ", ".join(str(v) for v in values)
    shown = ", ".join(str(v) for v in values[:limit])
    remaining = len(values) - limit
    return f"{shown}, ... [+] Truncated {remaining} Entries"


def render_email_body(template: str, row: dict, mv_truncate_limit: int = 3) -> str:
    """
    Replace ``$variable_name$`` tokens in *template* with values from *row*.

    - Scalar values are inserted as-is.
    - List (multivalue) values are formatted via ``_truncate_multivalue``.
    - Tokens that don't match any key in *row* are left untouched so the
      caller can see which tokens failed to resolve.
    """
    import re

    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key not in row:
            return match.group(0)  # leave unresolved token as-is
        val = row[key]
        if isinstance(val, list):
            return _truncate_multivalue(val, limit=mv_truncate_limit)
        if val is None or (isinstance(val, float) and val != val):
            return ""
        return str(val)

    return re.sub(r'\$([a-zA-Z_][a-zA-Z0-9_]*)\$', _replacer, template)


# ---------------------------------------------------------------------
# Backwards compatibility layer
# ---------------------------------------------------------------------
def _format_results_for_email(results: Any, max_rows: int = 200) -> str:
    """
    Best-effort formatting to keep legacy integrations stable.

    - If results looks like a pandas DataFrame, render a truncated plain-text table.
    - Otherwise stringify.
    """
    if results is None:
        return ""

    # Lazy pandas detection to avoid unnecessary import cost unless needed.
    try:
        import pandas as pd  # type: ignore
        if isinstance(results, pd.DataFrame):
            df = results
            if len(df) > max_rows:
                logger.warning("[!] Results exceed max_rows=%s; truncating for email body.", max_rows)
                df = df.head(max_rows)

            # Use a stable, readable format.
            try:
                return df.to_string(index=False)
            except Exception:
                # Fallback if df contains odd types
                return str(df.head(max_rows).to_dict(orient="records"))
    except Exception:
        # If pandas isn't available or detection fails, fall through.
        pass

    return str(results)


def email_results(*args: Any, **kwargs: Any) -> None:
    """
    Legacy entrypoint expected by QueryEngine.py: `email_results(...)`.

    Since we don't have your historical signature in this thread, this function is intentionally
    flexible and supports common calling patterns:

    Supported keyword arguments (preferred):
      - to_addrs / recipients / email_to
      - subject
      - body (optional)
      - results / df / dataframe (optional; will be formatted if provided and body missing)

    If callers pass positional args, we attempt:
      - email_results(to_addrs, subject, body_or_results)

    This function only validates SMTP config when attempting to send.
    """
    to_addrs = kwargs.get("to_addrs") or kwargs.get("recipients") or kwargs.get("email_to")
    subject = kwargs.get("subject")
    body = kwargs.get("body")

    results = kwargs.get("results")
    if results is None:
        results = kwargs.get("df")
    if results is None:
        results = kwargs.get("dataframe")

    # Positional fallback: (to_addrs, subject, body_or_results)
    if to_addrs is None and len(args) >= 1:
        to_addrs = args[0]
    if subject is None and len(args) >= 2:
        subject = args[1]
    if body is None and len(args) >= 3:
        body = args[2]

    if body is None and results is not None:
        body = _format_results_for_email(results)

    if body is None:
        body = ""

    if not to_addrs or not subject:
        logger.error("[x] email_results() missing required fields (to_addrs and subject).")
        raise ValueError("email_results() requires at least to_addrs and subject.")

    logger.info("[i] email_results() invoked (legacy shim).")
    send_email(subject=str(subject), body=str(body), to_addrs=to_addrs)

