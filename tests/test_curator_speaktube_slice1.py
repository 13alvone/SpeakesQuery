"""Curator ↔ speaktube contract - slice 1 tests.

Phase 6 / Bet 5 slice 1 (2026-05-16). Covers:

* Schema + IMMUTABLE-routing drift guards for the three new
  log_writer categories (curator_telemetry, curator_reflections,
  curator_playlist).
* The four REST endpoints (GET /api/playlist/today, GET
  /api/dignity/today, POST /api/reflections, POST /api/growth_dial)
  against the speaktube contract.
* Frozen-column snapshots so the IMMUTABLE schemas can't lose a
  column once shipped (same additivity-only rule as the OEB pick
  journal - see CLAUDE.md "Do Not" pin).
* Takeout import CLI: per-parser unit tests + end-to-end against a
  synthetic Takeout fixture.
* The /api/growth_dial endpoint round-trips through global_settings
  and the new value sticks.

The test suite is structured so any future schema change forces a
deliberate edit to the frozen snapshots - same defence-in-depth
pattern OEB Wave 2 pins for the trading record.
"""

from __future__ import annotations

import json
import time
import shutil
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


# ── Helpers / fixtures ──────────────────────────────────────────────


@pytest.fixture
def isolated_immutable(tmp_path, monkeypatch):
    """Redirect ``settings.immutable_dir()`` to a clean tmp path so
    log writes + REST reads land in an isolated tree.

    Also resets the LogWriter singleton so a stale writer doesn't
    keep the old immutable_root cached across tests.
    """
    from global_settings import get_settings
    from functionality.log_writer import LogWriter

    settings = get_settings()
    settings.set("immutable_root", str(tmp_path / "IMM"))
    LogWriter.reset_for_tests()
    yield tmp_path / "IMM"
    # Restore to default - other tests in the session expect the
    # canonical immutable_root.
    settings.reset("immutable_root")
    LogWriter.reset_for_tests()


# ── 1. Schema additivity guards ─────────────────────────────────────


_CURATOR_TELEMETRY_FROZEN_COLS = {
    "_epoch", "event_ts_iso", "event_date", "event_type",
    "video_external_id", "chosen_by", "run_date", "position",
    "slot_kind", "watched_seconds", "total_seconds", "rating",
    "reason", "kind", "content", "query", "raw_json",
}

_CURATOR_REFLECTIONS_FROZEN_COLS = {
    "_epoch", "event_ts_iso", "date", "kind", "content",
    "video_external_id", "source",
}

_CURATOR_PLAYLIST_FROZEN_COLS = {
    "_epoch", "run_date", "composed_at_iso", "growth_dial", "theme",
    "position", "slot_kind", "rationale",
    "external_id", "url", "title", "channel_name",
    "thumbnail_url", "published_at",
    "duration_seconds", "interest_score", "growth_score",
    "slop_score", "score_reasoning",
    "thin_history_active",  # slice 10 2026-05-17
}


def test_curator_telemetry_schema_is_additive_only():
    """Removing a column from curator_telemetry breaks every historical
    SPQL query touching it. ADD columns; never remove them."""
    from functionality.log_writer import SCHEMAS
    actual = set(SCHEMAS["curator_telemetry"])
    missing = _CURATOR_TELEMETRY_FROZEN_COLS - actual
    assert not missing, (
        f"curator_telemetry schema dropped frozen column(s): {sorted(missing)}. "
        "IMMUTABLE schemas are additive-only - see CLAUDE.md Do Not pin."
    )


def test_curator_reflections_schema_is_additive_only():
    from functionality.log_writer import SCHEMAS
    actual = set(SCHEMAS["curator_reflections"])
    missing = _CURATOR_REFLECTIONS_FROZEN_COLS - actual
    assert not missing, (
        f"curator_reflections schema dropped frozen column(s): {sorted(missing)}."
    )


def test_curator_playlist_schema_is_additive_only():
    from functionality.log_writer import SCHEMAS
    actual = set(SCHEMAS["curator_playlist"])
    missing = _CURATOR_PLAYLIST_FROZEN_COLS - actual
    assert not missing, (
        f"curator_playlist schema dropped frozen column(s): {sorted(missing)}."
    )


def test_all_three_curator_categories_route_to_immutable():
    """Pins the IMMUTABLE_CATEGORIES membership for each curator
    schema - the protected-from-cleanup property is what makes the
    decade-horizon viewing record actually durable."""
    from functionality.log_writer import IMMUTABLE_CATEGORIES
    assert "curator_telemetry" in IMMUTABLE_CATEGORIES
    assert "curator_reflections" in IMMUTABLE_CATEGORIES
    assert "curator_playlist" in IMMUTABLE_CATEGORIES


# ── 2. Log writer helpers route correctly ───────────────────────────


