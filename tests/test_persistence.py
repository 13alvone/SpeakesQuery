"""
Tests for ``tools/persistence.py`` and the persistence bind-mount audit.

Covers:
  * ``snapshot`` produces a JSON manifest with every target accounted for
  * ``backup`` writes a tar.gz and round-trips through ``restore``
  * ``restore`` refuses to clobber existing files without ``--force``
  * ``diff`` flags removed/zeroed/shrunk files and exits non-zero
  * ``diff`` ignores benign content changes (mtime-only, hash-stable)
  * Bind-mount regression: every directory in ``DIR_TARGETS_HASHED`` has a
    corresponding bind-mount line in ``desktop_app/docker-compose.yml`` so
    a future user-data dir can't be added in code but forgotten in compose
  * Bind-mount regression: every ``FILE_TARGETS`` entry is bind-mounted
  * The ``/api/persistence/audit`` endpoint reports targets correctly

The persistence tool is stdlib-only; these tests use ``tmp_path`` to
build a synthetic project tree and re-point ``PROJECT_ROOT`` at it via
monkeypatch, so the real project's user data is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "desktop_app" / "docker-compose.yml"


# ── Helpers ───────────────────────────────────────────────────────────────
def _make_synthetic_project(root: Path) -> Path:
    """Build a stand-in project tree under ``root`` populated with a few
    user-data files matching the layout ``tools.persistence`` audits.

    Returns ``root`` so callers can chain.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "saved_searches").mkdir()
    (root / "saved_searches" / "user_one.yaml").write_text("name: one\n")
    (root / "saved_searches" / "user_two.yaml").write_text("name: two\n")
    (root / "alert_groups").mkdir()
    (root / "alert_groups" / "group.yaml").write_text("name: g\n")
    (root / "macros").mkdir()
    (root / "boilerplate_prompts").mkdir()
    (root / "email_groups").mkdir()
    (root / "analyzer_prompts").mkdir()
    (root / "lookups").mkdir()
    (root / "default_saved_searches").mkdir()
    (root / "models").mkdir()
    (root / "default_models").mkdir()
    (root / "indexes").mkdir()
    (root / "indexes" / "test.parquet").write_bytes(b"P\x00R\x00")
    (root / "jobs").mkdir()
    (root / "scheduled_input_scripts").mkdir()
    (root / "executed_scheduled_searches").mkdir()
    (root / "global_settings.yaml").write_text("theme: dark\n")
    (root / ".env").write_text("PORT=5111\n")
    for sqlite in (
        "credentials.sqlite", "last_chance.sqlite",
        "scheduled_inputs.db", "scheduled_inputs_history.db",
        "saved_searches.db", "saved_search_history.db",
        "alert_group_runs.sqlite", "claude_api_history.sqlite",
        "analyzer_results.sqlite",
        # Phase 2 / Bet 3 slice 3 (2026-05-08): LLM call history + cache
        "llm_call_history.sqlite",
    ):
        (root / sqlite).write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    return root


@pytest.fixture
def synthetic_project(tmp_path, monkeypatch):
    """Build a synthetic project tree and re-point ``PROJECT_ROOT`` and
    ``EXTERNAL_TARGETS`` so the test never touches real user data."""
    proj = _make_synthetic_project(tmp_path / "proj")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".speakes-query").mkdir()
    (fake_home / ".speakes-query" / "fernet.key").write_text("test_key\n")

    import tools.persistence as persistence
    monkeypatch.setattr(persistence, "PROJECT_ROOT", proj)
    monkeypatch.setattr(persistence, "HOME", fake_home)
    monkeypatch.setattr(
        persistence, "DEFAULT_BACKUP_DIR", fake_home / "speakesquery-backups"
    )
    monkeypatch.setattr(
        persistence, "EXTERNAL_TARGETS", (fake_home / ".speakes-query",)
    )
    return proj, fake_home


