"""
Tests for the hour-of-day bar chart added under each Schedule-page
heatmap. The chart projects ``by_hour_total`` (already in the
``/api/schedule/heatmap`` response) onto a 24-bar SVG so the user can
spot the busiest hour at a glance.

Coverage
--------
* The two new containers (``sched-bar-count`` / ``sched-bar-data``)
  exist on the Schedule page, each anchored under its matching heatmap.
* ``renderHourBar`` is defined and the heatmap loader calls it for both
  the count and data distributions.
* The chart stays inline SVG - no Chart.js / D3 / Recharts re-introduced.
* The peak bar is highlighted (different fill from non-peak bars) so the
  user's stated goal - "see visually which bar is highest" - is met.
* The renderer reads ``by_hour_total`` (24 ints), matching the API
  contract from ``schedule_visualization.compute_hour_distribution``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_PATH = REPO_ROOT / "desktop_app" / "ui.html"


def _ui() -> str:
    return UI_PATH.read_text(encoding="utf-8")


class TestBarContainersPresent:
    def test_count_bar_container_exists(self):
        assert 'id="sched-bar-count"' in _ui(), (
            "Hour-of-day bar chart container for the COUNT heatmap is "
            "missing - users lose the at-a-glance overload signal."
        )

    def test_data_bar_container_exists(self):
        assert 'id="sched-bar-data"' in _ui(), (
            "Hour-of-day bar chart container for the DATA heatmap is "
            "missing - users lose the at-a-glance overload signal."
        )

    def test_count_bar_anchored_under_count_heatmap(self):
        ui = _ui()
        i_heat = ui.find('id="sched-heatmap-count"')
        i_bar = ui.find('id="sched-bar-count"')
        assert i_heat != -1 and i_bar != -1
        assert i_heat < i_bar, (
            "The COUNT bar chart must render after the COUNT heatmap "
            "so they read as a vertically stacked pair."
        )

    def test_data_bar_anchored_under_data_heatmap(self):
        ui = _ui()
        i_heat = ui.find('id="sched-heatmap-data"')
        i_bar = ui.find('id="sched-bar-data"')
        assert i_heat != -1 and i_bar != -1
        assert i_heat < i_bar, (
            "The DATA bar chart must render after the DATA heatmap "
            "so they read as a vertically stacked pair."
        )


class TestRendererWiring:
    def test_render_function_defined(self):
        assert "function renderHourBar(" in _ui(), (
            "renderHourBar must exist - heatmap loader calls it."
        )

    def test_loader_invokes_renderer_for_both_views(self):
        ui = _ui()
        m = re.search(
            r"async function loadScheduleHeatmap\(\)\s*\{([\s\S]+?)\n  \}",
            ui,
        )
        assert m, "loadScheduleHeatmap definition not found"
        body = m.group(1)
        assert "renderHourBar('sched-bar-count'" in body, (
            "loadScheduleHeatmap must call renderHourBar for the count "
            "view alongside renderHeatmap."
        )
        assert "renderHourBar('sched-bar-data'" in body, (
            "loadScheduleHeatmap must call renderHourBar for the data "
            "view alongside renderHeatmap."
        )

    def test_renderer_reads_by_hour_total(self):
        ui = _ui()
        m = re.search(
            r"function renderHourBar\([^\)]*\)\s*\{([\s\S]+?)\n  \}",
            ui,
        )
        assert m, "renderHourBar function body not found"
        body = m.group(1)
        assert "by_hour_total" in body, (
            "renderHourBar must consume `by_hour_total` from the heatmap "
            "API response - that's the 24-int array the heatmap is "
            "already projecting onto its day rows."
        )


class TestVisualContract:
    """The user asked for `the highest bar is obvious'. Pin the visual
    cues that deliver that - peak highlight + inline SVG + tooltip."""

    def test_peak_bar_uses_distinct_fill(self):
        ui = _ui()
        m = re.search(
            r"function renderHourBar\([^\)]*\)\s*\{([\s\S]+?)\n  \}",
            ui,
        )
        assert m
        body = m.group(1)
        assert "peakIdx" in body, (
            "renderHourBar must compute a peak index so the tallest "
            "bar can be drawn in a distinct color."
        )
        # Peak fill must differ from the regular bar fill so the user's
        # eye can lock onto it. Both colors should be present in the
        # function body.
        assert body.count("COLOR_PEAK") >= 2, (
            "Peak color constant must be referenced (definition + use)"
        )
        assert "COLOR_BAR" in body, "Non-peak bar color constant missing"

    def test_renderer_uses_inline_svg(self):
        ui = _ui()
        # Inline-SVG contract from Wave 6 - same rule applies here.
        # Patterns tightened 2026-05-09 (slice 7) for the same reason
        # as test_wave6_schedule_volume - see that file's comment.
        assert '<svg viewBox=' in ui
        for forbidden in (
            "chart.js", "Chart.js", "new Chart(",
            "d3.select(", "from 'recharts'", 'from "recharts"',
        ):
            assert forbidden not in ui, (
                f"The Schedule page must stay chart-library-free; "
                f"found {forbidden!r}"
            )

    def test_bar_has_hover_tooltip(self):
        ui = _ui()
        m = re.search(
            r"function renderHourBar\([^\)]*\)\s*\{([\s\S]+?)\n  \}",
            ui,
        )
        assert m
        body = m.group(1)
        # Native SVG <title> tooltip - same approach as the heatmap +
        # Wave 6 charts. No fancy popover.
        assert "<title>" in body, (
            "Bars must carry a <title> so hovering reveals the exact "
            "hour + value."
        )
