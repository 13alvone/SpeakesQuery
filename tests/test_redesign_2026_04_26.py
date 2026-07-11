"""
Drift guards for the SpeakesQuery UI redesign shipped 2026-04-26 (1.0.0-rc1).

The four-wave visual redesign moved the SPA from functional-but-ad-hoc to
Grafana / Datadog / Splunk-class observability polish.  These tests pin
the design system primitives, chrome structure, status taxonomy, query
surface contracts, and accessibility baseline so accidental regressions
fail loud rather than slipping past a visual review.

Layout matches the four shipped waves:

* Wave 1 - layered tokens, Bulma drop, ``.btn`` system, sticky chrome,
  Lucide sprite, dark default, a11y baseline.
* Wave 2 - pills, banners, empty-state, ``.spinner`` / ``.skeleton``,
  refreshed tables, unified ``.dialog`` primitive.
* Wave 3 - query surface polish + collapsible fields sidebar.
* Wave 4 - chrome-button Lucide migration, ``<label for>`` pairings,
  ``prefers-reduced-motion`` hardening.

Tests at the bottom of the file (``TestRedesignBehavior``) exercise the
new interactive primitives end-to-end via Playwright - theme switching,
fields-sidebar collapse + localStorage persistence, and the chrome
skip-to-content link.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ui import helpers
from tests.ui.pages import navigate_to


UI_PATH = Path(__file__).parent.parent / "desktop_app" / "ui.html"


def _ui_text() -> str:
    return UI_PATH.read_text(encoding="utf-8")


def _reduced_motion_block(text: str) -> str:
    """Return the body of the ``@media (prefers-reduced-motion: reduce)`` block.

    The block lives inside the inline ``<style>`` and contains both the
    global ``*``-reset (Wave 1) and the per-element overrides (Wave 4).
    Returns ``""`` if the at-rule cannot be located.
    """
    m = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{",
        text,
    )
    if not m:
        return ""
    # Walk forward, tracking brace depth, to find the matching closing brace.
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return text[start:i]


# ════════════════════════════════════════════════════════════════════
# Wave 1 - Foundations: tokens, Bulma drop, .btn, chrome, sprite
# ════════════════════════════════════════════════════════════════════


class TestWave1Tokens:
    """Layered design tokens (color, spacing, type, radius, motion)."""

    def test_dark_is_default_theme(self):
        """The new default theme is dark (was light pre-redesign)."""
        text = _ui_text()
        assert re.search(r'<html\s+lang="en"\s+data-theme="dark"', text), (
            "Default theme should be dark. Look for "
            '`<html lang="en" data-theme="dark">` near the top of ui.html.'
        )

    def test_bulma_cdn_link_removed(self):
        """The Bulma CDN ``<link>`` was dropped - design system is self-contained."""
        text = _ui_text()
        assert "cdn.jsdelivr.net/npm/bulma" not in text, (
            "A Bulma CDN <link> was re-added to ui.html. The redesign retired "
            "this dependency; primitives now live inline. If you genuinely need "
            "Bulma back, bundle the CSS source rather than re-adding the CDN link."
        )

    def test_four_themes_defined(self):
        text = _ui_text()
        for theme in ("dark", "light", "night", "cyber"):
            assert f'[data-theme="{theme}"]' in text, (
                f"Theme `{theme}` missing from ui.html. All four themes "
                f'must remain defined as `[data-theme="{theme}"]` selector blocks.'
            )

    def test_spacing_scale_present(self):
        """4px-base spacing scale ``--space-0`` through ``--space-8``."""
        text = _ui_text()
        for stop in ("0", "1", "2", "3", "4", "5", "6", "7", "8"):
            assert f"--space-{stop}:" in text, (
                f"Spacing token `--space-{stop}` missing. The 4px-base scale "
                f"(0/4/8/12/16/24/32/48/64) is foundational to the design system."
            )

    def test_type_scale_present(self):
        """7-step modular type scale ``--text-2xs`` … ``--text-xl``."""
        text = _ui_text()
        for size in ("2xs", "xs", "sm", "base", "md", "lg", "xl"):
            assert f"--text-{size}:" in text, (
                f"Type token `--text-{size}` missing. The 7-step modular scale "
                f"is required by every component's typography."
            )

    def test_radius_scale_present(self):
        text = _ui_text()
        for stop in ("sm", "md", "lg", "full"):
            assert f"--radius-{stop}:" in text

    def test_motion_tokens_present(self):
        text = _ui_text()
        for token in (
            "--motion-fast",
            "--motion-base",
            "--motion-slow",
            "--ease-out",
            "--ease-in-out",
        ):
            assert f"{token}:" in text

    def test_layered_token_aliases_present(self):
        """Legacy tokens alias to new semantic tokens (non-disruptive refactor)."""
        text = _ui_text()
        assert re.search(r"--primary:\s*var\(--accent\)", text), (
            "Legacy alias `--primary: var(--accent)` missing. The layered "
            "alias pattern keeps existing component CSS working untouched."
        )
        assert re.search(r"--bg:\s*var\(--surface-0\)", text), (
            "Legacy alias `--bg: var(--surface-0)` missing."
        )
        assert re.search(r"--radius:\s*var\(--radius-md\)", text), (
            "Legacy alias `--radius: var(--radius-md)` missing."
        )

    def test_status_intent_tokens_present(self):
        """Five-intent status ladder: info / success / warn / error / critical."""
        text = _ui_text()
        for intent in ("info", "success", "warn", "error", "critical"):
            for slot in ("bg", "fg", "border"):
                token = f"--status-{intent}-{slot}"
                assert f"{token}:" in text, f"Status token `{token}` missing."

    def test_global_reduced_motion_at_rule(self):
        text = _ui_text()
        assert "@media (prefers-reduced-motion: reduce)" in text, (
            "`@media (prefers-reduced-motion: reduce)` rule missing. WCAG "
            "compliance requires honoring user motion preferences."
        )


class TestWave1Chrome:
    """48px sticky chrome bar with semantic landmarks."""

    def test_skip_to_content_link(self):
        text = _ui_text()
        assert 'class="skip-to-content"' in text, (
            "`<a class=\"skip-to-content\">` missing - keyboard users need a "
            "skip link as the first focusable element on the page."
        )
        assert 'href="#main-content"' in text

    def test_main_landmark_present(self):
        text = _ui_text()
        assert re.search(r'<main\s+id="main-content"\s+tabindex="-1">', text), (
            '`<main id="main-content" tabindex="-1">` missing. Required for '
            "the skip-link target and as the document's primary semantic landmark."
        )

    def test_chrome_role_banner(self):
        text = _ui_text()
        assert re.search(r'<header\s+class="app-chrome"\s+role="banner">', text), (
            '`<header class="app-chrome" role="banner">` missing. The chrome '
            "must declare a banner landmark for assistive tech."
        )

    def test_chrome_height_token(self):
        text = _ui_text()
        # Token may be visually aligned with extra whitespace inside the block,
        # so match whitespace-tolerantly.
        assert re.search(r"--chrome-height:\s*48px", text), (
            "Chrome height token `--chrome-height: 48px` missing or wrong value."
        )

    def test_theme_switcher_buttons(self):
        text = _ui_text()
        for theme in ("dark", "light", "night", "cyber"):
            pattern = rf'class="theme-btn[^"]*"\s+data-theme="{theme}"'
            assert re.search(pattern, text), (
                f"Theme-switcher button for `{theme}` missing or malformed. "
                "Each theme requires a `<button class=\"theme-btn\" "
                f"data-theme=\"{theme}\">`."
            )

    def test_dark_button_active_by_default(self):
        text = _ui_text()
        assert re.search(
            r'class="theme-btn active"\s+data-theme="dark"', text
        ), (
            "Default `.active` class should be on the dark theme-btn "
            "(matches `<html data-theme=\"dark\">`)."
        )

    def test_lucide_sprite_root_present(self):
        text = _ui_text()
        assert 'class="icon-sprite"' in text, (
            "`<svg class=\"icon-sprite\">` missing. Lucide icons need a "
            "single inline sprite for `<use href=\"#i-…\"/>` references."
        )

    def test_w1_lucide_icons_in_sprite(self):
        text = _ui_text()
        for icon in (
            "i-sun",
            "i-moon",
            "i-flame",
            "i-sparkles",
            "i-clock",
            "i-search",
            "i-x",
            "i-chevron-down",
        ):
            assert f'id="{icon}"' in text, (
                f"Lucide icon symbol `{icon}` missing from sprite (Wave 1 set)."
            )


class TestWave1Buttons:
    """`.btn` system + legacy `.button.is-*` parity."""

    def test_btn_shapes_defined(self):
        text = _ui_text()
        for shape in ("solid", "subtle", "ghost", "outline"):
            assert f".btn--{shape}" in text, (
                f"Button shape `.btn--{shape}` missing."
            )

    def test_btn_sizes_defined(self):
        text = _ui_text()
        for size in ("sm", "md", "lg"):
            assert f".btn--{size}" in text

    def test_btn_intents_defined(self):
        text = _ui_text()
        for intent in ("accent", "success", "danger"):
            assert re.search(rf"\.btn--{intent}\.btn--solid", text), (
                f"Button intent `.btn--{intent}.btn--solid` missing."
            )

    def test_btn_icon_variant(self):
        text = _ui_text()
        assert ".btn--icon" in text

    def test_legacy_button_intents_styled(self):
        """Bulma is dropped; we own every `.button.is-*` style now."""
        text = _ui_text()
        for intent in (
            "primary",
            "link",
            "light",
            "info",
            "success",
            "warning",
            "danger",
        ):
            assert f".button.is-{intent}" in text, (
                f"Legacy `.button.is-{intent}` selector missing styling. "
                f"Bulma was dropped, so we must provide our own rule."
            )


class TestWave1BulmaPrimitives:
    """Classes still in markup that Bulma used to provide must have own CSS."""

    def test_section_primitive(self):
        text = _ui_text()
        assert re.search(r"\.section\s*\{", text)
        assert ".section.is-fluid" in text

    def test_container_primitive(self):
        text = _ui_text()
        assert re.search(r"\.container\s*\{", text)
        assert ".container.is-fluid" in text

    def test_columns_primitive(self):
        text = _ui_text()
        assert re.search(r"\.columns\s*\{", text)
        assert re.search(r"\.column\s*\{", text)
        assert ".column.is-one-quarter" in text

    def test_form_primitives(self):
        text = _ui_text()
        for cls in (".input", ".field", ".control", ".label"):
            pattern = rf"{re.escape(cls)}\s*[,{{]"
            assert re.search(pattern, text), (
                f"Form primitive `{cls}` missing CSS rule. Required for the "
                f"SPA forms to render."
            )

    def test_box_primitive(self):
        text = _ui_text()
        assert re.search(r"\.box\s*\{", text)

    def test_notification_intents(self):
        text = _ui_text()
        for intent in ("primary", "info", "success", "warning", "danger"):
            assert f".notification.is-{intent}" in text


# ════════════════════════════════════════════════════════════════════
# Wave 2 - Display primitives: pills, banners, empty-state, dialog
# ════════════════════════════════════════════════════════════════════


class TestWave2StatusPills:
    def test_pill_5_intent_ladder(self):
        text = _ui_text()
        for intent in ("info", "success", "warn", "error", "critical"):
            assert f".pill--{intent}" in text, (
                f"Status pill intent `.pill--{intent}` missing."
            )

    def test_status_badge_legacy_aliases(self):
        text = _ui_text()
        for cls in (
            ".status-badge.success",
            ".status-badge.failed",
            ".status-badge.info",
            ".status-badge.warn",
            ".status-badge.error",
            ".status-badge.critical",
        ):
            assert cls in text, (
                f"`{cls}` missing. The legacy `.status-badge` taxonomy "
                "must extend the new 5-intent ladder."
            )

    def test_pill_left_bar_secondary_signal(self):
        """Color is paired with a left bar so meaning isn't carried by hue alone."""
        text = _ui_text()
        m = re.search(
            r"\.pill,\s*\.status-badge\s*\{[^}]*border-left:\s*2px",
            text,
            re.DOTALL,
        )
        assert m, (
            "`.pill` / `.status-badge` should set a 2px `border-left` as a "
            "secondary signal beyond color (color-blind safe)."
        )