def test_log_curator_telemetry_writes_to_immutable(isolated_immutable):
    """log_curator_telemetry() must produce a parquet under
    indexes/IMMUTABLE/curator_telemetry/."""
    from functionality.log_writer import (
        log_curator_telemetry, flush_all,
    )
    log_curator_telemetry(
        event_ts_iso="2026-05-16T09:14:22-07:00",
        event_date="2026-05-16",
        event_type="play_start",
        video_external_id="dQw4w9WgXcQ",
        chosen_by="curator",
        run_date="2026-05-16",
        position=3,
        slot_kind="main",
    )
    flush_all()
    parquets = list((isolated_immutable / "curator_telemetry").rglob("*.parquet"))
    assert parquets, "log_curator_telemetry produced no parquet output"
    df = pd.read_parquet(parquets[0])
    assert "event_type" in df.columns
    assert df.iloc[0]["event_type"] == "play_start"
    assert df.iloc[0]["video_external_id"] == "dQw4w9WgXcQ"


def test_log_curator_reflection_writes_to_immutable(isolated_immutable):
    from functionality.log_writer import (
        log_curator_reflection, flush_all,
    )
    log_curator_reflection(
        event_ts_iso="2026-05-16T10:00:00-07:00",
        date="2026-05-16",
        kind="eod",
        content="Today was a slow news day.",
    )
    flush_all()
    parquets = list((isolated_immutable / "curator_reflections").rglob("*.parquet"))
    assert parquets
    df = pd.read_parquet(parquets[0])
    assert df.iloc[0]["kind"] == "eod"
    assert df.iloc[0]["content"] == "Today was a slow news day."


def test_log_curator_playlist_item_writes_to_immutable(isolated_immutable):
    from functionality.log_writer import (
        log_curator_playlist_item, flush_all,
    )
    log_curator_playlist_item(
        run_date="2026-05-16",
        composed_at_iso="2026-05-16T05:00:00Z",
        growth_dial=0.25,
        position=1,
        slot_kind="main",
        rationale="High channel affinity",
        external_id="vid123",
        url="https://www.youtube.com/watch?v=vid123",
        title="Test video",
    )
    flush_all()
    parquets = list((isolated_immutable / "curator_playlist").rglob("*.parquet"))
    assert parquets
    df = pd.read_parquet(parquets[0])
    assert df.iloc[0]["growth_dial"] == pytest.approx(0.25)


# ── 3. Global settings - curator_growth_dial validation ─────────────


def test_curator_growth_dial_default_in_range():
    """Slice 8 (2026-05-17): default flipped from 0.15 (old 0..1
    semantics) to -0.7 (new bipolar semantics, same "mostly familiar"
    intent)."""
    from global_settings import DEFAULTS
    val = DEFAULTS["curator_growth_dial"]
    assert isinstance(val, float)
    assert -1.0 <= val <= 1.0
    # Pin the new default explicitly - drift guard so a future change
    # doesn't silently flip operator behaviour.
    assert val == pytest.approx(-0.7)


def test_curator_growth_dial_validator_accepts_full_bipolar_range():
    """Slice 8 (2026-05-17): validator widened from [0.0, 1.0] to
    [-1.0, +1.0]. Speaktube's slider sends -1..+1; before slice 8 the
    backend silently rejected negatives so the slider's left half was
    a no-op."""
    from global_settings import _validate_key, DEFAULTS
    # In-range - boundaries + midpoints both sides
    assert _validate_key("curator_growth_dial", -1.0, DEFAULTS) is None
    assert _validate_key("curator_growth_dial", -0.7, DEFAULTS) is None
    assert _validate_key("curator_growth_dial", 0.0, DEFAULTS) is None
    assert _validate_key("curator_growth_dial", 0.5, DEFAULTS) is None
    assert _validate_key("curator_growth_dial", 1.0, DEFAULTS) is None
    # Out of range - past either boundary
    assert _validate_key("curator_growth_dial", -1.1, DEFAULTS) is not None
    assert _validate_key("curator_growth_dial", 1.1, DEFAULTS) is not None
    # Wrong types still rejected
    assert _validate_key("curator_growth_dial", "0.5", DEFAULTS) is not None
    assert _validate_key("curator_growth_dial", True, DEFAULTS) is not None


def test_curator_speaktube_base_url_validator_rejects_invalid_scheme():
    from global_settings import _validate_key, DEFAULTS
    assert _validate_key("curator_speaktube_base_url", "ftp://speaktube/", DEFAULTS) is not None
    assert _validate_key("curator_speaktube_base_url", "speaktube.local", DEFAULTS) is not None
    assert _validate_key("curator_speaktube_base_url", "http://localhost:8080", DEFAULTS) is None
    assert _validate_key("curator_speaktube_base_url", "https://example.com", DEFAULTS) is None
    assert _validate_key("curator_speaktube_base_url", "", DEFAULTS) is None  # disable


