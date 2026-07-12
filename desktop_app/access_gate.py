"""Access-token gate for the Flask server (weakness audit W11b, 2026-07-12).

The Jupyter model, minimum viable: a single generated token, checked
server-side on every request when the gate is active. This is NOT a
multi-user auth system (admin/user roles are deliberately out of scope
here) - it exists so that a server reachable beyond loopback is never
reachable UNAUTHENTICATED. The app contains a credential vault UI and
an opt-in unrestricted script tier; "reachable" must never equal
"code execution for anyone on the network".

Activation rules (resolve_auth_required):
- ``SPEAKESQUERY_AUTH=on``  (or 1/true/required)  -> always gated
- ``SPEAKESQUERY_AUTH=off`` (or 0/false/disabled) -> never gated
  (for operators who front the app with a reverse proxy that does its
  own auth - an eyes-open, explicit choice)
- otherwise: gated exactly when the bind address is not loopback.
  Bare-metal default (HOST=127.0.0.1) and the PyWebView desktop app
  stay frictionless; Docker (HOST=0.0.0.0 inside the container) and
  any deliberate LAN bind are gated. Combined with the compose file's
  127.0.0.1 host port mapping this is defense in depth: two
  independent settings must BOTH be loosened before an unauthenticated
  LAN request can reach a route.

Token: ``~/.speakes-query/access_token`` (same out-of-repo directory as
the credential-vault master key; 0600). Generated on first use by
either install.sh or this module - whichever runs first - so the URL
printed at install and the token the server checks always agree.
``SPEAKESQUERY_ACCESS_TOKEN`` overrides the file when set (containers
with read-only secret mounts).

Presenting the token (any one of):
- ``?token=<tok>`` query parameter - accepted on any request; on
  success the response also sets the session cookie so the browser
  address bar can be cleaned by hand and navigation keeps working.
- ``X-SpeakesQuery-Token: <tok>`` header (curl / scripts)
- ``Authorization: Bearer <tok>`` header
- the session cookie set after any of the above

``/healthz`` is the single exempt path: the Docker HEALTHCHECK and
install.sh readiness probe must work without a token (it leaks nothing
but liveness).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from flask import g, jsonify, request

logger = logging.getLogger(__name__)

COOKIE_NAME = "spq_access"
HEADER_NAME = "X-SpeakesQuery-Token"
EXEMPT_PATHS = frozenset({"/healthz"})
TOKEN_ENV_VAR = "SPEAKESQUERY_ACCESS_TOKEN"
AUTH_ENV_VAR = "SPEAKESQUERY_AUTH"
DEFAULT_TOKEN_PATH = "~/.speakes-query/access_token"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_UNAUTHORIZED_HTML = """<!doctype html>
<html><head><title>SpeakesQuery - token required</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40em; margin: 4em auto;">
<h2>Access token required</h2>
<p>This SpeakesQuery server is bound beyond localhost, so requests must
carry the access token (printed by <code>./install.sh</code>).</p>
<p>Open the app as:</p>
<pre>http://&lt;this host&gt;:&lt;port&gt;/?token=&lt;your token&gt;</pre>
<p>The token lives on the server machine at
<code>~/.speakes-query/access_token</code>.</p>
</body></html>"""


def resolve_auth_required(host: "str | None" = None) -> bool:
    """Decide whether the token gate is active.

    Explicit ``SPEAKESQUERY_AUTH`` wins; otherwise gate iff the bind
    address is not loopback.
    """
    explicit = os.environ.get(AUTH_ENV_VAR, "").strip().lower()
    if explicit in ("off", "0", "false", "disabled", "no"):
        return False
    if explicit in ("on", "1", "true", "required", "yes"):
        return True
    if host is None:
        host = os.environ.get("HOST", "127.0.0.1")
    return host.strip() not in _LOOPBACK_HOSTS


def load_or_create_token(token_path: "str | Path | None" = None) -> str:
    """Return the access token, generating + persisting one if absent.

    ``SPEAKESQUERY_ACCESS_TOKEN`` (env) wins over the file. The file is
    created 0600 in the same out-of-repo directory as the credential
    vault master key.
    """
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token

    path = Path(token_path or DEFAULT_TOKEN_PATH).expanduser()
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Small single-line secret: write via a same-directory temp file +
    # os.replace so a crash can't leave a half-written token, and the
    # 0600 mode is set BEFORE the file becomes visible at its real name.
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    logger.info("[i] Access token generated at %s", path)
    return token


def _presented_token() -> "str | None":
    """Extract the token from the request, wherever it was presented."""
    query_token = request.args.get("token", "").strip()
    if query_token:
        return query_token
    header_token = request.headers.get(HEADER_NAME, "").strip()
    if header_token:
        return header_token
    bearer = request.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        return bearer[len("Bearer "):].strip()
    cookie_token = request.cookies.get(COOKIE_NAME, "").strip()
    if cookie_token:
        return cookie_token
    return None


def install_gate(app, token_path: "str | Path | None" = None) -> None:
    """Register the gate on ``app``.

    Reads activation + token ONCE at install time into ``app.config``
    (keys ``SPQ_AUTH_REQUIRED`` / ``SPQ_ACCESS_TOKEN``) so tests can
    flip them per-case without re-importing the server module. The
    token is only materialized on disk when the gate is active - a
    loopback dev install writes nothing.
    """
    auth_required = resolve_auth_required()
    app.config.setdefault("SPQ_AUTH_REQUIRED", auth_required)
    if auth_required:
        app.config.setdefault("SPQ_ACCESS_TOKEN", load_or_create_token(token_path))
        logger.info(
            "[i] Access-token gate ACTIVE (bind is non-loopback or "
            "%s=on). Token file: %s (override: %s env var).",
            AUTH_ENV_VAR,
            Path(token_path or DEFAULT_TOKEN_PATH).expanduser(),
            TOKEN_ENV_VAR,
        )
    else:
        app.config.setdefault("SPQ_ACCESS_TOKEN", None)

    @app.before_request
    def _access_gate():  # noqa: F811 - Flask hook, name is cosmetic
        if not app.config.get("SPQ_AUTH_REQUIRED"):
            return None
        if request.path in EXEMPT_PATHS:
            return None
        expected = app.config.get("SPQ_ACCESS_TOKEN") or ""
        if not expected:
            # Misconfiguration (gate on, no token). Fail CLOSED: an
            # empty expected token must never compare equal.
            logger.warning("[!] Access gate active but no token configured")
            return jsonify({"status": "error", "message": "access token required"}), 401
        presented = _presented_token()
        if presented and hmac.compare_digest(presented, expected):
            # Remember token arrival via query param so after_request
            # can promote it to the session cookie.
            if request.args.get("token"):
                g.spq_set_access_cookie = True
            return None
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "access token required"}), 401
        return _UNAUTHORIZED_HTML, 401, {"Content-Type": "text/html; charset=utf-8"}

    @app.after_request
    def _promote_token_to_cookie(response):
        if getattr(g, "spq_set_access_cookie", False):
            response.set_cookie(
                COOKIE_NAME,
                app.config.get("SPQ_ACCESS_TOKEN") or "",
                httponly=True,
                samesite="Lax",
                # No `secure=True`: the app serves plain HTTP on
                # localhost/LAN today. TLS lands with the Drop #1
                # security work; revisit then.
            )
        return response

    @app.route("/healthz")
    def healthz():
        """Liveness probe - exempt from the token gate by design.

        Used by the Docker HEALTHCHECK and install.sh readiness loop.
        Returns liveness only; no version, no config, no data.
        """
        return jsonify({"status": "ok"})
