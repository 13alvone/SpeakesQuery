"""Curator keyword preferences endpoint - slice 11 tests.

Phase 6 / Bet 5 slice 11 (2026-05-17, speaktube req #10). Covers:

1. **Settings** - 3 new settings registered in all 5 places
2. **IMMUTABLE schema** - curator_keyword_prefs additive-only
3. **POST /api/preferences/keywords** - case-insensitive dedup +
   accumulation across requests
4. **GET /api/preferences/keywords** - active-pool query (since
   last composition / fallback window)
5. **read_active_curator_keyword_pool helper** - pure-function path
6. **Dispatcher hook** - `_maybe_apply_keyword_boost` boosts
   interest_score on title matches; runtime keyword pool flows to
   `$KEYWORD_POOL` prompt substitution
7. **Composer prompt** - $KEYWORD_POOL placeholder present
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_immutable(tmp_path, monkeypatch):
    """Redirect immutable_dir() to a temp path + reset log writer."""
    from global_settings import get_settings
    from functionality.log_writer import LogWriter
    s = get_settings()
    s.set("immutable_root", str(tmp_path / "IMM"))
    LogWriter.reset_for_tests()
    yield tmp_path / "IMM"
    s.reset("immutable_root")
    LogWriter.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_keyword_settings():
    """Reset slice-11 settings between tests."""
    from global_settings import get_settings
    s = get_settings()
    yield
    for key in (
        "curator_keyword_boost_enabled",
        "curator_keyword_boost_amount",
        "curator_keyword_pool_fallback_seconds",
    ):
        try:
            s.reset(key)
        except Exception:
            pass


# ── 1. Settings registration + 5-place drift ────────────────────────


def test_keyword_pref_settings_defaults():
    from global_settings import DEFAULTS
    assert DEFAULTS["curator_keyword_boost_enabled"] is True
    assert DEFAULTS["curator_keyword_boost_amount"] == pytest.approx(0.2)
    assert DEFAULTS["curator_keyword_pool_fallback_seconds"] == 86400


def test_keyword_pref_validators_enforce_ranges():
    from global_settings import _validate_key, DEFAULTS

    # enabled (bool)
    assert _validate_key("curator_keyword_boost_enabled", True, DEFAULTS) is None
    assert _validate_key("curator_keyword_boost_enabled", False, DEFAULTS) is None
    assert _validate_key("curator_keyword_boost_enabled", "true", DEFAULTS) is not None

    # boost_amount (float 0.0-1.0)
    assert _validate_key("curator_keyword_boost_amount", 0.0, DEFAULTS) is None
    assert _validate_key("curator_keyword_boost_amount", 0.5, DEFAULTS) is None
    assert _validate_key("curator_keyword_boost_amount", 1.0, DEFAULTS) is None
    assert _validate_key("curator_keyword_boost_amount", -0.1, DEFAULTS) is not None
    assert _validate_key("curator_keyword_boost_amount", 1.1, DEFAULTS) is not None
    assert _validate_key("curator_keyword_boost_amount", True, DEFAULTS) is not None

    # pool_fallback_seconds (int 3600-604800)
    assert _validate_key("curator_keyword_pool_fallback_seconds", 3600, DEFAULTS) is None
    assert _validate_key("curator_keyword_pool_fallback_seconds", 86400, DEFAULTS) is None
    assert _validate_key("curator_keyword_pool_fallback_seconds", 604800, DEFAULTS) is None
    assert _validate_key("curator_keyword_pool_fallback_seconds", 3599, DEFAULTS) is not None
    assert _validate_key("curator_keyword_pool_fallback_seconds", 604801, DEFAULTS) is not None


def test_keyword_pref_defaults_yaml_mirrors_in_code():
    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "global_settings.defaults.yaml") as f:
        yaml_defaults = yaml.safe_load(f)
    assert "curator_keyword_boost_enabled" in yaml_defaults
    assert "curator_keyword_boost_amount" in yaml_defaults
    assert "curator_keyword_pool_fallback_seconds" in yaml_defaults


def test_keyword_pref_ui_registered():
    root = Path(__file__).resolve().parent.parent
    ui = (root / "desktop_app" / "ui.html").read_text()
    assert 'id="set-curator-keyword-boost-enabled"' in ui
    assert 'id="set-curator-keyword-boost-amount"' in ui
    assert 'id="set-curator-keyword-pool-fallback-seconds"' in ui
    assert "'curator_keyword_boost_enabled'" in ui
    assert "'curator_keyword_boost_amount'" in ui
    assert "'curator_keyword_pool_fallback_seconds'" in ui


# ── 2. IMMUTABLE schema + log helper ────────────────────────────────


def test_curator_keyword_prefs_schema_is_additive_only():
    """Frozen-column snapshot. Once shipped, removing or renaming
    a column breaks every historical SPQL query touching it."""
    from functionality.log_writer import SCHEMAS
    frozen = {
        "_epoch", "event_ts_iso", "keyword", "source", "raw_request",
    }
    actual = set(SCHEMAS["curator_keyword_prefs"])
    missing = frozen - actual
    assert not missing, (
        f"curator_keyword_prefs schema dropped frozen column(s): "
        f"{sorted(missing)}. IMMUTABLE schemas are additive-only - "
        f"see CLAUDE.md Do Not pin."
    )


def test_curator_keyword_prefs_routes_to_immutable():
    from functionality.log_writer import IMMUTABLE_CATEGORIES
    assert "curator_keyword_prefs" in IMMUTABLE_CATEGORIES


def test_log_curator_keyword_pref_writes_to_immutable(isolated_immutable):
    from functionality.log_writer import (
        log_curator_keyword_pref, flush_all,
    )
    log_curator_keyword_pref(
        event_ts_iso="2026-05-17T09:00:00+00:00",
        keyword="rare earth magnets",
        source="api_post",
        raw_request='{"keywords": ["rare earth magnets"]}',
    )
    flush_all()
    parquets = list((isolated_immutable / "curator_keyword_prefs").rglob("*.parquet"))
    assert parquets, "no curator_keyword_prefs parquet written"
    df = pd.read_parquet(parquets[0])
    assert "keyword" in df.columns
    assert df.iloc[0]["keyword"] == "rare earth magnets"
    assert df.iloc[0]["source"] == "api_post"


# ── 3. read_active_curator_keyword_pool helper ──────────────────────


def test_active_pool_empty_when_no_storage(isolated_immutable):
    from functionality.log_writer import read_active_curator_keyword_pool
    assert read_active_curator_keyword_pool(fallback_seconds=86400) == []


def test_active_pool_returns_recent_keywords(isolated_immutable):
    from functionality.log_writer import (
        log_curator_keyword_pref, read_active_curator_keyword_pool, flush_all,
    )
    log_curator_keyword_pref(
        event_ts_iso="2026-05-17T09:00:00+00:00",
        keyword="Joinery",
        source="api_post",
    )
    log_curator_keyword_pref(
        event_ts_iso="2026-05-17T09:01:00+00:00",
        keyword="public-domain noir",
        source="api_post",
    )
    flush_all()
    pool = read_active_curator_keyword_pool(fallback_seconds=86400)
    assert set(pool) == {"Joinery", "public-domain noir"}


def test_active_pool_dedups_case_insensitively_keep_first(isolated_immutable):
    """Two POSTs of the same case-insensitively-equal keyword collapse
    to one entry - the FIRST-posted casing wins per the speaktube spec
    (\"Joinery\" and \"joinery\" collapse to one)."""
    from functionality.log_writer import (
        log_curator_keyword_pref, read_active_curator_keyword_pool, flush_all,
    )
    log_curator_keyword_pref(
        event_ts_iso="2026-05-17T09:00:00+00:00",
        keyword="Joinery",
        source="api_post",
    )
    # Slight epoch bump so DuckDB FIRST() sees a clear ordering
    time.sleep(0.01)
    log_curator_keyword_pref(
        event_ts_iso="2026-05-17T09:01:00+00:00",
        keyword="joinery",
        source="api_post",
    )
    flush_all()
    pool = read_active_curator_keyword_pool(fallback_seconds=86400)
    assert len(pool) == 1
    # First-posted casing wins
    assert pool[0] == "Joinery"


def test_active_pool_excludes_keywords_older_than_fallback_window(isolated_immutable):
    """When no curator_playlist composition exists yet, the active pool
    is bounded by the fallback window. Keywords older than that are
    excluded."""
    from functionality.log_writer import (
        emit, read_active_curator_keyword_pool, flush_all,
    )
    # Old keyword: 2 days ago
    emit("curator_keyword_prefs", {
        "_epoch": int(time.time()) - (2 * 86400),
        "event_ts_iso": "2026-05-15T09:00:00+00:00",
        "keyword": "stale_keyword",
        "source": "api_post",
        "raw_request": "{}",
    })
    # Recent keyword: now
    emit("curator_keyword_prefs", {
        "_epoch": int(time.time()),
        "event_ts_iso": "2026-05-17T09:00:00+00:00",
        "keyword": "fresh_keyword",
        "source": "api_post",
        "raw_request": "{}",
    })
    flush_all()
    # 24h fallback - only fresh_keyword qualifies
    pool = read_active_curator_keyword_pool(fallback_seconds=86400)
    assert pool == ["fresh_keyword"]


def test_active_pool_resets_after_composition(isolated_immutable):
    """Keywords POSTed BEFORE the most-recent composer fire don't
    appear in the active pool - they've been \"consumed\" by the fire.
    Keywords POSTed AFTER do appear."""
    from functionality.log_writer import (
        log_curator_keyword_pref, log_curator_playlist_item,
        read_active_curator_keyword_pool, emit, flush_all,
    )
    # Pre-composition keyword (5 minutes ago)
    emit("curator_keyword_prefs", {
        "_epoch": int(time.time()) - 300,
        "event_ts_iso": "2026-05-17T08:55:00+00:00",
        "keyword": "before_compose",
        "source": "api_post",
        "raw_request": "{}",
    })
    flush_all()
    # Composition (4 minutes ago)
    log_curator_playlist_item(
        run_date="2026-05-17",
        composed_at_iso="2026-05-17T08:56:00+00:00",
        growth_dial=-0.2,
        position=1, slot_kind="main", rationale="r",
        external_id="v1", url="u", title="t", channel_name="c",
    )
    flush_all()
    # Force composition's _epoch to be 1+ second older than the
    # subsequent POST so DuckDB's `_epoch > cutoff` filter accepts it.
    # In real use the composer takes minutes between fire and the
    # operator's next POST, so this is realistic.
    time.sleep(1.1)
    # Post-composition keyword (now)
    log_curator_keyword_pref(
        event_ts_iso="2026-05-17T09:00:00+00:00",
        keyword="after_compose",
        source="api_post",
    )
    flush_all()
    pool = read_active_curator_keyword_pool(fallback_seconds=86400)
    # Only the after-composition keyword qualifies
    assert pool == ["after_compose"]


# ── 4. POST /api/preferences/keywords ───────────────────────────────


def test_post_keywords_writes_to_storage(isolated_immutable, client):
    resp = client.post(
        "/api/preferences/keywords",
        json={"keywords": ["rare earth magnets", "public-domain noir"]},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["added"] == 2
    assert payload["skipped"] == 0
    assert payload["pool_size"] == 2


def test_post_keywords_dedupes_against_active_pool(isolated_immutable, client):
    """A second POST of the same keyword (case-insensitively equal)
    is skipped - not added again, not 400'd."""
    client.post(
        "/api/preferences/keywords",
        json={"keywords": ["Joinery"]},
    )
    resp = client.post(
        "/api/preferences/keywords",
        json={"keywords": ["joinery", "rare earth magnets"]},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["added"] == 1  # only "rare earth magnets"
    assert payload["skipped"] == 1  # "joinery" deduped
    assert payload["pool_size"] == 2


def test_post_keywords_dedupes_within_single_request(isolated_immutable, client):
    """A single POST with [\"A\", \"a\"] writes only one row."""
    resp = client.post(
        "/api/preferences/keywords",
        json={"keywords": ["Joinery", "joinery", "JOINERY"]},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["added"] == 1
    assert payload["pool_size"] == 1


def test_post_keywords_rejects_non_list_body(isolated_immutable, client):
    resp = client.post(
        "/api/preferences/keywords",
        json={"keywords": "rare earth magnets"},  # string, not list
    )
    assert resp.status_code == 400


def test_post_keywords_rejects_empty_list(isolated_immutable, client):
    resp = client.post(
        "/api/preferences/keywords",
        json={"keywords": []},
    )
    assert resp.status_code == 400


def test_post_keywords_skips_empty_and_non_string_entries(isolated_immutable, client):
    """Whitespace-only / non-string entries are skipped silently
    (per the speaktube spec: \"split on commas, trim whitespace, drop
    empties\")."""
    resp = client.post(
        "/api/preferences/keywords",
        json={"keywords": ["valid_kw", "   ", "", 42, None, "another_valid"]},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["added"] == 2  # valid_kw + another_valid


# ── 5. GET /api/preferences/keywords ────────────────────────────────


def test_get_keywords_empty_pool(isolated_immutable, client):
    resp = client.get("/api/preferences/keywords")
    assert resp.status_code == 200
    assert resp.get_json() == {"keywords": []}


def test_get_keywords_returns_active_pool(isolated_immutable, client):
    client.post(
        "/api/preferences/keywords",
        json={"keywords": ["alpha", "beta"]},
    )
    resp = client.get("/api/preferences/keywords")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload["keywords"]) == {"alpha", "beta"}


# ── 6. Dispatcher hook (boost) ──────────────────────────────────────


class TestKeywordBoostHook:
    """The dispatcher's `_maybe_apply_keyword_boost` boosts
    interest_score on candidates whose title matches an active-pool
    keyword (case-insensitive substring)."""

    def test_hook_noops_for_non_playlist_ag(self):
        """Only playlist AGs participate."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        df = pd.DataFrame([
            {"title": "anything", "interest_score": 0.5},
        ])
        group = {"output_kind": "picks"}  # not playlist
        out_df, kws = AlertGroupDispatcher._maybe_apply_keyword_boost(
            df, group, group_name="test",
        )
        assert kws == []
        # df unchanged
        assert out_df.iloc[0]["interest_score"] == pytest.approx(0.5)

    def test_hook_noops_when_disabled(self, isolated_immutable):
        from alert_groups.dispatcher import AlertGroupDispatcher
        from functionality.log_writer import log_curator_keyword_pref, flush_all
        from global_settings import get_settings
        get_settings().set("curator_keyword_boost_enabled", False)
        log_curator_keyword_pref(
            event_ts_iso="2026-05-17T09:00:00+00:00",
            keyword="joinery",
            source="api_post",
        )
        flush_all()
        df = pd.DataFrame([{"title": "Joinery video", "interest_score": 0.4}])
        out_df, kws = AlertGroupDispatcher._maybe_apply_keyword_boost(
            df, {"output_kind": "playlist"}, group_name="test",
        )
        # Disabled - no boost applied, no keywords returned
        assert kws == []
        assert out_df.iloc[0]["interest_score"] == pytest.approx(0.4)

    def test_hook_boosts_matching_titles(self, isolated_immutable):
        """Title contains the keyword (case-insensitive substring) →
        interest_score += boost_amount, clamped to 1.0."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from functionality.log_writer import log_curator_keyword_pref, flush_all
        from global_settings import get_settings
        get_settings().set("curator_keyword_boost_enabled", True)
        get_settings().set("curator_keyword_boost_amount", 0.25)
        log_curator_keyword_pref(
            event_ts_iso="2026-05-17T09:00:00+00:00",
            keyword="joinery",
            source="api_post",
        )
        flush_all()
        df = pd.DataFrame([
            {"title": "Japanese joinery demo", "interest_score": 0.50},
            {"title": "Cooking pasta", "interest_score": 0.30},
            {"title": "JOINERY 101", "interest_score": 0.90},
        ])
        out_df, kws = AlertGroupDispatcher._maybe_apply_keyword_boost(
            df, {"output_kind": "playlist"}, group_name="test",
        )
        assert set(kws) == {"joinery"}
        # Row 0: 0.50 + 0.25 = 0.75 (matches)
        assert out_df.iloc[0]["interest_score"] == pytest.approx(0.75)
        # Row 1: unchanged
        assert out_df.iloc[1]["interest_score"] == pytest.approx(0.30)
        # Row 2: 0.90 + 0.25 = 1.15 → clamped to 1.0
        assert out_df.iloc[2]["interest_score"] == pytest.approx(1.0)

    def test_hook_returns_empty_when_pool_empty(self, isolated_immutable):
        """No keywords POSTed → no boost, no keyword list."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        df = pd.DataFrame([{"title": "Joinery video", "interest_score": 0.4}])
        out_df, kws = AlertGroupDispatcher._maybe_apply_keyword_boost(
            df, {"output_kind": "playlist"}, group_name="test",
        )
        assert kws == []
        assert out_df.iloc[0]["interest_score"] == pytest.approx(0.4)

    def test_hook_handles_missing_title_column(self, isolated_immutable):
        """DataFrame without a 'title' column → no boost, but still
        returns the active keyword list (for $KEYWORD_POOL injection
        in the prompt)."""
        from alert_groups.dispatcher import AlertGroupDispatcher
        from functionality.log_writer import log_curator_keyword_pref, flush_all
        log_curator_keyword_pref(
            event_ts_iso="2026-05-17T09:00:00+00:00",
            keyword="joinery", source="api_post",
        )
        flush_all()
        df = pd.DataFrame([{"video_external_id": "v1", "interest_score": 0.5}])
        out_df, kws = AlertGroupDispatcher._maybe_apply_keyword_boost(
            df, {"output_kind": "playlist"}, group_name="test",
        )
        # Keywords returned (for prompt injection) but no boost applied
        assert kws == ["joinery"]
        assert out_df.iloc[0]["interest_score"] == pytest.approx(0.5)


# ── 7. Composer prompt placeholder + dispatcher substitution ────────


def test_default_ag_prompt_uses_keyword_pool_placeholder():
    """Drift guard: the composer YAML must contain $KEYWORD_POOL
    (NOT a hard-coded 'no keywords' phrase that the dispatcher
    can't substitute)."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "default_alert_groups" / "curator_playlist_composer.yaml") as f:
        ag = yaml.safe_load(f)
    prompt_text = ag.get("prompt_text", "")
    assert "$KEYWORD_POOL" in prompt_text, (
        "Composer prompt missing $KEYWORD_POOL placeholder - the "
        "dispatcher can't surface the active keyword pool without it."
    )


def test_dispatcher_substitutes_keyword_pool_placeholder():
    """Source-inspection drift guard: _run_inner must contain the
    $KEYWORD_POOL substitution."""
    import inspect
    from alert_groups.dispatcher import AlertGroupDispatcher
    src = inspect.getsource(AlertGroupDispatcher._run_inner)
    assert "$KEYWORD_POOL" in src, (
        "_run_inner() must substitute $KEYWORD_POOL in prompt_text"
    )
    assert "runtime_keyword_pool" in src, (
        "_run_inner() must reference runtime_keyword_pool"
    )


def test_dispatcher_wires_keyword_hook_after_topic_scoring():
    """The feeder loop in _run_inner must call
    `_maybe_apply_keyword_boost` AFTER topic-scoring (so the boost
    stacks on top of topic-similarity interest_score)."""
    import inspect
    from alert_groups.dispatcher import AlertGroupDispatcher
    src = inspect.getsource(AlertGroupDispatcher._run_inner)
    # The hook is called in the feeder loop
    assert "_maybe_apply_keyword_boost" in src, (
        "_run_inner() must call _maybe_apply_keyword_boost"
    )
    # AFTER topic-scoring
    topic_idx = src.index("_maybe_apply_topic_scoring")
    keyword_idx = src.index("_maybe_apply_keyword_boost")
    assert keyword_idx > topic_idx, (
        "_maybe_apply_keyword_boost must run AFTER _maybe_apply_topic_scoring"
    )
