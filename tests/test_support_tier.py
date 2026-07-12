"""Connector support-tier guards (weakness audit W13, 2026-07-12).

135 connectors maintained by one person is an overpromise unless the
promise is tiered. Every library script now carries an explicit
``support_tier``:

- ``core``    - documented, stable API; maintained with the project.
- ``example`` - author-provided reference on an unofficial or fragile
                endpoint (HTML scrape, undocumented API); can break
                without warning when the upstream changes. The UI badges
                these and the library page carries a use-at-your-own-risk
                disclaimer.

Pins:
1. Every script declares the field explicitly (no silent default in
   the data files; the loader's fallback is "example" - fail-safe).
2. The example set is FROZEN here: promoting or demoting a script is a
   deliberate act that updates this list in the same commit (and the
   README connector count).
3. The loader exposes the field; the UI renders the badge + disclaimer.
4. The README's "N connectors (M core)" stays in sync with the data.
"""

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "script_library" / "scripts"
UI_HTML = PROJECT_ROOT / "desktop_app" / "ui.html"
README = PROJECT_ROOT / "README.md"

# Frozen classification (2026-07-12). Rationale per script:
#   earnings_calendar_72h   - api.nasdaq.com: unofficial, UA-gated
#   espn_injuries_feed      - site.api.espn.com: undocumented; the ESPN
#                             payload-drift precedent from the ledger
#   github_trending_repos   - HTML scrape of github.com/trending
#   google_trends_signals   - trends.google.com: unofficial endpoint
#   jsonplaceholder_posts   - demo/testing API; an example by nature
EXPECTED_EXAMPLE_TIER = {
    "earnings_calendar_72h",
    "espn_injuries_feed",
    "github_trending_repos",
    "google_trends_signals",
    "jsonplaceholder_posts",
}


def _all_scripts() -> dict:
    scripts = {}
    for path in sorted(SCRIPTS_DIR.glob("*.json")):
        scripts[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return scripts


class TestTierField:
    def test_every_script_declares_support_tier_explicitly(self):
        missing = [
            sid for sid, data in _all_scripts().items()
            if "support_tier" not in data
        ]
        assert missing == [], (
            f"scripts missing explicit support_tier: {missing} - new "
            f"scripts must declare their tier (default-by-omission would "
            f"silently claim or disclaim maintenance)"
        )

    def test_tier_values_are_valid(self):
        bad = {
            sid: data["support_tier"] for sid, data in _all_scripts().items()
            if data.get("support_tier") not in ("core", "example")
        }
        assert bad == {}, f"invalid support_tier values: {bad}"

    def test_example_set_is_frozen(self):
        actual = {
            sid for sid, data in _all_scripts().items()
            if data.get("support_tier") == "example"
        }
        assert actual == EXPECTED_EXAMPLE_TIER, (
            f"support-tier classification drifted. Newly demoted: "
            f"{sorted(actual - EXPECTED_EXAMPLE_TIER)}; newly promoted: "
            f"{sorted(EXPECTED_EXAMPLE_TIER - actual)}. Tier moves are "
            f"deliberate: update EXPECTED_EXAMPLE_TIER, the README "
            f"connector count, and the library docs in the same commit."
        )


class TestLoaderExposure:
    def test_list_scripts_carries_tier(self):
        from script_library import list_scripts
        scripts = list_scripts()
        assert scripts, "script library is empty?"
        for s in scripts:
            assert s.get("support_tier") in ("core", "example")

    def test_loader_defaults_to_example_when_absent(self, tmp_path, monkeypatch):
        # Fail-safe: an unclassified script must never claim maintenance.
        import script_library
        stray = tmp_path / "unclassified_script.json"
        stray.write_text(json.dumps({
            "title": "T", "description": "d", "category": "c",
            "api_url": "https://example.com", "requires_credentials": [],
            "suggested_cron": "0 * * * *", "suggested_subdirectory": "x/y",
            "tags": ["free"], "code": "GENERATE_RESULTS(df)",
        }))
        monkeypatch.setattr(script_library, "SCRIPTS_DIR", str(tmp_path))
        [meta] = script_library.list_scripts()
        assert meta["support_tier"] == "example"


class TestUiContract:
    def test_badge_and_disclaimer_present(self):
        ui = UI_HTML.read_text(encoding="utf-8")
        assert "example-tier" in ui, "library card must badge example-tier scripts"
        assert 'id="lib-tier-disclaimer"' in ui, (
            "library page must keep the use-at-your-own-risk disclaimer"
        )
        assert ".lib-tag.example" in ui, "example badge CSS missing"


class TestReadmeCountSync:
    def test_readme_states_tiered_connector_count(self):
        scripts = _all_scripts()
        total = len(scripts)
        core = sum(
            1 for d in scripts.values() if d.get("support_tier") == "core"
        )
        readme = README.read_text(encoding="utf-8")
        expected = f"{total} connectors ({core} core)"
        assert expected in readme, (
            f"README must state the tiered count {expected!r} - the flat "
            f"connector count overpromises maintenance (W13)"
        )

    def test_docs_cover_support_tiers(self):
        etiquette = (
            PROJECT_ROOT / "docs" / "lang" / "09_ingestion_etiquette.md"
        ).read_text(encoding="utf-8")
        assert re.search(r"support[ _-]tier", etiquette, re.IGNORECASE), (
            "09_ingestion_etiquette.md must document the support-tier "
            "system"
        )
