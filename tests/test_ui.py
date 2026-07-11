#!/usr/bin/env python3
"""
UI Test Framework - YAML-driven browser test runner.

Discovers YAML test definitions under tests/yaml/tier6_ui/ and
tests/yaml/tier7_ui_regression/, then executes them in a real
(headless) browser via Playwright.

Mirrors the parametrized pattern in test_api.py.
"""

import re
import pytest
from tests.conftest import collect_all_yaml_tests, make_test_id
from tests.ui.pages import navigate_to
from tests.ui import helpers


# ---------------------------------------------------------------------------
# Collect all YAML-driven UI test cases
# ---------------------------------------------------------------------------

ALL_UI_TESTS = (
    collect_all_yaml_tests(subdir="tier6_ui")
    + collect_all_yaml_tests(subdir="tier7_ui_regression")
)


# ---------------------------------------------------------------------------
# Action dispatcher
# ---------------------------------------------------------------------------

def dispatch_ui_action(page, action):
    """Execute a single UI action defined in a YAML test step."""
    action_type = action["action"]

    if action_type == "navigate":
        navigate_to(page, action["page"])

    elif action_type == "click":
        page.click(action["selector"])

    elif action_type == "fill":
        page.fill(action["selector"], str(action["value"]))

    elif action_type == "clear":
        page.fill(action["selector"], "")

    elif action_type == "check":
        page.check(action["selector"])

    elif action_type == "uncheck":
        page.uncheck(action["selector"])

    elif action_type == "select":
        page.select_option(action["selector"], action["value"])

    elif action_type == "wait":
        page.wait_for_selector(
            action["selector"],
            state=action.get("state", "visible"),
            timeout=action.get("timeout", 5000),
        )

    elif action_type == "wait_for_response":
        page.wait_for_response(
            lambda r: action["url_contains"] in r.url,
            timeout=action.get("timeout", 10000),
        )

    elif action_type == "clear_localstorage":
        page.evaluate("localStorage.clear()")

    elif action_type == "set_localstorage":
        key = action["key"]
        value = action["value"]
        page.evaluate(f"localStorage.setItem('{key}', '{value}')")

    elif action_type == "reload":
        page.reload(wait_until="networkidle")

    elif action_type == "press":
        page.press(action.get("selector", "body"), action["key"])

    elif action_type == "js":
        page.evaluate(action["code"])

    elif action_type == "key":
        page.press(action.get("selector", "body"), action["key"])

    else:
        raise ValueError(f"Unknown UI action type: {action_type!r}")


# ---------------------------------------------------------------------------
# Assertion runner
# ---------------------------------------------------------------------------

def assert_ui_expectations(page, expect):
    """Run all assertion checks from a test case's ``expect`` block."""

    # Active page
    if "active_page" in expect:
        helpers.assert_page_visible(page, expect["active_page"])

    # Visible elements
    if "visible" in expect:
        for sel in expect["visible"]:
            helpers.assert_visible(page, sel)

    # Hidden elements
    if "hidden" in expect:
        for sel in expect["hidden"]:
            helpers.assert_hidden(page, sel)

    # Text content
    if "text" in expect:
        for check in expect["text"]:
            helpers.assert_text(page, check["selector"], check["value"])

    # Text contains
    if "text_contains" in expect:
        for check in expect["text_contains"]:
            helpers.assert_text_contains(
                page, check["selector"], check["value"]
            )

    # Input values
    if "input_value" in expect:
        for check in expect["input_value"]:
            helpers.assert_input_value(
                page, check["selector"], str(check["value"])
            )

    # Input value contains (substring match for dynamic values)
    if "input_value_contains" in expect:
        for check in expect["input_value_contains"]:
            helpers.assert_input_value_contains(
                page, check["selector"], str(check["value"])
            )

    # Element count
    if "count" in expect:
        for check in expect["count"]:
            helpers.assert_count(page, check["selector"], check["value"])

    # Minimum element count
    if "count_gte" in expect:
        for check in expect["count_gte"]:
            helpers.assert_count_gte(page, check["selector"], check["value"])

    # Attribute values
    if "attribute" in expect:
        for check in expect["attribute"]:
            helpers.assert_attribute(
                page, check["selector"], check["attr"], check["value"]
            )

    # CSS class
    if "has_class" in expect:
        for check in expect["has_class"]:
            helpers.assert_has_class(page, check["selector"], check["value"])

    # Notification toast
    if "notification" in expect:
        helpers.assert_notification(
            page,
            notification_type=expect["notification"].get("type", "success"),
            text=expect["notification"].get("text"),
        )

    # No notification
    if "no_notification" in expect and expect["no_notification"]:
        helpers.assert_no_notification(page)

    # Disabled/enabled
    if "disabled" in expect:
        for sel in expect["disabled"]:
            helpers.assert_disabled(page, sel)

    if "enabled" in expect:
        for sel in expect["enabled"]:
            helpers.assert_enabled(page, sel)


# ---------------------------------------------------------------------------
# Parametrized test entry point
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tc",
    ALL_UI_TESTS,
    ids=[make_test_id(tc) for tc in ALL_UI_TESTS],
)
def test_ui(page, tc):
    """Execute a single YAML-driven UI test case."""
    # Execute steps
    steps = tc.get("steps", [])
    for step in steps:
        dispatch_ui_action(page, step)

    # Run assertions
    expect = tc.get("expect", {})
    if expect:
        assert_ui_expectations(page, expect)
