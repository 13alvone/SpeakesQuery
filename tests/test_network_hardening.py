"""Network-exposure hardening drift guards (weakness audit W11a + W15, 2026-07-12).

A default install must never expose the unauthenticated app (credential
vault UI + unrestricted script tier = code execution) beyond the machine
it runs on. These tests pin the three load-bearing facts:

1. The Docker host port mapping binds to 127.0.0.1 unless BIND_ADDR is
   explicitly set (the LAN opt-in).
2. The image ships a HEALTHCHECK so a hung/crashed server is visible to
   Docker and to install.sh readiness detection.
3. The README's manual `docker run` example does not undo the hardening
   that compose provides.

If any of these fail after an edit to docker-compose.yml / Dockerfile /
README.md, the edit re-opened the reputation-ending-headline shape
described in the 2026-07-11 deep audit. Do not weaken these assertions;
LAN exposure belongs behind the explicit BIND_ADDR opt-in plus the
docs/lang/06_application_guide.md "Network Exposure" walkthrough.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "desktop_app" / "Dockerfile"
README_PATH = PROJECT_ROOT / "README.md"


class TestComposeLoopbackDefault:
    def test_ports_mapping_defaults_to_loopback(self):
        compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
        assert '"${BIND_ADDR:-127.0.0.1}:${PORT:-5111}:5111"' in compose_text, (
            "docker-compose.yml host port mapping must bind to 127.0.0.1 by "
            "default with BIND_ADDR as the explicit LAN opt-in (W11a)"
        )

    def test_no_bare_port_mapping_remains(self):
        compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
        # A mapping like "5111:5111" or "${PORT:-5111}:5111" with no leading
        # host address binds to 0.0.0.0. None may exist in the compose file.
        for line in compose_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            mapping = stripped[2:].strip().strip('"').strip("'")
            if re.match(r"^(\$\{PORT[^}]*\}|\d+):\d+$", mapping):
                raise AssertionError(
                    f"Bare host port mapping found in docker-compose.yml: "
                    f"{mapping!r} binds to all interfaces; prefix it with "
                    f"${{BIND_ADDR:-127.0.0.1}}:"
                )

    def test_lan_opt_in_documented_in_env_example(self):
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "BIND_ADDR" in env_example, (
            ".env.example must document the BIND_ADDR LAN opt-in so operators "
            "find the sanctioned path instead of editing compose"
        )


class TestDockerHealthcheck:
    def test_dockerfile_ships_healthcheck(self):
        dockerfile_text = DOCKERFILE_PATH.read_text(encoding="utf-8")
        assert "HEALTHCHECK" in dockerfile_text, (
            "desktop_app/Dockerfile must keep its HEALTHCHECK (W15: hung "
            "server visibility + install.sh readiness detection)"
        )

    def test_compose_keeps_restart_policy(self):
        compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
        assert "restart: unless-stopped" in compose_text, (
            "docker-compose.yml must keep restart: unless-stopped (W15 "
            "robustness-not-redundancy decision)"
        )


class TestReadmeDoesNotUndoHardening:
    def test_manual_docker_run_example_binds_loopback(self):
        readme_text = README_PATH.read_text(encoding="utf-8")
        for match in re.finditer(r"-p\s+([\w.${}:-]+:\d+)", readme_text):
            mapping = match.group(1)
            assert mapping.startswith("127.0.0.1:") or mapping.startswith(
                "${BIND_ADDR"
            ), (
                f"README docker run example publishes {mapping!r} on all "
                f"interfaces; bind it to 127.0.0.1 like the compose default"
            )

    def test_readme_states_localhost_only_default(self):
        readme_text = README_PATH.read_text(encoding="utf-8")
        assert "Localhost-only by default" in readme_text, (
            "README must keep the loud localhost-only-by-default callout in "
            "the Docker section (W11a docs requirement)"
        )
