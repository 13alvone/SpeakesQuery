#!/usr/bin/env python3
"""
SpeakesQuery Schedule Operations Report - PDF Generator
─────────────────────────────────────────────────────

Generates a polished, branded PDF report of the entire scheduled-job
landscape: executive summary, heatmaps, recent activity charts, per-AG
health breakdown, anomalies, and a full appendix.

Designed for:
- One-click download from the Schedule page (`/api/schedule/pdf`)
- Cron'd weekly archive runs (`python -m tools.schedule_pdf --output ...`)

Renderer: WeasyPrint (HTML+CSS → PDF). Pure Python at module level; the
WeasyPrint dependency is gracefully detected at runtime so the main
application doesn't fail-import if it's missing.

The HTML template is inline so this module ships standalone.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Project root resolution - works whether invoked as a module or directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ───────────────────────────────────────────────────────────────────
# Brand assets - extracted from desktop_app/ui.html so the PDF matches
# the SPA's visual identity. The SVG below is a simplified mark+wordmark.
# ───────────────────────────────────────────────────────────────────

_LOGO_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 940 210"
     preserveAspectRatio="xMidYMid meet"
     style="display:block;width:100%;height:auto;">
  <rect x="16" y="16" width="170" height="178" rx="22" fill="#0f1729"/>
  <rect x="16" y="16" width="170" height="28" rx="22" fill="#1a2540"/>
  <rect x="16" y="32" width="170" height="12" fill="#1a2540"/>
  <circle cx="42" cy="30" r="3.5" fill="#FF5F57" opacity="0.85"/>
  <circle cx="56" cy="30" r="3.5" fill="#FEBC2E" opacity="0.85"/>
  <circle cx="70" cy="30" r="3.5" fill="#28C840" opacity="0.85"/>
  <rect x="38" y="56" width="88" height="7" rx="3" fill="#4f9fde" opacity="0.30"/>
  <rect x="38" y="71" width="124" height="7" rx="3" fill="#4f9fde" opacity="0.50"/>
  <rect x="38" y="86" width="66" height="7" rx="3" fill="#4f9fde" opacity="0.45"/>
  <rect x="38" y="101" width="108" height="7" rx="3" fill="#4f9fde" opacity="0.65"/>
  <rect x="38" y="116" width="78" height="7" rx="3" fill="#4f9fde" opacity="0.70"/>
  <rect x="38" y="131" width="130" height="7" rx="3" fill="#4f9fde" opacity="0.85"/>
  <rect x="38" y="146" width="96" height="7" rx="3" fill="#4f9fde" opacity="0.95"/>
  <text x="38" y="174" font-family="'SF Mono','Cascadia Code','Fira Code',monospace"
        font-size="13" font-weight="600" fill="#4f9fde">&gt;_</text>
  <text x="216" y="124"
        font-family="'Segoe UI','SF Pro Display','Helvetica Neue',sans-serif"
        font-size="90" font-weight="700" letter-spacing="-2" fill="#0f1729">Speakes</text>
  <text x="612" y="124"
        font-family="'Segoe UI','SF Pro Display','Helvetica Neue',sans-serif"
        font-size="90" font-weight="700" letter-spacing="1" fill="#4f9fde">Query</text>
</svg>
'''.strip()


# ───────────────────────────────────────────────────────────────────
# Heatmap shading - matches desktop_app/ui.html palette so PDF readers
# see the same colors as on-screen.
# ───────────────────────────────────────────────────────────────────

_HEAT_BAND_BG = (
    (0,    'rgba(34, 139, 230, 0.06)'),  # zero / very light
    (1,    'rgba(34, 139, 230, 0.12)'),
    (2,    'rgba(34, 139, 230, 0.22)'),
    (3,    'rgba(34, 139, 230, 0.32)'),
    (5,    'rgba(34, 139, 230, 0.45)'),
    (10,   'rgba(34, 139, 230, 0.60)'),
    (20,   'rgba(34, 139, 230, 0.78)'),
    (50,   'rgba(220, 99, 50, 0.85)'),   # orange = hot
    (100,  'rgba(220, 99, 50, 0.95)'),
)
_HEAT_BAND_FG = (
    (5, '#0f1729'),    # dark text on light cells
    (50, '#ffffff'),   # white text on hot cells
)
_DOW_LABELS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def _heat_color(value: float | None) -> tuple[str, str]:
    """Return (bg, fg) for a heatmap cell based on intensity."""
    if value is None or value <= 0:
        return _HEAT_BAND_BG[0][1], '#9ba6b8'  # muted text on near-empty cells
    bg = _HEAT_BAND_BG[0][1]
    for threshold, color in _HEAT_BAND_BG:
        if value >= threshold:
            bg = color
    fg = '#0f1729'
    for threshold, color in _HEAT_BAND_FG:
        if value >= threshold:
            fg = color
    return bg, fg


def _format_cell_value(value: float | None, *, is_volume: bool = False) -> str:
    """Format a heatmap cell label."""
    if value is None or value == 0:
        return ''
    if is_volume:
        if value >= 1000:
            return f'{int(value / 1000)}k'
        return str(int(value))
    return str(int(value))


def _render_heatmap_svg(
    *,
    title: str,
    by_dow_hour: dict,
    is_volume: bool = False,
    has_data_mask: dict | None = None,
) -> str:
    """Render a 7×24 heatmap as inline SVG (no external deps)."""
    cell_w = 32
    cell_h = 22
    label_w = 36
    header_h = 22
    width = label_w + 24 * cell_w
    height = header_h + 7 * cell_h

    parts = [
        # NOTE: do NOT use height="auto" as an SVG attribute - that's a
        # CSS keyword, invalid on the SVG element itself, and WeasyPrint
        # silently collapses the SVG to zero height. Drive sizing via
        # CSS in the style attribute, which is valid and respected.
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="display:block;width:100%;height:auto;'
        f'font-family: \'Inter\',\'Segoe UI\',\'SF Pro Display\',sans-serif;">'
    ]

    # Top row: hour labels 00–23
    parts.append(
        f'<text x="0" y="{header_h - 6}" font-size="9" '
        f'fill="#5a6478" font-weight="600">UTC</text>'
    )
    for hour in range(24):
        x = label_w + hour * cell_w + cell_w // 2
        parts.append(
            f'<text x="{x}" y="{header_h - 6}" font-size="9" '
            f'text-anchor="middle" fill="#5a6478">{hour:02d}</text>'
        )

    # Body rows: day of week
    for dow in range(7):
        y_top = header_h + dow * cell_h
        parts.append(
            f'<text x="{label_w - 4}" y="{y_top + cell_h * 0.65}" '
            f'font-size="10" text-anchor="end" '
            f'fill="#5a6478" font-weight="600">{_DOW_LABELS[dow]}</text>'
        )
        for hour in range(24):
            value = by_dow_hour.get(dow, [0] * 24)[hour]
            has_data = True
            if has_data_mask is not None:
                has_data = has_data_mask.get(dow, [False] * 24)[hour]
            if not has_data:
                # No-data cells get a hatched / " - " treatment for the volume map
                bg = 'rgba(155, 166, 184, 0.08)'
                fg = '#9ba6b8'
                cell_text = ' - '
            else:
                bg, fg = _heat_color(value)
                cell_text = _format_cell_value(value, is_volume=is_volume)
            x = label_w + hour * cell_w
            parts.append(
                f'<rect x="{x}" y="{y_top}" width="{cell_w}" height="{cell_h}" '
                f'fill="{bg}" stroke="rgba(155, 166, 184, 0.20)" '
                f'stroke-width="0.5"/>'
            )
            if cell_text:
                parts.append(
                    f'<text x="{x + cell_w // 2}" y="{y_top + cell_h * 0.68}" '
                    f'font-size="9" text-anchor="middle" fill="{fg}" '
                    f'font-weight="500">{cell_text}</text>'
                )

    parts.append('</svg>')
    return ''.join(parts)


# ───────────────────────────────────────────────────────────────────
# Daily activity charts - bar (stacked by kind) + line (rows ingested)
# ───────────────────────────────────────────────────────────────────


def _render_activity_charts_svg(buckets: list[dict]) -> str:
    """Render a stacked-bar (executions/day by kind) + line (rows/day) SVG."""
    if not buckets:
        return '<p style="color:#5a6478; font-size: 11px;">No recent activity data.</p>'

    chart_w = 760
    bar_h = 180
    line_h = 110
    padding = {'l': 50, 'r': 12, 't': 16, 'b': 26}
    inner_w = chart_w - padding['l'] - padding['r']

    n = len(buckets)
    bar_total_w = inner_w
    slot = bar_total_w / max(n, 1)
    bar_w = max(4, slot * 0.65)

    def _stack(bucket: dict) -> tuple[int, int, int]:
        return (
            int(bucket.get('ingestion_runs') or 0),
            int(bucket.get('search_runs') or 0),
            int(bucket.get('ag_dispatches') or 0),
        )

    max_stack = max(
        (sum(_stack(b)) for b in buckets), default=1
    ) or 1
    max_rows = max(
        (int(b.get('rows_ingested') or 0) for b in buckets), default=1
    ) or 1

    colors = {
        'ingestion':   '#4f9fde',
        'search':      '#7fc5a8',
        'ag':          '#dc6332',
    }

    out = [
        # See note in _render_heatmap_svg - drive sizing via CSS, not the
        # invalid `height="auto"` SVG attribute.
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {chart_w} {bar_h + line_h + 60}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="display:block;width:100%;height:auto;'
        f'font-family: \'Inter\',\'Segoe UI\',sans-serif;">'
    ]

    # ── Bar chart background grid (light) ─────────────────────────
    out.append(
        f'<rect x="{padding["l"]}" y="{padding["t"]}" '
        f'width="{inner_w}" height="{bar_h}" fill="#fafbfd" '
        f'stroke="#e7ecf3"/>'
    )
    # Y-axis labels for bar chart (0, max/2, max)
    for frac, val in ((0, 0), (0.5, max_stack // 2), (1, max_stack)):
        y = padding['t'] + bar_h - (frac * bar_h)
        out.append(
            f'<line x1="{padding["l"]}" y1="{y}" '
            f'x2="{padding["l"] + inner_w}" y2="{y}" '
            f'stroke="#e7ecf3" stroke-dasharray="2,2"/>'
        )
        out.append(
            f'<text x="{padding["l"] - 6}" y="{y + 3}" font-size="9" '
            f'text-anchor="end" fill="#5a6478">{val}</text>'
        )

    # Stacked bars
    for i, b in enumerate(buckets):
        ing, srch, ag = _stack(b)
        x = padding['l'] + i * slot + (slot - bar_w) / 2
        y_base = padding['t'] + bar_h
        # ingestion (bottom)
        if ing > 0:
            h = (ing / max_stack) * bar_h
            out.append(
                f'<rect x="{x:.1f}" y="{y_base - h:.1f}" '
                f'width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{colors["ingestion"]}" opacity="0.85"/>'
            )
            y_base -= h
        if srch > 0:
            h = (srch / max_stack) * bar_h
            out.append(
                f'<rect x="{x:.1f}" y="{y_base - h:.1f}" '
                f'width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{colors["search"]}" opacity="0.85"/>'
            )
            y_base -= h
        if ag > 0:
            h = (ag / max_stack) * bar_h
            out.append(
                f'<rect x="{x:.1f}" y="{y_base - h:.1f}" '
                f'width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{colors["ag"]}" opacity="0.95"/>'
            )

    # Date labels - every Nth bucket
    label_step = max(1, n // 8)
    for i, b in enumerate(buckets):
        if i % label_step == 0 or i == n - 1:
            x = padding['l'] + i * slot + slot / 2
            short = b.get('date', '')[5:]  # MM-DD
            out.append(
                f'<text x="{x:.1f}" y="{padding["t"] + bar_h + 14}" font-size="9" '
                f'text-anchor="middle" fill="#5a6478">{short}</text>'
            )

    # Bar legend
    legend_y = padding['t'] + bar_h + 32
    legend_items = [
        ('Ingestion', colors['ingestion']),
        ('Saved-Search', colors['search']),
        ('Alert-Group', colors['ag']),
    ]
    lx = padding['l']
    for label, color in legend_items:
        out.append(
            f'<rect x="{lx}" y="{legend_y - 8}" width="10" height="10" '
            f'fill="{color}" opacity="0.85"/>'
        )
        out.append(
            f'<text x="{lx + 14}" y="{legend_y}" font-size="10" '
            f'fill="#3b4560">{label}</text>'
        )
        lx += 110

    # ── Line chart (rows ingested) ────────────────────────────────
    line_top = padding['t'] + bar_h + 50
    out.append(
        f'<rect x="{padding["l"]}" y="{line_top}" '
        f'width="{inner_w}" height="{line_h}" fill="#fafbfd" '
        f'stroke="#e7ecf3"/>'
    )
    out.append(
        f'<text x="{padding["l"]}" y="{line_top - 4}" font-size="10" '
        f'fill="#3b4560" font-weight="600">Rows ingested per day</text>'
    )
    # Y axis labels
    for frac, val in (
        (0, 0),
        (0.5, max_rows // 2),
        (1, max_rows),
    ):
        y = line_top + line_h - (frac * line_h)
        out.append(
            f'<text x="{padding["l"] - 6}" y="{y + 3}" font-size="9" '
            f'text-anchor="end" fill="#5a6478">{val:,}</text>'
        )

    # Polyline + filled area
    points = []
    for i, b in enumerate(buckets):
        rows = int(b.get('rows_ingested') or 0)
        x = padding['l'] + i * slot + slot / 2
        y = line_top + line_h - (rows / max_rows) * line_h
        points.append((x, y))

    if points:
        # Filled area (subtle) under the line
        area = ['M', f'{points[0][0]:.1f}', f'{line_top + line_h:.1f}']
        for x, y in points:
            area += ['L', f'{x:.1f}', f'{y:.1f}']
        area += ['L', f'{points[-1][0]:.1f}', f'{line_top + line_h:.1f}', 'Z']
        out.append(
            f'<path d="{" ".join(area)}" fill="#4f9fde" opacity="0.15"/>'
        )
        # Line itself
        path = ['M', f'{points[0][0]:.1f}', f'{points[0][1]:.1f}']
        for x, y in points[1:]:
            path += ['L', f'{x:.1f}', f'{y:.1f}']
        out.append(
            f'<path d="{" ".join(path)}" fill="none" '
            f'stroke="#4f9fde" stroke-width="2"/>'
        )
        # Dots at each datapoint
        for x, y in points:
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" '
                f'fill="#4f9fde" stroke="white" stroke-width="0.8"/>'
            )

    out.append('</svg>')
    return ''.join(out)


# ───────────────────────────────────────────────────────────────────
# Anomaly detection - turns the raw job table into operator-relevant
# bullet points: degraded jobs, latency outliers, never-ran tasks, etc.
# ───────────────────────────────────────────────────────────────────


def _identify_anomalies(jobs: list[dict]) -> dict[str, list[dict]]:
    """Categorise jobs into ops-relevant buckets."""
    enabled = [j for j in jobs if not j.get('disabled')]
    disabled = [j for j in jobs if j.get('disabled')]

    never_ran = [
        j for j in enabled
        if (j.get('run_count') or 0) == 0
    ]

    zero_rows = [
        j for j in enabled
        if (j.get('avg_row_count') is not None
            and j.get('avg_row_count') == 0
            and j.get('kind') != 'alert_group')
    ]

    # Failing runs: any status=="error" among the last-N runs. Before
    # 2026-07-01 a job erroring on EVERY recent run showed avg rows " - "
    # and escaped both the never-ran and zero-rows buckets - the report
    # looked clean while the job was broken.
    failing = sorted(
        (j for j in enabled if (j.get('error_count') or 0) > 0),
        key=lambda j: -(j.get('error_count') or 0),
    )

    # Latency outliers: top 5% of avg_duration_ms across enabled jobs
    durations = [
        (j, j.get('avg_duration_ms') or 0)
        for j in enabled
        if j.get('avg_duration_ms') is not None
    ]
    durations.sort(key=lambda t: -t[1])
    p95_threshold = 30_000  # 30s - anything slower is worth flagging
    latency_outliers = [
        j for j, d in durations
        if d >= p95_threshold
    ][:10]

    return {
        'never_ran':        never_ran,
        'zero_rows':        zero_rows,
        'failing':          failing,
        'latency_outliers': latency_outliers,
        'disabled':         disabled,
    }


# ───────────────────────────────────────────────────────────────────
# Per-AG breakdown - pairs each alert group with its feeders + their
# health (rows + duration). Built off the existing AG store + the same
# job history dict the heatmap uses.
# ───────────────────────────────────────────────────────────────────


def _build_per_ag_blocks(
    jobs: list[dict],
    history: dict | None = None,
) -> list[dict]:
    """For each enabled AG, list its feeder saved-searches + per-feeder health.

    ``jobs`` only contains CRON-SCHEDULED work, but a feeder saved search
    is equally valid with an empty cron - the AG dispatcher executes it
    on demand at dispatch time (``purpose: alert_group_feeder``, e.g. the
    Slice B/C ``github_hot_repos_today`` / ``ai_papers_new_today``
    feeders). Those must NOT be reported as MISSING: the saved-search
    store is the authority for existence, and ``history`` (the
    ``gather_run_history()`` dict) carries their real health because the
    dispatcher logs every on-demand feeder execution to ``search_runs``.
    Caught 2026-07-01: the report flagged two healthy dispatch-time
    feeders as MISSING while the AGs dispatched fine.
    """
    by_kind = {'alert_group': [], 'saved_search_lookup': {}}
    for j in jobs:
        if j['kind'] == 'alert_group' and not j.get('disabled'):
            by_kind['alert_group'].append(j)
        elif j['kind'] == 'saved_search':
            by_kind['saved_search_lookup'][j['name']] = j

    if history is None:
        try:
            from schedule_visualization import gather_run_history
            history = gather_run_history()
        except Exception as exc:
            logger.warning('[!] Could not gather run history: %s', exc)
            history = {}

    # The saved-search store knows about non-cron (dispatch-time) feeders
    # that never appear in the scheduled-jobs list.
    try:
        from saved_search_store import SavedSearchStore
        ss_store = SavedSearchStore()
        ss_store.initialize()
        installed_searches = {
            (row.get('name') or '')
            for row in (ss_store.list_searches() or [])
        }
    except Exception as exc:
        logger.warning('[!] Could not load saved_search_store: %s', exc)
        installed_searches = None  # unknown - don't claim MISSING

    # Pull the AG → feeders mapping from the store
    try:
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store.initialize()
        groups = store.list_groups()
    except Exception as exc:
        logger.warning('[!] Could not load alert_group_store: %s', exc)
        groups = []

    feeders_by_ag = {
        g.get('name'): list(g.get('search_names') or [])
        for g in (groups or [])
    }

    out = []
    for ag in by_kind['alert_group']:
        feeders = feeders_by_ag.get(ag['name'], [])
        feeder_rows = []
        for fname in feeders:
            f = by_kind['saved_search_lookup'].get(fname)
            if f is None:
                # Distinguish intentional Wave-3 manual-return placeholders
                # (`*_reserved_picks`) from actually-broken-or-undeployed
                # feeders. Reserved-picks SSes have empty cron schedules
                # (invoked on demand by the AG dispatcher, never on cron),
                # so they don't appear in the cron-scheduled jobs list.
                # Surfacing them as MISSING in every AG was confusing
                # operators trying to spot real broken feeders. Caught
                # 2026-05-04 in the schedule-PDF iteration.
                if fname.endswith('_reserved_picks'):
                    feeder_rows.append({
                        'name': fname,
                        'status': 'placeholder',
                        'avg_rows': None,
                        'avg_duration_ms': None,
                    })
                    continue
                if installed_searches is None or fname in installed_searches:
                    # Installed (or store unreadable - benefit of the
                    # doubt) but not cron-scheduled: a dispatch-time
                    # feeder. Health comes from the dispatcher's
                    # search_runs rows when it has fired.
                    h = (history or {}).get(f'saved_search::{fname}') or {}
                    run_count = h.get('run_count') or 0
                    avg_rc = h.get('avg_row_count')
                    if run_count == 0:
                        status = 'on_demand'
                    elif (h.get('error_count') or 0) >= run_count:
                        status = 'failing'
                    elif avg_rc == 0 or avg_rc is None:
                        status = 'empty'
                    else:
                        status = 'ok'
                    feeder_rows.append({
                        'name': fname,
                        'status': status,
                        'avg_rows': avg_rc,
                        'avg_duration_ms': h.get('avg_duration_ms'),
                    })
                else:
                    feeder_rows.append({
                        'name': fname,
                        'status': 'missing',
                        'avg_rows': None,
                        'avg_duration_ms': None,
                    })
                continue
            run_count = f.get('run_count') or 0
            avg_rc = f.get('avg_row_count')
            if run_count == 0:
                status = 'never_ran'
            elif (f.get('error_count') or 0) >= run_count:
                # Every recent run errored - before 2026-07-01 this
                # rendered as EMPTY, indistinguishable from a quiet day.
                status = 'failing'
            elif avg_rc == 0 or avg_rc is None:
                status = 'empty'
            else:
                status = 'ok'
            feeder_rows.append({
                'name': fname,
                'status': status,
                'avg_rows': avg_rc,
                'avg_duration_ms': f.get('avg_duration_ms'),
            })
        out.append({
            'name': ag['name'],
            'cron': ag.get('cron', ''),
            'next_firing': ag.get('next_firing_iso', ''),
            'feeder_count': ag.get('feeder_count', len(feeders)),
            'feeders': feeder_rows,
        })
    return out


# ───────────────────────────────────────────────────────────────────
# CSS - inline so the file ships standalone. WeasyPrint supports
# CSS3 properties, paged-media `@page`, custom fonts (via @font-face),
# and most flexbox/grid constructs.
# ───────────────────────────────────────────────────────────────────

_CSS = """
@page {
    size: Letter;
    margin: 14mm 12mm 18mm 12mm;
    @top-center {
        content: "SpeakesQuery Schedule Operations Report";
        font-family: 'Inter', 'Segoe UI', sans-serif;
        font-size: 8pt;
        color: #5a6478;
    }
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Inter', 'Segoe UI', sans-serif;
        font-size: 8pt;
        color: #5a6478;
    }
    @bottom-left {
        content: string(report-date);
        font-family: 'Inter', 'Segoe UI', sans-serif;
        font-size: 8pt;
        color: #5a6478;
    }
}
@page :first {
    @top-center { content: none; }
    @bottom-right { content: none; }
    @bottom-left { content: none; }
    margin: 0;
}

