"""
Regression tests for the Schedule PDF audit fixes shipped 2026-05-01.

Each test pins one of the 9 issues from the user's audit so we don't
re-introduce them. Most assertions are HTML/CSS/SVG string-presence
checks against the rendered template - much faster than re-rendering
the whole PDF, and the contracts they pin are exactly the ones that
were broken.

End-to-end PDF binary inspection (page count, byte structure) lives in
``test_schedule_pdf.py`` and is unaffected by these patches.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_summary() -> tuple[dict, list[dict]]:
    """A minimal but realistic summary + buckets payload to feed _render_html."""
    return (
        {
            'generated_at_epoch': int(datetime(
                2026, 5, 1, 20, 57, tzinfo=timezone.utc,
            ).timestamp()),
            'lookahead_days': 7,
            'history_lookback_runs': 5,
            'jobs': [
                {
                    'kind': 'ingestion',
                    'name': 'job_alpha',
                    # Long cron so we exercise the nowrap + column-width
                    # logic - historically this would wrap to 3 lines and
                    # split across page breaks.
                    'cron': '30 10,15 * * 1-5',
                    'next_firing_epoch': 9999999999,
                    'next_firing_iso': '2026-05-02T05:00:00+00:00',
                    'firings_in_lookahead': 14,
                    'avg_row_count': 12,
                    'avg_duration_ms': 250,
                    'run_count': 5,
                    'disabled': False,
                },
                {
                    'kind': 'alert_group',
                    'name': 'options_edge_brief',
                    'cron': '30 10,15 * * 1-5',
                    'next_firing_epoch': 9999999999,
                    'next_firing_iso': '2026-05-02T10:30:00+00:00',
                    'firings_in_lookahead': 10,
                    'avg_row_count': None,
                    'avg_duration_ms': 2650000,  # ≥ 30s - latency outlier
                    'run_count': 5,
                    'disabled': False,
                    'feeder_count': 6,
                },
                {
                    'kind': 'saved_search',
                    'name': 'disabled_search',
                    'cron': '0 12 * * *',
                    'next_firing_epoch': 9999999999,
                    'next_firing_iso': '2026-05-03T12:00:00+00:00',
                    'firings_in_lookahead': 1,
                    'avg_row_count': None,
                    'avg_duration_ms': 1000,
                    'run_count': 3,
                    'disabled': True,
                },
            ],
            'hour_distribution': {
                'by_dow_hour': {d: [1] * 24 for d in range(7)},
                'by_hour_total': [7] * 24,
                'total_firings': 7 * 24,
            },
            'data_distribution': {
                'by_dow_hour': {d: [10] * 24 for d in range(7)},
                'by_dow_hour_has_data': {d: [True] * 24 for d in range(7)},
                'by_hour_total': [70] * 24,
            },
            'summary': {
                'total_jobs': 3,
                'total_jobs_disabled': 1,
                'by_kind': {'ingestion': 1, 'saved_search': 1, 'alert_group': 1},
                'busiest_hour_utc': 5,
                'busiest_hour_count': 7,
                'biggest_data_hour_utc': 0,
                'biggest_data_hour_total': 70,
            },
        },
        [
            {'date': '2026-04-25', 'ingestion_runs': 3, 'search_runs': 2,
             'ag_dispatches': 1, 'rows_ingested': 100},
            {'date': '2026-04-26', 'ingestion_runs': 5, 'search_runs': 4,
             'ag_dispatches': 2, 'rows_ingested': 250},
        ],
    )


@pytest.fixture
def rendered_html() -> str:
    from tools.schedule_pdf import _render_html
    summary, buckets = _make_summary()
    return _render_html(summary, buckets, project_root=PROJECT_ROOT)


# ── Issue #1 + #2: charts must actually render in the PDF ─────────────
class TestSvgSizingForWeasyPrint:
    """Both heatmap and activity-chart SVGs were emitting an SVG attribute
    `height="auto"` - invalid SVG syntax (height must be a length on the
    element). Browsers tolerated it but WeasyPrint silently collapsed
    the SVG to zero height, leaving empty boxes on pages 4-5 of the PDF.
    Drive sizing via CSS in `style=` instead."""

    def test_heatmap_svg_does_not_use_invalid_height_auto_attribute(
        self, rendered_html
    ):
        # No SVG <svg ...> tag should carry height="auto" as an attribute
        # - that's invalid SVG and was the root cause of the missing
        # heatmaps in the earlier PDF render.
        for m in re.finditer(r'<svg\b[^>]*>', rendered_html):
            assert 'height="auto"' not in m.group(0), (
                f'SVG attribute height="auto" is invalid; sets the SVG '
                f'to zero height in WeasyPrint. Tag: {m.group(0)[:200]}'
            )

    def test_heatmap_svg_drives_sizing_via_css_style(self, rendered_html):
        # Heatmap SVG must drive sizing via CSS - that's the pattern that
        # actually renders in WeasyPrint.
        assert re.search(
            r'<svg\b[^>]*style="[^"]*width:\s*100%\s*;\s*height:\s*auto',
            rendered_html,
        ), 'Heatmap SVG must use style="width:100%;height:auto" for sizing'

    def test_logo_svg_drives_sizing_via_css_style(self, rendered_html):
        # Logo SVG previously had an explicit pixel width attribute that
        # caused the wordmark to clip past the page edge. Now driven via
        # CSS so the .cover-logo container's max-width controls.
        # (viewBox widened 840→940 on 2026-07-10 for the longer
        # "Speakes" wordmark.)
        m = re.search(r'<svg\b[^>]*viewBox="0 0 940 210"[^>]*>', rendered_html)
        assert m, 'Logo SVG with viewBox 940×210 expected'
        tag = m.group(0)
        assert 'width="940"' not in tag, (
            'Logo SVG must NOT carry width="940" attribute - that '
            'over-rode CSS scaling and clipped the wordmark.'
        )
        assert 'style=' in tag and 'width:100%' in tag, (
            'Logo must size via CSS style attribute'
        )


# ── Issue #3: cover layout (blank page 1, wordmark clip, page-3 bleed) ─
class TestCoverPageLayout:

    def test_report_date_anchor_lives_inside_cover(self, rendered_html):
        """Anchor MUST be inside .cover. When it preceded the cover as a
        sibling, WeasyPrint rendered an empty 0-height block as page 1
        and pushed the cover to page 2 with a footer - that produced
        the blank page 1 + cover-bleed-to-page-3 cluster of bugs."""
        # Find positions of the cover opening tag and the anchor.
        cover_start = rendered_html.find('<div class="cover">')
        cover_end_idx = rendered_html.find(
            '</div>', rendered_html.find('cover-accent-strip'),
        )
        anchor_idx = rendered_html.find('class="report-date-anchor"')
        assert cover_start != -1, 'Cover div not found'
        assert anchor_idx != -1, 'Report date anchor not found'
        assert cover_start < anchor_idx < cover_end_idx, (
            'report-date-anchor MUST live INSIDE .cover, otherwise the '
            'empty anchor element claims page 1 and bumps the cover off.'
        )

    def test_cover_anchor_is_inline_not_block(self, rendered_html):
        """The anchor must use a tag that doesn't add layout weight.
        A <div> behaved as a block element with implicit height in some
        WeasyPrint paths; <span> reliably renders as inline 0-content."""
        assert '<span class="report-date-anchor"' in rendered_html, (
            'Anchor must be a <span> (inline), not a <div>'
        )

    def test_cover_height_caps_page_overflow(self):
        """Cover height must be set so it CANNOT overflow a Letter page
        (279.4mm tall) under @page :first margin:0 - a min-height of
        250mm with a tall cover-meta-grid was overflowing onto page 3."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        # The .cover rule should set an explicit height that fits within
        # one Letter page (≤ 279.4mm) but is large enough to leave room
        # for content (> 240mm).
        m = re.search(r'\.cover\s*\{([^}]+)\}', css, re.DOTALL)
        assert m, '.cover rule not found'
        block = m.group(1)
        height_match = re.search(r'height:\s*(\d+)mm', block)
        assert height_match, '.cover must set an explicit height'
        height_mm = int(height_match.group(1))
        assert 240 <= height_mm <= 279, (
            f'.cover height={height_mm}mm - must be between 240 and 279 '
            f'so it fits one Letter page without overflowing.'
        )

    def test_cover_logo_max_width_constrains_inline_svg(self):
        """The cover-logo container must constrain the SVG so the wordmark
        can't overflow horizontally past the cover edge (the original
        bug clipped 'SpeakesQuery' to 'Spe…Que' off the right margin)."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(r'\.cover-logo\s*\{([^}]+)\}', css, re.DOTALL)
        assert m
        block = m.group(1)
        assert 'max-width' in block or re.search(r'width:\s*\d', block), (
            '.cover-logo must constrain the inner SVG width to avoid '
            'horizontal overflow.'
        )


# ── Issue #4: appendix rows must not split across page breaks ─────────
class TestAppendixRowKeepsTogether:

    def test_table_row_has_break_inside_avoid(self):
        """Each appendix <tr> must carry break-inside:avoid so multi-line
        cells (CRON, NEXT RUN) can never split a row across pages."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        # Match `table.jobs tr { ... }` or `table.jobs tr {...}` - must
        # contain a break-inside or page-break-inside avoid declaration.
        m = re.search(
            r'table\.jobs\s+tr\s*\{([^}]+)\}', css, re.DOTALL,
        )
        assert m, 'table.jobs tr selector not found'
        block = m.group(1)
        assert ('page-break-inside: avoid' in block
                or 'break-inside: avoid' in block), (
            'table.jobs tr must declare break-inside:avoid'
        )