class TestWave2Banner:
    def test_banner_5_intent(self):
        text = _ui_text()
        for intent in ("info", "success", "warn", "error", "critical"):
            assert f".banner--{intent}" in text

    def test_banner_slots(self):
        text = _ui_text()
        for slot in (
            "__icon",
            "__body",
            "__title",
            "__message",
            "__actions",
            "__close",
        ):
            assert f".banner{slot}" in text


class TestWave2EmptyState:
    def test_empty_state_slots(self):
        text = _ui_text()
        for slot in ("__icon", "__title", "__body", "__actions"):
            assert f".empty-state{slot}" in text


class TestWave2SpinnerSkeleton:
    def test_spinner_paired_with_legacy_alias(self):
        """`.spinner` (new) and `.spin-ring` (legacy alias) share the same rule."""
        text = _ui_text()
        assert re.search(r"\.spinner,\s*\n?\s*\.spin-ring", text), (
            "Both `.spinner` and `.spin-ring` should be styled in the same "
            "comma-grouped rule (legacy alias pattern)."
        )

    def test_spinner_sizes(self):
        text = _ui_text()
        assert ".spinner--sm" in text
        assert ".spinner--lg" in text

    def test_skeleton_variants(self):
        text = _ui_text()
        for variant in ("--text", "--title", "--block", "--circle", "--row"):
            assert f".skeleton{variant}" in text