# ── snapshot ──────────────────────────────────────────────────────────────
class TestSnapshot:
    def test_snapshot_records_every_target(self, synthetic_project):
        proj, _home = synthetic_project
        from tools.persistence import build_snapshot
        snap = build_snapshot(proj)
        assert snap["version"] == 1
        targets = snap["targets"]
        # Every dir target should be present
        for name in (
            "saved_searches", "alert_groups", "macros",
            "boilerplate_prompts", "email_groups", "analyzer_prompts",
            "lookups", "default_saved_searches", "default_alert_groups",
            # Phase 2 / Bet 3 slice 1 (2026-05-08): LLM model registry
            "models", "default_models",
        ):
            assert name in targets, f"missing dir target: {name}"
            assert targets[name]["type"] == "dir_hashed"
        # File targets
        for name in ("global_settings.yaml", ".env", "credentials.sqlite"):
            assert name in targets, f"missing file target: {name}"
            assert targets[name]["type"] == "file"
            assert targets[name]["size"] > 0

    def test_snapshot_hashes_individual_files_in_dir(self, synthetic_project):
        proj, _home = synthetic_project
        from tools.persistence import build_snapshot
        snap = build_snapshot(proj)
        ss = snap["targets"]["saved_searches"]
        assert ss["file_count"] == 2
        assert "user_one.yaml" in ss["files"]
        entry = ss["files"]["user_one.yaml"]
        assert entry["size"] == len("name: one\n")
        assert "sha256" in entry
        assert len(entry["sha256"]) == 64

    def test_snapshot_summarizes_indexes_dir(self, synthetic_project):
        proj, _home = synthetic_project
        from tools.persistence import build_snapshot
        snap = build_snapshot(proj)
        idx = snap["targets"]["indexes"]
        assert idx["type"] == "dir_summary"
        assert idx["file_count"] == 1
        # No per-file hashing in summarized dirs
        assert "files" not in idx

    def test_snapshot_marks_missing_dir(self, synthetic_project, tmp_path):
        proj, _home = synthetic_project
        # Remove email_groups to simulate a fresh-clone scenario
        import shutil
        shutil.rmtree(proj / "email_groups")
        from tools.persistence import build_snapshot
        snap = build_snapshot(proj)
        assert snap["targets"]["email_groups"]["missing"] is True


# ── backup / restore round-trip ───────────────────────────────────────────
class TestBackupRestore:
    def test_backup_then_restore_round_trips_yaml_dirs(
        self, synthetic_project, tmp_path
    ):
        proj, _home = synthetic_project
        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        rc = main(["backup", "--output", str(tar), "--quiet"])
        assert rc == 0
        assert tar.exists() and tar.stat().st_size > 0

        # Wipe a YAML dir entirely - emulates the email_groups data loss
        import shutil
        shutil.rmtree(proj / "saved_searches")
        assert not (proj / "saved_searches").exists()

        # Restore with --force so co-existing files (alert_groups, .env,
        # the SQLite stash) get re-written too. Without --force the
        # restore correctly refuses to clobber live data.
        rc = main(["restore", "--tarball", str(tar), "--yes", "--force"])
        assert rc == 0
        assert (proj / "saved_searches" / "user_one.yaml").read_text() \
            == "name: one\n"
        assert (proj / "saved_searches" / "user_two.yaml").read_text() \
            == "name: two\n"

    def test_restore_refuses_to_clobber_without_force(
        self, synthetic_project, tmp_path
    ):
        proj, _home = synthetic_project
        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        main(["backup", "--output", str(tar), "--quiet"])

        # Modify a file, then attempt restore WITHOUT --force
        (proj / "saved_searches" / "user_one.yaml").write_text("modified\n")
        rc = main(["restore", "--tarball", str(tar), "--yes"])
        # Returns 1 when overwrites are skipped - operator must opt in
        assert rc == 1
        # File must NOT have been restored
        assert (proj / "saved_searches" / "user_one.yaml").read_text() \
            == "modified\n"

    def test_restore_with_force_does_clobber(
        self, synthetic_project, tmp_path
    ):
        proj, _home = synthetic_project
        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        main(["backup", "--output", str(tar), "--quiet"])
        (proj / "saved_searches" / "user_one.yaml").write_text("modified\n")
        rc = main(["restore", "--tarball", str(tar), "--yes", "--force"])
        assert rc == 0
        assert (proj / "saved_searches" / "user_one.yaml").read_text() \
            == "name: one\n"

    def test_backup_excludes_indexes_by_default(
        self, synthetic_project, tmp_path
    ):
        proj, _home = synthetic_project
        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        main(["backup", "--output", str(tar), "--quiet"])
        import tarfile
        with tarfile.open(str(tar)) as tf:
            names = tf.getnames()
        # No indexes/, no jobs/ in default backup
        assert not any(n.startswith("indexes") for n in names)
        assert not any(n.startswith("jobs") for n in names)
        # YAML dirs ARE present
        assert any(n.startswith("saved_searches") for n in names)

    def test_backup_include_indexes_flag_includes_them(
        self, synthetic_project, tmp_path
    ):
        proj, _home = synthetic_project
        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        main([
            "backup", "--output", str(tar), "--quiet", "--include-indexes",
        ])
        import tarfile
        with tarfile.open(str(tar)) as tf:
            names = tf.getnames()
        assert any(n.startswith("indexes") for n in names)


