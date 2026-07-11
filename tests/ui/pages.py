"""
Page navigation and state helpers for UI tests.

Provides helpers to navigate between pages, query the current page,
and manage themes - all via Playwright page objects.
"""

from tests.ui.selectors import PAGES, NAV_TAB, THEME_BTN, HTML_ELEMENT


def navigate_to(page, page_name):
    """Click a nav tab and wait for the target page to become visible.

    The 2026-04-26 dropdown nav redesign hides leaf ``.nav-tab`` buttons
    inside ``.nav-dropdown`` panels until their parent ``.nav-group`` is
    opened. Open the parent dropdown first so Playwright's
    visibility-aware ``page.click()`` succeeds on the leaf.

    Parameters
    ----------
    page : playwright.sync_api.Page
        The Playwright page object.
    page_name : str
        A key from ``selectors.PAGES`` (e.g. "query", "settings", "macros").
    """
    page_id = PAGES[page_name]
    tab_selector = NAV_TAB.format(page_id=page_id)
    group = page.evaluate(
        "(sel) => document.querySelector(sel)?.getAttribute('data-group')",
        tab_selector,
    )
    if group:
        page.click(f'button.nav-group[data-group="{group}"]')
    page.click(tab_selector)
    page.wait_for_selector(
        f"#{page_id}.active",
        state="visible",
        timeout=5000,
    )


def current_page_id(page):
    """Return the ``id`` attribute of the currently active page ``<div>``."""
    active = page.locator(".page.active")
    if active.count() == 0:
        return None
    return active.first.get_attribute("id")


def get_theme(page):
    """Return the current ``data-theme`` value on ``<html>``."""
    return page.locator(HTML_ELEMENT).get_attribute("data-theme")


def set_theme(page, theme):
    """Switch the theme and verify the ``data-theme`` attribute updated."""
    page.click(THEME_BTN.format(theme=theme))
    actual = get_theme(page)
    assert actual == theme, f"Expected theme '{theme}', got '{actual}'"