# ── Issue #5: cron column must not wrap ───────────────────────────────
class TestCronColumnNoWrap:

    def test_cron_cell_has_nowrap(self):
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(
            r'table\.jobs\s+td\.cron\s*\{([^}]+)\}', css, re.DOTALL,
        )
        assert m, 'td.cron rule not found'
        block = m.group(1)
        assert 'white-space: nowrap' in block or 'white-space:nowrap' in block

    def test_table_uses_fixed_layout(self):
        """Without table-layout:fixed, the cron column expands to fit
        its longest content under auto layout and pushes the STATE
        column off the right edge."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(r'table\.jobs\s*\{([^}]+)\}', css, re.DOTALL)
        assert m, 'table.jobs rule not found'
        assert 'table-layout: fixed' in m.group(1), (
            'table.jobs must use table-layout:fixed so column widths '
            'are honored exactly.'
        )

    def test_appendix_table_has_colgroup(self, rendered_html):
        """Explicit <colgroup> with widths is the only reliable way to
        keep all 9 columns on the page under fixed layout."""
        # Find the appendix table block
        appendix_idx = rendered_html.find('All Scheduled Jobs')
        assert appendix_idx != -1
        tail = rendered_html[appendix_idx:]
        assert '<colgroup>' in tail
        # Should declare exactly 9 <col ...> elements (one per column)
        col_count = tail[:tail.find('</colgroup>')].count('<col ')
        assert col_count == 9, f'Expected 9 <col> elements, got {col_count}'


# ── Issue #6: cover footer suppression (Page X of N starts at cover) ──
class TestCoverFooterSuppression:

    def test_first_page_rule_suppresses_footer(self):
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(
            r'@page\s+:first\s*\{([^{}]*(?:\{[^}]*\}[^{}]*)*)\}',
            css, re.DOTALL,
        )
        assert m, '@page :first rule not found'
        block = m.group(1)
        # Footer slots must be content:none on the cover page
        assert '@bottom-right { content: none' in block, (
            'Cover page must suppress the bottom-right page counter.'
        )
        assert '@top-center { content: none' in block, (
            'Cover page must suppress the top-center running header.'
        )


# ── Issue #7: no tofu glyphs in section headings ──────────────────────
class TestNoTofuGlyphs:

    def test_latency_outliers_uses_inter_safe_glyph(self, rendered_html):
        """The previous ⏱ stopwatch (U+23F1) rendered as a tofu box
        because WeasyPrint's default font stack lacks an emoji font.
        Replace with a glyph that's covered by Inter / Segoe UI."""
        # Locate the latency-outliers H3
        m = re.search(
            r'<h3>([^<]*Latency outliers)</h3>', rendered_html,
        )
        assert m, 'Latency outliers heading not found'
        heading = m.group(1)
        # Must NOT contain the broken stopwatch glyph
        assert '⏱' not in heading, (
            'Stopwatch glyph U+23F1 ⏱ is not in the default WeasyPrint '
            'font stack - renders as tofu.'
        )
        # Must be a recognised Inter-safe symbol like ▲ / ⚠ / ●
        safe_prefixes = ('▲', '⚠', '●', '↑', '•')
        first_char = heading.lstrip()[0] if heading.strip() else ''
        assert first_char in safe_prefixes, (
            f'Latency-outliers heading prefix {first_char!r} is not in '
            f'the verified-safe set; replace with one of: ▲ ⚠ ● ↑ •'
        )