class TestWave2Tables:
    def test_data_table_modifiers(self):
        text = _ui_text()
        assert ".data-table--compact" in text
        assert ".data-table--no-stripe" in text

    def test_data_table_uppercase_headers(self):
        """Grafana-class small-caps header convention."""
        text = _ui_text()
        m = re.search(
            r"\.data-table\s+th\s*\{[^}]*text-transform:\s*uppercase",
            text,
            re.DOTALL,
        )
        assert m, (
            "`.data-table th` should set `text-transform: uppercase` "
            "(Grafana-class small-caps header convention)."
        )


class TestWave2Dialog:
    def test_dialog_three_sizes(self):
        text = _ui_text()
        for size in ("sm", "md", "lg"):
            assert f".dialog--{size}" in text

    def test_dialog_slots(self):
        text = _ui_text()
        for slot in (
            "__backdrop",
            "__header",
            "__title",
            "__close",
            "__body",
            "__footer",
        ):
            assert f".dialog{slot}" in text

    def test_modal_backdrop_harmonization(self):
        """The three legacy modal patterns share a single backdrop tone rule."""
        text = _ui_text()
        m = re.search(
            r"\.welcome-backdrop,\s*\n?\s*\.yaml-modal-backdrop,\s*\n?\s*"
            r"\.history-modal-backdrop\s*\{",
            text,
        )
        assert m, (
            "Backdrop harmonization rule missing. The three legacy modal "
            "patterns (welcome / yaml-modal / history-modal) should share a "
            "single backdrop tone via a comma-grouped selector."
        )


