#!/usr/bin/env python3
"""
Pins for two 2026-04-30 fixes:

1. AlertGroupStore now seeds defaults from default_alert_groups/ into
   alert_groups/ on initialize() - never overwriting user edits. The
   alert_groups/*.yaml tree is gitignored so `git pull` cannot clobber
   UI-customised AGs (the original data-loss bug).

2. The Alert Groups list page Enable/Disable button now shows the NEXT
   ACTION ("Enable" when disabled, "Disable" when enabled) instead of
   the CURRENT STATE - a click visibly changes the label, removing the
   "click does nothing" UX bug.

Drift-guard structure (mirrors test_persistence.py + tests/test_wave5_*):
- Layer 1: store unit tests (seed copies missing, never overwrites,
  idempotent across re-runs, handles missing defaults dir gracefully)
- Layer 2: install_default + list_defaults helper tests
- Layer 3: file-system drift guards (every default present in tracked
  default_alert_groups/, .gitignore actually ignores user yamls,
  install.sh creates default_alert_groups/, persistence.py knows
  about the new dir, docker-compose has the bind mount)
- Layer 4: HTML contract tests (button label is action-oriented, click
  handler still calls toggleAlertGroup with the right argument)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from alert_group_store import AlertGroupStore, DEFAULTS_DIR
import alert_group_store as ags_mod


# ===========================================================================
# Layer 1 - Seed function unit tests
# ===========================================================================


@pytest.fixture
def temp_groups_dir(tmp_path):
    """Provide a clean temp alert_groups/ for the store under test."""
    groups = tmp_path / "alert_groups"
    groups.mkdir()
    store = AlertGroupStore()
    store._dir = groups
    yield store, groups


class TestSeedDefaults:
    """``_seed_defaults`` copies missing yamls and never overwrites."""

    def test_empty_target_seeds_all_defaults(self, temp_groups_dir):
        store, groups = temp_groups_dir
        store._seed_defaults()
        seeded = sorted(p.name for p in groups.glob("*.yaml"))
        defaults = sorted(p.name for p in DEFAULTS_DIR.glob("*.yaml"))
        assert seeded == defaults, (
            "Every default yaml must land in alert_groups/ on a fresh init"
        )

    def test_idempotent_re_run(self, temp_groups_dir):
        store, groups = temp_groups_dir
        store._seed_defaults()
        first_count = len(list(groups.glob("*.yaml")))
        store._seed_defaults()  # second run should be a no-op
        assert len(list(groups.glob("*.yaml"))) == first_count

    def test_user_edit_preserved_across_seed(self, temp_groups_dir):
        """The whole point - UI customisations must survive a re-seed."""
        store, groups = temp_groups_dir
        store._seed_defaults()

        # Pick any seeded yaml and add a "user customisation" marker
        target = next(groups.glob("*.yaml"))
        original = target.read_text()
        marker = "\n# USER_CUSTOMIZATION_DO_NOT_OVERWRITE\n"
        target.write_text(original + marker)

        store._seed_defaults()  # must be a no-op for this file

        assert marker in target.read_text(), (
            "User edit was overwritten by re-seed - the silent-data-loss "
            "regression has returned"
        )

    def test_deleted_default_is_restored(self, temp_groups_dir):
        store, groups = temp_groups_dir
        store._seed_defaults()
        deleted = next(groups.glob("*.yaml"))
        deleted_name = deleted.name
        deleted.unlink()
        store._seed_defaults()
        assert (groups / deleted_name).exists(), (
            "Deleting a default and re-seeding must restore it"
        )

    def test_missing_defaults_dir_logs_and_returns(self, temp_groups_dir):
        """A missing defaults dir is a deployment misconfiguration but must
        not raise - the store still has to initialise so the UI loads."""
        store, _ = temp_groups_dir
        store._defaults_dir = Path("/nonexistent/defaults")
        store._seed_defaults()  # should not raise


class TestInstallDefault:
    """``install_default`` is the on-demand version used by the Feeder
    Health UI to pull a single default the user previously deleted."""

    def test_returns_false_when_target_exists_no_overwrite(self, temp_groups_dir):
        store, groups = temp_groups_dir
        store._seed_defaults()
        any_name = next(groups.glob("*.yaml")).stem
        assert store.install_default(any_name, overwrite=False) is False

    def test_returns_false_for_missing_default(self, temp_groups_dir):
        store, _ = temp_groups_dir
        assert store.install_default("not_a_real_default") is False

    def test_overwrite_true_replaces_existing(self, temp_groups_dir):
        store, groups = temp_groups_dir
        store._seed_defaults()
        target = next(groups.glob("*.yaml"))
        target.write_text("CUSTOMISED\n")
        result = store.install_default(target.stem, overwrite=True)
        assert result is True
        assert target.read_text() != "CUSTOMISED\n"

    def test_install_after_delete_restores(self, temp_groups_dir):
        store, groups = temp_groups_dir
        store._seed_defaults()
        target = next(groups.glob("*.yaml"))
        name = target.stem
        target.unlink()
        assert store.install_default(name) is True
        assert (groups / f"{name}.yaml").exists()

    def test_empty_name_returns_false(self, temp_groups_dir):
        store, _ = temp_groups_dir
        assert store.install_default("") is False
        assert store.install_default(None) is False  # type: ignore[arg-type]


class TestListDefaults:
    def test_returns_all_default_names(self, temp_groups_dir):
        store, _ = temp_groups_dir
        listed = store.list_defaults()
        on_disk = sorted(p.stem for p in DEFAULTS_DIR.glob("*.yaml"))
        assert listed == on_disk

    def test_empty_when_defaults_dir_missing(self, temp_groups_dir):
        store, _ = temp_groups_dir
        store._defaults_dir = Path("/nonexistent")
        assert store.list_defaults() == []


# ===========================================================================
# Layer 2 - File-system + config drift guards
# ===========================================================================


class TestDefaultAlertGroupsTracked:
    """The default_alert_groups/ tree must exist and be tracked in git
    (otherwise a fresh clone has no working AGs to seed)."""

    def test_directory_exists(self):
        assert DEFAULTS_DIR.is_dir(), (
            f"default_alert_groups/ missing - fresh clones will have no AGs"
        )

    def test_has_at_least_one_yaml(self):
        yamls = list(DEFAULTS_DIR.glob("*.yaml"))
        assert len(yamls) > 0, "default_alert_groups/ has no YAML templates"

    def test_all_defaults_are_tracked_in_git(self):
        """Every yaml in default_alert_groups/ must be `git ls-files`-visible."""
        try:
            tracked = subprocess.check_output(
                ["git", "ls-files", "default_alert_groups/*.yaml"],
                cwd=PROJECT_ROOT,
                text=True,
            ).strip().splitlines()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("git not available or not a repo")

        on_disk = sorted(
            f"default_alert_groups/{p.name}" for p in DEFAULTS_DIR.glob("*.yaml")
        )
        assert sorted(tracked) == on_disk, (
            "Some default_alert_groups/*.yaml files are not tracked in git - "
            "they will be missing on a fresh clone"
        )


class TestAlertGroupsYamlGitignored:
    """User AG yamls must be gitignored - that's the whole fix."""

    def test_alert_groups_yaml_pattern_in_gitignore(self):
        gitignore = (PROJECT_ROOT / ".gitignore").read_text()
        assert "/alert_groups/*.yaml" in gitignore, (
            ".gitignore must include `/alert_groups/*.yaml` so user-edited "
            "AGs are not overwritten by `git pull`"
        )

    def test_check_ignore_actually_ignores_a_test_yaml(self, tmp_path):
        """Use git check-ignore as the authoritative test."""
        try:
            # Run from project root with a representative AG name
            result = subprocess.run(
                ["git", "check-ignore", "-v", "alert_groups/test_user_ag.yaml"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            pytest.skip("git not available")
        # exit 0 = file IS ignored; exit 1 = NOT ignored
        assert result.returncode == 0, (
            f"`git check-ignore` says alert_groups/*.yaml is NOT ignored: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}. "
            f"This means UI edits will be reverted by every `git pull`."
        )

    def test_alert_groups_python_files_still_tracked(self):
        """The .py files in alert_groups/ are CODE and must stay tracked."""
        try:
            tracked = subprocess.check_output(
                ["git", "ls-files", "alert_groups/*.py"],
                cwd=PROJECT_ROOT,
                text=True,
            ).strip().splitlines()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("git not available")
        # Expect at least the well-known modules
        expected = {
            "alert_groups/__init__.py",
            "alert_groups/dispatcher.py",
            "alert_groups/builder.py",
        }
        assert expected.issubset(set(tracked)), (
            f"Code files in alert_groups/ should still be tracked. "
            f"Found tracked: {tracked}"
        )


class TestPersistenceAndInstallContract:
    """The drift-guard pattern: every user-data dir must be in BOTH
    persistence.py AND install.sh AND docker-compose.yml AND .gitignore (where applicable)."""

    def test_default_alert_groups_in_dir_targets_hashed(self):
        from tools.persistence import DIR_TARGETS_HASHED
        assert "default_alert_groups" in DIR_TARGETS_HASHED
        assert "alert_groups" in DIR_TARGETS_HASHED

    def test_install_sh_creates_default_alert_groups(self):
        install_sh = (PROJECT_ROOT / "install.sh").read_text()
        assert "default_alert_groups" in install_sh, (
            "install.sh mkdir block must create default_alert_groups/ "
            "or fresh installs may fail to seed"
        )

    def test_docker_compose_bind_mounts_default_alert_groups(self):
        compose = (PROJECT_ROOT / "desktop_app" / "docker-compose.yml").read_text()
        assert "../default_alert_groups:/app/default_alert_groups" in compose, (
            "docker-compose must bind-mount default_alert_groups/ so the "
            "container's seed function reads the host's tracked templates"
        )

    def test_alert_groups_bind_mount_still_present(self):
        compose = (PROJECT_ROOT / "desktop_app" / "docker-compose.yml").read_text()
        assert "../alert_groups:/app/alert_groups" in compose, (
            "alert_groups bind mount removed - user data will not persist"
        )


# ===========================================================================
# Layer 3 - Enable/Disable button HTML contract
# ===========================================================================


UI_HTML = (PROJECT_ROOT / "desktop_app" / "ui.html").read_text()


class TestEnableDisableButtonContract:
    """The Enable/Disable button must show the NEXT ACTION, not the current
    state. Pre-2026-04-30 it read 'Enabled'/'Disabled' (current state) so
    every click appeared to do nothing - the row reloaded but the label
    stayed the same."""

    def test_button_label_is_action_oriented(self):
        """Source must NOT contain the old state-oriented label expression."""
        assert "'Disabled' : 'Enabled'" not in UI_HTML, (
            "Found the buggy state-oriented label `'Disabled' : 'Enabled'` - "
            "this is the regression that made the button appear broken. "
            "Use action-oriented `'Enable' : 'Disable'` instead."
        )

    def test_button_uses_action_oriented_text(self):
        """The new action-oriented label must be present."""
        assert "isDisabled ? 'Enable' : 'Disable'" in UI_HTML, (
            "Action-oriented button label missing from ui.html"
        )

    def test_click_handler_passes_current_state(self):
        """The click handler signature is unchanged - it still gets the
        current `disabled` flag so it can compute the right API endpoint."""
        assert "toggleAlertGroup(g.name, isDisabled)" in UI_HTML

    def test_toggle_handler_calls_correct_endpoint(self):
        """toggleAlertGroup must POST to /enable when disabled, /disable
        when enabled - the inversion is what makes the click do work."""
        # The function uses ``currentlyDisabled ? 'enable' : 'disable'`` to
        # pick the action - that's the contract.
        assert "currentlyDisabled ? 'enable' : 'disable'" in UI_HTML

    def test_button_data_attributes_present_for_test_hooks(self):
        """The data-* attrs let other test layers (or future Selenium runs)
        find buttons by AG name without relying on text content."""
        assert "statusBtn.dataset.agName" in UI_HTML
        assert "statusBtn.dataset.agState" in UI_HTML
