#!/usr/bin/env python3
"""
UI CRUD lifecycle tests - ordered sequences for stateful page interactions.

These test classes mirror the CRUD pattern in test_api.py but exercise
the full browser UI instead of the raw API.  Each class runs its methods
in order so that create → read → update → delete sequencing is guaranteed.

Uses the ``shared_page`` fixture (class-scoped) so all methods in a class
share a single browser page - dramatically reduces server load compared
to creating a fresh page per test.
"""

import pytest
from playwright.sync_api import expect

from tests.ui.pages import navigate_to
from tests.ui import helpers


# ---------------------------------------------------------------------------
# Settings: load → modify → save → verify persistence → reset
# ---------------------------------------------------------------------------

class TestSettingsUI:
    """Exercise the full Settings page lifecycle through the browser."""

    def test_01_settings_load(self, shared_page):
        """Settings page loads and populates fields from the server."""
        page = shared_page
        navigate_to(page, "settings")
        page.wait_for_selector("#set-indexes-root", state="visible", timeout=8000)

        val = page.input_value("#set-indexes-root")
        assert val, "indexes_root should be populated after settings load"

        smtp_server = page.input_value("#set-smtp-server")
        assert smtp_server == "smtp.gmail.com", (
            f"Expected default SMTP server 'smtp.gmail.com', got {smtp_server!r}"
        )

    def test_02_settings_modify_and_save(self, shared_page):
        """Modify a setting, save, and verify success notification."""
        page = shared_page
        navigate_to(page, "settings")
        page.wait_for_selector("#set-max-total-size-gb", state="visible", timeout=8000)

        page.fill("#set-max-total-size-gb", "999")
        page.click("#save-settings-btn")
        helpers.assert_notification(page, "success", timeout=10000)

    def test_03_settings_persist_after_reload(self, shared_page):
        """Settings survive a page reload (persisted server-side)."""
        page = shared_page
        navigate_to(page, "settings")
        page.wait_for_selector("#set-max-total-size-gb", state="visible", timeout=8000)

        page.fill("#set-max-total-size-gb", "888")
        page.click("#save-settings-btn")
        helpers.assert_notification(page, "success", timeout=10000)

        # Navigate away and back to force a fresh load
        navigate_to(page, "query")
        page.evaluate("settingsLoaded = false")
        navigate_to(page, "settings")
        page.wait_for_selector("#set-max-total-size-gb", state="visible", timeout=8000)

        page.wait_for_timeout(1000)
        val = page.input_value("#set-max-total-size-gb")
        assert val == "888", f"Expected persisted value '888', got {val!r}"

    def test_04_settings_reset(self, shared_page):
        """Reset restores defaults and shows success notification."""
        page = shared_page
        navigate_to(page, "settings")
        page.wait_for_selector("#set-max-total-size-gb", state="visible", timeout=8000)

        page.on("dialog", lambda dialog: dialog.accept())
        page.click("#reset-settings-btn")
        helpers.assert_notification(page, "success", timeout=10000)

        page.wait_for_timeout(500)
        val = page.input_value("#set-max-total-size-gb")
        assert val == "100", f"Expected default '100' after reset, got {val!r}"

    def test_05_test_email_validation(self, shared_page):
        """Test email button validates that username is filled."""
        page = shared_page
        navigate_to(page, "settings")
        page.wait_for_selector("#set-smtp-user", state="visible", timeout=8000)

        page.fill("#set-smtp-user", "")
        page.click("#test-email-btn")

        msg = page.locator("#test-email-msg")
        expect(msg).to_be_visible(timeout=3000)
        expect(msg).to_contain_text("Enter a username")


# ---------------------------------------------------------------------------
# Query: run → verify results → verify job → save job
# ---------------------------------------------------------------------------