class TestWave2LucideIcons:
    def test_w2_icons_in_sprite(self):
        text = _ui_text()
        for icon in (
            "i-check",
            "i-check-circle",
            "i-alert-triangle",
            "i-info",
            "i-x-circle",
            "i-octagon-alert",
            "i-loader",
            "i-inbox",
            "i-chevron-right",
        ):
            assert f'id="{icon}"' in text, (
                f"Lucide icon symbol `{icon}` missing from sprite (Wave 2 set)."
            )


# ════════════════════════════════════════════════════════════════════
# Wave 3 - Query surface
# ════════════════════════════════════════════════════════════════════


class TestWave3QueryField:
    def test_query_uses_mono_font_token(self):
        text = _ui_text()
        m = re.search(
            r"#query\s*\{[^}]*font-family:\s*var\(--font-mono\)",
            text,
            re.DOTALL,
        )
        assert m, (
            "`#query` should use `var(--font-mono)`, not a hardcoded font stack."
        )

    def test_query_focus_uses_accent_ring(self):
        text = _ui_text()
        m = re.search(
            r"#query:focus[^}]*box-shadow:\s*0\s+0\s+0\s+2px\s+var\(--accent-ring\)",
            text,
            re.DOTALL,
        )
        assert m, "`#query:focus` should apply a 2px `var(--accent-ring)` outset."

    def test_qf_toggle_accent_on_checked(self):
        text = _ui_text()
        assert ".qf-toggle:has(input:checked)" in text, (
            "`.qf-toggle:has(input:checked)` missing. The auto-format toggle "
            "should change to the accent color when checked, providing visual "
            "feedback that the toggle is on."
        )


