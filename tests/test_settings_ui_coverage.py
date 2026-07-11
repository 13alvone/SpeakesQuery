"""
Coverage guard: every global setting in ``global_settings.DEFAULTS`` must
have a matching UI input in ``desktop_app/ui.html`` AND a corresponding
entry in the Settings page's ``settingsFields`` dict.

Added 2026-04-20 after auditing and finding 17 of 49 settings with no UI
input (logs index, Claude robustness, alert-group defaults, SEC contact).
This test fails loud next time someone adds a setting to DEFAULTS without
wiring the UI side - the most-common drift surface for this codebase.
"""

from __future__ import annotations

import re
from pathlib import Path

from global_settings import DEFAULTS


UI_PATH = Path(__file__).parent.parent / "desktop_app" / "ui.html"


def _ui_text() -> str:
    return UI_PATH.read_text(encoding="utf-8")


def _html_input_ids() -> set[str]:
    return set(re.findall(r'id="(set-[a-z0-9-]+)"', _ui_text()))


def _settings_fields_map() -> dict[str, str]:
    """Parse the settingsFields dict literal from ui.html.

    Returns ``{setting_key: html_input_id}`` for every entry.
    """
    pairs = re.findall(
        r"'([a-z_][a-z0-9_]*)':\s*\{\s*el:\s*\(\)\s*=>\s*"
        r"document\.getElementById\('(set-[a-z0-9-]+)'\)",
        _ui_text(),
    )
    return dict(pairs)


class TestSettingsUiCoverage:
    def test_every_default_has_settings_field_entry(self):
        """Every ``DEFAULTS`` key must have a ``settingsFields`` entry."""
        fields = _settings_fields_map()
        missing = sorted(set(DEFAULTS.keys()) - set(fields.keys()))
        assert not missing, (
            f"{len(missing)} setting(s) declared in DEFAULTS but not in the "
            f"UI's settingsFields dict - they cannot be edited via the "
            f"Settings page. Add an entry in ui.html next to the existing "
            f"fields:\n  " + "\n  ".join(missing)
        )

    def test_every_settings_field_has_html_input(self):
        """Every ``settingsFields`` entry must reference a real input id."""
        fields = _settings_fields_map()
        html_ids = _html_input_ids()
        missing = [
            (key, iid) for key, iid in fields.items() if iid not in html_ids
        ]
        assert not missing, (
            f"{len(missing)} settingsFields entry/entries point at ids that "
            f"don't exist in ui.html - their inputs will never render. Fix:\n"
            + "\n".join(f"  {k} -> {iid}" for k, iid in missing)
        )

    def test_no_dead_settings_field_entries(self):
        """Every ``settingsFields`` key must also be in ``DEFAULTS``.

        Catches the opposite drift: a setting was removed from DEFAULTS
        but the UI never cleaned up its entry. Stale entries look
        harmless but silently drop any value a user tries to save.
        """
        fields = _settings_fields_map()
        stale = sorted(set(fields.keys()) - set(DEFAULTS.keys()))
        assert not stale, (
            f"{len(stale)} settingsFields entries reference keys no longer in "
            f"DEFAULTS - remove the stale entries from ui.html:\n  "
            + "\n  ".join(stale)
        )