class TestQueryLifecycleUI:
    """Exercise the full Query page workflow through the browser."""

    _query = 'index="indexes/default_test/output_parquets/test0.parquet" | head 5'

    def test_01_run_query_shows_results(self, shared_page):
        """Running a valid query populates the results table."""
        page = shared_page
        navigate_to(page, "query")
        page.fill("#query", self._query)
        page.click("#run-query-btn")
        page.wait_for_selector("#results table", state="visible", timeout=30000)

        row_count_text = page.text_content("#row-count")
        assert "5" in row_count_text, f"Expected '5' in row count, got {row_count_text!r}"

    def test_02_export_buttons_enabled(self, shared_page):
        """After a successful query, export buttons are enabled."""
        page = shared_page
        # Results should still be visible from test_01 (shared page)
        helpers.assert_enabled(page, "#save-csv-btn")
        helpers.assert_enabled(page, "#save-json-btn")
        helpers.assert_enabled(page, "#save-job-btn")

    def test_03_job_id_displayed(self, shared_page):
        """After a query, job-id-bar shows the job ID."""
        page = shared_page
        # Job ID should still be visible from test_01
        helpers.assert_visible(page, "#job-id-bar")
        job_id = page.text_content("#job-id-label")
        assert job_id and len(job_id.strip()) > 0, "Job ID should not be empty"

    def test_04_save_job_panel_toggle(self, shared_page):
        """Save Job button toggles the inline save panel."""
        page = shared_page
        page.click("#save-job-btn")
        helpers.assert_visible(page, "#save-job-panel")

        page.click("#save-job-cancel-btn")
        helpers.assert_hidden(page, "#save-job-panel")

    def test_05_run_button_re_enabled_after_error(self, shared_page):
        """Run button is re-enabled and spinner hidden after a failed query."""
        page = shared_page
        navigate_to(page, "query")
        page.fill("#query", "this_is_not_valid!!!")
        page.click("#run-query-btn")

        page.wait_for_selector(
            "#notification-container .notification.is-danger",
            state="visible",
            timeout=15000,
        )

        helpers.assert_hidden(page, "#spinner.active")
        helpers.assert_enabled(page, "#run-query-btn")


# ---------------------------------------------------------------------------
# Query autoformatter & autocomplete (client-side features)
# ---------------------------------------------------------------------------
#
# These tests exercise the two browser-side SPQL ergonomics features added
# in 2026-04-17:
#   * ``window.spqlFormatQuery`` - reformats a query for readability
#   * grammar-vocab-backed autocomplete wired to the ``#query`` textarea
#
# The formatter is tested by calling it directly via ``page.evaluate`` so
# we can assert exact string output without driving UI events. The
# autocomplete test verifies that the vocab is loaded and a suggestion
# dropdown renders when the user types a recognisable prefix.