* { box-sizing: border-box; }

body {
    font-family: 'Inter', 'Segoe UI', 'SF Pro Display', 'Helvetica Neue', sans-serif;
    font-size: 10pt;
    color: #14202f;
    line-height: 1.45;
    margin: 0;
}

.report-date-anchor { string-set: report-date attr(data-date); }

/* ── Cover page ────────────────────────────────────────────────── */
/* Cover claims @page :first via CSS Paged Media, so it MUST be the
   very first thing in <body> (no anchor div ahead of it) and its
   content MUST fit the no-margin page area to avoid bleeding to a
   second page. Letter is 215.9 × 279.4mm; padding tuned to leave
   slack for the logo, hero text, subtitle, 4-cell meta-grid and
   accent strip without breaking pages. */
.cover {
    page-break-after: always;
    break-after: page;
    page-break-inside: avoid;
    break-inside: avoid;
    padding: 28mm 22mm 14mm 22mm;
    background: linear-gradient(180deg, #0f1729 0%, #1a2540 65%, #233459 100%);
    color: #e8ecf3;
    /* Slightly under one Letter page edge-to-edge so the cover never
       overflows even if the first-page margin override is ignored. */
    height: 268mm;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    overflow: hidden;
}
.cover-logo {
    width: 86mm;
    margin-bottom: 14mm;
    /* Defensive: ensure inline SVG can't overrun horizontally */
    max-width: 100%;
    overflow: hidden;
}
.cover-logo svg { display: block; width: 100%; height: auto; }
.cover-eyebrow {
    font-size: 9pt;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: #4f9fde;
    margin-bottom: 6mm;
}
.cover-title {
    font-size: 32pt;
    font-weight: 700;
    line-height: 1.1;
    margin: 0 0 8mm 0;
    color: #ffffff;
    letter-spacing: -0.02em;
}
.cover-subtitle {
    font-size: 12pt;
    color: #a8b3c7;
    max-width: 130mm;
    line-height: 1.4;
    margin-bottom: 12mm;
}
.cover-meta-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6mm;
    border-top: 1px solid #2a3145;
    padding-top: 8mm;
    margin-top: auto;
}
.cover-meta-cell { display: flex; flex-direction: column; min-width: 0; }
.cover-meta-label {
    font-size: 8pt;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: #7e8aa3;
    margin-bottom: 2mm;
}
.cover-meta-value {
    font-size: 11pt;
    color: #e8ecf3;
    font-weight: 500;
    /* Compact stamp like "2026-05-01 · 20:57 UTC" should never wrap;
       the human-format date below uses NBSP between time and zone. */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.cover-accent-strip {
    height: 5mm;
    background: linear-gradient(90deg, #4f9fde 0%, #7fc5a8 50%, #dc6332 100%);
    margin: 6mm -22mm 0 -22mm;
}

/* ── Section headings ─────────────────────────────────────────── */
h1, h2, h3 { color: #14202f; }
h1 { font-size: 22pt; margin: 0 0 6mm 0; font-weight: 700; letter-spacing: -0.01em; }
h2 {
    font-size: 14pt; font-weight: 600;
    margin: 8mm 0 3mm 0;
    padding-bottom: 2mm;
    border-bottom: 2px solid #4f9fde;
    color: #14202f;
}
h3 { font-size: 11pt; font-weight: 600; margin: 4mm 0 2mm 0; color: #14202f; }
.section { page-break-inside: avoid; margin-bottom: 6mm; }
.muted { color: #5a6478; font-size: 9pt; }
.lead {
    font-size: 11pt;
    line-height: 1.6;
    color: #14202f;
    background: linear-gradient(180deg, #f5f8fc 0%, #ffffff 100%);
    padding: 5mm 6mm;
    border-left: 4px solid #4f9fde;
    margin-bottom: 5mm;
}

/* ── Stat tiles ───────────────────────────────────────────────── */
.stat-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 3mm;
    margin-bottom: 6mm;
}
.stat-tile {
    background: #ffffff;
    border: 1px solid #e7ecf3;
    border-top: 3px solid #4f9fde;
    padding: 4mm 5mm;
    border-radius: 1mm;
}
.stat-label {
    font-size: 7pt;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: #5a6478;
    font-weight: 600;
    margin-bottom: 2mm;
}
.stat-value { font-size: 18pt; font-weight: 700; color: #14202f; }
.stat-detail { font-size: 8pt; color: #5a6478; margin-top: 1mm; }
.stat-tile.alt { border-top-color: #dc6332; }
.stat-tile.alt2 { border-top-color: #7fc5a8; }

/* ── Heatmap container ────────────────────────────────────────── */
.heatmap-box {
    background: #ffffff;
    border: 1px solid #e7ecf3;
    padding: 4mm 4mm 3mm 4mm;
    margin-bottom: 4mm;
    border-radius: 1mm;
}
.heatmap-legend {
    display: flex;
    gap: 4mm;
    align-items: center;
    margin-top: 2mm;
    font-size: 8pt;
    color: #5a6478;
    flex-wrap: wrap;
}
.heatmap-legend-swatch {
    display: inline-block;
    width: 4mm;
    height: 3mm;
    margin-right: 1mm;
    border: 1px solid rgba(155, 166, 184, 0.3);
    vertical-align: middle;
}

/* ── Tables ───────────────────────────────────────────────────── */
/* `table-layout: fixed` is required: the auto layout would size CRON
   to its longest content and shove the STATE column off the right
   edge (caught 2026-05-01 - the right-most column was being clipped
   mid-letter). Explicit <colgroup> widths total 100% of the table,
   which is sized to the printable area via width:100%. */
table.jobs {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: 8pt;
}
/* Keep each appendix row whole - without this the multi-line CRON cell
   can split across a page break, leaving an orphan cron expression
   floating with no name/kind on the next page. */
table.jobs tr {
    page-break-inside: avoid;
    break-inside: avoid;
}
/* Repeat header on every page so a continuation table still reads. */
table.jobs thead {
    display: table-header-group;
}
table.jobs th, table.jobs td {
    padding: 1.4mm 2mm;
    text-align: left;
    border-bottom: 1px solid #e7ecf3;
    vertical-align: top;
    word-wrap: break-word;
    overflow-wrap: break-word;
}
/* Vertical separators between adjacent cells. Without these, under
   table-layout:fixed the KIND pill visually butted against the NAME
   text ("INGESTIONtest script"). Slightly darker on TH so it reads
   against the header background (#f5f8fc) - a too-pale separator
   disappeared into the header and made "RUN HIST." run into "STATE". */
table.jobs th + th { border-left: 1px solid #c9d1de; }
table.jobs td + td { border-left: 1px solid #eef1f6; }
table.jobs th {
    background: #f5f8fc;
    color: #3b4560;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 7pt;
    border-bottom: 2px solid #d2d8e3;
    /* Headers are allowed to wrap to 2 lines so "Run Hist." and "Avg
       Rows" don't overflow narrow columns. nowrap stays on td.cron
       below - that's where overflow caused the page-split bug. */
}
table.jobs tr.disabled { background: #fafbfd; color: #9ba6b8; }
table.jobs td.cron, table.jobs td.name {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    font-size: 8pt;
}
/* Cron expressions are short (≤ ~17 chars even with day-of-week ranges).
   Stop them from wrapping onto 2-3 lines - that's what was triggering
   the tall-row/page-split bug - but allow the cron column to be slim
   enough that the STATE column fits. */
table.jobs td.cron {
    white-space: nowrap;
    font-size: 7.5pt;
}
table.jobs td.right { text-align: right; }
.kind-tag {
    display: inline-block;
    font-size: 7pt;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 0.4mm 1.4mm;
    border-radius: 1mm;
    font-weight: 600;
    /* Guarantee a visible gap to the next cell's content even when
       table-layout:fixed compresses the KIND column tight. */
    margin-right: 0.5mm;
}
.kind-tag.ingestion   { background: #e3effa; color: #2563a8; }
.kind-tag.saved_search { background: #e6f4ec; color: #2c7a4f; }
.kind-tag.alert_group  { background: #faece4; color: #b8501e; }
.disabled-tag { font-size: 7pt; color: #b8501e; font-weight: 600; }

/* ── AG breakdown blocks ──────────────────────────────────────── */
.ag-block {
    background: #ffffff;
    border: 1px solid #e7ecf3;
    border-left: 4px solid #dc6332;
    padding: 3mm 4mm;
    margin-bottom: 3mm;
    page-break-inside: avoid;
}
.ag-block-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 2mm;
}
.ag-name { font-weight: 700; font-size: 11pt; color: #14202f; }
.ag-cron {
    font-family: 'SF Mono', monospace;
    font-size: 8pt;
    color: #5a6478;
    background: #f5f8fc;
    padding: 0.5mm 2mm;
    border-radius: 1mm;
}
.feeder-row {
    display: flex;
    align-items: baseline;
    padding: 0.8mm 0;
    border-top: 1px dashed #e7ecf3;
    font-size: 9pt;
}
.feeder-row:first-child { border-top: none; }
.feeder-name { flex: 1; color: #14202f; font-family: 'SF Mono', monospace; font-size: 8pt; }
.feeder-pill {
    font-size: 7pt;
    padding: 0.4mm 1.4mm;
    border-radius: 1mm;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-right: 2mm;
}
.feeder-pill.ok      { background: #e6f4ec; color: #2c7a4f; }
.feeder-pill.empty   { background: #fef0d9; color: #a06400; }
.feeder-pill.never_ran { background: #fbe7e0; color: #b8501e; }
.feeder-pill.missing { background: #f1e3f5; color: #7a3a8e; }
.feeder-pill.placeholder { background: #eef1f4; color: #5a6478; }
.feeder-pill.on_demand { background: #e3edf9; color: #2b5f9e; }
.feeder-pill.failing { background: #fbdfe2; color: #b01e2e; }
.feeder-meta { font-size: 8pt; color: #5a6478; min-width: 30mm; text-align: right; }

/* ── Anomaly bullet list ──────────────────────────────────────── */
.anomalies-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 4mm;
}
.anomaly-card {
    background: #ffffff;
    border: 1px solid #e7ecf3;
    padding: 3mm 4mm;
    border-radius: 1mm;
    page-break-inside: avoid;
}
.anomaly-card h3 { margin-top: 0; }
.anomaly-card ul { margin: 0; padding-left: 4mm; }
.anomaly-card li { margin-bottom: 1mm; font-size: 9pt; }
.anomaly-card .empty-state {
    color: #2c7a4f;
    font-size: 9pt;
    font-style: italic;
}
.anomaly-card.flag-red    { border-left: 3px solid #b8501e; }
.anomaly-card.flag-orange { border-left: 3px solid #dc6332; }
.anomaly-card.flag-yellow { border-left: 3px solid #d49530; }
.anomaly-card.flag-blue   { border-left: 3px solid #4f9fde; }

/* ── Layout helpers ───────────────────────────────────────────── */
.cron-mono {
    font-family: 'SF Mono', 'Cascadia Code', monospace;
    font-size: 8pt;
    color: #14202f;
}
"""


# ───────────────────────────────────────────────────────────────────
# HTML template assembly
# ───────────────────────────────────────────────────────────────────


def _format_iso_short(iso: str | None) -> str:
    if not iso:
        return ' - '
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M UTC')
    except (ValueError, TypeError):
        return iso[:19].replace('T', ' ')


def _format_duration_ms(ms: float | None) -> str:
    if ms is None:
        return ' - '
    if ms < 1000:
        return f'{int(ms)}ms'
    if ms < 60_000:
        return f'{ms / 1000:.2f}s'
    return f'{ms / 60_000:.2f}m'


def _format_rows(value: float | None) -> str:
    if value is None:
        return ' - '
    if value >= 1000:
        return f'{value / 1000:.1f}k'
    return f'{value:.1f}' if value % 1 else f'{int(value)}'


def _build_executive_lead(summary: dict, volume_buckets: list[dict]) -> str:
    """One-paragraph headline summarising the system state."""
    s = summary.get('summary') or {}
    total = s.get('total_jobs', 0)
    disabled = s.get('total_jobs_disabled', 0)
    by_kind = s.get('by_kind') or {}
    busy_h = s.get('busiest_hour_utc')
    busy_n = s.get('busiest_hour_count') or 0
    big_h = s.get('biggest_data_hour_utc')
    big_n = s.get('biggest_data_hour_total') or 0
    total_firings = (summary.get('hour_distribution') or {}).get('total_firings', 0)
    lookahead = summary.get('lookahead_days', 7)

    rows_total = sum(int(b.get('rows_ingested') or 0) for b in volume_buckets)
    runs_total = sum(
        int(b.get('ingestion_runs') or 0)
        + int(b.get('search_runs') or 0)
        + int(b.get('ag_dispatches') or 0)
        for b in volume_buckets
    )
    days = len(volume_buckets) or 14

    bits = []
    bits.append(
        f'<strong>{total:,} scheduled jobs</strong> '
        f'({by_kind.get("ingestion", 0)} ingestion, '
        f'{by_kind.get("saved_search", 0)} saved searches, '
        f'{by_kind.get("alert_group", 0)} alert groups)'
        + (f', <strong>{disabled} disabled</strong>' if disabled else '')
        + '.'
    )
    bits.append(
        f'Across the next <strong>{lookahead} days</strong>, '
        f'<strong>{total_firings:,} firings</strong> are expected.'
    )
    if rows_total:
        bits.append(
            f'Last <strong>{days} days</strong> produced '
            f'<strong>{rows_total:,} rows</strong> ingested across '
            f'{runs_total:,} job executions.'
        )
    if busy_h is not None:
        bits.append(
            f'Busiest UTC hour: <strong>{busy_h:02d}:00</strong> '
            f'({busy_n} firings/cell).'
        )
    if big_h is not None and big_n:
        bits.append(
            f'Biggest-data UTC hour: <strong>{big_h:02d}:00</strong> '
            f'(~{int(big_n):,} expected rows/cell).'
        )
    return ' '.join(bits)


def _render_jobs_table(jobs: list[dict]) -> str:
    """Full-job appendix table - sorted by next firing then name."""
    rows = sorted(
        jobs,
        key=lambda j: (
            j.get('next_firing_epoch') or 9_999_999_999,
            j.get('name') or '',
        )
    )
    # Explicit column widths - sum to 100% of the printable area. Tuned
    # so the longest cron we ship ("30 10,15 * * 1-5", 17 chars) fits at
    # 7.5pt monospace, names truncate gracefully into 2 lines, and the
    # STATE column has room for the DISABLED pill on the right edge.
    out = [
        '<table class="jobs">',
        # Column widths sum to 100%. Tuned for: "ALERT GROUP" / "SAVED
        # SEARCH" wrap to 2 lines comfortably in KIND; longest cron
        # ("30 10,15 * * 1-5") fits one line in CRON at 7.5pt mono;
        # multi-word headers (Avg Rows, Run Hist.) wrap to 2 lines
        # without overflowing into adjacent cells.
        '<colgroup>',
        '<col style="width:10%;"/>',    # KIND     ("ALERT GROUP" 2-line)
        '<col style="width:21%;"/>',    # NAME
        '<col style="width:15%;"/>',    # CRON     (17-char max @ 7.5pt mono)
        '<col style="width:13%;"/>',    # NEXT RUN (UTC)
        '<col style="width:8%;"/>',     # FIRINGS  (single word, must fit one line)
        '<col style="width:7%;"/>',     # AVG ROWS (2-line header OK)
        '<col style="width:7%;"/>',     # AVG DUR  (2-line header OK)
        '<col style="width:8%;"/>',     # RUN HIST.(2-line header OK)
        '<col style="width:11%;"/>',    # STATE
        '</colgroup>',
        '<thead><tr>',
        '<th>Kind</th>',
        '<th>Name</th>',
        '<th>Cron</th>',
        '<th>Next Run (UTC)</th>',
        '<th class="right">Firings</th>',
        '<th class="right">Avg Rows</th>',
        '<th class="right">Avg Dur</th>',
        '<th class="right">Run Hist.</th>',
        '<th>State</th>',
        '</tr></thead>',
        '<tbody>',
    ]
    disabled_html = '<span class="disabled-tag">DISABLED</span>'
    enabled_html = '<span class="muted">enabled</span>'
    for j in rows:
        kind = j.get('kind', '?')
        cls = 'disabled' if j.get('disabled') else ''
        kind_class = kind  # ingestion / saved_search / alert_group
        state_html = disabled_html if j.get('disabled') else enabled_html
        out.append(
            f'<tr class="{cls}">'
            f'<td><span class="kind-tag {kind_class}">{kind.replace("_", " ")}</span></td>'
            f'<td class="name">{_html_esc(j.get("name", ""))}</td>'
            f'<td class="cron">{_html_esc(j.get("cron", ""))}</td>'
            f'<td>{_html_esc(_format_iso_short(j.get("next_firing_iso")))}</td>'
            f'<td class="right">{j.get("firings_in_lookahead", 0)}</td>'
            f'<td class="right">{_format_rows(j.get("avg_row_count"))}</td>'
            f'<td class="right">{_format_duration_ms(j.get("avg_duration_ms"))}</td>'
            f'<td class="right">{j.get("run_count", 0)}</td>'
            f'<td>{state_html}</td>'
            '</tr>'
        )
    out.append('</tbody></table>')
    return ''.join(out)


def _render_per_ag_section(blocks: list[dict]) -> str:
    """Render the per-AG breakdown section."""
    if not blocks:
        return '<p class="muted">No enabled alert groups.</p>'
    pieces = []
    for b in blocks:
        pieces.append('<div class="ag-block">')
        pieces.append('<div class="ag-block-head">')
        pieces.append(f'<span class="ag-name">{_html_esc(b["name"])}</span>')
        pieces.append(f'<span class="ag-cron">{_html_esc(b["cron"])}</span>')
        pieces.append('</div>')
        pieces.append(
            f'<div class="muted" style="font-size:8pt; margin-bottom:2mm;">'
            f'Next firing: {_html_esc(_format_iso_short(b["next_firing"]))} · '
            f'{b["feeder_count"]} feeder(s)'
            f'</div>'
        )
        if not b['feeders']:
            pieces.append('<div class="muted">(no feeders defined)</div>')
        for f in b['feeders']:
            status = f['status']
            label = {
                'ok':           'OK',
                'empty':        'EMPTY',
                'never_ran':    'NEVER RAN',
                'failing':      'FAILING',
                'on_demand':    'ON-DEMAND',
                'missing':      'MISSING',
                'placeholder':  'PLACEHOLDER',
            }.get(status, status.upper())
            meta_parts = []
            if f['avg_rows'] is not None:
                meta_parts.append(f'avg {_format_rows(f["avg_rows"])} rows')
            if f['avg_duration_ms'] is not None:
                meta_parts.append(_format_duration_ms(f['avg_duration_ms']))
            meta = ' · '.join(meta_parts) or ' - '
            pieces.append('<div class="feeder-row">')
            pieces.append(
                f'<span class="feeder-pill {status}">{label}</span>'
            )
            pieces.append(f'<span class="feeder-name">{_html_esc(f["name"])}</span>')
            pieces.append(f'<span class="feeder-meta">{_html_esc(meta)}</span>')
            pieces.append('</div>')
        pieces.append('</div>')
    return ''.join(pieces)


def _render_anomalies_section(anomalies: dict) -> str:
    def _list(jobs: list[dict], formatter) -> str:
        if not jobs:
            return '<div class="empty-state">None - clean!</div>'
        items = ['<ul>']
        for j in jobs[:12]:
            items.append('<li>' + formatter(j) + '</li>')
        items.append('</ul>')
        if len(jobs) > 12:
            items.append(
                f'<div class="muted" style="margin-top:1mm;">'
                f'+{len(jobs) - 12} more (see appendix)</div>'
            )
        return ''.join(items)

    def _fmt_basic(j):
        return (
            f'<span class="cron-mono">{_html_esc(j.get("name", ""))}</span> '
            f'<span class="muted"> - {_html_esc(j.get("cron", ""))}</span>'
        )

    def _fmt_latency(j):
        return (
            f'<span class="cron-mono">{_html_esc(j.get("name", ""))}</span> '
            f'<span class="muted"> - {_format_duration_ms(j.get("avg_duration_ms"))} '
            f'avg ({_html_esc(j.get("cron", ""))})</span>'
        )

    def _fmt_failing(j):
        errs = j.get('error_count') or 0
        runs = j.get('run_count') or 0
        return (
            f'<span class="cron-mono">{_html_esc(j.get("name", ""))}</span> '
            f'<span class="muted"> - {errs} of {runs} recent runs errored '
            f'({_html_esc(j.get("cron", ""))})</span>'
        )

    pieces = ['<div class="anomalies-grid">']
    pieces.append(
        '<div class="anomaly-card flag-red">'
        '<h3>▲ Failing runs</h3>'
        '<div class="muted" style="font-size:8pt; margin-bottom:2mm;">'
        'Jobs whose recent runs logged status=error. A job erroring on every '
        'run produces no rows and no data - check its error_message in '
        '<code>indexes/logs/search_runs</code> / <code>indexes/logs/ingestion</code>.</div>'
        + _list(anomalies.get('failing', []), _fmt_failing)
        + '</div>'
    )
    pieces.append(
        '<div class="anomaly-card flag-orange">'
        '<h3>⚠ Never ran</h3>'
        '<div class="muted" style="font-size:8pt; margin-bottom:2mm;">'
        'No history rows in the last 30 days. Likely never deployed, scheduler stalled, or just-added.</div>'
        + _list(anomalies['never_ran'], _fmt_basic)
        + '</div>'
    )
    pieces.append(
        '<div class="anomaly-card flag-yellow">'
        '<h3>⚠ Empty output</h3>'
        '<div class="muted" style="font-size:8pt; margin-bottom:2mm;">'
        'Tasks that ran but produced 0 rows on average. Filter too aggressive, source dry, or sentinel-row misconfiguration.</div>'
        + _list(anomalies['zero_rows'], _fmt_basic)
        + '</div>'
    )
    pieces.append(
        '<div class="anomaly-card flag-red">'
        # Use ▲ (BLACK UP-POINTING TRIANGLE U+25B2) - universal Inter /
        # Segoe / SF Pro coverage. The previous ⏱ stopwatch glyph
        # (U+23F1) rendered as a tofu box because WeasyPrint's default
        # font stack lacks an emoji/dingbat font.
        '<h3>▲ Latency outliers</h3>'
        '<div class="muted" style="font-size:8pt; margin-bottom:2mm;">'
        'Tasks with avg duration ≥ 30s. Worth checking for stuck network calls or unindexed reads.</div>'
        + _list(anomalies['latency_outliers'], _fmt_latency)
        + '</div>'
    )
    pieces.append(
        '<div class="anomaly-card flag-blue">'
        '<h3>○ Disabled</h3>'
        '<div class="muted" style="font-size:8pt; margin-bottom:2mm;">'
        'Tasks intentionally turned off. Excluded from heatmap counts unless you toggle "Include disabled".</div>'
        + _list(anomalies['disabled'], _fmt_basic)
        + '</div>'
    )
    pieces.append('</div>')
    return ''.join(pieces)


def _html_esc(s: Any) -> str:
    """Lightweight HTML escaping."""
    if s is None:
        return ''
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _render_html(
    summary: dict,
    volume_buckets: list[dict],
    *,
    project_root: Path,
) -> str:
    """Assemble the full report HTML."""
    jobs = summary.get('jobs') or []
    anomalies = _identify_anomalies(jobs)
    ag_blocks = _build_per_ag_blocks(jobs)

    s = summary.get('summary') or {}
    total = s.get('total_jobs', 0)
    disabled = s.get('total_jobs_disabled', 0)
    by_kind = s.get('by_kind') or {}
    busy_h = s.get('busiest_hour_utc')
    busy_n = s.get('busiest_hour_count') or 0
    big_h = s.get('biggest_data_hour_utc')
    big_n = s.get('biggest_data_hour_total') or 0
    lookahead = summary.get('lookahead_days', 7)
    history_runs = summary.get('history_lookback_runs', 5)

    generated_dt = datetime.fromtimestamp(
        summary.get('generated_at_epoch') or int(datetime.now(timezone.utc).timestamp()),
        tz=timezone.utc,
    )
    generated_iso = generated_dt.strftime('%Y-%m-%d %H:%M UTC')
    # Compact stamp ("Fri, 01 May 2026 · 20:57 UTC") so the value fits
    # the narrow 2-col cover-meta cell without wrapping (full %A %B was
    # too long, even with white-space:nowrap on the cell).
    generated_human = generated_dt.strftime('%a, %d %b %Y · %H:%M') + ' UTC'

    # Heatmaps
    hour_dist = summary.get('hour_distribution') or {}
    data_dist = summary.get('data_distribution') or {}
    firing_heatmap = _render_heatmap_svg(
        title='Firing Count Heatmap',
        by_dow_hour=hour_dist.get('by_dow_hour') or {},
        is_volume=False,
    )
    volume_heatmap = _render_heatmap_svg(
        title='Expected Data Volume Heatmap',
        by_dow_hour=data_dist.get('by_dow_hour') or {},
        is_volume=True,
        has_data_mask=data_dist.get('by_dow_hour_has_data'),
    )
    activity_chart = _render_activity_charts_svg(volume_buckets)

    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        '<title>SpeakesQuery Schedule Operations Report</title>',
        f'<style>{_CSS}</style>',
        '</head><body>',

        # ─── COVER ────────────────────────────────────────────────
        # The report-date-anchor MUST live inside the cover (not before
        # it). When the anchor was a sibling preceding .cover, WeasyPrint
        # rendered it as an empty 0-height block on its own page - that
        # phantom became "Page 1" with @page :first margin:0 styling
        # while the cover got pushed to page 2 with the regular margins,
        # producing a blank first page AND making the cover bleed onto a
        # third page because the cover's height target assumed @page :first.
        '<div class="cover">',
        f'<span class="report-date-anchor" data-date="{generated_iso}"></span>',
        '<div>',
        f'<div class="cover-logo">{_LOGO_SVG}</div>',
        '<div class="cover-eyebrow">Schedule Operations Report</div>',
        '<h1 class="cover-title">Every job. Every cron.<br/>Every cell.</h1>',
        '<p class="cover-subtitle">'
        'A full audit of your SpeakesQuery scheduling landscape - '
        'firings, data volume, per-AG health, and what to look at next.'
        '</p>',
        '</div>',
        '<div class="cover-meta-grid">',
        f'<div class="cover-meta-cell"><span class="cover-meta-label">Generated</span><span class="cover-meta-value">{generated_human}</span></div>',
        f'<div class="cover-meta-cell"><span class="cover-meta-label">Lookahead window</span><span class="cover-meta-value">{lookahead} days</span></div>',
        f'<div class="cover-meta-cell"><span class="cover-meta-label">Total scheduled jobs</span><span class="cover-meta-value">{total} ({disabled} disabled)</span></div>',
        f'<div class="cover-meta-cell"><span class="cover-meta-label">Recent activity window</span><span class="cover-meta-value">{len(volume_buckets)} days</span></div>',
        '</div>',
        '<div class="cover-accent-strip"></div>',
        '</div>',  # /cover

        # ─── EXECUTIVE SUMMARY ────────────────────────────────────
        '<div class="section">',
        '<h1>Executive Summary</h1>',
        f'<div class="lead">{_build_executive_lead(summary, volume_buckets)}</div>',
        '<div class="stat-grid">',
        f'<div class="stat-tile"><div class="stat-label">Total Jobs</div><div class="stat-value">{total:,}</div><div class="stat-detail">{disabled} disabled</div></div>',
        f'<div class="stat-tile"><div class="stat-label">Ingestion Tasks</div><div class="stat-value">{by_kind.get("ingestion", 0):,}</div><div class="stat-detail">scheduled scrapers</div></div>',
        f'<div class="stat-tile"><div class="stat-label">Saved Searches</div><div class="stat-value">{by_kind.get("saved_search", 0):,}</div><div class="stat-detail">scheduled SPQL queries</div></div>',
        f'<div class="stat-tile alt2"><div class="stat-label">Alert Groups</div><div class="stat-value">{by_kind.get("alert_group", 0):,}</div><div class="stat-detail">multi-search dispatchers</div></div>',
        f'<div class="stat-tile alt"><div class="stat-label">Busiest UTC Hour</div><div class="stat-value">{f"{busy_h:02d}:00" if busy_h is not None else " - "}</div><div class="stat-detail">{busy_n} firings/cell</div></div>',
        f'<div class="stat-tile alt"><div class="stat-label">Biggest-Data UTC Hour</div><div class="stat-value">{f"{big_h:02d}:00" if big_h is not None else " - "}</div><div class="stat-detail">~{int(big_n):,} expected rows/cell</div></div>',
        '</div>',
        '</div>',

        # ─── HEATMAPS ─────────────────────────────────────────────
        '<div class="section">',
        '<h2>Firing Count Heatmap (UTC)</h2>',
        '<p class="muted">Cells colored by count of expected firings during that day-of-week × hour slot, summed across all matching jobs over the next ' + str(lookahead) + ' days. Empty cells = no firings expected. Orange shading marks the heaviest cells.</p>',
        '<div class="heatmap-box">',
        firing_heatmap,
        '</div>',
        '</div>',

        '<div class="section">',
        '<h2>Expected Data Volume Heatmap (UTC)</h2>',
        '<p class="muted">Cells colored by expected total row count = (firings × avg row count from last ' + str(history_runs) + ' runs). The " - " mark means we don\'t yet have history for any job that fires in that cell - a fresh deploy or just-rotated run history.</p>',
        '<div class="heatmap-box">',
        volume_heatmap,
        '</div>',
        '</div>',

        # ─── RECENT ACTIVITY ──────────────────────────────────────
        '<div class="section">',
        '<h2>Recent Activity</h2>',
        '<p class="muted">Stacked bar = count of executions per UTC day, partitioned by job kind. Filled-area line = rows of data ingested per UTC day. Empty days are shown as zero so platform gaps surface visually.</p>',
        f'<div class="heatmap-box">{activity_chart}</div>',
        '</div>',

        # ─── PER-AG HEALTH ────────────────────────────────────────
        '<div class="section">',
        '<h2>Per-AG Feeder Health</h2>',
        '<p class="muted">Each enabled alert group with its feeders. A feeder is OK if its last ' + str(history_runs) + ' runs averaged > 0 rows; EMPTY if average is 0 (filter too tight, source dry, or sentinel row); FAILING if every recent run errored; NEVER RAN if no history rows in the lookback window; ON-DEMAND for installed dispatch-time feeders (empty cron - the AG dispatcher runs them at dispatch) that have not fired yet; PLACEHOLDER for manual-return slots (e.g. <code>*_reserved_picks</code>); MISSING only when the saved search is not installed at all - install it from Feeder Health.</p>',
        _render_per_ag_section(ag_blocks),
        '</div>',

        # ─── ANOMALIES ────────────────────────────────────────────
        '<div class="section">',
        '<h2>Highlights & Anomalies</h2>',
        '<p class="muted">Operator-visible signals. None of these are errors per se - they\'re prompts to check whether the underlying behavior is intentional.</p>',
        _render_anomalies_section(anomalies),
        '</div>',

        # ─── APPENDIX ─────────────────────────────────────────────
        '<div class="section" style="page-break-before: always;">',
        '<h1>Appendix · All Scheduled Jobs</h1>',
        f'<p class="muted">Complete inventory, sorted by next-firing time. {total} job(s); {disabled} disabled.</p>',
        _render_jobs_table(jobs),
        '</div>',

        '</body></html>',
    ]
    return ''.join(parts)


# ───────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────


def build_pdf_bytes(
    *,
    lookahead_days: int = 7,
    history_runs: int = 5,
    history_days: int = 30,
    include_disabled: bool = False,
    activity_days: int = 14,
    project_root: Path | None = None,
) -> bytes:
    """Build the PDF report and return raw bytes.

    Raises ``RuntimeError`` if WeasyPrint isn't installed (with a clear
    install hint).
    """
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError(
            'WeasyPrint not installed. Install via pip (and Homebrew '
            'pango/cairo on macOS, or apt libpango/libcairo on Linux). '
            f'Original error: {exc}'
        ) from exc

    if project_root is None:
        project_root = _PROJECT_ROOT

    from schedule_visualization import build_schedule_summary, compute_daily_volume

    summary = build_schedule_summary(
        project_root=project_root,
        lookahead_days=lookahead_days,
        history_lookback_runs=history_runs,
        history_lookback_days=history_days,
        include_disabled=include_disabled,
    )
    buckets = compute_daily_volume(
        project_root=project_root,
        days=activity_days,
    )

    html_str = _render_html(summary, buckets, project_root=project_root)

    pdf_bytes = HTML(string=html_str, base_url=str(project_root)).write_pdf()
    return pdf_bytes


# ───────────────────────────────────────────────────────────────────
# CLI - for cron'd archive runs and manual ops
# ───────────────────────────────────────────────────────────────────


def _cli() -> int:
    parser = argparse.ArgumentParser(
        prog='python -m tools.schedule_pdf',
        description='Generate the SpeakesQuery Schedule Operations Report PDF.',
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default=None,
        help='Output PDF path. Default: schedule-report-YYYYMMDD-HHMM.pdf in CWD.',
    )
    parser.add_argument('--lookahead-days', type=int, default=7)
    parser.add_argument('--history-runs', type=int, default=5)
    parser.add_argument('--history-days', type=int, default=30)
    parser.add_argument('--activity-days', type=int, default=14)
    parser.add_argument('--include-disabled', action='store_true')
    args = parser.parse_args()

    if args.output is None:
        ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
        args.output = f'schedule-report-{ts}.pdf'

    out_path = Path(args.output).resolve()
    print(f'[i] Building PDF → {out_path}')
    pdf = build_pdf_bytes(
        lookahead_days=args.lookahead_days,
        history_runs=args.history_runs,
        history_days=args.history_days,
        include_disabled=args.include_disabled,
        activity_days=args.activity_days,
    )
    out_path.write_bytes(pdf)
    print(f'[i] Wrote {len(pdf):,} bytes ({len(pdf) / 1024:.1f} KB)')
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
