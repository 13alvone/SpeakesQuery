"""Curator candidate ingestion (YouTube RSS) - slice 1.5 tests.

Phase 6 / Bet 5 slice 1.5 (2026-05-16). Covers the script_library
``curator_youtube_rss_pull`` script end-to-end:

* Schema sanity for the new candidate parquet output
* Watch-history-priority sort joins subscriptions ↔ history correctly
* Atom XML parsing extracts every required field
* Empty-state fallback (no subscriptions yet) emits the info_row
* Allowlist drift guard for ``www.youtube.com``
* No-`_`-prefixed-names / no-tuple-unpacks adapter for the sandbox is
  exercised by ``test_script_library.py``'s sandbox compilation gate.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pandas as pd
import pytest


# ── Helpers ─────────────────────────────────────────────────────────


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "script_library" / "scripts" / "curator_youtube_rss_pull.json"
)


_MOCK_ATOM = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
    'xmlns:media="http://search.yahoo.com/mrss/" '
    'xmlns="http://www.w3.org/2005/Atom">'
    '<entry>'
    '<yt:videoId>dQw4w9WgXcQ</yt:videoId>'
    '<title>First video</title>'
    '<published>2026-05-15T16:30:00+00:00</published>'
    '<author>'
    '<name>RSS-Reported Channel Name</name>'
    '<uri>https://www.youtube.com/channel/CHID</uri>'
    '</author>'
    '<media:group>'
    '<media:description>First description.</media:description>'
    '<media:thumbnail url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" width="480" height="360" />'
    '</media:group>'
    '</entry>'
    '<entry>'
    '<yt:videoId>abcdefghIJK</yt:videoId>'
    '<title>Second video</title>'
    '<published>2026-05-14T09:00:00+00:00</published>'
    '<author>'
    '<name>RSS-Reported Channel Name</name>'
    '</author>'
    '<media:group>'
    '<media:description>Second description.</media:description>'
    '<media:thumbnail url="https://i.ytimg.com/vi/abcdefghIJK/hqdefault.jpg" width="480" height="360" />'
    '</media:group>'
    '</entry>'
    '</feed>'
)


def _seed_subscriptions(immutable_root: Path, channels: list[dict]) -> None:
    """Write a synthetic subscriptions parquet under <root>/curator_takeout/subscriptions/."""
    target = immutable_root / "curator_takeout" / "subscriptions"
    target.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(channels, columns=["_epoch", "channel_id", "channel_url", "channel_title"])
    df.to_parquet(target / "test_subs.parquet", index=False)


def _seed_watch_history(immutable_root: Path, watches: list[dict]) -> None:
    """Write a synthetic watch_history parquet under <root>/curator_takeout/watch_history/."""
    target = immutable_root / "curator_takeout" / "watch_history"
    target.mkdir(parents=True, exist_ok=True)
    cols = [
        "_epoch", "video_id", "video_url", "video_title",
        "channel_id", "channel_url", "channel_name",
        "watched_iso", "tz_abbrev",
    ]
    df = pd.DataFrame(watches, columns=cols)
    df.to_parquet(target / "test_hist.parquet", index=False)


def _run_script_against_isolated_immutable(
    monkeypatch, tmp_path, mock_atom: str = _MOCK_ATOM,
) -> pd.DataFrame:
    """Execute the curator_youtube_rss_pull script with HTTP mocked + cwd
    set so the script's hard-coded relative paths resolve under the
    test's tmp_path.

    The script uses ``pd.read_parquet('indexes/IMMUTABLE/curator_takeout/...')``
    with a RELATIVE path, so we cd into ``tmp_path`` (which has the
    seeded ``indexes/IMMUTABLE/`` layout) before executing the code.
    """
    import os
    from scheduled_input_engine.executor import CodeExecutor

    spec = json.loads(SCRIPT_PATH.read_text())
    code = spec["code"]

    # Build the response factory
    def _factory(*args, **kwargs):
        resp = unittest.mock.Mock()
        resp.status_code = 200
        resp.text = mock_atom
        resp.content = mock_atom.encode()
        resp.json.return_value = {}
        resp.raise_for_status = unittest.mock.Mock()
        return resp

    prev_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with unittest.mock.patch("requests.get", side_effect=_factory):
            executor = CodeExecutor(code, test_mode=True, trust_level="sandboxed")
            result = executor.execute_test()
    finally:
        os.chdir(prev_cwd)

    assert result["status"] == "pass", (
        f"Script execution failed: {result.get('errors')}"
    )
    return pd.DataFrame(result["head"])


# ── 1. Allowlist drift guard ────────────────────────────────────────


def test_youtube_dot_com_is_allowlisted():
    """Without ``www.youtube.com`` on the allowlist, the sandboxed
    script can't reach YouTube's RSS endpoint. Pinned so a future
    allowlist cleanup doesn't silently lock the curator out."""
    from global_settings import DEFAULTS
    patterns = DEFAULTS["allowed_api_domains"]
    assert any(
        "youtube" in p for p in patterns
    ), "youtube.com missing from allowed_api_domains"