class TestWave3JobIdBar:
    def test_job_id_bar_default_hidden(self):
        text = _ui_text()
        m = re.search(r"#job-id-bar\s*\{[^}]*display:\s*none", text, re.DOTALL)
        assert m, "`#job-id-bar` default rule should set `display: none`."

    def test_job_id_bar_active_activator(self):
        """Caught as a self-inflicted bug in Wave 3 when first migrated."""
        text = _ui_text()
        assert "#job-id-bar.active" in text, (
            "`#job-id-bar.active` activator rule missing. Without it, the "
            "element is permanently hidden after migrating from inline "
            "`style=\"display:none\"` to class-based visibility. (Caught + "
            "fixed in Wave 3 mid-test-run.)"
        )


class TestWave3FieldsSidebar:
    def test_fields_sidebar_collapsible_css(self):
        text = _ui_text()
        assert "#fields-sidebar.is-collapsed" in text, (
            "`#fields-sidebar.is-collapsed` CSS rule missing. The collapsible "
            "behavior depends on this class toggling the sidebar narrower."
        )

    def test_fields_toggle_button_in_markup(self):
        text = _ui_text()
        assert re.search(
            r'<button\s+id="fields-toggle-btn"[^>]*class="[^"]*fields-toggle',
            text,
        ), (
            '`<button id="fields-toggle-btn" class="fields-toggle">` missing '
            "from #fields-sidebar markup."
        )

    def test_fields_toggle_uses_chevron_icon(self):
        text = _ui_text()
        m = re.search(
            r'id="fields-toggle-btn"[^>]*>.*?<use\s+href="#i-chevron-right"',
            text,
            re.DOTALL,
        )
        assert m, (
            "Fields-toggle button should reference `<use href=\"#i-chevron-right\"/>`."
        )

    def test_fields_toggle_localstorage_persistence(self):
        text = _ui_text()
        assert "speakesquery_fields_collapsed" in text, (
            "Fields-toggle state should persist via `localStorage` key "
            "`speakesquery_fields_collapsed`."
        )


# ════════════════════════════════════════════════════════════════════
# Wave 4 - Lucide chrome migration + a11y + reduced-motion hardening
# ════════════════════════════════════════════════════════════════════


