"""Cross-source ad-hoc search endpoint - slice 12 tests.

Phase 6 / Bet 5 slice 12 (2026-05-17, speaktube req #11). The new
``GET /api/search?q=...`` endpoint searches the already-ingested
candidate pool (``indexes/IMMUTABLE/curator_candidates/``) and
returns results in the same JSON shape as ``/api/playlist/today``
so the speaktube renderer reuses one code path.

Tests cover:
1. Required ``q`` param + 400 on missing/empty
2. Substring match (case-insensitive) against title
3. Multiple whitespace-separated tokens are OR'd
4. ``sources`` filter (comma-separated)
5. ``limit`` param (default + clamp)
6. Response shape parity with ``/api/playlist/today``
7. ``score_reasoning`` carries the match annotation
8. Slop scoring applied (same regex as composer feeder)
9. Empty result returns 200 with empty items[] (NOT 404)
10. Info-rows from ingestion scripts excluded
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _seed_candidates(immutable_root: Path, rows: list[dict]) -> None:
    """Write a synthetic curator_candidates parquet under
    indexes/IMMUTABLE/curator_candidates/."""
    target = immutable_root / "curator_candidates"
    target.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_parquet(target / "test_candidates.parquet", index=False)


def _candidate_row(**overrides) -> dict:
    """Build a synthetic curator_candidates row with canonical defaults."""
    import time as _time
    base = {
        "_epoch": int(_time.time()),
        "discovered_at_epoch": int(_time.time()),
        "source": "youtube_rss",
        "video_external_id": "default_vid",
        "video_url": "https://www.youtube.com/watch?v=default_vid",
        "title": "Default title",
        "channel_id": "UCdefault",
        "channel_name": "Default channel",
        "channel_url": "",
        "published_iso": "2026-05-15T12:00:00+00:00",
        "description": "",
        "duration_seconds": None,
        "thumbnail_url": "",
        "raw_blob": "{}",
    }
    base.update(overrides)
    return base


# ── 1. Required q param ─────────────────────────────────────────────


def test_search_rejects_missing_q(isolated_immutable, client):
    resp = client.get("/api/search")
    assert resp.status_code == 400


def test_search_rejects_empty_q(isolated_immutable, client):
    resp = client.get("/api/search?q=")
    assert resp.status_code == 400


def test_search_rejects_whitespace_only_q(isolated_immutable, client):
    resp = client.get("/api/search?q=%20%20")  # urlencoded "  "
    assert resp.status_code == 400


# ── 2. Empty result when no candidates ──────────────────────────────


def test_search_returns_empty_items_when_no_candidates(isolated_immutable, client):
    """No ingestion yet - empty items[] (NOT 404), same shape as the
    playlist endpoint's empty case."""
    resp = client.get("/api/search?q=anything")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["items"] == []
    # Shape parity with /api/playlist/today
    assert "run_date" in payload
    assert "growth_dial" in payload
    assert "growth_dial_stored" in payload
    assert payload["thin_history_active"] is False
    assert "theme" in payload


# ── 3. Title substring match (case-insensitive) ─────────────────────


def test_search_matches_title_case_insensitive(isolated_immutable, client):
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="vid_joinery",
            title="Japanese Joinery demo",
        ),
        _candidate_row(
            video_external_id="vid_cooking",
            title="Cooking pasta",
        ),
    ])
    # lowercase query should match "Japanese Joinery"
    resp = client.get("/api/search?q=joinery")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["video"]["external_id"] == "vid_joinery"


def test_search_multiple_tokens_are_or_d(isolated_immutable, client):
    _seed_candidates(isolated_immutable, [
        _candidate_row(video_external_id="v_alpha", title="Alpha video"),
        _candidate_row(video_external_id="v_beta", title="Beta video"),
        _candidate_row(video_external_id="v_gamma", title="Gamma video"),
    ])
    # "alpha gamma" should match Alpha + Gamma (NOT Beta)
    resp = client.get("/api/search?q=alpha+gamma")
    assert resp.status_code == 200
    eids = {it["video"]["external_id"] for it in resp.get_json()["items"]}
    assert eids == {"v_alpha", "v_gamma"}


