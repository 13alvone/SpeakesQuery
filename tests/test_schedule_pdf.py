"""
Tests for tools/schedule_pdf.py - the polished PDF report builder
shipped 2026-05-01.

Covers:
- Module imports cleanly (no syntax errors, no missing deps in test env)
- HTML renderer produces something resembling HTML with the key sections
- Heatmap SVG has the right shape (7 rows × 24 columns)
- Activity chart SVG renders both bar + line components
- Anomaly detection categorises jobs correctly
- Per-AG block builder pairs feeders with health
- /api/schedule/pdf endpoint returns valid PDF magic bytes when
  WeasyPrint is installed; returns 503 with a hint otherwise
- CLI generates a real PDF file (skipped if WeasyPrint not installed)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Skip the whole suite if WeasyPrint isn't installed - these tests
# exercise the public render path which calls into WeasyPrint at the
# end. Module-level smoke tests still run.
weasyprint_available = True
try:
    import weasyprint  # noqa: F401
except ImportError:
    weasyprint_available = False


def test_module_imports_cleanly():
    """Importing the module must not raise even when WeasyPrint is missing."""
    from tools import schedule_pdf
    assert hasattr(schedule_pdf, 'build_pdf_bytes')
    assert hasattr(schedule_pdf, '_render_html')


def test_heat_color_thresholds():
    """The heat-color helper assigns the right band based on intensity."""
    from tools.schedule_pdf import _heat_color

    bg_zero, fg_zero = _heat_color(0)
    bg_low, _ = _heat_color(2)
    bg_mid, _ = _heat_color(15)
    bg_hot, fg_hot = _heat_color(60)

    # Zero is the palest; hot is the darkest (orange family)
    assert 'rgba(34, 139, 230' in bg_zero  # blue family near-empty
    assert 'rgba(34, 139, 230' in bg_low
    assert 'rgba(34, 139, 230' in bg_mid
    assert 'rgba(220, 99, 50' in bg_hot   # orange = hot

    # Hot cells use white text for contrast
    assert fg_hot == '#ffffff'
    # Empty cells use muted text
    assert fg_zero != '#ffffff'


def test_format_cell_value_volume_vs_count():
    """Volume cells get k-suffix at >= 1000; count cells stay raw."""
    from tools.schedule_pdf import _format_cell_value
    assert _format_cell_value(0) == ''
    assert _format_cell_value(None) == ''
    assert _format_cell_value(7) == '7'
    assert _format_cell_value(7, is_volume=True) == '7'
    assert _format_cell_value(2400, is_volume=True) == '2k'
    assert _format_cell_value(2400, is_volume=False) == '2400'


def test_render_heatmap_svg_shape():
    """7 rows (Mon..Sun) × 24 cols + day labels + UTC header."""
    from tools.schedule_pdf import _render_heatmap_svg
    by_dow_hour = {dow: [dow] * 24 for dow in range(7)}
    svg = _render_heatmap_svg(
        title='Test',
        by_dow_hour=by_dow_hour,
        is_volume=False,
    )
    assert svg.startswith('<svg')
    assert svg.endswith('</svg>')
    # 7×24 = 168 cells
    assert svg.count('<rect ') == 7 * 24
    # All 7 day labels present
    for label in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'):
        assert label in svg
    # 24 hour labels (00..23) - sample a couple
    assert '>00<' in svg
    assert '>23<' in svg


def test_render_heatmap_volume_with_no_data_mask():
    """Volume mode renders an em-dash for cells where has_data_mask is False."""
    from tools.schedule_pdf import _render_heatmap_svg
    by_dow_hour = {dow: [0] * 24 for dow in range(7)}
    has_mask = {dow: [False] * 24 for dow in range(7)}
    svg = _render_heatmap_svg(
        title='Test',
        by_dow_hour=by_dow_hour,
        is_volume=True,
        has_data_mask=has_mask,
    )
    # No-data cells render the em-dash sentinel
    assert ' - ' in svg


def test_render_activity_charts_with_data():
    """Bar + line chart SVG includes legend + axis labels + dots."""
    from tools.schedule_pdf import _render_activity_charts_svg
    buckets = [
        {
            'date': f'2026-04-{i+10:02d}',
            'ingestion_runs': 30 + i,
            'search_runs': 100 + i * 5,
            'ag_dispatches': 5 + (i % 3),
            'rows_ingested': 1500 + i * 100,
        }
        for i in range(14)
    ]
    svg = _render_activity_charts_svg(buckets)
    assert svg.startswith('<svg')
    # Legend labels
    assert 'Ingestion' in svg
    assert 'Saved-Search' in svg
    assert 'Alert-Group' in svg
    # Line chart label
    assert 'Rows ingested per day' in svg
    # Should have 14 datapoints → 14 circles
    assert svg.count('<circle ') == 14


def test_render_activity_charts_empty_buckets():
    """Empty input degrades to an inline message, not a crash."""
    from tools.schedule_pdf import _render_activity_charts_svg
    out = _render_activity_charts_svg([])
    assert 'No recent activity' in out


def test_identify_anomalies_categorisation():
    """Each anomaly bucket only catches matching jobs."""
    from tools.schedule_pdf import _identify_anomalies
    jobs = [
        # Healthy ingestion job
        {'kind': 'ingestion', 'name': 'A', 'cron': '0 * * * *',
         'disabled': False, 'run_count': 5, 'avg_row_count': 100,
         'avg_duration_ms': 200},
        # Never-ran saved search
        {'kind': 'saved_search', 'name': 'B', 'cron': '0 0 * * *',
         'disabled': False, 'run_count': 0, 'avg_row_count': None,
         'avg_duration_ms': None},
        # Empty output saved search
        {'kind': 'saved_search', 'name': 'C', 'cron': '0 0 * * *',
         'disabled': False, 'run_count': 5, 'avg_row_count': 0,
         'avg_duration_ms': 50},
        # Latency outlier ingestion
        {'kind': 'ingestion', 'name': 'D', 'cron': '0 0 * * *',
         'disabled': False, 'run_count': 3, 'avg_row_count': 50,
         'avg_duration_ms': 60_000},
        # Disabled
        {'kind': 'saved_search', 'name': 'E', 'cron': '0 0 * * *',
         'disabled': True, 'run_count': 5, 'avg_row_count': 50,
         'avg_duration_ms': 100},
        # Failing every recent run - pre-2026-07-01 this job showed
        # avg_row_count None (" - ") and escaped every bucket.
        {'kind': 'saved_search', 'name': 'F', 'cron': '0 0 * * *',
         'disabled': False, 'run_count': 5, 'avg_row_count': None,
         'avg_duration_ms': 90, 'error_count': 5},
    ]
    out = _identify_anomalies(jobs)
    assert [j['name'] for j in out['never_ran']] == ['B']
    assert [j['name'] for j in out['zero_rows']] == ['C']
    assert [j['name'] for j in out['latency_outliers']] == ['D']
    assert [j['name'] for j in out['disabled']] == ['E']
    assert [j['name'] for j in out['failing']] == ['F'], (
        "Jobs with errored recent runs must land in the failing bucket"
    )


def test_format_helpers():
    """Duration + row + ISO formatters handle None and edge cases."""
    from tools.schedule_pdf import (
        _format_duration_ms,
        _format_rows,
        _format_iso_short,
    )
    assert _format_duration_ms(None) == ' - '
    assert _format_duration_ms(500) == '500ms'
    assert _format_duration_ms(1500) == '1.50s'
    assert _format_duration_ms(125_000).endswith('m')

    assert _format_rows(None) == ' - '
    assert _format_rows(0) == '0'
    assert _format_rows(450) == '450'
    assert _format_rows(2500) == '2.5k'

    assert _format_iso_short(None) == ' - '
    assert 'UTC' in _format_iso_short('2026-05-01T13:30:00+00:00')


def test_render_html_contains_all_sections(monkeypatch):
    """The full HTML render includes cover, exec summary, heatmaps,
    activity, AG blocks, anomalies, and the appendix."""
    from tools import schedule_pdf

    # Monkey-patch the AG store so the test doesn't need real YAML files
    class FakeAGStore:
        def initialize(self):
            pass

        def list_groups(self):
            return [{
                'name': 'test_brief',
                'search_names': ['feeder_a', 'feeder_b'],
            }]

    import alert_group_store
    monkeypatch.setattr(alert_group_store, 'AlertGroupStore', FakeAGStore)

    summary = {
        'generated_at_epoch': 1777605000,
        'lookahead_days': 7,
        'history_lookback_runs': 5,
        'jobs': [
            {'kind': 'alert_group', 'name': 'test_brief', 'cron': '0 12 * * *',
             'disabled': False, 'feeder_count': 2, 'next_firing_iso': '2026-05-02T12:00:00+00:00',
             'next_firing_epoch': 1777680000, 'firings_in_lookahead': 7,
             'run_count': 3, 'avg_row_count': None, 'avg_duration_ms': 1500},
            {'kind': 'saved_search', 'name': 'feeder_a', 'cron': '0 11 * * *',
             'disabled': False, 'next_firing_iso': '2026-05-02T11:00:00+00:00',
             'next_firing_epoch': 1777676400, 'firings_in_lookahead': 7,
             'run_count': 5, 'avg_row_count': 12, 'avg_duration_ms': 250},
            {'kind': 'saved_search', 'name': 'feeder_b', 'cron': '0 11 * * *',
             'disabled': False, 'next_firing_iso': '2026-05-02T11:00:00+00:00',
             'next_firing_epoch': 1777676400, 'firings_in_lookahead': 7,
             'run_count': 5, 'avg_row_count': 0, 'avg_duration_ms': 100},
        ],
        'hour_distribution': {
            'by_dow_hour': {dow: [1] * 24 for dow in range(7)},
            'by_hour_total': [7] * 24,
            'total_firings': 168,
        },
        'data_distribution': {
            'by_dow_hour': {dow: [0.0] * 24 for dow in range(7)},
            'by_dow_hour_has_data': {dow: [False] * 24 for dow in range(7)},
            'by_hour_total': [0.0] * 24,
        },
        'summary': {
            'total_jobs': 3,
            'total_jobs_disabled': 0,
            'by_kind': {'ingestion': 0, 'saved_search': 2, 'alert_group': 1},
            'busiest_hour_utc': 11,
            'busiest_hour_count': 2,
            'biggest_data_hour_utc': None,
            'biggest_data_hour_total': 0,
        },
    }
    buckets = [
        {'date': '2026-04-30', 'ingestion_runs': 5, 'search_runs': 20,
         'ag_dispatches': 1, 'rows_ingested': 100},
    ]
    html = schedule_pdf._render_html(
        summary, buckets, project_root=PROJECT_ROOT,
    )
    # Cover page
    assert 'Schedule Operations Report' in html
    assert 'Every job. Every cron.' in html
    # Executive summary
    assert 'Executive Summary' in html
    assert 'Total Jobs' in html
    # Heatmaps
    assert 'Firing Count Heatmap' in html
    assert 'Expected Data Volume Heatmap' in html
    # Activity charts
    assert 'Recent Activity' in html
    # Per-AG section
    assert 'Per-AG Feeder Health' in html
    assert 'test_brief' in html
    assert 'feeder_a' in html
    assert 'feeder_b' in html
    # feeder_a should show as OK (avg_row_count > 0); feeder_b as EMPTY
    assert 'feeder-pill ok' in html
    assert 'feeder-pill empty' in html
    # Anomalies
    assert 'Highlights' in html
    # Appendix
    assert 'All Scheduled Jobs' in html
    # Job table includes our test rows
    assert 'feeder_a' in html
    # Brand color used somewhere
    assert '#4f9fde' in html


@pytest.mark.skipif(
    not weasyprint_available,
    reason='WeasyPrint not installed - skipping end-to-end render test',
)
def test_build_pdf_bytes_returns_pdf():
    """End-to-end: build_pdf_bytes returns valid PDF magic-byte output."""
    from tools.schedule_pdf import build_pdf_bytes
    pdf = build_pdf_bytes(
        lookahead_days=7,
        history_runs=5,
        history_days=30,
        activity_days=7,
    )
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b'%PDF')
    assert len(pdf) > 5000  # at least a few KB - meaningful content


@pytest.mark.skipif(
    not weasyprint_available,
    reason='WeasyPrint not installed - skipping endpoint render test',
)
def test_endpoint_returns_pdf_with_correct_headers():
    """GET /api/schedule/pdf returns 200 + application/pdf + filename."""
    # Lazy-import to avoid Flask app initialisation at collection time
    sys.path.insert(0, str(PROJECT_ROOT))
    from desktop_app.server import app
    client = app.test_client()
    r = client.get('/api/schedule/pdf?activity_days=7&lookahead_days=7')
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.headers['Content-Type'] == 'application/pdf'
    assert 'attachment' in r.headers['Content-Disposition']
    assert 'speakesquery-schedule-report-' in r.headers['Content-Disposition']
    assert r.data.startswith(b'%PDF')


def test_endpoint_503_when_weasyprint_missing(monkeypatch):
    """When WeasyPrint isn't installed, the endpoint returns 503 + hint."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from desktop_app.server import app

    # Simulate WeasyPrint missing by patching build_pdf_bytes to raise
    # the exact RuntimeError the real module raises.
    from tools import schedule_pdf

    def _fake_build(**kwargs):  # noqa: E306
        raise RuntimeError(
            'WeasyPrint not installed. Install via pip (test simulation).'
        )

    monkeypatch.setattr(schedule_pdf, 'build_pdf_bytes', _fake_build)

    client = app.test_client()
    r = client.get('/api/schedule/pdf')
    assert r.status_code == 503
    body = r.get_json()
    assert body['status'] == 'error'
    assert 'WeasyPrint' in body['message']
    assert 'hint' in body  # actionable install guidance


