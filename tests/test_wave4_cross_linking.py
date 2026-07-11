"""
Tests for Wave 4 (2026-04-25): cross-link topology endpoint + UI badges
+ tab bar polish.

Coverage
--------
* ``GET /api/topology`` returns the canonical adjacency graph with the
  expected shape.
* Edge resolution is correct: a saved search that queries a subdir
  matches the task whose `subdirectory` field equals that subdir; an
  alert group that names a saved search reverse-links onto that
  search's `alert_groups` array; tasks reverse-link to the searches
  that read their subdirs and the AGs that consume those searches.
* Frontend contracts (static text scan):
    - Each of the three list renderers (Searches / Ingestion / Alert
      Groups) calls ``getTopology()`` and consumes ``_topologyCache``
      via ``_xlBadgeRow`` / ``_xlChip``.
    - Cross-tab nav helpers: ``navigateToSavedSearch``,
      ``navigateToAlertGroup`` exist and target rows by
      ``data-search-name`` / ``data-ag-row-name`` (matching the
      attribute set by the renderers).
    - Tab bar reorder: every existing `data-page` value is still
      present (no page accidentally removed) and the five expected
      group labels appear in order.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _engine_started():
    from scheduled_input_engine import start_engine
    start_engine()


@pytest.fixture
def client():
    from desktop_app.server import app
    app.config["TESTING"] = True
    return app.test_client()


# ── Backend: /api/topology ─────────────────────────────────────────────
class TestTopologyEndpoint:
    def test_returns_success_with_required_shape(self, client):
        resp = client.get("/api/topology")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        for key in ("searches", "tasks", "alert_groups", "scripts"):
            assert key in data, f"missing {key} in topology response"
            assert isinstance(data[key], list)

    def test_search_carries_indexes_subdirs_tasks_alert_groups(
        self, client
    ):
        resp = client.get("/api/topology")
        data = resp.get_json()
        # If any search exists at all, it must carry the four edge fields
        if not data["searches"]:
            pytest.skip("no saved searches in this test environment")
        s = data["searches"][0]
        for key in ("name", "indexes", "subdirs", "tasks", "alert_groups"):
            assert key in s, f"saved search missing {key}"

    def test_task_carries_feeds_searches_and_feeds_alert_groups(
        self, client
    ):
        resp = client.get("/api/topology")
        data = resp.get_json()
        if not data["tasks"]:
            pytest.skip("no ingestion tasks in this test environment")
        t = data["tasks"][0]
        for key in (
            "id", "title", "subdirectory",
            "feeds_searches", "feeds_alert_groups",
        ):
            assert key in t, f"task missing {key}"

    def test_alert_group_feeders_resolved(self, client):
        resp = client.get("/api/topology")
        data = resp.get_json()
        if not data["alert_groups"]:
            pytest.skip("no alert groups in this test environment")
        g = data["alert_groups"][0]
        assert "feeders" in g
        # Each feeder dict carries either resolved metadata or `missing`
        for f in g["feeders"]:
            assert "search_name" in f
            assert ("indexes" in f) or ("missing" in f)

    def test_search_to_ag_reverse_link_consistent(self, client):
        """If AG X has search Y in its search_names, then search Y's
        alert_groups list must contain X. Pin this invariant - the
        whole UI relies on it."""
        resp = client.get("/api/topology")
        data = resp.get_json()
        searches_by_name = {s["name"]: s for s in data["searches"]}
        for g in data["alert_groups"]:
            for sn in g["search_names"]:
                if sn in searches_by_name:
                    assert g["name"] in searches_by_name[sn]["alert_groups"], (
                        f"AG {g['name']} references search {sn} but {sn}'s "
                        f"alert_groups list does not back-reference it"
                    )

    def test_task_to_search_reverse_link_consistent(self, client):
        """If search S targets subdir D and task T's subdirectory==D,
        then T.feeds_searches must contain S.name."""
        resp = client.get("/api/topology")
        data = resp.get_json()
        tasks_by_subdir: dict[str, list] = {}
        for t in data["tasks"]:
            sd = t.get("subdirectory", "")
            if sd:
                tasks_by_subdir.setdefault(sd, []).append(t)
        for s in data["searches"]:
            for sd in s["subdirs"]:
                for t in tasks_by_subdir.get(sd, []):
                    assert s["name"] in t["feeds_searches"], (
                        f"task #{t['id']} (subdir={sd}) read by "
                        f"search {s['name']} but task's "
                        f"feeds_searches list does not include it"
                    )


# ── Frontend contract regressions (static text scan) ───────────────────
class TestFrontendContracts:
    def _ui(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def test_topology_helper_present(self):
        ui = self._ui()
        assert "function getTopology" in ui or "async function getTopology" in ui, (
            "Wave 4 expects a getTopology() helper that fetches "
            "/api/topology and caches the result."
        )
        assert "/api/topology" in ui, (
            "ui.html must reference the /api/topology endpoint."
        )

    def test_badge_primitives_present(self):
        ui = self._ui()
        assert "_xlChip" in ui, (
            "Wave 4 badge primitive _xlChip must exist."
        )
        assert "_xlBadgeRow" in ui, (
            "Wave 4 badge primitive _xlBadgeRow must exist."
        )

    def test_search_renderer_uses_topology(self):
        ui = self._ui()
        # The Searches loader must await topology before rendering rows.
        m = re.search(
            r"async function loadSearches\([\s\S]+?\}\s*\n", ui,
        )
        assert m, "loadSearches function not found"
        body = m.group(0)
        assert "getTopology" in body, (
            "loadSearches must call getTopology() to populate badges"
        )

    def test_ingestion_renderer_uses_topology(self):
        ui = self._ui()
        m = re.search(
            r"async function loadSiScripts\([\s\S]+?\}\s*\n", ui,
        )
        assert m, "loadSiScripts function not found"
        body = m.group(0)
        assert "getTopology" in body, (
            "loadSiScripts must call getTopology() to populate badges"
        )

    def test_alert_groups_renderer_uses_topology(self):
        ui = self._ui()
        m = re.search(
            r"async function loadAlertGroups\([\s\S]+?\n  \}\s*\n", ui,
        )
        assert m, "loadAlertGroups function not found"
        body = m.group(0)
        assert "getTopology" in body, (
            "loadAlertGroups must call getTopology() to populate badges"
        )

    def test_navigation_helpers_present(self):
        ui = self._ui()
        for helper in (
            "navigateToSavedSearch",
            "navigateToAlertGroup",
            "navigateToIngestionTask",  # Wave 2 helper, still required
        ):
            assert f"function {helper}" in ui, (
                f"Wave 4 cross-tab nav helper {helper}() must exist"
            )

    def test_search_row_data_attribute_set(self):
        ui = self._ui()
        # Renderer must tag rows so navigateToSavedSearch can find them.
        assert "tr.dataset.searchName = s.name" in ui, (
            "Search rows must carry data-search-name for cross-tab nav"
        )

    def test_ag_row_data_attribute_set(self):
        ui = self._ui()
        assert "tr.dataset.agRowName = g.name" in ui, (
            "AG rows must carry data-ag-row-name for cross-tab nav"
        )

    def test_navigation_helper_selectors_match_data_attributes(self):
        ui = self._ui()
        # If you change one side, change both. This test fails loud.
        assert 'tr[data-search-name="' in ui, (
            "navigateToSavedSearch must select rows by data-search-name"
        )
        assert 'tr[data-ag-row-name="' in ui, (
            "navigateToAlertGroup must select rows by data-ag-row-name"
        )


# ── Tab bar reorder regression ─────────────────────────────────────────
class TestTabBarReorder:
    EXPECTED_PAGES = [
        # Data
        "page-query", "page-lookups", "page-import",
        # Search
        "page-create-search", "page-searches", "page-macros",
        # Ingestion
        "page-create-ingestion", "page-ingestion", "page-library",
        # Alerts
        "page-alert-groups", "page-email-groups", "page-schedule",
        # Develop (Phase 3 / Bet 4 slice 4 - 2026-05-09)
        "page-notebooks",
        # Help
        "page-settings", "page-docs",
    ]

    EXPECTED_GROUP_LABELS = [
        "Data", "Search", "Ingestion", "Alerts", "Develop", "Help",
    ]

    def _ui(self) -> str:
        return (REPO_ROOT / "desktop_app" / "ui.html").read_text(
            encoding="utf-8"
        )

    def _nav_block(self, ui: str) -> str:
        """Return the inner contents of the ``<div class="nav-tabs">``
        container. The 2026-04-26 dropdown redesign nests
        ``<div class="nav-group-wrapper">`` and ``<div class="nav-dropdown">``
        inside, so a naive non-greedy regex stops at the first inner
        ``</div>``. Walk the brackets manually for robustness."""
        start_match = re.search(r'<div class="nav-tabs"[^>]*>', ui)
        assert start_match, "nav-tabs container not found"
        i = start_match.end()
        depth = 1
        while i < len(ui) and depth > 0:
            open_at = ui.find("<div", i)
            close_at = ui.find("</div>", i)
            if close_at == -1:
                break
            if open_at != -1 and open_at < close_at:
                depth += 1
                i = open_at + 4
            else:
                depth -= 1
                if depth == 0:
                    return ui[start_match.end():close_at]
                i = close_at + 6
        raise AssertionError("nav-tabs container is unbalanced")

    def test_every_expected_page_still_exists(self):
        """The 2026-04-26 dropdown redesign must not drop any page. If a
        page is removed intentionally, update EXPECTED_PAGES in the same
        commit."""
        ui = self._ui()
        nav = self._nav_block(ui)
        for page in self.EXPECTED_PAGES:
            assert f'data-page="{page}"' in nav, (
                f"tab for {page} missing from nav after dropdown redesign"
            )

    def test_pages_appear_in_grouped_order(self):
        """Tabs must appear in the grouped order. The whole point of
        the reorder is that related tabs sit next to each other."""
        ui = self._ui()
        nav = self._nav_block(ui)
        positions = {
            page: nav.find(f'data-page="{page}"')
            for page in self.EXPECTED_PAGES
        }
        sorted_by_position = sorted(
            self.EXPECTED_PAGES, key=lambda p: positions[p]
        )
        assert sorted_by_position == self.EXPECTED_PAGES, (
            f"Tabs are out of expected order. Found: {sorted_by_position}"
        )

    def test_group_labels_present_in_order(self):
        """The 2026-04-26 redesign moved group labels from
        ``<span class="nav-group-label">`` siblings into the new
        ``<button class="nav-group" data-group="...">`` dropdown
        triggers. The label text now lives inside the button body."""
        ui = self._ui()
        nav = self._nav_block(ui)
        label_positions = []
        for label in self.EXPECTED_GROUP_LABELS:
            # Match the opening of a <button class="nav-group" ...> and the
            # label text on the next non-empty line. Tolerate attribute order.
            m = re.search(
                r'<button[^>]*class="nav-group"[^>]*'
                r'data-group="[^"]+"[^>]*>\s*'
                rf'{label}\b',
                nav,
            )
            assert m, f"group button for label {label!r} missing from nav"
            label_positions.append(m.start())
        assert label_positions == sorted(label_positions), (
            "group buttons are out of expected order"
        )

    def test_data_group_attribute_on_every_nav_tab(self):
        ui = self._ui()
        nav = self._nav_block(ui)
        # Every nav-tab button must carry data-group so the styling +
        # future per-group behaviour have a hook.
        tab_count = len(re.findall(r'class="nav-tab[^"]*"\s+data-page=', nav))
        grouped_count = len(re.findall(
            r'class="nav-tab[^"]*"\s+data-page="[^"]+"\s+data-group=', nav,
        ))
        assert tab_count == grouped_count, (
            f"{tab_count - grouped_count} nav-tab(s) missing data-group "
            f"attribute"
        )

    def test_nav_tabs_container_supports_wrapping(self):
        ui = self._ui()
        # CSS rule for .nav-tabs must include flex-wrap so the nav row
        # wraps on narrow viewports instead of overflowing.
        m = re.search(r"\.nav-tabs\s*\{([\s\S]+?)\}", ui)
        assert m, "could not find .nav-tabs CSS rule"
        rule = m.group(1)
        assert "flex-wrap: wrap" in rule, (
            "Tab bar must use flex-wrap so tabs wrap on narrow "
            "viewports instead of overflowing."
        )