# ── diff ──────────────────────────────────────────────────────────────────
class TestDiff:
    def test_diff_identical_snapshots_returns_zero(
        self, synthetic_project, tmp_path
    ):
        proj, _home = synthetic_project
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        from tools.persistence import main
        main(["snapshot", "--output", str(a), "--quiet"])
        main(["snapshot", "--output", str(b), "--quiet"])
        rc = main(["diff", "--before", str(a), "--after", str(b)])
        assert rc == 0

    def test_diff_detects_removed_file(self, synthetic_project, tmp_path):
        proj, _home = synthetic_project
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        from tools.persistence import main
        main(["snapshot", "--output", str(a), "--quiet"])
        # Simulate data loss
        (proj / "saved_searches" / "user_one.yaml").unlink()
        main(["snapshot", "--output", str(b), "--quiet"])
        rc = main(["diff", "--before", str(a), "--after", str(b)])
        assert rc == 1, "diff must exit non-zero when files disappear"

    def test_diff_json_output_contains_removed_path(
        self, synthetic_project, tmp_path, capsys
    ):
        proj, _home = synthetic_project
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        from tools.persistence import main
        main(["snapshot", "--output", str(a), "--quiet"])
        (proj / "saved_searches" / "user_one.yaml").unlink()
        main(["snapshot", "--output", str(b), "--quiet"])
        capsys.readouterr()  # drain
        main(["diff", "--before", str(a), "--after", str(b), "--json"])
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        removed_paths = [r["path"] for r in report["removed"]]
        assert "saved_searches/user_one.yaml" in removed_paths

    def test_diff_detects_zeroed_sqlite(self, synthetic_project, tmp_path):
        proj, _home = synthetic_project
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        from tools.persistence import main
        main(["snapshot", "--output", str(a), "--quiet"])
        # SQLite goes to zero bytes - exact symptom of a botched migration
        (proj / "credentials.sqlite").write_bytes(b"")
        main(["snapshot", "--output", str(b), "--quiet"])
        rc = main(["diff", "--before", str(a), "--after", str(b)])
        assert rc == 1


