#!/usr/bin/env python3
"""
Regression tests for Docker bind-mount parity on project-root SQLite
files.

**The incident that motivated these tests (2026-04-21):**

A user reported the Settings → Claude API History section "not capturing
anything" even though ``indexes/logs/claude_api/*.parquet`` had rows.
Root cause: ``claude_api_history.sqlite`` was referenced by code
(``analyzers/claude_history_store.py``) but MISSING from:

  * ``install.sh``'s ``touch`` list (so Docker created it as a directory
    on first `up`, corrupting any subsequent file-mount expectation).
  * ``desktop_app/docker-compose.yml``'s ``volumes`` list (so the file
    lived on the ephemeral container FS - wiped on every ``./update.sh``).

Any time a SQLite file is introduced at the project root, BOTH must be
updated or history is silently destroyed across restarts. These tests
walk the code, find every project-root SQLite path, and confirm each is
correctly wired into both places.

Also checked: a startup sanity check in ``ClaudeHistoryStore._init_db``
that raises loudly when the sqlite path turns out to be a directory (the
fingerprint of "Docker auto-created the bind-mount target").
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =====================================================================
# Part 1: Identify every project-root SQLite file referenced in code
# =====================================================================

# Map of project-root sqlite filename → short human label. If a new
# SQLite file lands here, add it with a hint so the mount/touch tests
# automatically start enforcing its presence.
PROJECT_ROOT_SQLITE_FILES = {
    "credentials.sqlite":         "Fernet-encrypted API-key vault",
    "last_chance.sqlite":         "30-day soft-delete recovery",
    "scheduled_inputs.db":        "scheduled ingestion tasks",
    "scheduled_inputs_history.db": "ingestion run history",
    "saved_searches.db":          "legacy saved-searches (YAML stores are current)",
    "saved_search_history.db":    "saved-search execution history",
    "alert_group_runs.sqlite":    "AG dispatch audit trail",
    "claude_api_history.sqlite":  "full Claude request+response forensic audit",
    "analyzer_results.sqlite":    "analyzer results + daily budget + batch_requests pending state",
    "llm_call_history.sqlite":    "provider-agnostic LLM history + content-hash cache (cache hits cost real money)",
    "notebook_cache.sqlite":      "notebook reactive-cell result cache",
}


class TestProjectRootSqliteCoverage:
    """Guard: every sqlite filename referenced from a project-root
    `_PROJECT_ROOT / "*.sqlite"` expression in code must appear in
    ``PROJECT_ROOT_SQLITE_FILES`` - so the mount/touch tests below
    automatically cover it."""

    def test_code_references_are_in_registry(self):
        """Scan code for ``_PROJECT_ROOT / "<something>.sqlite|.db"`` and
        fail if anything new shows up without being added here.

        This is a "paper-trail" test: any new root-level SQLite must be
        registered + mounted + touched in one go, or this fails on CI."""
        pattern = re.compile(
            r'_PROJECT_ROOT\s*/\s*["\']([\w\-]+\.(?:sqlite|db))["\']'
        )
        found = set()
        for py in PROJECT_ROOT.rglob("*.py"):
            s = str(py)
            if (
                "/.speakesQueryDevEnv/" in s
                or "/env/" in s
                or "/.venv/" in s
                or "/.claude/" in s
                or "/site-packages/" in s
                or "/tests/" in s
            ):
                continue
            try:
                text = py.read_text(errors="ignore")
            except OSError:
                continue
            for m in pattern.finditer(text):
                found.add(m.group(1))
        # Allow additional references outside this pattern (absolute
        # paths elsewhere); we only fail on *new* files that show up via
        # this specific idiom and aren't registered.
        unregistered = found - set(PROJECT_ROOT_SQLITE_FILES)
        assert not unregistered, (
            "These project-root SQLite files are referenced in code but "
            "NOT listed in PROJECT_ROOT_SQLITE_FILES in this test file. "
            "Add them + update install.sh's touch list + docker-compose "
            f"volumes: {sorted(unregistered)}"
        )


# =====================================================================
# Part 2: install.sh touches every registered SQLite
# =====================================================================

class TestInstallShTouches:

    def test_install_sh_touches_every_registered_sqlite(self):
        install_sh = (PROJECT_ROOT / "install.sh").read_text()
        missing = []
        for fname in PROJECT_ROOT_SQLITE_FILES:
            # install.sh uses "$PROJECT_ROOT/<fname>" in its touch list
            if f'$PROJECT_ROOT/{fname}' not in install_sh:
                missing.append(fname)
        assert not missing, (
            "install.sh is missing these SQLite files from its touch "
            "list:\n  " + "\n  ".join(missing) +
            "\n\nWithout touching the file before `docker compose up`, "
            "Docker will create the bind-mount target as a directory "
            "and subsequent sqlite3.connect() will fail silently."
        )


# =====================================================================
# Part 3: docker-compose.yml mounts every registered SQLite
# =====================================================================

class TestDockerComposeMounts:

    def test_compose_mounts_every_registered_sqlite(self):
        compose_path = PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
        spec = yaml.safe_load(compose_path.read_text())
        svc = spec["services"]["speakesquery-desktop"]
        volumes = svc.get("volumes", [])
        # Extract target paths (right side of the colon)
        targets = set()
        for v in volumes:
            if ":" in v:
                _, target = v.rsplit(":", 1)
                targets.add(target.strip())
        missing = []
        for fname in PROJECT_ROOT_SQLITE_FILES:
            if f"/app/{fname}" not in targets:
                missing.append(fname)
        assert not missing, (
            "desktop_app/docker-compose.yml is missing these SQLite "
            "files from its volumes list:\n  " + "\n  ".join(missing) +
            "\n\nThese files live on the ephemeral container FS and get "
            "wiped on every restart - including every `./update.sh`. "
            "Each must be bind-mounted with `../<file>:/app/<file>`."
        )


# =====================================================================
# Part 4: Startup sanity check - bind-mount-as-directory detection
# =====================================================================

class TestBindMountAsDirectoryGuard:
    """``ClaudeHistoryStore._init_db`` must raise with an actionable
    message when the db path exists as a directory (Docker auto-created
    the bind-mount target because the host file was missing)."""

    def test_raises_on_directory_path(self, tmp_path):
        from analyzers.claude_history_store import ClaudeHistoryStore

        # Simulate the Docker failure mode: the "sqlite" path is a
        # directory, not a file.
        bad_path = tmp_path / "would_be_sqlite"
        bad_path.mkdir()

        with pytest.raises(RuntimeError) as exc_info:
            ClaudeHistoryStore(db_path=str(bad_path))
        msg = str(exc_info.value)
        assert "DIRECTORY" in msg, (
            "Error message should call out the directory-vs-file "
            "confusion; got: " + msg
        )
        # Should name both the fix steps + the supporting config files
        for hint in ("rm -rf", "touch", "install.sh", "docker-compose"):
            assert hint in msg, (
                f"Error message missing helpful hint '{hint}': {msg}"
            )

    def test_accepts_nonexistent_path(self, tmp_path):
        """When the path doesn't exist yet (fresh install), creation
        proceeds normally."""
        from analyzers.claude_history_store import ClaudeHistoryStore
        fresh = tmp_path / "fresh.sqlite"
        assert not fresh.exists()
        store = ClaudeHistoryStore(db_path=str(fresh))
        assert fresh.exists() and fresh.is_file()
        # Sanity: table created
        import sqlite3
        with sqlite3.connect(str(fresh)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='claude_api_calls'"
            )
            assert cur.fetchone() is not None


# =====================================================================
# Part 5: End-to-end - fresh install creates all the right files
# =====================================================================

class TestFreshInstallFilePresence:
    """On a freshly-checked-out repo, running install.sh's touch list
    produces all the files the docker-compose mounts reference. The
    pairing is the actual invariant that protects production - if either
    side drifts, we lose data."""

    def test_touch_and_mount_sets_are_identical_for_root_sqlite(self):
        # Parse install.sh's touch block
        install_sh = (PROJECT_ROOT / "install.sh").read_text()
        touched_root = set(
            re.findall(r'\$PROJECT_ROOT/([\w\-]+\.(?:sqlite|db))', install_sh)
        )

        # Parse docker-compose volumes
        compose_path = PROJECT_ROOT / "desktop_app" / "docker-compose.yml"
        spec = yaml.safe_load(compose_path.read_text())
        svc = spec["services"]["speakesquery-desktop"]
        mounted_root = set()
        for v in svc.get("volumes", []):
            # Look for ../<file>.sqlite:/app/<file>.sqlite pattern
            m = re.match(
                r'\.\./([\w\-]+\.(?:sqlite|db)):/app/([\w\-]+\.(?:sqlite|db))',
                v,
            )
            if m and m.group(1) == m.group(2):
                mounted_root.add(m.group(1))

        # Registered set is the source of truth. install.sh touch set and
        # docker-compose mount set must each be a SUPERSET of the
        # registered set.
        registered = set(PROJECT_ROOT_SQLITE_FILES)
        missing_touch = registered - touched_root
        missing_mount = registered - mounted_root
        assert not missing_touch, (
            f"install.sh missing touch for: {sorted(missing_touch)}"
        )
        assert not missing_mount, (
            f"docker-compose missing mount for: {sorted(missing_mount)}"
        )

        # Also flag extras that are touched/mounted but not registered
        # - those are likely legacy files worth cleaning up.
        extras_touch = touched_root - registered
        extras_mount = mounted_root - registered
        assert extras_touch == extras_mount, (
            "install.sh touch and docker-compose mount disagree on "
            f"non-registered files: touch-only={sorted(extras_touch - extras_mount)}, "
            f"mount-only={sorted(extras_mount - extras_touch)}"
        )
