#!/usr/bin/env python3
"""
SPQL Test Framework - pytest configuration and fixtures.

Provides the query executor fixture, YAML test discovery/parametrization,
and Playwright-based UI testing fixtures.
"""

import sys
import os
import glob
import signal
import socket
import subprocess
import time
import yaml
import pytest

# Ensure project root is on sys.path so imports resolve
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Change to project root so relative index paths resolve correctly
os.chdir(PROJECT_ROOT)

from query_engine.CmdExecutionBackend import run_query_and_return_results_df


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def run_query():
    """Return the query executor callable.

    Returns a function that takes a query string and returns (DataFrame, job_id).
    On error, the DataFrame will be None.
    """
    return run_query_and_return_results_df


@pytest.fixture(scope="session")
def client():
    """Flask test client for API endpoint testing.

    Creates an in-process test client - no running server needed.
    Starts the scheduled input engine so ingestion/credential endpoints work.
    """
    from scheduled_input_engine import start_engine
    start_engine()

    from desktop_app.server import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# UI testing fixtures (Playwright)
# ---------------------------------------------------------------------------

UI_PORT = 5199  # Dedicated port - avoids conflict with dev server on 5111


@pytest.fixture(scope="session")
def ui_server():
    """Start the Flask server in-process for Playwright to connect to.

    Uses a daemon thread (not a subprocess) for stability under heavy
    test load.  The thread dies automatically when the test session ends.
    """
    import threading
    from desktop_app.server import app as flask_app

    # Start the scheduled input engine so /api/si/* endpoints work
    try:
        from scheduled_input_engine import start_engine as _start_engine
        _start_engine()
    except Exception:
        pass  # Engine may already be running from the `client` fixture

    host = "127.0.0.1"
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host=host, port=UI_PORT, debug=False,
            use_reloader=False, threaded=True,
        ),
        daemon=True,
    )
    flask_thread.start()

    # Wait for server to accept connections (up to 15 seconds)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            sock = socket.create_connection((host, UI_PORT), timeout=1)
            sock.close()
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.3)
    else:
        raise RuntimeError(f"Flask server did not start on port {UI_PORT}")

    yield f"http://{host}:{UI_PORT}"


@pytest.fixture(scope="session")
def browser_instance():
    """Create a single Playwright browser instance for the session.

    Uses Chromium in headless mode by default.
    Set ``HEADED=1`` env var for visual debugging.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    headed = os.environ.get("HEADED", "0") == "1"
    browser = pw.chromium.launch(headless=not headed)

    yield browser

    browser.close()
    pw.stop()


# Pre-seeded storage values so first-run overlays (welcome, email setup,
# Claude key setup) never appear during UI tests. Running through the
# click-to-dismiss path is flaky because the Claude overlay appears on a
# 500 ms retry AFTER the email overlay closes - so by race conditions
# it sometimes surfaces mid-test and intercepts pointer events.
_SUPPRESS_OVERLAYS_JS = """
  try { localStorage.setItem('speakesquery_query_welcome', 'false'); } catch(_) {}
  try { localStorage.setItem('speakesquery_claude_key_setup_dismissed', 'true'); } catch(_) {}
  try { sessionStorage.setItem('speakesquery_email_setup_dismissed_session', 'true'); } catch(_) {}