@pytest.mark.skipif(
    not weasyprint_available,
    reason='WeasyPrint not installed - CLI test skipped',
)
def test_cli_writes_pdf_file(tmp_path):
    """`python -m tools.schedule_pdf --output X.pdf` writes a real PDF."""
    out = tmp_path / 'cli-out.pdf'
    result = subprocess.run(
        [sys.executable, '-m', 'tools.schedule_pdf',
         '--output', str(out),
         '--lookahead-days', '7',
         '--activity-days', '7'],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert out.read_bytes().startswith(b'%PDF')
    assert out.stat().st_size > 5000


# ── Per-AG feeder health: PLACEHOLDER vs MISSING distinction ───────


def test_reserved_picks_render_as_placeholder_not_missing(monkeypatch):
    """`*_reserved_picks` feeders are intentionally not on a cron - the
    AG dispatcher invokes them on demand. They have empty cron schedules
    and don't appear in the scheduler's job list. Pre-2026-05-05 the PDF
    renderer flagged them as MISSING in every AG (confusing - they look
    identical to genuinely-broken feeders). Now they render as
    PLACEHOLDER with neutral grey styling."""
    from tools import schedule_pdf

    # Synthetic jobs list - only one AG with two feeders, neither on cron
    jobs = [
        {
            'kind': 'alert_group',
            'name': 'test_brief',
            'disabled': False,
            'cron': '0 12 * * *',
            'next_firing_iso': '2026-05-06T12:00:00Z',
            'feeder_count': 2,
        },
        # one real feeder with run history
        {
            'kind': 'saved_search',
            'name': 'test_real_feeder',
            'run_count': 5,
            'avg_row_count': 12.0,
            'avg_duration_ms': 800,
        },
        # NB: test_reserved_picks is NOT in jobs (no cron → not scheduled)
    ]

    # AG store mock - returns one AG with two feeders, one of which is reserved_picks
    class _FakeAGStore:
        def initialize(self):
            return None

        def list_groups(self):
            return [{
                'name': 'test_brief',
                'search_names': ['test_real_feeder', 'test_reserved_picks'],
            }]

    monkeypatch.setattr('alert_group_store.AlertGroupStore', _FakeAGStore)

    blocks = schedule_pdf._build_per_ag_blocks(jobs)
    assert len(blocks) == 1
    feeders = blocks[0]['feeders']
    by_name = {f['name']: f for f in feeders}

    assert by_name['test_real_feeder']['status'] == 'ok'
    assert by_name['test_reserved_picks']['status'] == 'placeholder', (
        f"*_reserved_picks must render as PLACEHOLDER, got "
        f"{by_name['test_reserved_picks']['status']!r}"
    )


def test_genuinely_missing_feeder_still_renders_as_missing(monkeypatch):
    """Drift guard: a feeder that doesn't end with `_reserved_picks` and
    isn't in the scheduler's jobs list is genuinely missing - the
    PLACEHOLDER carve-out must not mask real broken feeders."""
    from tools import schedule_pdf

    jobs = [{
        'kind': 'alert_group',
        'name': 'test_brief',
        'disabled': False,
        'cron': '0 12 * * *',
        'next_firing_iso': '2026-05-06T12:00:00Z',
        'feeder_count': 1,
    }]

    class _FakeAGStore:
        def initialize(self):
            return None

        def list_groups(self):
            return [{
                'name': 'test_brief',
                'search_names': ['feeder_that_does_not_exist'],
            }]

    monkeypatch.setattr('alert_group_store.AlertGroupStore', _FakeAGStore)

    class _FakeSSStore:
        def initialize(self):
            return None

        def list_searches(self):
            return []  # nothing installed

    monkeypatch.setattr('saved_search_store.SavedSearchStore', _FakeSSStore)

    blocks = schedule_pdf._build_per_ag_blocks(jobs, history={})
    feeders = blocks[0]['feeders']
    assert feeders[0]['status'] == 'missing', (
        "Non-`_reserved_picks` feeders that aren't installed in the "
        "saved-search store must still surface as MISSING"
    )


def test_dispatch_time_feeder_not_flagged_missing(monkeypatch):
    """A feeder that EXISTS in the saved-search store but has no cron
    (``purpose: alert_group_feeder``, run on demand by the AG dispatcher
    - e.g. the Slice B/C ``github_hot_repos_today`` /
    ``ai_papers_new_today`` feeders) must NOT be reported MISSING.
    Caught 2026-07-01: the production schedule report flagged two
    healthy dispatch-time feeders as MISSING while their AGs dispatched
    fine. Health comes from the dispatcher's search_runs history rows."""
    from tools import schedule_pdf

    jobs = [{
        'kind': 'alert_group',
        'name': 'hot_repos_brief',
        'disabled': False,
        'cron': '30 8 * * *',
        'next_firing_iso': '2026-07-02T08:30:00Z',
        'feeder_count': 3,
    }]

    class _FakeAGStore:
        def initialize(self):
            return None

        def list_groups(self):
            return [{
                'name': 'hot_repos_brief',
                'search_names': [
                    'feeder_with_history',    # dispatched at least once
                    'feeder_never_dispatched',  # installed, no runs yet
                    'feeder_always_erroring',   # installed, all runs error
                ],
            }]

    class _FakeSSStore:
        def initialize(self):
            return None

        def list_searches(self):
            return [
                {'name': 'feeder_with_history'},
                {'name': 'feeder_never_dispatched'},
                {'name': 'feeder_always_erroring'},
            ]

    monkeypatch.setattr('alert_group_store.AlertGroupStore', _FakeAGStore)
    monkeypatch.setattr('saved_search_store.SavedSearchStore', _FakeSSStore)

    history = {
        'saved_search::feeder_with_history': {
            'run_count': 5, 'avg_row_count': 8.0,
            'avg_duration_ms': 900, 'error_count': 0,
        },
        'saved_search::feeder_always_erroring': {
            'run_count': 5, 'avg_row_count': None,
            'avg_duration_ms': 400, 'error_count': 5,
        },
    }

    blocks = schedule_pdf._build_per_ag_blocks(jobs, history=history)
    by_name = {f['name']: f for f in blocks[0]['feeders']}

    assert by_name['feeder_with_history']['status'] == 'ok', (
        "Installed dispatch-time feeder with healthy history must be OK, "
        f"got {by_name['feeder_with_history']['status']!r}"
    )
    assert by_name['feeder_with_history']['avg_rows'] == 8.0
    assert by_name['feeder_never_dispatched']['status'] == 'on_demand', (
        "Installed dispatch-time feeder with no runs yet must be "
        f"ON-DEMAND, got {by_name['feeder_never_dispatched']['status']!r}"
    )
    assert by_name['feeder_always_erroring']['status'] == 'failing', (
        "Installed feeder erroring on every recent run must be FAILING, "
        f"got {by_name['feeder_always_erroring']['status']!r}"
    )


def test_cron_feeder_with_all_error_runs_is_failing(monkeypatch):
    """A cron-scheduled feeder whose every recent run errored must render
    FAILING, not EMPTY - pre-2026-07-01 the two were conflated."""
    from tools import schedule_pdf

    jobs = [
        {
            'kind': 'alert_group',
            'name': 'test_brief',
            'disabled': False,
            'cron': '0 12 * * *',
            'next_firing_iso': '2026-07-02T12:00:00Z',
            'feeder_count': 1,
        },
        {
            'kind': 'saved_search',
            'name': 'broken_feeder',
            'run_count': 5,
            'avg_row_count': None,
            'avg_duration_ms': 400,
            'error_count': 5,
        },
    ]

    class _FakeAGStore:
        def initialize(self):
            return None

        def list_groups(self):
            return [{
                'name': 'test_brief',
                'search_names': ['broken_feeder'],
            }]

    monkeypatch.setattr('alert_group_store.AlertGroupStore', _FakeAGStore)

    blocks = schedule_pdf._build_per_ag_blocks(jobs, history={})
    assert blocks[0]['feeders'][0]['status'] == 'failing'


def test_on_demand_and_failing_labels_and_css():
    """Renderer emits ON-DEMAND / FAILING labels with matching CSS
    classes, and the section legend documents MISSING as installable."""
    from tools.schedule_pdf import _render_per_ag_section, _CSS

    blocks = [{
        'name': 'sample_brief',
        'cron': '30 8 * * *',
        'next_firing': '2026-07-02T08:30:00Z',
        'feeder_count': 2,
        'feeders': [
            {'name': 'sample_on_demand', 'status': 'on_demand',
             'avg_rows': None, 'avg_duration_ms': None},
            {'name': 'sample_failing', 'status': 'failing',
             'avg_rows': None, 'avg_duration_ms': 300},
        ],
    }]
    html = _render_per_ag_section(blocks)
    assert 'ON-DEMAND' in html
    assert 'feeder-pill on_demand' in html
    assert 'FAILING' in html
    assert 'feeder-pill failing' in html
    assert '.feeder-pill.on_demand' in _CSS
    assert '.feeder-pill.failing' in _CSS


def test_placeholder_label_and_css_class_in_renderer():
    """The HTML renderer must emit a 'PLACEHOLDER' label + matching
    CSS class for placeholder-status feeders, with neutral styling
    (not the loud purple of MISSING)."""
    from tools.schedule_pdf import _render_per_ag_section, _CSS

    blocks = [{
        'name': 'sample_brief',
        'cron': '0 12 * * *',
        'next_firing': '2026-05-06T12:00:00Z',
        'feeder_count': 1,
        'feeders': [{
            'name': 'sample_reserved_picks',
            'status': 'placeholder',
            'avg_rows': None,
            'avg_duration_ms': None,
        }],
    }]
    html = _render_per_ag_section(blocks)
    assert 'PLACEHOLDER' in html, "Renderer must emit PLACEHOLDER label"
    assert 'feeder-pill placeholder' in html, (
        "Pill must carry the `placeholder` CSS class for distinct styling"
    )
    # CSS rule must exist so the pill actually gets the neutral grey
    assert '.feeder-pill.placeholder' in _CSS