# ── 4. Sources filter ───────────────────────────────────────────────


def test_search_sources_filter(isolated_immutable, client):
    """When ?sources=youtube_rss,archive_org is set, only those source
    enum values appear in results."""
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="yt_v1", title="rare metals tutorial",
            source="youtube_rss",
        ),
        _candidate_row(
            video_external_id="ao_v1", title="rare earth metals doc",
            source="archive_org",
        ),
        _candidate_row(
            video_external_id="ts_v1", title="rare earth metals search",
            source="topic_search:youtube:0",
        ),
    ])
    resp = client.get("/api/search?q=rare&sources=youtube_rss,archive_org")
    assert resp.status_code == 200
    eids = {it["video"]["external_id"] for it in resp.get_json()["items"]}
    assert eids == {"yt_v1", "ao_v1"}  # topic_search excluded


# ── 5. Limit param ──────────────────────────────────────────────────


def test_search_default_limit_is_100(isolated_immutable, client):
    # Seed 150 matching candidates
    rows = [
        _candidate_row(
            video_external_id=f"match_{i:03d}",
            title=f"match title {i}",
            _epoch=int(__import__("time").time()) + i,  # distinct epochs
        )
        for i in range(150)
    ]
    _seed_candidates(isolated_immutable, rows)
    resp = client.get("/api/search?q=match")
    assert resp.status_code == 200
    assert len(resp.get_json()["items"]) == 100


def test_search_custom_limit(isolated_immutable, client):
    rows = [
        _candidate_row(
            video_external_id=f"m_{i}", title=f"match {i}",
            _epoch=int(__import__("time").time()) + i,
        )
        for i in range(50)
    ]
    _seed_candidates(isolated_immutable, rows)
    resp = client.get("/api/search?q=match&limit=20")
    assert resp.status_code == 200
    assert len(resp.get_json()["items"]) == 20


def test_search_limit_clamped_at_max(isolated_immutable, client):
    """Soft cap at 1000 - anything higher gets clamped."""
    rows = [
        _candidate_row(
            video_external_id=f"m_{i}", title=f"match {i}",
            _epoch=int(__import__("time").time()) + i,
        )
        for i in range(10)
    ]
    _seed_candidates(isolated_immutable, rows)
    resp = client.get("/api/search?q=match&limit=99999")
    assert resp.status_code == 200
    # Only 10 candidates, so we get all 10 (cap doesn't kick in)
    assert len(resp.get_json()["items"]) == 10


def test_search_limit_invalid_falls_back_to_default(isolated_immutable, client):
    """Non-int limit (e.g. ?limit=foo) falls back to default 100."""
    rows = [
        _candidate_row(
            video_external_id=f"m_{i}", title=f"match {i}",
            _epoch=int(__import__("time").time()) + i,
        )
        for i in range(105)
    ]
    _seed_candidates(isolated_immutable, rows)
    resp = client.get("/api/search?q=match&limit=invalid")
    assert resp.status_code == 200
    assert len(resp.get_json()["items"]) == 100


# ── 6. Response shape parity with /api/playlist/today ───────────────


def test_search_response_video_object_shape(isolated_immutable, client):
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="vid_shape",
            video_url="https://www.youtube.com/watch?v=vid_shape",
            title="Shape test video",
            channel_name="Channel Shape",
            thumbnail_url="https://i.ytimg.com/vi/vid_shape/hq.jpg",
            published_iso="2026-05-15T12:00:00+00:00",
            duration_seconds=300,
            source="youtube_rss",
        ),
    ])
    resp = client.get("/api/search?q=shape")
    item = resp.get_json()["items"][0]
    # Top-level item fields
    assert item["position"] == 1
    assert item["slot_kind"] == "main"
    assert item["rationale"] == ""
    # video object fields (same shape as /api/playlist/today)
    v = item["video"]
    assert v["external_id"] == "vid_shape"
    assert v["url"] == "https://www.youtube.com/watch?v=vid_shape"
    assert v["title"] == "Shape test video"
    assert v["channel_name"] == "Channel Shape"
    assert v["thumbnail_url"] == "https://i.ytimg.com/vi/vid_shape/hq.jpg"
    assert v["published_at"] == "2026-05-15T12:00:00+00:00"
    assert v["duration_seconds"] == 300
    assert v["interest_score"] == pytest.approx(1.0)
    assert v["growth_score"] is None
    assert "slop_score" in v
    assert "score_reasoning" in v
    assert "shape" in v["score_reasoning"].lower()