class TestWave4LucideChromeIcons:
    def test_w4_icons_in_sprite(self):
        text = _ui_text()
        for icon in (
            "i-play",
            "i-save",
            "i-copy",
            "i-calendar",
            "i-database",
            "i-file-text",
            "i-wand",
        ):
            assert f'id="{icon}"' in text, (
                f"Lucide icon symbol `{icon}` missing from sprite (Wave 4 set)."
            )

    @pytest.mark.parametrize(
        "btn_id, icon",
        [
            ("run-query-btn", "i-play"),
            ("save-job-btn", "i-save"),
            ("copy-loadjob-btn", "i-copy"),
            ("schedule-search-btn", "i-calendar"),
            ("expand-macros-btn", "i-wand"),
            ("time-chooser-btn", "i-clock"),
        ],
    )
    def test_chrome_button_uses_lucide_icon(self, btn_id: str, icon: str):
        text = _ui_text()
        m = re.search(
            rf'id="{btn_id}"[^>]*>.*?<use\s+href="#{icon}"',
            text,
            re.DOTALL,
        )
        assert m, (
            f"`#{btn_id}` should contain `<use href=\"#{icon}\"/>`. The "
            f"Wave 4 emoji-to-Lucide migration replaced the prior emoji "
            f"affordance with a sprite icon."
        )

    def test_time_chooser_label_span(self):
        """Inner span lets JS update label text without wiping the icon."""
        text = _ui_text()
        assert re.search(
            r'<span\s+id="time-chooser-label">All Time</span>', text
        ), (
            '`<span id="time-chooser-label">All Time</span>` missing. The '
            "inner span is required so `tcUpdateLabel()` can change the text "
            "without erasing the SVG icon."
        )


class TestWave4LabelForA11y:
    @pytest.mark.parametrize(
        "input_id",
        [
            "es-smtp-user",
            "es-smtp-password",
            "es-smtp-from",
            "es-test-to",
            "save-job-name",
            "save-job-ttl",
            "cks-api-key",
        ],
    )
    def test_label_for_pairing(self, input_id: str):
        text = _ui_text()
        assert f'<label for="{input_id}"' in text, (
            f"`<label for=\"{input_id}\">` missing. Screen readers cannot "
            f"announce the input's purpose without the for/id pairing."
        )

    def test_gmail_input_username_autocomplete(self):
        """Required for password-manager pairing with es-smtp-password."""
        text = _ui_text()
        m = re.search(
            r'id="es-smtp-user"[^>]*autocomplete="username"', text
        )
        assert m, (
            '`#es-smtp-user` should have `autocomplete="username"` so '
            "password managers properly pair it with the App Password field. "
            "(Without this, autofill may leave Gmail empty or misalign credentials.)"
        )

    def test_time_chooser_aria_label(self):
        text = _ui_text()
        assert re.search(
            r'id="time-chooser-btn"[^>]*aria-label="Time range"', text
        )

    def test_server_clock_aria_live(self):
        """The server clock badge updates dynamically; AT needs aria-live."""
        text = _ui_text()
        m = re.search(
            r'id="server-clock-badge"[^>]*aria-live="polite"', text, re.DOTALL
        )
        assert m, (
            '`#server-clock-badge` should have `aria-live="polite"` so screen '
            "readers announce time updates without interrupting other speech."
        )

    @pytest.mark.parametrize(
        "btn_id, expected_text",
        [
            ("run-query-btn", "Run Query"),
            ("save-job-btn", "Save Job"),
            ("copy-loadjob-btn", "Copy loadjob"),
            ("schedule-search-btn", "Schedule This Search"),
            ("expand-macros-btn", "Expand Macros"),
        ],
    )
    def test_chrome_button_retains_visible_text(
        self, btn_id: str, expected_text: str
    ):
        """Icon-paired buttons must still carry the visible text label.

        The icons are decorative; the text is what announces. Removing the
        text would force screen-reader users onto an `aria-label` alone,
        and it would silently degrade the button's discoverability for all
        users on themes / fonts where the icon is ambiguous.
        """
        text = _ui_text()
        m = re.search(
            rf'id="{btn_id}"[^>]*>(.*?)</button>',
            text,
            re.DOTALL,
        )
        assert m, f"Could not find `<button id=\"{btn_id}\">…</button>` block."
        body = m.group(1)
        assert expected_text in body, (
            f"`#{btn_id}` should still carry the visible text "
            f'"{expected_text}" alongside its Lucide icon.'
        )