# ── Issue #8: cover Generated stamp must not wrap ─────────────────────
class TestGeneratedDateNoWrap:

    def test_cover_meta_value_is_nowrap(self):
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(
            r'\.cover-meta-value\s*\{([^}]+)\}', css, re.DOTALL,
        )
        assert m, '.cover-meta-value rule not found'
        block = m.group(1)
        assert 'white-space: nowrap' in block or 'white-space:nowrap' in block

    def test_compact_date_format(self, rendered_html):
        """Generated stamp uses %a / %b (Fri / May) instead of full
        weekday + full month - keeps the value short enough that it
        doesn't have to wrap at all in the narrow 2-col cover-meta cell."""
        # Anywhere in the rendered HTML, the GENERATED value should NOT
        # be the long form "Friday, ... May ...".
        # Match any 4-digit-year stamp inside cover-meta.
        m = re.search(
            r'cover-meta-label">Generated</span><span [^>]*>([^<]+)</span>',
            rendered_html,
        )
        assert m, 'Generated stamp not found'
        stamp = m.group(1).strip()
        # %A produces "Friday" - must not be in the rendered stamp.
        long_weekdays = (
            'Monday', 'Tuesday', 'Wednesday', 'Thursday',
            'Friday', 'Saturday', 'Sunday',
        )
        for wkday in long_weekdays:
            assert wkday not in stamp, (
                f'Generated stamp uses long weekday {wkday!r} - must use '
                f'%a abbreviation (Mon/Tue/.../Sun) to fit cell width.'
            )