def test_search_constructs_url_for_archive_org_when_missing(isolated_immutable, client):
    """Archive.org rows might have empty video_url in older parquets.
    Reconstruct from external_id + source."""
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="charade_1963",
            video_url="",  # empty
            title="Charade 1963 film",
            source="archive_org",
        ),
    ])
    resp = client.get("/api/search?q=charade")
    v = resp.get_json()["items"][0]["video"]
    assert v["url"] == "https://archive.org/details/charade_1963"


# ── 7. Slop scoring ─────────────────────────────────────────────────


def test_search_slop_score_high_for_clickbait_pattern(isolated_immutable, client):
    """Same regex as the composer feeder: clickbait patterns get
    slop_score=0.8."""
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="clickbait_v1",
            title="You won't believe what happened next",
        ),
    ])
    resp = client.get("/api/search?q=believe")
    v = resp.get_json()["items"][0]["video"]
    assert v["slop_score"] == pytest.approx(0.8)


def test_search_slop_score_low_for_normal_title(isolated_immutable, client):
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="normal_v1",
            title="Educational lecture on physics",
        ),
    ])
    resp = client.get("/api/search?q=lecture")
    v = resp.get_json()["items"][0]["video"]
    assert v["slop_score"] == pytest.approx(0.1)


# ── 8. Info-rows excluded ───────────────────────────────────────────


def test_search_excludes_info_rows(isolated_immutable, client):
    """Ingestion scripts emit ``source='..._info'`` placeholder rows
    when there's nothing else to write. Search must NOT surface those."""
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="",
            title="No subscriptions found at indexes/IMMUTABLE/curator_takeout/subscriptions",
            source="youtube_rss_info",
        ),
        _candidate_row(
            video_external_id="real_vid",
            title="Real subscriptions video",
            source="youtube_rss",
        ),
    ])
    resp = client.get("/api/search?q=subscriptions")
    eids = {it["video"]["external_id"] for it in resp.get_json()["items"]}
    assert eids == {"real_vid"}


# ── 9. Sorted by _epoch DESC (most-recently-discovered first) ───────


def test_search_results_sorted_by_recency(isolated_immutable, client):
    import time as _time
    base = int(_time.time())
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="oldest", title="match item",
            _epoch=base,
        ),
        _candidate_row(
            video_external_id="middle", title="match item",
            _epoch=base + 100,
        ),
        _candidate_row(
            video_external_id="newest", title="match item",
            _epoch=base + 200,
        ),
    ])
    resp = client.get("/api/search?q=match")
    eids = [it["video"]["external_id"] for it in resp.get_json()["items"]]
    assert eids == ["newest", "middle", "oldest"]


# ── 10. Regex-special characters in q don't break the query ─────────


def test_search_regex_special_chars_in_q_dont_crash(isolated_immutable, client):
    """User input like 'C++' or '1+1' or '(x)' should be treated as
    literal substrings, not regex metacharacters. The endpoint
    re.escape()s each token before joining."""
    _seed_candidates(isolated_immutable, [
        _candidate_row(
            video_external_id="cpp_v1",
            title="C++ programming tutorial",
        ),
    ])
    resp = client.get("/api/search?q=" + "C%2B%2B")  # "C++" urlencoded
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert len(items) == 1
    assert items[0]["video"]["external_id"] == "cpp_v1"
