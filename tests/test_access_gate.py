"""Access-token gate tests (weakness audit W11b, 2026-07-12).

The Jupyter model: one generated token, enforced server-side via
before_request whenever the bind address is non-loopback (which
includes every Docker install, where HOST=0.0.0.0 inside the
container) or SPEAKESQUERY_AUTH=on. Loopback dev runs and the
PyWebView desktop app stay token-free.

Covers: activation resolution, token persistence + permissions, all
four presentation forms (query param, X-SpeakesQuery-Token header,
Bearer header, cookie), the /healthz exemption (the Docker HEALTHCHECK
depends on it), fail-closed behavior on a missing token config, and
drift guards on the Dockerfile / install.sh probe targets.
"""

import os
import re
import stat
from pathlib import Path

import pytest

from desktop_app.access_gate import (
    AUTH_ENV_VAR,
    COOKIE_NAME,
    HEADER_NAME,
    TOKEN_ENV_VAR,
    load_or_create_token,
    resolve_auth_required,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Activation resolution
# ---------------------------------------------------------------------------

class TestResolveAuthRequired:
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(AUTH_ENV_VAR, raising=False)
        monkeypatch.delenv("HOST", raising=False)

    def test_loopback_defaults_off(self, monkeypatch):
        self._clean_env(monkeypatch)
        for host in ("127.0.0.1", "localhost", "::1"):
            assert resolve_auth_required(host) is False

    def test_non_loopback_defaults_on(self, monkeypatch):
        self._clean_env(monkeypatch)
        for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5"):
            assert resolve_auth_required(host) is True

    def test_host_env_var_used_when_no_arg(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv("HOST", "0.0.0.0")
        assert resolve_auth_required() is True
        monkeypatch.setenv("HOST", "127.0.0.1")
        assert resolve_auth_required() is False

    def test_explicit_on_wins_over_loopback(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv(AUTH_ENV_VAR, "on")
        assert resolve_auth_required("127.0.0.1") is True

    def test_explicit_off_wins_over_lan_bind(self, monkeypatch):
        self._clean_env(monkeypatch)
        monkeypatch.setenv(AUTH_ENV_VAR, "off")
        assert resolve_auth_required("0.0.0.0") is False

    def test_docker_shape_is_gated(self, monkeypatch):
        # The compose file sets HOST=0.0.0.0 - every Docker install
        # must come up gated. This is the W11 protection itself.
        self._clean_env(monkeypatch)
        monkeypatch.setenv("HOST", "0.0.0.0")
        assert resolve_auth_required() is True


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

class TestLoadOrCreateToken:
    def test_generates_and_persists(self, tmp_path, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        token_file = tmp_path / "access_token"
        token = load_or_create_token(token_file)
        assert len(token) >= 32
        assert token_file.read_text().strip() == token
        # Re-load returns the SAME token (stable across restarts).
        assert load_or_create_token(token_file) == token

    def test_file_mode_is_0600(self, tmp_path, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        token_file = tmp_path / "access_token"
        load_or_create_token(token_file)
        mode = stat.S_IMODE(os.stat(token_file).st_mode)
        assert mode == 0o600, f"token file mode is {oct(mode)}, expected 0600"

    def test_env_var_overrides_file(self, tmp_path, monkeypatch):
        token_file = tmp_path / "access_token"
        token_file.write_text("file-token\n")
        monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")
        assert load_or_create_token(token_file) == "env-token"

    def test_no_tmp_file_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        load_or_create_token(tmp_path / "access_token")
        strays = [p for p in tmp_path.iterdir() if p.name != "access_token"]
        assert strays == []


# ---------------------------------------------------------------------------
# Request gating (uses the shared client fixture; the gate is off by
# default under tests because nothing sets HOST - each test flips
# app.config and restores it via the fixture below)
# ---------------------------------------------------------------------------

@pytest.fixture()
def gated_client(client):
    from desktop_app.server import app
    prior_required = app.config.get("SPQ_AUTH_REQUIRED")
    prior_token = app.config.get("SPQ_ACCESS_TOKEN")
    app.config["SPQ_AUTH_REQUIRED"] = True
    app.config["SPQ_ACCESS_TOKEN"] = "test-token-abc123"
    try:
        yield client
    finally:
        app.config["SPQ_AUTH_REQUIRED"] = prior_required
        app.config["SPQ_ACCESS_TOKEN"] = prior_token


class TestGateEnforcement:
    def test_gate_off_by_default_under_tests(self, client):
        from desktop_app.server import app
        assert app.config.get("SPQ_AUTH_REQUIRED") is False
        assert client.get("/").status_code == 200

    def test_api_401_without_token(self, gated_client):
        response = gated_client.get("/api/tree")
        assert response.status_code == 401
        assert response.get_json()["status"] == "error"

    def test_root_401_is_helpful_html(self, gated_client):
        response = gated_client.get("/")
        assert response.status_code == 401
        assert b"token" in response.data.lower()

    def test_header_token_accepted(self, gated_client):
        response = gated_client.get(
            "/api/tree", headers={HEADER_NAME: "test-token-abc123"}
        )
        assert response.status_code == 200

    def test_bearer_token_accepted(self, gated_client):
        response = gated_client.get(
            "/api/tree", headers={"Authorization": "Bearer test-token-abc123"}
        )
        assert response.status_code == 200

    def test_query_param_accepted_and_promoted_to_cookie(self, gated_client):
        response = gated_client.get("/?token=test-token-abc123")
        assert response.status_code == 200
        assert COOKIE_NAME in (response.headers.get("Set-Cookie") or "")
        # The cookie now carries the session - no token in later URLs.
        assert gated_client.get("/").status_code == 200
        assert gated_client.get("/api/tree").status_code == 200

    def test_wrong_token_rejected(self, gated_client):
        assert gated_client.get(
            "/api/tree", headers={HEADER_NAME: "wrong"}
        ).status_code == 401
        assert gated_client.get("/?token=wrong").status_code == 401

    def test_healthz_exempt(self, gated_client):
        response = gated_client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json() == {"status": "ok"}

    def test_fails_closed_when_token_missing(self, gated_client):
        from desktop_app.server import app
        app.config["SPQ_ACCESS_TOKEN"] = None
        # No token configured while gated: EVERYTHING (except healthz)
        # is refused - never silently open.
        assert gated_client.get("/api/tree").status_code == 401
        assert gated_client.get("/?token=").status_code == 401
        assert gated_client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Drift guards - the healthcheck/install plumbing must target /healthz
# (probing / would 401 under the gate and mark healthy containers
# unhealthy / hang installs)
# ---------------------------------------------------------------------------

class TestProbePlumbing:
    def test_dockerfile_healthcheck_targets_healthz(self):
        dockerfile = (
            PROJECT_ROOT / "desktop_app" / "Dockerfile"
        ).read_text(encoding="utf-8")
        match = re.search(r"HEALTHCHECK[\s\S]*?CMD (.+)", dockerfile)
        assert match and "/healthz" in match.group(1), (
            "Dockerfile HEALTHCHECK must probe /healthz - the gate 401s "
            "every other path when active"
        )

    def test_install_sh_readiness_targets_healthz(self):
        install_sh = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
        assert "/healthz" in install_sh, (
            "install.sh readiness probe must target /healthz"
        )

    def test_install_sh_generates_and_prints_token(self):
        install_sh = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
        assert ".speakes-query/access_token" in install_sh
        assert "?token=" in install_sh, (
            "install.sh must print the ready-to-open ?token= URL "
            "(the Jupyter model)"
        )