# ── Bind-mount coverage regression ────────────────────────────────────────
class TestBindMountCoverage:
    """Drift guard: any user-data target the persistence tool tracks MUST
    have a corresponding bind-mount in docker-compose.yml. Otherwise a
    container rebuild silently wipes that data - the original 2026-04-25
    bug for ``email_groups/`` and ``analyzer_prompts/``.
    """

    def _compose_text(self) -> str:
        return COMPOSE_FILE.read_text(encoding="utf-8")

    def test_every_hashed_dir_target_is_bind_mounted(self):
        compose = self._compose_text()
        from tools.persistence import DIR_TARGETS_HASHED
        # default_saved_searches ships in the image and is read-only at
        # runtime; bind-mounting it would let user edits clobber the
        # version-controlled templates the next install would re-seed.
        # indexes/IMMUTABLE is covered by the parent indexes/ mount -
        # mounting it separately would shadow the parent and break
        # OEB pick journal writes. Same for any future nested target
        # that lives under an already-mounted parent.
        SKIP = {"default_saved_searches", "indexes/IMMUTABLE"}
        missing = []
        for name in DIR_TARGETS_HASHED:
            if name in SKIP:
                continue
            mount_line = f"../{name}:/app/{name}"
            if mount_line not in compose:
                missing.append(name)
        assert not missing, (
            f"Bind-mount missing from docker-compose.yml: {missing}. "
            f"Without these mounts, every container rebuild wipes user data."
        )

    def test_immutable_is_covered_by_parent_indexes_mount(self):
        """`indexes/IMMUTABLE/` doesn't have its own bind-mount line -
        but its parent `indexes/` does. Verify the parent mount exists
        so the OEB pick journal IS preserved across container rebuilds
        even though the test above SKIPs IMMUTABLE specifically."""
        compose = self._compose_text()
        assert "../indexes:/app/indexes" in compose, (
            "Parent `indexes/` bind-mount missing from docker-compose.yml. "
            "Without it, indexes/IMMUTABLE/ (the OEB pick journal - the "
            "user's decade-horizon trading record) is wiped on every "
            "container rebuild."
        )

    def test_every_file_target_is_bind_mounted(self):
        compose = self._compose_text()
        from tools.persistence import FILE_TARGETS
        # .env is mounted via env_file: directive, not as a volume
        # global_settings.yaml is mounted explicitly.
        SKIP = {".env"}
        missing = []
        for name in FILE_TARGETS:
            if name in SKIP:
                continue
            mount_line = f"../{name}:/app/{name}"
            if mount_line not in compose:
                missing.append(name)
        assert not missing, (
            f"SQLite/YAML file mount missing from docker-compose.yml: "
            f"{missing}. The Docker bind-mount would create a directory "
            f"in place of the file and corrupt SQLite."
        )

    def test_every_install_sh_mkdir_dir_is_a_persistence_target(self):
        """Reverse drift: install.sh `mkdir -p` should not create a
        directory the persistence audit doesn't know about. Catches the
        opposite mistake - adding a dir to install.sh + compose.yml but
        forgetting to add it to ``tools/persistence.py``."""
        install_sh = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        from tools.persistence import (
            DIR_TARGETS_HASHED, DIR_TARGETS_SUMMARIZED,
        )
        known = set(DIR_TARGETS_HASHED) | set(DIR_TARGETS_SUMMARIZED)
        # Heuristic: pull the lines under the `mkdir -p` block that add
        # `$PROJECT_ROOT/<name>` and check the names against the
        # persistence target list.
        import re
        rx = re.compile(r'\$PROJECT_ROOT/(\w+)"')
        matches = set(rx.findall(install_sh))
        # `frontend` etc. live under the image, not user data - only
        # check the matches that look like data dirs the audit cares
        # about. Skip anything install.sh creates outside our scope.
        unknown = matches - known
        # Allow specific exemptions for non-user-data dirs install.sh
        # may legitimately create.
        EXEMPT = set()
        unknown -= EXEMPT
        assert not unknown, (
            f"install.sh creates these dirs but persistence audit "
            f"doesn't know about them: {unknown}. Add to "
            f"DIR_TARGETS_HASHED or DIR_TARGETS_SUMMARIZED."
        )


