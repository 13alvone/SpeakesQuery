"""
Drift guard - `<thing>/<x>.yaml` (live mirror) must match
`default_<thing>/<x>.yaml` (template) on the `query` field.

Other fields (`cron_schedule`, `timezone`, `lookback`, `email_address`,
`disabled`) are intentionally user-customizable - operators tune them
via the UI/API without re-seeding from defaults. The `query` field is
the part of an SS/AG that encodes the analyst-side intent and should
stay synced between the template and the live mirror.

Caught 2026-05-06 audit while planning a broader drift-guard story:
out of 47 default-vs-live differences across SSes + AGs, only one
was a real query-level divergence (spbeb_kalshi_sports). The rest
were cron customizations (35) and timezone explicit-vs-fallback
cosmetic differences (11) - both intentional and not worth pinning.

If a future fix updates `default_saved_searches/<x>.yaml::query` but
forgets to mirror to `saved_searches/<x>.yaml::query` (or vice versa),
this guard fails loud and tells the engineer to sync them.

Companion to the existing drift guards:
- `reference_local_yaml_vs_live_drift_footgun.md` - Mode A/B drift
  between local YAML and live deployment via API (requires running
  instance, not test-environment runnable)
- `tests/test_default_saved_searches_parse.py` - SPQL syntax + bug
  patterns
- `tests/test_cron_compat.py::test_no_numeric_dow_in_any_user_mutable_cron_field`
  - named-day-of-week enforcement
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _enumerate_synced_yamls():
    """For each (default_<thing>, <thing>) pair, yield the YAMLs that
    exist in BOTH directories. We can't pin drift on YAMLs that only
    exist in one place - those represent either a brand-new template
    not yet seeded or a legacy live-only customization."""
    pairs = [
        (PROJECT_ROOT / "default_saved_searches", PROJECT_ROOT / "saved_searches"),
        (PROJECT_ROOT / "default_alert_groups", PROJECT_ROOT / "alert_groups"),
    ]
    out = []
    for default_dir, live_dir in pairs:
        if not default_dir.is_dir() or not live_dir.is_dir():
            continue
        for default_file in sorted(default_dir.glob("*.yaml")):
            live_file = live_dir / default_file.name
            if live_file.exists():
                kind = "ss" if "saved_searches" in str(default_dir) else "ag"
                out.append((kind, default_file.stem, default_file, live_file))
    return out


_SYNCED_YAMLS = _enumerate_synced_yamls()


def _normalize_query(q):
    """Normalise a query string for diffing - strip leading/trailing
    whitespace + collapse internal blank lines so cosmetic whitespace
    drift doesn't fail the guard."""
    if q is None:
        return ""
    if not isinstance(q, str):
        return str(q)
    lines = [line.rstrip() for line in q.strip().split("\n")]
    return "\n".join(line for line in lines if line)


@pytest.mark.parametrize(
    "kind,name,default_file,live_file",
    _SYNCED_YAMLS,
    ids=[f"{kind}/{name}" for kind, name, _, _ in _SYNCED_YAMLS],
)
def test_query_field_matches_between_default_and_live(kind, name, default_file, live_file):
    """The `query` field is the analyst-side intent of a saved search.
    For paired YAMLs (default template + live mirror), it must stay
    synced. Cron schedules, timezone, and other operational knobs are
    intentionally user-customizable and excluded from this guard."""
    if kind != "ss":
        # Alert groups have a `prompt_text` field instead of `query`.
        # Skip - covered by a separate test below.
        pytest.skip("AG comparison handled separately")

    default_spec = yaml.safe_load(default_file.read_text()) or {}
    live_spec = yaml.safe_load(live_file.read_text()) or {}
    default_q = _normalize_query(default_spec.get("query"))
    live_q = _normalize_query(live_spec.get("query"))

    assert default_q == live_q, (
        f"`query` field drift between {default_file.name} (default) and "
        f"{live_file.name} (live mirror). One of them was edited without "
        f"mirroring to the other. Sync the two - typically the LIVE form "
        f"is the most-recent intent (since it's PUT to the API after "
        f"iteration). If the LIVE form is correct, copy it into "
        f"`{default_file.relative_to(PROJECT_ROOT)}`. If the DEFAULT "
        f"form is correct, copy it into `{live_file.relative_to(PROJECT_ROOT)}` "
        f"and PUT to live via the API.\n\n"
        f"=== DEFAULT ({default_file.name}) ===\n{default_q}\n\n"
        f"=== LIVE ({live_file.name}) ===\n{live_q}"
    )


@pytest.mark.parametrize(
    "kind,name,default_file,live_file",
    _SYNCED_YAMLS,
    ids=[f"{kind}/{name}" for kind, name, _, _ in _SYNCED_YAMLS],
)
def test_prompt_text_matches_between_default_and_live(kind, name, default_file, live_file):
    """For alert groups: the `prompt_text` field is the Claude-facing
    instruction. Same reasoning as `query` for SSes - must stay synced
    between the default template and the live mirror."""
    if kind != "ag":
        pytest.skip("SS comparison handled separately")

    default_spec = yaml.safe_load(default_file.read_text()) or {}
    live_spec = yaml.safe_load(live_file.read_text()) or {}
    default_p = _normalize_query(default_spec.get("prompt_text"))
    live_p = _normalize_query(live_spec.get("prompt_text"))

    assert default_p == live_p, (
        f"`prompt_text` field drift between {default_file.name} (default) "
        f"and {live_file.name} (live mirror). Sync the two.\n\n"
        f"=== DEFAULT ({default_file.name}) (first 500 chars) ===\n"
        f"{default_p[:500]}\n\n"
        f"=== LIVE ({live_file.name}) (first 500 chars) ===\n"
        f"{live_p[:500]}"
    )


def test_search_names_match_for_alert_groups():
    """For each AG, the `search_names` list (which feeders the AG
    dispatches) must match between default + live. Adding/removing
    feeders is a structural change that should be reflected in the
    template so new installs get the same set."""
    drift = []
    for kind, name, default_file, live_file in _SYNCED_YAMLS:
        if kind != "ag":
            continue
        default_spec = yaml.safe_load(default_file.read_text()) or {}
        live_spec = yaml.safe_load(live_file.read_text()) or {}
        default_names = list(default_spec.get("search_names") or [])
        live_names = list(live_spec.get("search_names") or [])
        if default_names != live_names:
            only_in_default = set(default_names) - set(live_names)
            only_in_live = set(live_names) - set(default_names)
            drift.append({
                "name": name,
                "only_in_default": sorted(only_in_default),
                "only_in_live": sorted(only_in_live),
            })

    assert not drift, (
        f"`search_names` drift between default + live alert groups:\n"
        + "\n".join(
            f"  {d['name']}:\n"
            f"    only_in_default: {d['only_in_default']}\n"
            f"    only_in_live:    {d['only_in_live']}"
            for d in drift
        )
        + "\n\nSync the feeder list between the template and live mirror."
    )