def test_defaults_yaml_mirrors_curator_settings():
    """Drift guard: every new in-code default must also appear in
    global_settings.defaults.yaml so a fresh install behaves the same."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "global_settings.defaults.yaml") as f:
        yaml_defaults = yaml.safe_load(f)
    assert "curator_growth_dial" in yaml_defaults
    assert "curator_speaktube_base_url" in yaml_defaults
    assert "curator_telemetry_lookback_days" in yaml_defaults
    # Slice 6 (2026-05-17): hybrid expansion target
    assert "curator_playlist_target_count" in yaml_defaults


def test_curator_playlist_target_count_default_and_validator():
    """Slice 6 (2026-05-17): target_count drives hybrid expansion in
    the dispatcher. Default 500; range [20, 5000]. Validator must
    reject out-of-range AND non-integer types."""
    from global_settings import DEFAULTS, _validate_key
    val = DEFAULTS["curator_playlist_target_count"]
    assert isinstance(val, int)
    assert 20 <= val <= 5000

    # In-range
    assert _validate_key("curator_playlist_target_count", 20, DEFAULTS) is None
    assert _validate_key("curator_playlist_target_count", 500, DEFAULTS) is None
    assert _validate_key("curator_playlist_target_count", 5000, DEFAULTS) is None

    # Out of range
    assert _validate_key("curator_playlist_target_count", 19, DEFAULTS) is not None
    assert _validate_key("curator_playlist_target_count", 5001, DEFAULTS) is not None
    assert _validate_key("curator_playlist_target_count", 0, DEFAULTS) is not None

    # Wrong types
    assert _validate_key("curator_playlist_target_count", "500", DEFAULTS) is not None
    assert _validate_key("curator_playlist_target_count", 500.5, DEFAULTS) is not None
    # bool is technically an int in Python - validator must catch it
    assert _validate_key("curator_playlist_target_count", True, DEFAULTS) is not None


def test_curator_playlist_target_count_ui_registered():
    """Slice 6 (2026-05-17): the Settings form input + JS mapping must
    both exist for the new setting (5-place drift catch - UI is the
    most-skipped layer)."""
    root = Path(__file__).resolve().parent.parent
    ui_html = (root / "desktop_app" / "ui.html").read_text()
    assert 'id="set-curator-playlist-target-count"' in ui_html, (
        "Settings form input missing for curator_playlist_target_count"
    )
    assert "'curator_playlist_target_count'" in ui_html, (
        "JS settings-map entry missing for curator_playlist_target_count"
    )


# ── 4. /api/playlist/today contract ─────────────────────────────────


def test_playlist_today_returns_404_when_no_data(isolated_immutable, client):
    """Per the contract: 404 when nothing has been composed yet, NOT
    200 + empty items. The speaktube renderer distinguishes the two."""
    resp = client.get("/api/playlist/today")
    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload["status"] == "error"
    assert "error_class" in payload


def test_playlist_today_returns_shaped_json_when_data_exists(isolated_immutable, client):
    """Pin the response shape against the speaktube contract.

    Two items with the load-bearing fields populated; the renderer
    fields are tolerated as null/empty. Order is by position ASC.
    """
    from functionality.log_writer import (
        log_curator_playlist_item, flush_all,
    )
    composed_at = "2026-05-16T05:00:00Z"
    log_curator_playlist_item(
        run_date="2026-05-16",
        composed_at_iso=composed_at,
        growth_dial=0.18,
        theme="1997_cable_surf",
        position=2,
        slot_kind="surprise",
        rationale="Exploration pick",
        external_id="vid002",
        url="https://www.youtube.com/watch?v=vid002",
        title="Second pick",
        channel_name="Channel B",
        duration_seconds=1200,
        interest_score=0.55,
        growth_score=0.80,
        slop_score=0.10,
        score_reasoning="surprise slot",
    )
    log_curator_playlist_item(
        run_date="2026-05-16",
        composed_at_iso=composed_at,
        growth_dial=0.18,
        theme="1997_cable_surf",
        position=1,
        slot_kind="main",
        rationale="Top affinity pick",
        external_id="vid001",
        url="https://www.youtube.com/watch?v=vid001",
        title="First pick",
        channel_name="Channel A",
        duration_seconds=1742,
        interest_score=0.91,
        growth_score=0.05,
        slop_score=0.02,
        score_reasoning="main slot",
    )
    flush_all()

    resp = client.get("/api/playlist/today")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["run_date"] == "2026-05-16"
    assert payload["theme"] == "1997_cable_surf"
    assert payload["growth_dial"] == pytest.approx(0.18)
    items = payload["items"]
    assert len(items) == 2
    assert items[0]["position"] == 1
    assert items[0]["slot_kind"] == "main"
    assert items[0]["video"]["external_id"] == "vid001"
    assert items[0]["video"]["url"] == "https://www.youtube.com/watch?v=vid001"
    assert items[0]["video"]["interest_score"] == pytest.approx(0.91)
    assert items[1]["position"] == 2
    assert items[1]["slot_kind"] == "surprise"


def test_playlist_today_threads_thumbnail_url_and_published_at(
    isolated_immutable, client,
):
    """Slice 4 (2026-05-17): speaktube requests ``thumbnail_url`` and
    ``published_at`` on the ``video`` object so the player can render
    card images + relative-time labels + a publication-date sort.

    Both must round-trip through the IMMUTABLE write + endpoint read.
    The empty-string case is load-bearing: when the LLM omits the
    field (or the source ingestion didn't supply one), the response
    must carry an explicit empty string. The speaktube renderer
    distinguishes empty (falls back to YouTube synthesis for
    thumbnails, curator-order for missing dates) from key-absent
    (would be a contract break - the renderer reads the field by
    name with truthiness check, not presence check).
    """
    from functionality.log_writer import log_curator_playlist_item, flush_all

    composed_at = "2026-05-17T05:00:00Z"
    # Item 1: candidate row supplied both fields → composer threaded
    # them verbatim into the LLM output → log_writer stored them.
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=composed_at,
        growth_dial=0.15, position=1, slot_kind="main",
        rationale="populated",
        external_id="vidPop",
        url="https://www.youtube.com/watch?v=vidPop",
        title="Populated", channel_name="Channel P",
        thumbnail_url="https://i.ytimg.com/vi/vidPop/hqdefault.jpg",
        published_at="2026-05-15T16:30:00+00:00",
    )
    # Item 2: composer omitted both kwargs (LLM dropped them, or
    # candidate had no thumbnail / no publication date). Helper
    # defaults to empty string. The endpoint must echo back "" - NOT
    # elide the keys.
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=composed_at,
        growth_dial=0.15, position=2, slot_kind="surprise",
        rationale="missing",
        external_id="vidMiss",
        url="https://www.youtube.com/watch?v=vidMiss",
        title="Missing thumb + date", channel_name="Channel M",
    )
    flush_all()

    resp = client.get("/api/playlist/today")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 2

    # Position 1: both fields present verbatim.
    v1 = items[0]["video"]
    assert v1["thumbnail_url"] == "https://i.ytimg.com/vi/vidPop/hqdefault.jpg"
    assert v1["published_at"] == "2026-05-15T16:30:00+00:00"

    # Position 2: keys MUST exist in the response (speaktube reads
    # them by name); values are empty strings.
    v2 = items[1]["video"]
    assert "thumbnail_url" in v2, (
        "Slice 4 contract: speaktube renderer reads "
        "video.thumbnail_url by name. Eliding the key on empty "
        "breaks the player."
    )
    assert "published_at" in v2, (
        "Slice 4 contract: speaktube renderer reads "
        "video.published_at by name."
    )
    assert v2["thumbnail_url"] == ""
    assert v2["published_at"] == ""


def test_playlist_today_returns_only_latest_composition_when_multiple_today(
    isolated_immutable, client,
):
    """When the composer fires multiple times on the same run_date
    (manual + cron, or two manual fires), `/api/playlist/today` MUST
    return only the LATEST composition - not the union of all of them.

    Caught 2026-05-17 during the slice 3 production-readiness audit:
    three back-to-back composer fires produced 13+13+14=40 items in
    the API response when speaktube expected 14. The endpoint filtered
    by `MAX(run_date)` but NOT by `MAX(composed_at_iso) WITHIN
    run_date`, so all of today's compositions stacked.

    Fix landed in the same audit commit; this test pins the regression.
    """
    from functionality.log_writer import log_curator_playlist_item, flush_all

    # First (earlier) composition: 2 items
    early_iso = "2026-05-17T03:13:08+00:00"
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=early_iso,
        growth_dial=0.15, position=1, slot_kind="main",
        rationale="early1", external_id="early1",
        url="https://www.youtube.com/watch?v=early1",
        title="Early pick 1", channel_name="Old Channel",
    )
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=early_iso,
        growth_dial=0.15, position=2, slot_kind="main",
        rationale="early2", external_id="early2",
        url="https://www.youtube.com/watch?v=early2",
        title="Early pick 2", channel_name="Old Channel",
    )
    # Later (latest) composition: 3 items - should be the only ones returned
    late_iso = "2026-05-17T07:36:15+00:00"
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=late_iso,
        growth_dial=0.20, position=1, slot_kind="main",
        rationale="latest1", external_id="latest1",
        url="https://www.youtube.com/watch?v=latest1",
        title="Latest pick 1", channel_name="New Channel",
    )
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=late_iso,
        growth_dial=0.20, position=2, slot_kind="surprise",
        rationale="latest2", external_id="latest2",
        url="https://www.youtube.com/watch?v=latest2",
        title="Latest pick 2", channel_name="New Channel",
    )
    log_curator_playlist_item(
        run_date="2026-05-17", composed_at_iso=late_iso,
        growth_dial=0.20, position=3, slot_kind="surprise",
        rationale="latest3", external_id="latest3",
        url="https://www.youtube.com/watch?v=latest3",
        title="Latest pick 3", channel_name="New Channel",
    )
    flush_all()

    resp = client.get("/api/playlist/today")
    assert resp.status_code == 200
    payload = resp.get_json()
    # Must be EXACTLY 3 items (latest composition only), not 5 (the union)
    items = payload["items"]
    assert len(items) == 3, (
        f"/api/playlist/today returned {len(items)} items but should return "
        "only the latest composition's 3 items. Earlier composition is "
        "leaking through - check the composed_at_iso filter in "
        "desktop_app/server.py::api_curator_playlist_today."
    )
    # All returned items must be from the LATER composition
    for it in items:
        assert it["video"]["channel_name"] == "New Channel", (
            f"Earlier composition leaked: got channel "
            f"{it['video']['channel_name']!r}, expected 'New Channel'"
        )
    # growth_dial reflects the LATER composition's value (0.20, not 0.15)
    assert payload["growth_dial"] == pytest.approx(0.20)


def test_playlist_today_date_param_also_filters_to_latest_composition(
    isolated_immutable, client,
):
    """Same filter rule applies when ``?date=YYYY-MM-DD`` is passed
    explicitly. Mirrors `test_playlist_today_returns_only_latest_composition_when_multiple_today`
    for the date-filtered branch.
    """
    from functionality.log_writer import log_curator_playlist_item, flush_all
    log_curator_playlist_item(
        run_date="2026-05-10", composed_at_iso="2026-05-10T05:00:00+00:00",
        growth_dial=0.05, position=1, slot_kind="main",
        rationale="first", external_id="a1",
        url="https://www.youtube.com/watch?v=a1", title="First",
        channel_name="Old",
    )
    log_curator_playlist_item(
        run_date="2026-05-10", composed_at_iso="2026-05-10T10:00:00+00:00",
        growth_dial=0.50, position=1, slot_kind="main",
        rationale="latest", external_id="a2",
        url="https://www.youtube.com/watch?v=a2", title="Latest",
        channel_name="New",
    )
    flush_all()
    resp = client.get("/api/playlist/today?date=2026-05-10")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["video"]["channel_name"] == "New"


def test_playlist_today_with_date_param_filters(isolated_immutable, client):
    from functionality.log_writer import log_curator_playlist_item, flush_all
    log_curator_playlist_item(
        run_date="2026-05-15", composed_at_iso="2026-05-15T05:00:00Z",
        growth_dial=0.10, position=1, slot_kind="main",
        rationale="yesterday", external_id="y1", url="https://www.youtube.com/watch?v=y1",
    )
    log_curator_playlist_item(
        run_date="2026-05-16", composed_at_iso="2026-05-16T05:00:00Z",
        growth_dial=0.20, position=1, slot_kind="main",
        rationale="today", external_id="t1", url="https://www.youtube.com/watch?v=t1",
    )
    flush_all()

    resp = client.get("/api/playlist/today?date=2026-05-15")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["video"]["external_id"] == "y1"


def test_playlist_today_rejects_malformed_date(isolated_immutable, client):
    resp = client.get("/api/playlist/today?date=not-a-date")
    assert resp.status_code == 400


# ── 5. /api/dignity/today contract ──────────────────────────────────


def test_dignity_today_returns_null_when_no_telemetry(isolated_immutable, client):
    """Per the contract: ``dignity_pct: null`` when no plays yet -
    speaktube renders this as "offline" rather than "0%"."""
    resp = client.get("/api/dignity/today")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["dignity_pct"] is None
    assert payload["total_plays"] == 0
    assert payload["chosen_plays"] == 0


def test_dignity_today_computes_ratio_correctly(isolated_immutable, client):
    """3 of 4 plays curator/user-chosen → 75.0%. One play is a
    recommendation (passive); shouldn't count as chosen."""
    import datetime as _dt
    from functionality.log_writer import (
        log_curator_telemetry, flush_all,
    )
    today_iso = _dt.date.today().isoformat()
    events = [
        ("play_start", "curator"),
        ("play_start", "user_manual"),
        ("play_end", "curator"),
        ("play_start", "recommendation"),
    ]
    for et, chosen in events:
        log_curator_telemetry(
            event_ts_iso=f"{today_iso}T09:00:00-07:00",
            event_date=today_iso,
            event_type=et,
            video_external_id=f"vid_{chosen}",
            chosen_by=chosen,
        )
    flush_all()

    resp = client.get("/api/dignity/today")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total_plays"] == 4
    assert payload["chosen_plays"] == 3
    assert payload["dignity_pct"] == pytest.approx(75.0)


def test_dignity_today_filters_to_date(isolated_immutable, client):
    """Plays from other dates must not count toward today's score."""
    from functionality.log_writer import log_curator_telemetry, flush_all
    log_curator_telemetry(
        event_ts_iso="2026-05-15T09:00:00-07:00",
        event_date="2026-05-15",
        event_type="play_start",
        chosen_by="curator",
    )
    log_curator_telemetry(
        event_ts_iso="2026-05-16T09:00:00-07:00",
        event_date="2026-05-16",
        event_type="play_start",
        chosen_by="recommendation",
    )
    flush_all()

    resp = client.get("/api/dignity/today?date=2026-05-15")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total_plays"] == 1
    assert payload["chosen_plays"] == 1
    assert payload["dignity_pct"] == pytest.approx(100.0)


# ── 6. /api/reflections contract ────────────────────────────────────


def test_post_reflections_creates_row(isolated_immutable, client):
    resp = client.post(
        "/api/reflections",
        json={
            "date": "2026-05-16",
            "kind": "eod",
            "content": "Today I noticed I kept skipping fast-cut compilations.",
        },
    )
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert "id" in payload

    parquets = list((isolated_immutable / "curator_reflections").rglob("*.parquet"))
    assert parquets
    df = pd.read_parquet(parquets[0])
    assert len(df) == 1
    assert df.iloc[0]["kind"] == "eod"
    assert df.iloc[0]["source"] == "api_post"


def test_post_reflections_per_video_requires_external_id(isolated_immutable, client):
    resp = client.post(
        "/api/reflections",
        json={
            "date": "2026-05-16",
            "kind": "per_video",
            "content": "Beautiful pacing.",
        },
    )
    assert resp.status_code == 400


def test_post_reflections_rejects_bad_date_format(isolated_immutable, client):
    resp = client.post(
        "/api/reflections",
        json={"date": "May 16 2026", "kind": "eod", "content": "x"},
    )
    assert resp.status_code == 400


def test_post_reflections_rejects_unknown_kind(isolated_immutable, client):
    resp = client.post(
        "/api/reflections",
        json={"date": "2026-05-16", "kind": "haiku", "content": "x"},
    )
    assert resp.status_code == 400


def test_post_reflections_rejects_empty_content(isolated_immutable, client):
    resp = client.post(
        "/api/reflections",
        json={"date": "2026-05-16", "kind": "eod", "content": "   "},
    )
    assert resp.status_code == 400


# ── 7. /api/growth_dial contract ────────────────────────────────────


def test_post_growth_dial_updates_setting(client):
    from global_settings import get_settings
    settings = get_settings()
    settings.reset("curator_growth_dial")
    resp = client.post(
        "/api/growth_dial",
        json={"value": 0.42, "set_at": "2026-05-16T09:00:00-07:00"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["curator_growth_dial"] == pytest.approx(0.42)
    assert float(settings.get("curator_growth_dial")) == pytest.approx(0.42)
    settings.reset("curator_growth_dial")


def test_post_growth_dial_rejects_out_of_range(client):
    """Slice 8 (2026-05-17): out-of-range is now anything outside
    [-1.0, +1.0]. -0.1 used to be rejected (old [0.0, 1.0]) but is
    now valid; 1.5 still out of range; -1.5 now rejected on the low
    side."""
    resp = client.post("/api/growth_dial", json={"value": 1.5})
    assert resp.status_code == 400
    resp = client.post("/api/growth_dial", json={"value": -1.5})
    assert resp.status_code == 400


def test_post_growth_dial_accepts_bipolar_range(client):
    """Slice 8 (2026-05-17): speaktube's slider sends -1..+1; the
    endpoint accepts the full range. Round-trips through global_settings."""
    from global_settings import get_settings
    settings = get_settings()
    settings.reset("curator_growth_dial")
    # Negative (familiarity bias) - would have been rejected pre-slice 8
    resp = client.post("/api/growth_dial", json={"value": -0.42})
    assert resp.status_code == 200
    assert resp.get_json()["curator_growth_dial"] == pytest.approx(-0.42)
    assert float(settings.get("curator_growth_dial")) == pytest.approx(-0.42)
    # Boundary low
    resp = client.post("/api/growth_dial", json={"value": -1.0})
    assert resp.status_code == 200
    # Boundary high
    resp = client.post("/api/growth_dial", json={"value": 1.0})
    assert resp.status_code == 200
    settings.reset("curator_growth_dial")


def test_post_growth_dial_rejects_non_numeric(client):
    resp = client.post("/api/growth_dial", json={"value": "high"})
    assert resp.status_code == 400


def test_post_growth_dial_rejects_bool(client):
    # True is technically a Python int subclass - must reject explicitly
    # so a JS truthy doesn't silently coerce to 1.0.
    resp = client.post("/api/growth_dial", json={"value": True})
    assert resp.status_code == 400


# ── 8. Allowlist drift guard ────────────────────────────────────────


def test_speaktube_host_is_allowlisted_for_ingestion():
    """The curator telemetry ingestion script HTTP-fetches the
    speaktube sidecar (default: same machine, localhost); without the
    host on the allowlist the sandboxed script can't reach the player.
    Pinned so a future allowlist cleanup doesn't silently lock the
    curator out."""
    from global_settings import DEFAULTS
    patterns = DEFAULTS["allowed_api_domains"]
    assert any(
        "localhost" in p for p in patterns
    ), "speaktube default host (localhost) missing from allowed_api_domains"


# ── 9. Takeout import - per-parser unit tests ───────────────────────


def _fake_takeout(tmp_path: Path) -> Path:
    """Build a minimal Google Takeout YouTube/ tree on disk."""
    root = tmp_path / "youtube_profile"
    (root / "subscriptions").mkdir(parents=True)
    (root / "playlists").mkdir(parents=True)
    (root / "history").mkdir(parents=True)

    (root / "subscriptions" / "subscriptions.csv").write_text(
        "Channel Id,Channel Url,Channel Title\n"
        "UC--70ql_IxJmhmqXqrkJrWQ,http://www.youtube.com/channel/UC--70ql_IxJmhmqXqrkJrWQ,dakota of earth\n"
        "UC--DwaiMV-jtO-6EvmKOnqg,http://www.youtube.com/channel/UC--DwaiMV-jtO-6EvmKOnqg,OALabs\n"
    )

    (root / "playlists" / "playlists.csv").write_text(
        "Playlist ID,Playlist Title (Original),Playlist Visibility,"
        "Playlist Video Order,Playlist Create Timestamp,Playlist Update Timestamp\n"
        "PLaLuibmB2o8M5MtpL0XmFD29OvpCoNUsw,Halarious,Private,Manual,"
        "2020-12-06T22:09:42+00:00,2026-05-04T07:38:27+00:00\n"
    )
    (root / "playlists" / "Watch later-videos.csv").write_text(
        "Video ID,Playlist Video Creation Timestamp\n"
        "mJ4_cHRe4jI,2018-10-16T01:24:18+00:00\n"
        "tQd_5as_cMY,2019-01-09T12:11:46+00:00\n"
    )

    (root / "history" / "watch-history.html").write_text(
        '<html><body>'
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
        '<div class="mdl-grid">'
        '<div class="content-cell mdl-cell mdl-cell--6-col">'
        'Watched\xa0<a href="https://www.youtube.com/watch?v=1ekRPHTcpcE">'
        'The Worst Humans of All Time Iceberg</a><br>'
        '<a href="https://www.youtube.com/channel/UC65IEk6PTfRZzyvrcYypHQg">'
        'Parallel Pipes</a><br>'
        'May 13, 2026, 8:28:07 PM PDT<br></div>'
        '</div></div></div>'
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
        '<div class="mdl-grid">'
        '<div class="content-cell mdl-cell mdl-cell--6-col">'
        'Watched\xa0<a href="https://www.youtube.com/watch?v=abc123XYZ45">'
        'Untitled deleted video</a><br>'
        'Jan 1, 2024, 12:00:00 AM UTC<br></div>'
        '</div></div></div>'
        '</body></html>',
        encoding="utf-8",
    )
    return root


def test_takeout_subscriptions_parser(tmp_path):
    from tools.curator_takeout_import import _parse_subscriptions
    root = _fake_takeout(tmp_path)
    rows = _parse_subscriptions(root / "subscriptions" / "subscriptions.csv")
    assert len(rows) == 2
    assert rows[0]["channel_id"] == "UC--70ql_IxJmhmqXqrkJrWQ"
    assert rows[0]["channel_title"] == "dakota of earth"


def test_takeout_playlist_videos_parser(tmp_path):
    from tools.curator_takeout_import import _parse_playlist_videos
    root = _fake_takeout(tmp_path)
    rows = _parse_playlist_videos(root / "playlists")
    assert len(rows) == 2
    assert all(r["playlist_name"] == "Watch later" for r in rows)
    assert rows[0]["video_id"] == "mJ4_cHRe4jI"
    assert rows[0]["video_url"] == "https://www.youtube.com/watch?v=mJ4_cHRe4jI"


def test_takeout_watch_history_parser_with_known_tz(tmp_path):
    from tools.curator_takeout_import import _parse_watch_history
    root = _fake_takeout(tmp_path)
    rows = _parse_watch_history(root / "history" / "watch-history.html")
    assert len(rows) == 2
    first = rows[0]
    assert first["video_id"] == "1ekRPHTcpcE"
    assert first["channel_name"] == "Parallel Pipes"
    # PDT = UTC-7. May 13 8:28:07 PM PDT → May 14 03:28:07 UTC.
    import datetime as _dt
    expected_utc = _dt.datetime(2026, 5, 14, 3, 28, 7, tzinfo=_dt.timezone.utc)
    assert first["_epoch"] == int(expected_utc.timestamp())
    assert first["tz_abbrev"] == "PDT"

    # Second entry has no channel - must not crash, just empty channel fields.
    second = rows[1]
    assert second["video_id"] == "abc123XYZ45"
    assert second["channel_name"] == ""
    assert second["tz_abbrev"] == "UTC"


def test_takeout_history_parser_handles_unknown_tz_gracefully(tmp_path):
    """Unknown TZ abbrev → row still lands with epoch=now (fallback)
    rather than crashing the whole parse."""
    from tools.curator_takeout_import import _parse_watch_history
    history = tmp_path / "history.html"
    history.write_text(
        '<div class="outer-cell">'
        '<div class="content-cell">'
        'Watched\xa0<a href="https://www.youtube.com/watch?v=xyz98765432">'
        'Test</a><br>'
        '<a href="https://www.youtube.com/channel/UC12345678901234567890ab">'
        'TestChannel</a><br>'
        'Mar 15, 2024, 10:00:00 AM XYZ<br></div>'
        '</div></div></div>',
        encoding="utf-8",
    )
    rows = _parse_watch_history(history)
    assert len(rows) == 1
    # Epoch should still be set (fallback to now) but TZ abbrev captured for forensics.
    assert rows[0]["_epoch"] > 0
    assert rows[0]["tz_abbrev"] == "XYZ"


# ── 10. Takeout import - end-to-end ─────────────────────────────────


def test_takeout_import_end_to_end(tmp_path):
    """Run the full CLI driver against a synthetic Takeout tree and
    verify all four parquets land + have the right row counts."""
    from tools.curator_takeout_import import import_takeout
    root = _fake_takeout(tmp_path)
    out = tmp_path / "out"
    report = import_takeout(root, out)
    assert report.subscriptions_rows == 2
    assert report.playlists_metadata_rows == 1
    assert report.playlist_videos_rows == 2
    assert report.watch_history_rows == 2
    assert not report.failed
    # All four parquets exist
    assert any((out / "subscriptions").rglob("*.parquet"))
    assert any((out / "playlists_metadata").rglob("*.parquet"))
    assert any((out / "playlist_videos").rglob("*.parquet"))
    assert any((out / "watch_history").rglob("*.parquet"))


def test_takeout_import_skips_missing_files_gracefully(tmp_path):
    """Missing Takeout artifacts → entry in report.skipped, NOT a failure.
    Lets a user import a partial export without flailing."""
    from tools.curator_takeout_import import import_takeout
    root = tmp_path / "youtube_profile"
    root.mkdir()
    # No subscriptions/ or playlists/ at all
    out = tmp_path / "out"
    report = import_takeout(root, out)
    assert report.subscriptions_rows == 0
    assert any("subscriptions" in s for s in report.skipped)


def test_takeout_import_returns_1_on_completely_empty(tmp_path):
    """If --root points at a real dir with no Takeout files, exit 1
    so a CI invocation surfaces the mistake."""
    from tools.curator_takeout_import import main
    empty = tmp_path / "empty"
    empty.mkdir()
    exit_code = main([
        "--root", str(empty),
        "--out", str(tmp_path / "out"),
    ])
    assert exit_code == 1


# ── 11. Telemetry pull ingestion script - structure ─────────────────


def test_telemetry_pull_script_is_valid_sandboxed_json():
    """Schema sanity: required fields present, trust_level sandboxed,
    suggested_subdirectory points at IMMUTABLE/curator_telemetry."""
    root = Path(__file__).resolve().parent.parent
    path = root / "script_library" / "scripts" / "curator_telemetry_pull.json"
    assert path.exists(), "curator_telemetry_pull.json missing"
    with open(path) as f:
        spec = json.load(f)
    for key in ("title", "description", "category", "api_url",
                "suggested_cron", "suggested_subdirectory", "trust_level", "code"):
        assert key in spec, f"missing key: {key}"
    assert spec["trust_level"] == "sandboxed"
    assert spec["suggested_subdirectory"] == "IMMUTABLE/curator_telemetry"
    # Code does NOT import from functionality.log_writer (sandboxed
    # imports allowlist would reject it). Pin so a future refactor
    # doesn't silently regress trust tier.
    assert "from functionality" not in spec["code"]
    assert "import functionality" not in spec["code"]


# ── 12. Money-leak canary (config-leak generalisation) ──────────────


def test_get_playlist_today_never_mutates_alert_group_state(isolated_immutable, client):
    """The endpoint reads from the IMMUTABLE pick journal - it must
    never call AG mutation methods. Mirrors the Phase 3 slice 9
    config-leak canary pattern."""
    from alert_group_store import AlertGroupStore
    with patch.object(
        AlertGroupStore, "save_group",
        side_effect=AssertionError("CONFIG LEAK: GET /api/playlist/today wrote to AG"),
    ):
        with patch.object(
            AlertGroupStore, "update_group",
            side_effect=AssertionError("CONFIG LEAK: GET /api/playlist/today wrote to AG"),
        ):
            # Whether 404 or 200, this must NEVER touch save_group/update_group.
            resp = client.get("/api/playlist/today")
            assert resp.status_code in (200, 404)


def test_get_dignity_today_never_mutates_alert_group_state(isolated_immutable, client):
    from alert_group_store import AlertGroupStore
    with patch.object(
        AlertGroupStore, "save_group",
        side_effect=AssertionError("CONFIG LEAK: GET /api/dignity/today wrote to AG"),
    ):
        with patch.object(
            AlertGroupStore, "update_group",
            side_effect=AssertionError("CONFIG LEAK: GET /api/dignity/today wrote to AG"),
        ):
            resp = client.get("/api/dignity/today")
            assert resp.status_code == 200