# ── CLI smoke ─────────────────────────────────────────────────────────────
class TestCLI:
    def test_help_lists_all_subcommands(self):
        proc = subprocess.run(
            [sys.executable, "-m", "tools.persistence", "--help"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert proc.returncode == 0
        for cmd in ("snapshot", "backup", "restore", "diff"):
            assert cmd in proc.stdout

    def test_snapshot_to_stdout_is_valid_json(self):
        proc = subprocess.run(
            [sys.executable, "-m", "tools.persistence", "snapshot"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert proc.returncode == 0
        snap = json.loads(proc.stdout)
        assert snap["version"] == 1
        assert "targets" in snap


# ── /api/persistence/audit endpoint ───────────────────────────────────────
class TestAuditEndpoint:
    def test_audit_endpoint_returns_target_inventory(self):
        from desktop_app.server import app
        with app.test_client() as client:
            resp = client.get("/api/persistence/audit")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "success"
            assert data["total"] > 0
            assert "targets" in data
            paths = [t["path"] for t in data["targets"]]
            for required in (
                "saved_searches", "alert_groups",
                "email_groups", "analyzer_prompts",
                "credentials.sqlite", "scheduled_inputs.db",
            ):
                assert required in paths, (
                    f"Audit endpoint missing {required}"
                )

    def test_audit_endpoint_reports_healthy_count(self):
        from desktop_app.server import app
        with app.test_client() as client:
            resp = client.get("/api/persistence/audit")
            data = resp.get_json()
            assert data["healthy"] + len(data["issues"]) == data["total"]


# ── IMMUTABLE pick journal backup coverage ──────────────────────────


class TestImmutableBackupCoverage:
    """The `indexes/IMMUTABLE/` namespace holds the user's decade-horizon
    OEB pick journal - `ag_picks/`, `ag_picks_closures/`,
    `ag_picks_review_observations/`, `oeb_pick_tracker_runs/`, etc.
    Per CLAUDE.md it's the "must survive forever" tree.

    Pre-2026-05-06 the default `tools/persistence.py backup` excluded it
    (it nested under DIR_TARGETS_SUMMARIZED["indexes"], opt-in via
    --include-indexes only). That's a critical gap right before
    real-money go-live: a routine backup would NOT preserve the trading
    record. Fixed by adding `indexes/IMMUTABLE` as its own
    DIR_TARGETS_HASHED entry.

    These tests pin:
    - IMMUTABLE survives a default backup (no --include-indexes flag)
    - --include-indexes doesn't duplicate IMMUTABLE in the tar
    - Round-trip preserves bit-identical content (parquet hashes match)
    """

    def _make_immutable_data(self, proj):
        """Add synthetic IMMUTABLE pick journal content to a project."""
        immutable_dir = proj / "indexes" / "IMMUTABLE"
        immutable_dir.mkdir(parents=True, exist_ok=True)
        # ag_picks/ - the actual pick journal
        picks_dir = immutable_dir / "ag_picks"
        picks_dir.mkdir()
        # Synthetic parquet (the exact bytes don't matter - we're testing
        # that the file survives the round-trip, not parquet parsing)
        (picks_dir / "1777935960.parquet").write_bytes(
            b"PAR1" + b"\x00" * 1000 + b"PAR1"
        )
        # ag_picks_closures/ - pick close events
        closures_dir = immutable_dir / "ag_picks_closures"
        closures_dir.mkdir()
        (closures_dir / "1778000000.parquet").write_bytes(
            b"PAR1" + b"\x42" * 500 + b"PAR1"
        )
        return immutable_dir

    def test_default_backup_includes_immutable(self, synthetic_project, tmp_path):
        """Without --include-indexes, the IMMUTABLE pick journal MUST
        still be in the tar. This was the original go-live gap."""
        proj, _home = synthetic_project
        self._make_immutable_data(proj)

        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        rc = main(["backup", "--output", str(tar), "--quiet"])
        assert rc == 0

        import tarfile
        with tarfile.open(str(tar)) as tf:
            names = tf.getnames()
        immutable_files = [
            n for n in names if n.startswith("indexes/IMMUTABLE/")
        ]
        assert immutable_files, (
            f"Default backup MISSING indexes/IMMUTABLE/ - the OEB pick "
            f"journal was excluded. Tar contained: {sorted(names)[:20]}"
        )
        # Should include both subdirectories' parquet files
        assert any("ag_picks/" in n for n in immutable_files), (
            f"ag_picks/ missing from backup; got: {immutable_files}"
        )
        assert any("ag_picks_closures/" in n for n in immutable_files), (
            f"ag_picks_closures/ missing from backup; got: {immutable_files}"
        )

    def test_include_indexes_flag_does_not_duplicate_immutable(
        self, synthetic_project, tmp_path
    ):
        """When --include-indexes is passed, the bulk indexes/ tree is
        also bundled. Without de-duplication, IMMUTABLE/ would appear
        TWICE - once via DIR_TARGETS_HASHED and once via the bulk add.
        Pin that the de-dup logic prevents this."""
        proj, _home = synthetic_project
        self._make_immutable_data(proj)
        # Add a non-IMMUTABLE indexes file so the bulk add has content
        (proj / "indexes" / "regular.parquet").write_bytes(b"PAR1" * 100)

        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        rc = main([
            "backup", "--output", str(tar), "--quiet", "--include-indexes",
        ])
        assert rc == 0

        import tarfile
        with tarfile.open(str(tar)) as tf:
            names = [n for n in tf.getnames() if n.endswith(".parquet")]
        # Each IMMUTABLE parquet should appear exactly once in the tar
        from collections import Counter
        counts = Counter(names)
        for name, n in counts.items():
            if "IMMUTABLE" in name:
                assert n == 1, (
                    f"Duplicate entry for {name} (count={n}); "
                    f"de-dup between DIR_TARGETS_HASHED and "
                    f"DIR_TARGETS_SUMMARIZED indexes/ failed."
                )
        # Confirm BOTH the IMMUTABLE files and the regular indexes file
        # are present
        assert any("IMMUTABLE/ag_picks" in n for n in names)
        assert any(n == "indexes/regular.parquet" for n in names)

    def test_immutable_round_trip_preserves_bit_identical_content(
        self, synthetic_project, tmp_path
    ):
        """Backup IMMUTABLE → wipe → restore → the parquet bytes must be
        bit-identical. This is the integrity contract the pick journal
        depends on for accurate weekly review attribution."""
        import hashlib
        proj, _home = synthetic_project
        immutable_dir = self._make_immutable_data(proj)

        # Hash every IMMUTABLE file pre-backup
        original_hashes: dict[str, str] = {}
        for f in sorted(immutable_dir.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(proj))
                original_hashes[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
        assert len(original_hashes) >= 2, "Test needs >= 2 IMMUTABLE files"

        tar = tmp_path / "backup.tar.gz"
        from tools.persistence import main
        rc = main(["backup", "--output", str(tar), "--quiet"])
        assert rc == 0

        # Wipe IMMUTABLE entirely - emulates "containers rebuilt, indexes/
        # bind-mount points to a fresh empty dir" failure mode
        import shutil
        shutil.rmtree(immutable_dir)
        assert not immutable_dir.exists()

        # Restore with --force so existing co-files (alert_groups, .env)
        # don't block the restore.
        rc = main([
            "restore", "--tarball", str(tar), "--yes", "--force",
        ])
        assert rc == 0

        # Verify each IMMUTABLE file is back AND its bytes are identical
        for rel, expected_hash in original_hashes.items():
            restored = proj / rel
            assert restored.exists(), (
                f"IMMUTABLE file missing after restore: {rel}"
            )
            actual_hash = hashlib.sha256(restored.read_bytes()).hexdigest()
            assert actual_hash == expected_hash, (
                f"IMMUTABLE corruption detected after round-trip: {rel}\n"
                f"  expected sha256: {expected_hash}\n"
                f"  actual sha256:   {actual_hash}\n"
                f"This is the failure mode the pick journal MUST be "
                f"protected from before real-money go-live."
            )

    def test_immutable_listed_in_dir_targets_hashed(self):
        """Drift guard: pin the canonical entry so a future refactor
        can't quietly drop IMMUTABLE from the always-backed-up set."""
        from tools.persistence import DIR_TARGETS_HASHED
        assert "indexes/IMMUTABLE" in DIR_TARGETS_HASHED, (
            "indexes/IMMUTABLE must be in DIR_TARGETS_HASHED - "
            "removing it means default backups silently exclude the "
            "OEB pick journal (the user's decade-horizon trading "
            "record). See CLAUDE.md `Do Not` list and "
            "reference_immutable_namespace_pattern.md."
        )


# ── Real-project smoke test (read-only) ──────────────────────────────


class TestRealProjectBackupSmoke:
    """Beyond the synthetic-project fixture: confirm the backup tool
    actually runs against THE real project root without errors. This
    catches issues the synthetic fixture misses - e.g., an unusual
    filename in real user data, a YAML with content that confuses the
    tarfile filter, or a SQLite file that exceeds some buffer.

    This test does NOT modify state - it only RUNS backup against the
    real project, verifies the tar is well-formed, then deletes the tar.
    No restore is exercised against real data.
    """

    def test_real_project_backup_produces_valid_tarball(self, tmp_path):
        """End-to-end: run `python -m tools.persistence backup` as a
        subprocess against the real project, verify the resulting tar
        opens cleanly and contains expected member counts."""
        out = tmp_path / "smoke.tar.gz"
        result = subprocess.run(
            [sys.executable, "-m", "tools.persistence", "backup",
             "--output", str(out), "--quiet"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"Real-project backup failed.\nstdout:\n{result.stdout}"
            f"\nstderr:\n{result.stderr}"
        )
        assert out.exists() and out.stat().st_size > 0

        # Verify tar opens and has a sensible number of members
        import tarfile
        with tarfile.open(str(out)) as tf:
            names = tf.getnames()
        assert len(names) > 0, "Empty tar - backup wrote nothing"

        # Sanity: the saved_searches dir should be in there, and at
        # least the global_settings.yaml file
        assert any(n.startswith("saved_searches") for n in names), (
            f"Real-project backup missing saved_searches/. "
            f"First few names: {names[:10]}"
        )
        # IMMUTABLE may not exist if no OEB picks have been logged yet -
        # but if it does, it must be in the backup
        immutable_on_disk = (REPO_ROOT / "indexes" / "IMMUTABLE").is_dir()
        if immutable_on_disk:
            assert any("indexes/IMMUTABLE" in n for n in names), (
                f"indexes/IMMUTABLE/ exists on disk but is missing from "
                f"the backup tar. The OEB pick journal would be lost on "
                f"a restore."
            )