def test_defaults_yaml_mirrors_youtube_allowlist():
    """Drift guard against the in-code allowlist vs the YAML default
    diverging - every fresh install reads from the YAML so a missing
    entry here would silently break ingestion on a new operator's box."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "global_settings.defaults.yaml") as f:
        yaml_defaults = yaml.safe_load(f)
    patterns = yaml_defaults["allowed_api_domains"]
    assert any("youtube" in p for p in patterns), (
        "youtube.com missing from allowed_api_domains in defaults YAML"
    )


# ── 2. Script JSON structure ────────────────────────────────────────


def test_rss_script_metadata_sanity():
    spec = json.loads(SCRIPT_PATH.read_text())
    assert spec["trust_level"] == "sandboxed"
    assert spec["suggested_subdirectory"] == "IMMUTABLE/curator_candidates"
    assert spec["api_url"].startswith("https://www.youtube.com/feeds/videos.xml")
    assert spec["requires_credentials"] == []
    # Sandbox compatibility - no `_`-prefixed names or tuple unpacks
    assert "from functionality" not in spec["code"]
    assert "import functionality" not in spec["code"]


def test_rss_script_declares_expected_columns():
    spec = json.loads(SCRIPT_PATH.read_text())
    code = spec["code"]
    # The script defines EXPECTED_COLUMNS as a Python literal. The
    # frozen set below pins the canonical curator-candidate schema -
    # adding columns is fine (additive), but renaming or dropping
    # them would break any SPQL query touching the candidate index.
    frozen = {
        "_epoch", "discovered_at_epoch", "source",
        "video_external_id", "video_url", "title",
        "channel_id", "channel_name", "channel_url",
        "published_iso", "description",
        "duration_seconds", "thumbnail_url", "raw_blob",
    }
    for col in frozen:
        assert f"'{col}'" in code, (
            f"curator_youtube_rss_pull is missing the frozen column "
            f"{col!r} in its EXPECTED_COLUMNS literal."
        )


# ── 3. Happy path - subscriptions + watch_history → candidate rows ─


def test_rss_pull_produces_candidate_rows_when_subscriptions_exist(monkeypatch, tmp_path):
    """Seed a subscriptions parquet, mock YouTube RSS, run the script
    in sandbox. Verify the canonical row shape + that priority sort
    placed the high-watch-count channel first."""
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [
        {
            "_epoch": 1, "channel_id": "UCheavily_watched_chan",
            "channel_url": "https://www.youtube.com/channel/UCheavily_watched_chan",
            "channel_title": "Heavily Watched",
        },
        {
            "_epoch": 1, "channel_id": "UClightly_watched_chan",
            "channel_url": "https://www.youtube.com/channel/UClightly_watched_chan",
            "channel_title": "Lightly Watched",
        },
    ])
    _seed_watch_history(immutable, [
        # 5 watches on Heavily, 1 on Lightly
        {"_epoch": i, "video_id": f"v{i}", "video_url": "",
         "video_title": "", "channel_id": "UCheavily_watched_chan",
         "channel_url": "", "channel_name": "Heavily Watched",
         "watched_iso": "", "tz_abbrev": "UTC"}
        for i in range(5)
    ] + [
        {"_epoch": 100, "video_id": "vl1", "video_url": "",
         "video_title": "", "channel_id": "UClightly_watched_chan",
         "channel_url": "", "channel_name": "Lightly Watched",
         "watched_iso": "", "tz_abbrev": "UTC"}
    ])

    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)

    # 2 channels × 2 entries per mock = 4 rows
    assert len(df) == 4
    assert set(df["source"].unique()) == {"youtube_rss"}
    assert "video_external_id" in df.columns
    assert "dQw4w9WgXcQ" in df["video_external_id"].tolist()
    assert "abcdefghIJK" in df["video_external_id"].tolist()
    # Author name from RSS wins over the subscription channel_title
    assert (df["channel_name"] == "RSS-Reported Channel Name").all()
    # video_url is yt-dlp-resolvable
    assert all(
        url.startswith("https://www.youtube.com/watch?v=")
        for url in df["video_url"]
    )
    # _epoch parsed from published date (May 15 2026 = 1779208200)
    import datetime as _dt
    expected_epoch = int(
        _dt.datetime(2026, 5, 15, 16, 30, tzinfo=_dt.timezone.utc).timestamp()
    )
    assert (df["_epoch"] == expected_epoch).any()


def test_rss_pull_priority_sort_prioritizes_high_watch_count(monkeypatch, tmp_path):
    """Verify the heavily-watched channel got queried FIRST (i.e. its
    rows appear in the output before the lightly-watched channel's).
    Within-channel order is stable so we can check the cross-channel
    ordering by index."""
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [
        # Intentionally insert "lightly" first in the subscription
        # parquet - the priority sort should still hoist "heavily"
        # to the top of the iteration order.
        {
            "_epoch": 1, "channel_id": "UClightly_watched_chan",
            "channel_url": "", "channel_title": "Lightly Watched",
        },
        {
            "_epoch": 1, "channel_id": "UCheavily_watched_chan",
            "channel_url": "", "channel_title": "Heavily Watched",
        },
    ])
    _seed_watch_history(immutable, [
        {"_epoch": i, "video_id": f"v{i}", "video_url": "",
         "video_title": "", "channel_id": "UCheavily_watched_chan",
         "channel_url": "", "channel_name": "Heavily Watched",
         "watched_iso": "", "tz_abbrev": "UTC"}
        for i in range(50)  # 50 watches → high priority
    ])

    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)

    # The first 2 rows should be for the heavily-watched channel
    assert df.iloc[0]["channel_id"] == "UCheavily_watched_chan"
    assert df.iloc[1]["channel_id"] == "UCheavily_watched_chan"
    assert df.iloc[2]["channel_id"] == "UClightly_watched_chan"


def test_rss_pull_emits_info_row_when_no_subscriptions(monkeypatch, tmp_path):
    """No subscriptions parquet on disk → script falls through to the
    info_row branch. Per the empty-input-tolerance convention, the
    output is a well-shaped 1-row DataFrame, NOT an exception."""
    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)
    assert len(df) == 1
    assert df.iloc[0]["source"] == "youtube_rss_info"
    assert "subscriptions" in df.iloc[0]["title"].lower() or \
           "subscriptions" in df.iloc[0].get("description", "").lower()


# ── 4. Per-entry parsing details ────────────────────────────────────


def test_rss_pull_parses_description_from_media_group(monkeypatch, tmp_path):
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [{
        "_epoch": 1, "channel_id": "UCtest_chan_for_descs",
        "channel_url": "", "channel_title": "T",
    }])
    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)
    descs = set(df["description"].tolist())
    assert "First description." in descs
    assert "Second description." in descs


def test_rss_pull_extracts_thumbnail_url_from_media_group(monkeypatch, tmp_path):
    """Slice 4 (2026-05-17): YouTube's RSS feed exposes the thumbnail
    URL as an attribute on ``<media:thumbnail>`` inside ``<media:group>``.
    The script extracts it so the speaktube player can render a card
    image without synthesizing the URL client-side."""
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [{
        "_epoch": 1, "channel_id": "UCtest_chan_for_thumbs",
        "channel_url": "", "channel_title": "T",
    }])
    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)
    thumbs = set(df["thumbnail_url"].tolist())
    assert "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" in thumbs
    assert "https://i.ytimg.com/vi/abcdefghIJK/hqdefault.jpg" in thumbs


def test_rss_pull_thumbnail_url_falls_back_to_empty_when_missing(monkeypatch, tmp_path):
    """Slice 4 (2026-05-17): if the feed omits ``<media:thumbnail>``
    (some malformed or legacy feeds do), the script must NOT raise - it lands empty
    string and the player falls back to its YouTube synthesis path."""
    atom_no_thumb = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns="http://www.w3.org/2005/Atom">'
        '<entry>'
        '<yt:videoId>NoThumbVid_X</yt:videoId>'
        '<title>Sole video</title>'
        '<published>2026-05-15T16:30:00+00:00</published>'
        '<author><name>Test Author</name></author>'
        '<media:group>'
        '<media:description>Only a description, no thumbnail.</media:description>'
        '</media:group>'
        '</entry>'
        '</feed>'
    )
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [{
        "_epoch": 1, "channel_id": "UCmissing_thumb_test",
        "channel_url": "", "channel_title": "T",
    }])
    df = _run_script_against_isolated_immutable(
        monkeypatch, tmp_path, mock_atom=atom_no_thumb,
    )
    assert len(df) == 1
    assert df.iloc[0]["video_external_id"] == "NoThumbVid_X"
    assert df.iloc[0]["thumbnail_url"] == ""


def test_rss_pull_raw_blob_preserves_entry_for_forward_compat(monkeypatch, tmp_path):
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [{
        "_epoch": 1, "channel_id": "UCtest_raw_blob",
        "channel_url": "", "channel_title": "T",
    }])
    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)
    # Every row's raw_blob carries the original <entry> markup -
    # forward-compat for future fields YouTube might add.
    for blob in df["raw_blob"]:
        assert "<entry>" in blob or "<entry " in blob
        assert "videoId" in blob


# ── 5. Failure tolerance ────────────────────────────────────────────


def test_rss_pull_skips_non_200_responses(monkeypatch, tmp_path):
    """Channel deleted / private / rate-limited → 404/429/500. The
    script must skip silently, not raise, and the other channels in
    the run still produce rows."""
    import os
    from scheduled_input_engine.executor import CodeExecutor

    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [
        {"_epoch": 1, "channel_id": "UC_will_404_xxxxxxxxxxxxx",
         "channel_url": "", "channel_title": "Dead"},
        {"_epoch": 1, "channel_id": "UC_alive_chan_xxxxxxxxxxxxx",
         "channel_url": "", "channel_title": "Alive"},
    ])

    # Mock that returns 404 for the first channel, 200+XML for the second.
    call_state = {"calls": 0}

    def _factory(url, *args, **kwargs):
        call_state["calls"] += 1
        resp = unittest.mock.Mock()
        if "UC_will_404" in url:
            resp.status_code = 404
            resp.text = "Not Found"
        else:
            resp.status_code = 200
            resp.text = _MOCK_ATOM
        resp.content = (resp.text or "").encode()
        resp.json.return_value = {}
        resp.raise_for_status = unittest.mock.Mock()
        return resp

    spec = json.loads(SCRIPT_PATH.read_text())
    code = spec["code"]
    prev_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with unittest.mock.patch("requests.get", side_effect=_factory):
            executor = CodeExecutor(code, test_mode=True, trust_level="sandboxed")
            result = executor.execute_test()
    finally:
        os.chdir(prev_cwd)

    assert result["status"] == "pass"
    df = pd.DataFrame(result["head"])
    # 1 channel succeeded × 2 entries = 2 rows. The 404 channel
    # contributed zero rows.
    assert len(df) == 2
    assert (df["channel_id"] == "UC_alive_chan_xxxxxxxxxxxxx").all()


# ── 6. Schema additivity drift guard ────────────────────────────────


_CURATOR_CANDIDATE_FROZEN_COLS = {
    "_epoch", "discovered_at_epoch", "source",
    "video_external_id", "video_url", "title",
    "channel_id", "channel_name", "channel_url",
    "published_iso", "description",
    "duration_seconds", "thumbnail_url", "raw_blob",
}


def test_curator_candidate_schema_is_additive_only_in_script(monkeypatch, tmp_path):
    """The candidate row schema is the contract between the ingestion
    layer (this script + any future archive.org / PBS / Vimeo source)
    and the slice-2 composer. Removing or renaming a column breaks the
    composer + any historical SPQL query touching the candidate index.

    Pinned at the script-output level (rather than a log_writer SCHEMAS
    entry, since candidates aren't routed through log_writer - they go
    through the standard ingestion pipeline). Future sources should
    emit the SAME columns.
    """
    immutable = tmp_path / "indexes" / "IMMUTABLE"
    _seed_subscriptions(immutable, [{
        "_epoch": 1, "channel_id": "UCschema_drift_guard",
        "channel_url": "", "channel_title": "T",
    }])
    df = _run_script_against_isolated_immutable(monkeypatch, tmp_path)
    missing = _CURATOR_CANDIDATE_FROZEN_COLS - set(df.columns)
    assert not missing, (
        f"curator_candidate row dropped frozen column(s) {sorted(missing)}. "
        f"Additive-only - see CLAUDE.md."
    )
