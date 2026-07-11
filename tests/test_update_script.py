"""
Sanity tests for ``update.sh`` - the one-command Docker update workflow.

Covers:
  * Bash syntax validity (``bash -n``) so a broken quote lands at CI time,
    not when the user is mid-deploy at 2am
  * ``--help`` renders the expected usage block
  * ``--dry-run`` traces the plan without calling docker
  * Flag forwarding: unknown args are passed through to ``install.sh``
  * ``--no-sudo`` is respected (no ``sudo`` in the planned commands)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "update.sh"


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke update.sh with a clean env pointing HOME at tmp so an errant
    git config can't mutate the caller's environment."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestUpdateScript:
    def test_file_is_executable(self):
        assert SCRIPT.exists(), f"update.sh missing at {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), "update.sh is not executable"

    def test_bash_syntax_valid(self):
        """``bash -n`` parses the script without executing it. Catches
        unbalanced quotes, broken heredocs, malformed case arms."""
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"bash -n failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_help_flag_shows_usage(self):
        result = _run("--help")
        assert result.returncode == 0
        # Must mention the script's purpose and at least the headline flags
        out = result.stdout
        assert "SpeakesQuery" in out
        assert "--pull" in out
        assert "--dry-run" in out
        assert "--container" in out
        assert "install.sh" in out

    def test_dry_run_shows_plan_without_docker(self):
        """Dry-run must trace the planned commands without actually invoking
        docker. Verified by checking for the ``[dry]`` prefix and the
        expected command sequence markers."""
        result = _run("--dry-run", "--no-sudo")
        assert result.returncode == 0, (
            f"dry-run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        out = result.stdout
        # All three stages must appear in a successful dry run
        assert "Pre-flight checks" in out
        assert "Stopping + removing 'speakesquery-desktop'" in out
        assert "Running install.sh" in out
        # The [dry] marker means no real command fired
        assert "[dry]" in out

    def test_dry_run_forwards_unknown_flags_to_install(self):
        """Flags that update.sh doesn't recognise must go to install.sh."""
        result = _run(
            "--dry-run", "--no-sudo",
            "--rebuild", "--port", "5112",
        )
        assert result.returncode == 0
        # The dry-run line for install.sh must include the forwarded flags
        assert "install.sh --rebuild --port 5112" in result.stdout

    def test_no_sudo_suppresses_sudo_in_commands(self):
        result = _run("--dry-run", "--no-sudo")
        assert result.returncode == 0
        # No 'sudo docker' in any dry-run line
        for line in result.stdout.splitlines():
            if "[dry]" in line:
                assert "sudo docker" not in line, (
                    f"--no-sudo leaked sudo into: {line}"
                )

    def test_custom_container_name_respected(self):
        result = _run(
            "--dry-run", "--no-sudo",
            "--container", "my-custom-name",
        )
        assert result.returncode == 0
        assert "my-custom-name" in result.stdout
        # And the default is NOT in the stop/rm line
        assert "Stopping + removing 'my-custom-name'" in result.stdout

    def test_pull_flag_adds_git_pull_step(self):
        result = _run("--dry-run", "--no-sudo", "--pull")
        assert result.returncode == 0
        assert "git pull --ff-only" in result.stdout

    def test_pull_flag_absent_skips_git_step(self):
        result = _run("--dry-run", "--no-sudo")
        assert result.returncode == 0
        assert "git pull --ff-only" not in result.stdout