class TestWave4ReducedMotion:
    """Explicit per-element animation overrides under prefers-reduced-motion."""

    def test_block_present(self):
        block = _reduced_motion_block(_ui_text())
        assert block, (
            "`@media (prefers-reduced-motion: reduce)` block could not be "
            "located. The Wave 4 hardening lives inside this at-rule."
        )

    def test_spinner_animation_off(self):
        block = _reduced_motion_block(_ui_text())
        m = re.search(
            r"\.spinner,\s*\n?\s*\.spin-ring\s*\{[^}]*animation:\s*none",
            block,
            re.DOTALL,
        )
        assert m, (
            "Under `prefers-reduced-motion`, `.spinner` / `.spin-ring` should "
            "have `animation: none` (not just frame-locked rotation from the "
            "global `*`-rule)."
        )

    def test_skeleton_animation_off(self):
        block = _reduced_motion_block(_ui_text())
        m = re.search(
            r"\.skeleton\s*\{[^}]*animation:\s*none", block, re.DOTALL
        )
        assert m, (
            "Under `prefers-reduced-motion`, `.skeleton` should have "
            "`animation: none` and a flat fallback background."
        )

    @pytest.mark.parametrize(
        "selector",
        [
            ".dialog",
            ".welcome-panel",
            ".email-setup-panel",
            ".yaml-modal-content",
            ".history-modal-content",
        ],
    )
    def test_modal_animations_off(self, selector: str):
        block = _reduced_motion_block(_ui_text())
        assert selector in block, (
            f"`{selector}` missing from the prefers-reduced-motion overrides. "
            "Modal entry/exit animations should be disabled for users who "
            "prefer reduced motion."
        )


# ════════════════════════════════════════════════════════════════════
# Behavioral coverage - a few Playwright tests for the new interactive
# pieces that drift guards can't catch as text.
# ════════════════════════════════════════════════════════════════════


class TestRedesignBehavior:
    """End-to-end smoke for the new redesign interactions."""

    def test_skip_to_content_link_targets_main(self, page):
        """Clicking the skip link should focus / scroll to ``#main-content``."""
        helpers.assert_visible(page, "#main-content")
        # The skip link is visually hidden until focused - assert the markup
        # exists with the right href so keyboard users can reach it.
        link = page.locator("a.skip-to-content")
        assert link.count() == 1, "exactly one skip-to-content link expected"
        assert link.get_attribute("href") == "#main-content"

    def test_chrome_is_sticky_header_role_banner(self, page):
        """Chrome must declare role=banner so AT identifies it as the page header."""
        chrome = page.locator("header.app-chrome[role='banner']")
        assert chrome.count() == 1
        assert chrome.is_visible()

    def test_dark_theme_is_default_on_first_load(self, page):
        """First-load default is dark; localStorage will only override on returning visits."""
        page.evaluate("() => localStorage.removeItem('speakesquery_theme')")
        page.reload(wait_until="domcontentloaded")
        theme = page.evaluate(
            "() => document.documentElement.getAttribute('data-theme')"
        )
        assert theme == "dark", f"Expected default theme 'dark', got {theme!r}"

    def test_no_bulma_cdn_request_at_runtime(self, page):
        """No external CDN should be hit for design-system CSS at page load.

        Wave 1 dropped the Bulma CDN in favor of self-contained primitives.
        A network watcher confirms the runtime contract - drift guards on
        the source file alone wouldn't catch a stray <link> added via JS.
        """
        bulma_requests: list[str] = []
        page.on(
            "request",
            lambda req: bulma_requests.append(req.url)
            if "bulma" in req.url.lower()
            else None,
        )
        page.reload(wait_until="domcontentloaded")
        # Give in-flight requests a moment to surface in the listener.
        page.wait_for_timeout(250)
        assert not bulma_requests, (
            f"Bulma CDN request(s) detected at runtime: {bulma_requests}. "
            "The design system is supposed to be fully self-contained."
        )


