"""
UI test assertion helpers and utilities.

Mirrors the assertion-helper pattern used in ``test_api.py`` and
``test_spql.py``, adapted for Playwright DOM assertions.
"""

import json
import re

from playwright.sync_api import expect

from tests.ui import selectors as sel


# ── Visibility ────────────────────────────────────────────────────────────────

def assert_visible(page, selector, timeout=5000):
    """Assert that an element matching *selector* is visible."""
    expect(page.locator(selector).first).to_be_visible(timeout=timeout)


def assert_hidden(page, selector, timeout=2000):
    """Assert that an element matching *selector* is NOT visible."""
    expect(page.locator(selector).first).not_to_be_visible(timeout=timeout)


def assert_page_visible(page, page_id):
    """Assert that the page container with *page_id* is visible and active."""
    loc = page.locator(f"#{page_id}")
    expect(loc).to_be_visible()
    cls = loc.get_attribute("class") or ""
    assert "active" in cls, f"#{page_id} is visible but missing 'active' class"


# ── Text / Value ──────────────────────────────────────────────────────────────

def assert_text(page, selector, expected, timeout=5000):
    """Assert the ``textContent`` of the first matching element."""
    expect(page.locator(selector).first).to_have_text(expected, timeout=timeout)


def assert_text_contains(page, selector, substring, timeout=5000):
    """Assert the text content of an element contains *substring*."""
    expect(page.locator(selector).first).to_contain_text(substring, timeout=timeout)


def assert_input_value(page, selector, expected):
    """Assert the ``value`` property of an ``<input>`` or ``<textarea>``."""
    expect(page.locator(selector).first).to_have_value(expected)


def assert_input_value_contains(page, selector, substring, timeout=5000):
    """Assert the ``value`` property of an ``<input>`` or ``<textarea>`` contains *substring*."""
    expect(page.locator(selector).first).to_have_value(
        re.compile(re.escape(substring)), timeout=timeout
    )


# ── Counts ────────────────────────────────────────────────────────────────────

def assert_count(page, selector, expected_count):
    """Assert the number of elements matching *selector*."""
    expect(page.locator(selector)).to_have_count(expected_count)


def assert_count_gte(page, selector, minimum):
    """Assert at least *minimum* elements match *selector*."""
    actual = page.locator(selector).count()
    assert actual >= minimum, (
        f"Expected >= {minimum} matches for '{selector}', got {actual}"
    )


# ── Attributes ────────────────────────────────────────────────────────────────

def assert_attribute(page, selector, attr, expected):
    """Assert that an element's attribute has the expected value."""
    loc = page.locator(selector).first
    expect(loc).to_have_attribute(attr, expected)


def assert_has_class(page, selector, class_name):
    """Assert that an element's class list includes *class_name*."""
    loc = page.locator(selector).first
    expect(loc).to_have_class(re.compile(re.escape(class_name)))


# ── Notifications (Toast System) ──────────────────────────────────────────────

def assert_notification(page, notification_type="success", text=None, timeout=5000):
    """Assert that a toast notification of the given type appears.

    Parameters
    ----------
    notification_type : str
        One of "success", "error", "primary".
    text : str, optional
        Substring the notification text must contain.
    """
    class_map = {
        "success": sel.NOTIFICATION_SUCCESS,
        "error":   sel.NOTIFICATION_ERROR,
        "primary": sel.NOTIFICATION_PRIMARY,
    }
    selector = class_map.get(notification_type, sel.NOTIFICATION)
    notif = page.locator(selector).first
    expect(notif).to_be_visible(timeout=timeout)
    if text:
        expect(notif).to_contain_text(text)


def assert_no_notification(page, timeout=1000):
    """Assert that no toast notification is currently visible."""
    expect(page.locator(sel.NOTIFICATION).first).not_to_be_visible(timeout=timeout)


# ── Disabled / Enabled ────────────────────────────────────────────────────────

def assert_disabled(page, selector):
    """Assert that a button or input is disabled."""
    expect(page.locator(selector).first).to_be_disabled()


def assert_enabled(page, selector):
    """Assert that a button or input is enabled."""
    expect(page.locator(selector).first).to_be_enabled()


# ── API Interception ──────────────────────────────────────────────────────────

def intercept_api(page, url_pattern, status=200, body=None):
    """Mock an API endpoint to return a canned JSON response.

    Useful for testing error states without needing server-side setup.

    Parameters
    ----------
    page : playwright.sync_api.Page
    url_pattern : str
        Glob pattern (e.g. ``"**/api/settings"``).
    status : int
        HTTP status code to return.
    body : dict or None
        JSON body to return.  Defaults to ``{}``.
    """
    def handler(route):
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body or {}),
        )
    page.route(url_pattern, handler)


def wait_for_api(page, url_substring, timeout=10000):
    """Wait for an API response whose URL contains *url_substring*.

    Returns the Playwright ``Response`` object.
    """
    with page.expect_response(
        lambda resp: url_substring in resp.url,
        timeout=timeout,
    ) as response_info:
        pass
    return response_info.value