# ── Issue #9: Next Run (UTC) header must not wrap ─────────────────────
class TestNextRunHeaderNoWrap:

    def test_appendix_th_nowrap_or_explicit_width(self):
        """TH cells in the appendix table need a strategy that keeps
        single-word headers like 'Firings' on one line and lets multi-
        word headers wrap predictably. The chosen approach is explicit
        col widths + no nowrap, which lets headers like 'Run Hist.'
        wrap to 2 lines without overflowing into adjacent cells."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(
            r'table\.jobs\s+th\s*\{([^}]+)\}', css, re.DOTALL,
        )
        assert m, 'table.jobs th rule not found'
        # Either nowrap (single-line headers) OR an explicit colgroup
        # is required. Our chosen path is colgroup + wrap, verified
        # already by TestCronColumnNoWrap::test_appendix_table_has_colgroup.

    def test_vertical_separators_prevent_header_collision(self):
        """Adjacent TH cells must have a darker vertical separator so
        'RUN HIST.' and 'STATE' don't visually merge against the light
        header background. Found 2026-05-01 - the same fix that
        prevents KIND pill butting against NAME text in body rows."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        # Look for `th + th { border-left ...}` rule
        m = re.search(
            r'table\.jobs\s+th\s*\+\s*th\s*\{([^}]+)\}', css, re.DOTALL,
        )
        assert m, 'th + th sibling rule must declare a vertical separator'
        block = m.group(1)
        assert 'border-left' in block, (
            'th + th rule must set border-left for visible cell separation'
        )


# ── Bonus: kind tag has guaranteed margin to next cell ────────────────
class TestKindTagBreathingRoom:

    def test_kind_tag_has_margin_right(self):
        """Without explicit margin-right on .kind-tag, the pill abutted
        the NAME cell with no visible gap when table-layout:fixed
        compressed the KIND column. Found 2026-05-01."""
        from tools import schedule_pdf
        css = schedule_pdf._CSS
        m = re.search(r'\.kind-tag\s*\{([^}]+)\}', css, re.DOTALL)
        assert m, '.kind-tag rule not found'
        block = m.group(1)
        assert 'margin-right' in block, (
            '.kind-tag must declare margin-right to guarantee a visible '
            'gap to the next cell content under table-layout:fixed.'
        )