class TestThemeSwitcherBehavior:
    """Each theme button should set ``<html data-theme>`` and persist via localStorage."""

    @pytest.mark.parametrize("theme", ["light", "night", "cyber", "dark"])
    def test_theme_button_updates_html_attribute(self, shared_page, theme: str):
        page = shared_page
        page.click(f'.theme-btn[data-theme="{theme}"]')
        # Wait briefly for the attribute to update.
        page.wait_for_function(
            f"() => document.documentElement.getAttribute('data-theme') === '{theme}'",
            timeout=2000,
        )
        active = page.evaluate(
            "() => document.querySelector('.theme-btn.active').getAttribute('data-theme')"
        )
        assert active == theme, (
            f"After clicking the {theme} theme button, the .active class "
            f"should follow it (got {active!r})."
        )


class TestFieldsSidebarToggleBehavior:
    """Collapsible fields sidebar - toggle behavior + localStorage persistence.

    The sidebar only becomes visible (``.active`` class) after a query runs,
    so each test that needs to click the toggle must first run a query.
    """

    _query = (
        'index="indexes/default_test/output_parquets/test0.parquet" | head 2'
    )

    def _run_query_and_wait_for_sidebar(self, page) -> None:
        page.fill("#query", self._query)
        page.click("#run-query-btn")
        page.wait_for_selector(
            "#fields-sidebar.active", state="visible", timeout=15000
        )

    def test_01_collapse_persists_to_localstorage(self, shared_page):
        page = shared_page
        navigate_to(page, "query")
        # Reset persisted state so this test starts from a known-good baseline.
        page.evaluate(
            "() => localStorage.removeItem('speakesquery_fields_collapsed')"
        )
        page.reload(wait_until="domcontentloaded")
        navigate_to(page, "query")
        self._run_query_and_wait_for_sidebar(page)

        # Sidebar should be expanded by default.
        assert not page.evaluate(
            "() => document.getElementById('fields-sidebar').classList.contains('is-collapsed')"
        )
        # And the toggle button should announce that it's currently expanded.
        aria_before = page.get_attribute("#fields-toggle-btn", "aria-expanded")
        assert aria_before == "true", (
            f'Expected aria-expanded="true" before collapse, got {aria_before!r}'
        )

        # Click toggle → collapses.
        page.click("#fields-toggle-btn")
        page.wait_for_function(
            "() => document.getElementById('fields-sidebar').classList.contains('is-collapsed')",
            timeout=2000,
        )

        # aria-expanded must follow the visual state - required for screen-reader
        # users to know the panel is now collapsed.
        aria_after = page.get_attribute("#fields-toggle-btn", "aria-expanded")
        assert aria_after == "false", (
            f'Expected aria-expanded="false" after collapse, got {aria_after!r}'
        )

        # localStorage should now record the collapsed state.
        stored = page.evaluate(
            "() => localStorage.getItem('speakesquery_fields_collapsed')"
        )
        assert stored == "1", (
            f"Expected localStorage flag '1' after collapse, got {stored!r}"
        )

    def test_02_state_restores_after_reload_then_expand(self, shared_page):
        page = shared_page
        # Reload - `is-collapsed` should restore from localStorage at startup.
        page.reload(wait_until="domcontentloaded")
        navigate_to(page, "query")
        # Run another query so the sidebar becomes visible (otherwise Playwright
        # cannot click the toggle - the parent `#fields-sidebar` is `display:
        # none` until results are loaded).
        self._run_query_and_wait_for_sidebar(page)

        is_collapsed = page.evaluate(
            "() => document.getElementById('fields-sidebar').classList.contains('is-collapsed')"
        )
        assert is_collapsed, (
            "After reloading the page, `#fields-sidebar` should still carry "
            "the `is-collapsed` class (restored from localStorage)."
        )

        # Click toggle → expand again, and the localStorage flag flips.
        page.click("#fields-toggle-btn")
        page.wait_for_function(
            "() => !document.getElementById('fields-sidebar').classList.contains('is-collapsed')",
            timeout=2000,
        )
        stored_after = page.evaluate(
            "() => localStorage.getItem('speakesquery_fields_collapsed')"
        )
        assert stored_after == "0", (
            f"Expected localStorage flag '0' after expanding, got {stored_after!r}"
        )