"""


@pytest.fixture(scope="function")
def page(browser_instance, ui_server):
    """Create a fresh browser page for each UI test.

    - Fresh context per test (no shared cookies/localStorage/DOM)
    - Navigates to the app root and waits for the Query page to be active
    - Auto-dismisses first-run overlays (welcome, email setup)
    """
    context = browser_instance.new_context(
        viewport={"width": 1440, "height": 900},
    )
    context.add_init_script(_SUPPRESS_OVERLAYS_JS)
    pg = context.new_page()
    pg.goto(ui_server, wait_until="domcontentloaded")

    # Wait for the app to be interactive (Query page is the default active page)
    pg.wait_for_selector(".page.active", state="visible", timeout=10000)

    # Dismiss first-run overlays if present
    _dismiss_overlays(pg)

    yield pg

    context.close()


@pytest.fixture(scope="class")
def shared_page(browser_instance, ui_server):
    """Shared browser page for CRUD lifecycle test classes.

    Unlike the function-scoped ``page`` fixture, this creates a SINGLE
    browser context that persists across all methods in the test class.
    This avoids overwhelming the Flask dev server with 10+ page loads
    per class during test_ui_crud.py.
    """
    context = browser_instance.new_context(
        viewport={"width": 1440, "height": 900},
    )
    context.add_init_script(_SUPPRESS_OVERLAYS_JS)
    pg = context.new_page()
    pg.goto(ui_server, wait_until="domcontentloaded")
    pg.wait_for_selector(".page.active", state="visible", timeout=10000)
    _dismiss_overlays(pg)

    yield pg

    context.close()


def _dismiss_overlays(page):
    """Close any first-run modals/overlays that would block interaction."""
    # Welcome overlay - click "Got it - let's go!"
    try:
        welcome_btn = page.locator("#welcome-go-btn")
        if welcome_btn.is_visible(timeout=1500):
            welcome_btn.click()
            page.wait_for_selector("#welcome-panel", state="hidden", timeout=2000)
    except Exception:
        pass

    # Email setup overlay - click the skip/close if visible
    try:
        email_backdrop = page.locator("#email-setup-backdrop")
        if email_backdrop.is_visible(timeout=500):
            # Look for a skip or close button
            skip = page.locator("#email-setup-panel button:has-text('Skip')")
            if skip.is_visible(timeout=500):
                skip.click()
            else:
                # Close by clicking the backdrop
                email_backdrop.click(position={"x": 5, "y": 5})
            page.wait_for_selector("#email-setup-panel", state="hidden", timeout=2000)
    except Exception:
        pass

    # Claude analyzer key setup overlay - queued behind the email overlay,
    # so it only becomes visible after the email backdrop clears (there's a
    # 500 ms retry loop in ui.html). Poll for up to a few seconds so we
    # reliably catch it; otherwise pointer-event interception breaks
    # subsequent tests that try to click anything on the Query page.
    try:
        claude_backdrop = page.locator("#claude-key-setup-backdrop")
        if claude_backdrop.is_visible(timeout=2500):
            skip = page.locator("#cks-skip-btn")
            if skip.is_visible(timeout=500):
                skip.click()
            else:
                claude_backdrop.click(position={"x": 5, "y": 5})
            page.wait_for_selector("#claude-key-setup-panel", state="hidden", timeout=2000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# YAML discovery helpers
# ---------------------------------------------------------------------------

YAML_DIR = os.path.join(os.path.dirname(__file__), "yaml")


def discover_yaml_files(subdir=None):
    """Walk *subdir* (or all of yaml/) and return every .yaml path."""
    search_root = os.path.join(YAML_DIR, subdir) if subdir else YAML_DIR
    return sorted(glob.glob(os.path.join(search_root, "**", "*.yaml"), recursive=True))


def load_tests_from_yaml(path):
    """Parse a single YAML file and return a list of test dicts."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return []
    return data.get("tests", [])


def collect_all_yaml_tests(subdir=None, exclude=None):
    """Collect every test case across all YAML files in *subdir*.

    *exclude* is an optional list of subdirectory prefixes to skip
    (e.g. ["tier5_api"] to exclude API tests from SPQL collection).
    """
    cases = []
    for path in discover_yaml_files(subdir):
        rel = os.path.relpath(path, YAML_DIR)
        if exclude and any(rel.startswith(ex) for ex in exclude):
            continue
        for tc in load_tests_from_yaml(path):
            tc["_source_file"] = rel
            cases.append(tc)
    return cases


def make_test_id(tc):
    """Generate a human-readable pytest node id from a test case dict."""
    src = tc.get("_source_file", "unknown")
    tid = tc.get("id", "?")
    title = tc.get("title", "untitled")
    return f"{src}::{tid}::{title}"