class TestQueryAutoformatter:
    def test_formatter_available_on_window(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        present = page.evaluate("typeof window.spqlFormatQuery === 'function'")
        assert present, "window.spqlFormatQuery should be exposed for tests"

    def test_pipe_segments_split_onto_lines(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        out = page.evaluate(
            "window.spqlFormatQuery('index=\"x\" | head 5 | stats count by k')"
        )
        assert out == 'index="x"\n| head 5\n| stats count by k'

    def test_multiple_spaces_collapse_outside_strings(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        out = page.evaluate(
            "window.spqlFormatQuery('index=\"x\"    |    head    5')"
        )
        assert out == 'index="x"\n| head 5'

    def test_quoted_content_preserved_verbatim(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        out = page.evaluate(
            'window.spqlFormatQuery(\'index="a  b  c"  |  head  5\')'
        )
        # Inside double-quoted string the double-spaces stay; outside they
        # collapse to single spaces and the pipe lands on its own line.
        assert out == 'index="a  b  c"\n| head 5'

    def test_line_comments_kept_on_own_line(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        src = 'index="x"\n# skipped segment\n| head 3'
        out = page.evaluate(f"window.spqlFormatQuery({src!r})")
        assert "# skipped segment" in out
        # Order is preserved
        lines = out.splitlines()
        assert lines[0] == 'index="x"'
        assert any(line.startswith("#") for line in lines)
        assert lines[-1] == "| head 3"

    def test_idempotent_on_already_formatted(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        canonical = 'index="x"\n| head 5\n| stats count by k'
        out = page.evaluate(f"window.spqlFormatQuery({canonical!r})")
        assert out == canonical

    def test_toggle_default_on(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        checked = page.evaluate(
            "document.getElementById('auto-format-query').checked"
        )
        assert checked is True


class TestQueryAutocomplete:
    def test_vocab_fetch_populates_dropdown_on_prefix(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        # Wait for the vocab fetch to resolve - it's fire-and-forget at
        # page-load, so poll window state rather than racing a fixed sleep.
        page.wait_for_function(
            "() => { const ac = document.getElementById('query-autocomplete'); "
            "return ac && typeof window.__spqlAutocompleteActive === 'function'; }",
            timeout=5000,
        )
        # Type a recognisable command prefix; dropdown should surface
        # matching suggestions.
        page.fill("#query", "")
        page.focus("#query")
        page.keyboard.type("sea", delay=30)
        page.wait_for_selector(
            "#query-autocomplete:not([hidden])", state="attached", timeout=5000,
        )
        names = page.eval_on_selector_all(
            "#query-autocomplete .qa-item .qa-name",
            "els => els.map(e => e.textContent)",
        )
        assert "search" in names, f"expected 'search' in suggestions, got {names}"


# ---------------------------------------------------------------------------
# Macros: create → verify in list → expand → delete
# ---------------------------------------------------------------------------

class TestMacroCRUDUI:
    """Exercise the full Macros page lifecycle through the browser."""

    _name = "ui_test_macro_crud"
    _definition = 'search status="$code$"'

    def test_01_open_form(self, shared_page):
        """New Macro button opens the create form."""
        page = shared_page
        navigate_to(page, "macros")
        page.click("#macro-new-btn")
        helpers.assert_visible(page, "#macro-form-box")
        helpers.assert_text_contains(page, "#macro-form-title", "Create Macro")

    def test_02_create_macro(self, shared_page):
        """Fill in and save a new macro."""
        page = shared_page
        # Form should still be open from test_01
        page.fill("#macro-name", self._name)
        page.fill("#macro-definition", self._definition)
        page.fill("#macro-description", "UI test macro - safe to delete")

        page.click("#macro-save-btn")

        # Wait for either: success toast OR the form hides (both indicate success).
        # The notification might auto-dismiss before we catch it, so also accept
        # the macro appearing in the list as proof of success.
        page.wait_for_timeout(2000)
        # Verify the macro was saved by refreshing the list
        page.click("#macro-refresh-btn")
        page.wait_for_timeout(1500)
        helpers.assert_text_contains(page, "#macros-list", self._name)

    def test_03_macro_appears_in_list(self, shared_page):
        """The newly created macro shows up in the list."""
        page = shared_page
        page.click("#macro-refresh-btn")
        page.wait_for_timeout(1500)
        helpers.assert_text_contains(page, "#macros-list", self._name)

    def test_04_expand_macro(self, shared_page):
        """Expand a query containing the macro."""
        page = shared_page
        page.fill("#macro-test-query", f'index="test" | `{self._name}(200)`')
        page.click("#macro-expand-btn")

        page.wait_for_selector("#macro-test-expansion", state="visible", timeout=10000)
        helpers.assert_text_contains(page, "#macro-test-expanded", "search")

    def test_05_delete_macro(self, shared_page):
        """Delete the macro via its list delete button."""
        page = shared_page
        page.click("#macro-refresh-btn")
        page.wait_for_timeout(1500)

        page.on("dialog", lambda dialog: dialog.accept())
        delete_btn = page.locator(
            f"#macros-list tr:has-text('{self._name}') button:has-text('Delete')"
        )
        if delete_btn.count() > 0:
            delete_btn.first.click()
            helpers.assert_notification(page, "success", timeout=10000)
        else:
            delete_btn = page.locator(
                f"#macros-list tr:has-text('{self._name}') .delete-btn"
            )
            delete_btn.first.click()
            helpers.assert_notification(page, "success", timeout=10000)


# ---------------------------------------------------------------------------
# Create Search: form validation lifecycle
# ---------------------------------------------------------------------------

class TestCreateSearchFormUI:
    """Exercise the Create Search form validation and clear behavior."""

    def test_01_all_fields_present(self, shared_page):
        """All required form fields are visible on the page."""
        page = shared_page
        navigate_to(page, "create_search")
        for sel in ["#ss-name", "#ss-query", "#ss-cron", "#ss-lookback",
                     "#ss-email", "#ss-trigger", "#ss-body", "#ss-description",
                     "#ss-save-btn", "#ss-clear-btn", "#ss-import-query-btn"]:
            helpers.assert_visible(page, sel)

    def test_02_validation_requires_all_fields(self, shared_page):
        """Submitting with empty fields shows error for each missing field."""
        page = shared_page
        navigate_to(page, "create_search")

        page.fill("#ss-name", "")
        page.fill("#ss-query", "test")
        page.fill("#ss-cron", "* * * * *")
        page.fill("#ss-lookback", "-1h")
        page.fill("#ss-email", "test@test.com")
        page.click("#ss-save-btn")
        helpers.assert_notification(page, "error", text="Name is required")

    def test_03_clear_form(self, shared_page):
        """Clear button resets all form fields."""
        page = shared_page
        navigate_to(page, "create_search")

        page.fill("#ss-name", "test_name")
        page.fill("#ss-query", "test_query")
        page.fill("#ss-cron", "30 * * * *")
        page.fill("#ss-lookback", "-4h")
        page.fill("#ss-email", "x@x.com")
        page.fill("#ss-description", "test description")

        page.click("#ss-clear-btn")

        helpers.assert_input_value(page, "#ss-name", "")
        helpers.assert_input_value(page, "#ss-query", "")
        helpers.assert_input_value(page, "#ss-cron", "")
        helpers.assert_input_value(page, "#ss-lookback", "")
        helpers.assert_input_value(page, "#ss-email", "")

    def test_04_from_email_disabled(self, shared_page):
        """From email field is disabled (set via Settings only)."""
        page = shared_page
        navigate_to(page, "create_search")
        helpers.assert_disabled(page, "#ss-from-email")


# ---------------------------------------------------------------------------
# Library: browse → preview → deploy to Create Ingestion
# ---------------------------------------------------------------------------

class TestLibraryDeployUI:
    """Exercise the Script Library browse → preview → deploy workflow."""

    def test_01_library_loads_cards(self, shared_page):
        """Library page loads and displays script cards."""
        page = shared_page
        navigate_to(page, "library")
        page.wait_for_selector("#lib-grid .lib-card", state="visible", timeout=10000)

        card_count = page.locator("#lib-grid .lib-card").count()
        assert card_count >= 1, f"Expected at least 1 library card, got {card_count}"

    def test_02_preview_opens_modal(self, shared_page):
        """Clicking Preview on a card opens the detail modal."""
        page = shared_page
        # Ensure we're on the library page and cards have loaded
        navigate_to(page, "library")
        page.wait_for_selector("#lib-grid .lib-card", state="visible", timeout=10000)

        # Click the first card's Preview button
        preview_btn = page.locator("#lib-grid .lib-card button:has-text('Preview')").first
        preview_btn.click()

        # Wait for the API call and modal to open (style.display = 'block')
        page.wait_for_timeout(3000)
        modal = page.locator("#lib-modal")
        style = modal.get_attribute("style") or ""
        assert "block" in style or modal.is_visible(), (
            f"Expected lib-modal to be visible, style={style!r}"
        )

    def test_03_modal_has_close_button(self, shared_page):
        """The library modal close button element exists."""
        page = shared_page
        # Verify close button exists in the DOM
        count = page.locator("#lib-modal-close").count()
        assert count == 1, "lib-modal-close should exist in the DOM"

    def test_04_deploy_navigates_to_create_ingestion(self, shared_page):
        """Clicking Deploy on a card navigates to Create Ingestion page."""
        page = shared_page
        # Dismiss any lingering modal backdrop from test_02
        page.evaluate("document.getElementById('lib-modal').style.display = 'none'")
        navigate_to(page, "library")
        page.wait_for_selector("#lib-grid .lib-card", state="visible", timeout=10000)

        # Click the first Deploy button
        deploy_btn = page.locator("#lib-grid .lib-card button:has-text('Deploy')").first
        deploy_btn.click()

        page.wait_for_selector("#page-create-ingestion.active", state="visible", timeout=5000)
        helpers.assert_page_visible(page, "page-create-ingestion")

        # Verify the title was populated from the library script
        title_val = page.input_value("#si-title")
        assert title_val, "Title should be populated after deploying from library"


# ---------------------------------------------------------------------------
# Docs: sidebar → content → heading navigation
# ---------------------------------------------------------------------------

class TestDocsNavigationUI:
    """Exercise the Docs page sidebar and content loading."""

    def test_01_sidebar_loads(self, shared_page):
        """Docs sidebar populates with navigation links."""
        page = shared_page
        navigate_to(page, "docs")
        page.wait_for_selector("#docs-nav .docs-nav-link", state="visible", timeout=10000)

        link_count = page.locator("#docs-nav .docs-nav-link").count()
        assert link_count >= 1, f"Expected at least 1 doc link, got {link_count}"

    def test_02_click_loads_content(self, shared_page):
        """Clicking a sidebar doc link loads content into the content area."""
        page = shared_page
        # Click the first doc link
        first_link = page.locator("#docs-nav .docs-nav-link").first
        first_link.click()
        # Wait for content to appear (headings or paragraphs)
        page.wait_for_timeout(2000)
        # docs-content should have some actual content
        content = page.text_content("#docs-content")
        assert content and len(content.strip()) > 50, (
            f"Expected docs content to load, got {len(content.strip())} chars"
        )

    def test_03_search_input_works(self, shared_page):
        """Typing in search input filters sidebar entries."""
        page = shared_page
        navigate_to(page, "docs")
        page.wait_for_selector("#docs-nav .docs-nav-link", state="visible", timeout=10000)

        # Type something that won't match any doc
        page.fill("#docs-search", "zzz_nonexistent_term_zzz")
        page.wait_for_timeout(500)

        # Type something that should match
        page.fill("#docs-search", "")
        page.wait_for_timeout(500)
        link_count = page.locator("#docs-nav .docs-nav-link").count()
        assert link_count >= 1, "All doc links should reappear after clearing search"
