"""
Drift guards for the 2026-04-26 dropdown-nav redesign.

The flat 14-tab top nav was replaced by 5 ``.nav-group`` dropdown
buttons (Data / Search / Ingestion / Alerts / Help). Each leaf
``.nav-tab`` lives inside a ``.nav-dropdown`` panel anchored under its
group button.

These tests pin the HTML/CSS/JS contract that other parts of the app
(``navigateToSavedSearch``, welcome doc cards, cross-tab nav helpers,
Playwright navigate_to) rely on:

* HTML - 5 ``.nav-group`` buttons with the right ``data-group`` values
* HTML - every leaf ``.nav-tab`` is nested in the matching dropdown
* CSS - dropdowns are ``display: none`` until parent hover or open
* CSS - ``.parent-active`` styles the group button when its leaf is active
* JS - close-on-outside-click, close-on-Escape, parent-active sync
* JS - initial seed call so the active page's parent is highlighted on load
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_HTML = REPO_ROOT / "desktop_app" / "ui.html"


@pytest.fixture(scope="module")
def ui() -> str:
    return UI_HTML.read_text(encoding="utf-8")


def _nav_block(ui_html: str) -> str:
    """Return the inner contents of ``<div class="nav-tabs">`` by walking
    div nesting manually - the dropdown panels nest divs inside, so a
    naive non-greedy regex would stop at the first inner ``</div>``."""
    start = re.search(r'<div class="nav-tabs"[^>]*>', ui_html)
    assert start, "nav-tabs container not found"
    i = start.end()
    depth = 1
    while i < len(ui_html) and depth > 0:
        open_at = ui_html.find("<div", i)
        close_at = ui_html.find("</div>", i)
        if close_at == -1:
            break
        if open_at != -1 and open_at < close_at:
            depth += 1
            i = open_at + 4
        else:
            depth -= 1
            if depth == 0:
                return ui_html[start.end():close_at]
            i = close_at + 6
    raise AssertionError("nav-tabs container is unbalanced")


# ── HTML contract ──────────────────────────────────────────────────────

EXPECTED_GROUPS = ["data", "search", "ingestion", "alerts", "develop", "help"]

EXPECTED_LEAVES_BY_GROUP = {
    "data":      ["page-query", "page-lookups", "page-import"],
    "search":    ["page-create-search", "page-searches", "page-macros"],
    "ingestion": [
        "page-create-ingestion", "page-ingestion", "page-library",
    ],
    "alerts":    [
        "page-alert-groups", "page-email-groups", "page-schedule",
    ],
    # Phase 3 / Bet 4 slice 4 (2026-05-09): "Develop" group introduced
    # with a single leaf (Notebooks). Phase 4 (Visual Builder) will
    # join as a sibling - extend this list when that ships.
    "develop":   ["page-notebooks"],
    "help":      ["page-settings", "page-docs"],
}


class TestDropdownStructure:
    def test_five_group_buttons_with_correct_data_group(self, ui):
        nav = _nav_block(ui)
        for group in EXPECTED_GROUPS:
            m = re.search(
                r'<button[^>]*class="nav-group"[^>]*'
                rf'data-group="{group}"',
                nav,
            )
            assert m, f"nav-group button for {group!r} missing"

    def test_each_group_button_has_aria_haspopup_and_aria_expanded(self, ui):
        nav = _nav_block(ui)
        for group in EXPECTED_GROUPS:
            # The button's full opening tag (everything up to the first '>')
            m = re.search(
                rf'<button[^>]*class="nav-group"[^>]*data-group="{group}"[^>]*>',
                nav,
            )
            assert m, f"nav-group button for {group!r} missing"
            tag = m.group(0)
            assert 'aria-haspopup="menu"' in tag, (
                f"{group!r} group button missing aria-haspopup"
            )
            assert 'aria-expanded="false"' in tag, (
                f"{group!r} group button must start with aria-expanded='false'"
            )

    def test_each_leaf_lives_inside_correct_dropdown(self, ui):
        nav = _nav_block(ui)
        for group, expected_leaves in EXPECTED_LEAVES_BY_GROUP.items():
            # Find the wrapper for this group, then inspect its dropdown.
            wrapper_match = re.search(
                rf'<div class="nav-group-wrapper">\s*'
                rf'<button[^>]*data-group="{group}"[\s\S]+?'
                rf'<div class="nav-dropdown"[^>]*>([\s\S]+?)</div>',
                nav,
            )
            assert wrapper_match, (
                f"could not locate dropdown panel for group {group!r}"
            )
            dropdown_body = wrapper_match.group(1)
            for page in expected_leaves:
                assert f'data-page="{page}"' in dropdown_body, (
                    f"leaf {page!r} expected inside {group!r} dropdown"
                )
                assert f'data-group="{group}"' in dropdown_body, (
                    f"leaf {page!r} must carry data-group='{group}' "
                    f"so navigate_to() can find its parent"
                )

    def test_no_orphan_leaves_outside_dropdowns(self, ui):
        """Every ``.nav-tab`` in the nav block must sit inside a
        ``.nav-dropdown`` - no flat siblings of group buttons. Otherwise
        the dropdown layout breaks and stray leaves render unstyled."""
        nav = _nav_block(ui)
        # Strip out every <div class="nav-dropdown">...</div> chunk;
        # whatever .nav-tab remains is an orphan.
        without_dropdowns = re.sub(
            r'<div class="nav-dropdown"[^>]*>[\s\S]+?</div>',
            "",
            nav,
        )
        orphans = re.findall(
            r'class="nav-tab[^"]*"\s+data-page="([^"]+)"',
            without_dropdowns,
        )
        assert orphans == [], (
            f"these leaves are not nested in a .nav-dropdown: {orphans}"
        )


# ── CSS contract ───────────────────────────────────────────────────────

class TestDropdownCss:
    def test_dropdown_hidden_by_default(self, ui):
        m = re.search(r"\.nav-dropdown\s*\{([\s\S]+?)\}", ui)
        assert m, ".nav-dropdown CSS rule missing"
        rule = m.group(1)
        assert "display: none" in rule, (
            ".nav-dropdown must default to display:none so panels stay "
            "hidden until their group is hovered or aria-expanded='true'"
        )
        assert "position: absolute" in rule, (
            ".nav-dropdown must be absolutely positioned under its group"
        )

    def test_dropdown_shown_on_wrapper_hover_or_open(self, ui):
        # Combined selector that includes both the hover state and the
        # aria-expanded sibling state. Either one must show the panel.
        m = re.search(
            r"\.nav-group-wrapper:hover\s+\.nav-dropdown[\s\S]+?display:\s*flex",
            ui,
        )
        assert m, (
            "missing CSS rule that shows .nav-dropdown when wrapper hovered"
        )
        m2 = re.search(
            r'\.nav-group\[aria-expanded="true"\]\s*\+\s*'
            r'\.nav-dropdown[\s\S]+?display:\s*flex',
            ui,
        )
        assert m2, (
            "missing CSS rule that shows .nav-dropdown when group is "
            "aria-expanded='true'"
        )

    def test_parent_active_styles_the_group_button(self, ui):
        m = re.search(r"\.nav-group\.parent-active\s*\{([\s\S]+?)\}", ui)
        assert m, ".nav-group.parent-active CSS rule missing"
        rule = m.group(1)
        # The group button should look 'selected' when one of its leaves
        # is the active page - accent border-bottom + bold weight.
        assert "border-bottom-color" in rule, (
            ".parent-active must paint a border so the user sees which "
            "group their active page belongs to"
        )

    def test_chevron_rotates_when_expanded(self, ui):
        # The chevron should flip to indicate the panel is open.
        m = re.search(
            r'\.nav-group\[aria-expanded="true"\]\s+\.nav-chevron[\s\S]+?'
            r"transform:\s*rotate",
            ui,
        )
        assert m, (
            "expected .nav-chevron to rotate when the parent group "
            "is aria-expanded='true'"
        )


# ── JS contract ────────────────────────────────────────────────────────

class TestDropdownJs:
    def test_close_helper_exists(self, ui):
        assert "_closeAllNavDropdowns" in ui, (
            "JS must define _closeAllNavDropdowns() so click handlers "
            "and the outside-click listener can share one closer"
        )

    def test_parent_highlight_helper_exists(self, ui):
        assert "_highlightParentForActiveTab" in ui, (
            "JS must define _highlightParentForActiveTab() to keep the "
            "group button's .parent-active class in sync with the active leaf"
        )

    def test_outside_click_closes_dropdowns(self, ui):
        # The document-level click listener must check whether the click
        # landed inside .nav-tabs and close all open dropdowns if not.
        m = re.search(
            r"document\.addEventListener\(\s*'click'[\s\S]+?"
            r"closest\('\.nav-tabs'\)[\s\S]+?_closeAllNavDropdowns",
            ui,
        )
        assert m, (
            "expected a document-level 'click' listener that calls "
            "_closeAllNavDropdowns when the click is outside .nav-tabs"
        )

    def test_escape_closes_dropdowns(self, ui):
        m = re.search(
            r"document\.addEventListener\(\s*'keydown'[\s\S]+?"
            r"key\s*===\s*'Escape'[\s\S]+?_closeAllNavDropdowns",
            ui,
        )
        assert m, (
            "expected a document-level 'keydown' listener that closes "
            "all dropdowns on Escape"
        )

    def test_leaf_click_closes_parent_dropdown(self, ui):
        # The .nav-tab click handler must call _closeAllNavDropdowns(null)
        # so selecting a leaf collapses its parent panel.
        m = re.search(
            r"querySelectorAll\('\.nav-tab'\)[\s\S]+?"
            r"addEventListener\('click'[\s\S]+?"
            r"_closeAllNavDropdowns\(null\)",
            ui,
        )
        assert m, (
            "leaf .nav-tab click handler must close all open dropdowns "
            "after activating the page"
        )

    def test_leaf_click_updates_parent_active_highlight(self, ui):
        m = re.search(
            r"querySelectorAll\('\.nav-tab'\)[\s\S]+?"
            r"addEventListener\('click'[\s\S]+?"
            r"_highlightParentForActiveTab",
            ui,
        )
        assert m, (
            "leaf .nav-tab click handler must call "
            "_highlightParentForActiveTab so the group button's "
            ".parent-active class follows the active leaf"
        )

    def test_initial_parent_highlight_is_seeded(self, ui):
        """The page loads with one leaf already marked .active (Query →
        Data). The boot path must call _highlightParentForActiveTab()
        once so the user sees their group selected before clicking."""
        # There must be at least one bare call to the helper that's NOT
        # inside the click handler. We approximate by counting calls:
        # one inside the handler, plus at least one outside.
        calls = re.findall(r"_highlightParentForActiveTab\(\)", ui)
        assert len(calls) >= 2, (
            "expected at least one initial-seed call to "
            "_highlightParentForActiveTab() in addition to the call "
            "inside the .nav-tab click handler"
        )


# ── Cross-tab nav callsite contract ────────────────────────────────────

class TestExistingCallsitesStillWork:
    """The 2026-04-26 redesign deliberately preserves leaf attributes so
    existing callsites that do
    ``document.querySelector('.nav-tab[data-page="X"]').click()``
    keep working without modification - ``.click()`` on an HTMLElement
    fires the click event regardless of CSS visibility."""

    KNOWN_LEAF_CLICK_TARGETS = [
        "page-query",
        "page-searches",
        "page-alert-groups",
        "page-create-search",
        "page-create-ingestion",
        "page-ingestion",
    ]

    def test_known_callsites_still_resolve(self, ui):
        nav = _nav_block(ui)
        for page in self.KNOWN_LEAF_CLICK_TARGETS:
            assert f'data-page="{page}"' in nav, (
                f"existing JS callsites click .nav-tab[data-page='{page}'] "
                f" - that selector must resolve in the new dropdown nav"
            )
