"""Curator candidate ingestion - Archive.org (slice 7) tests.

Phase 6 / Bet 5 slice 7 (2026-05-17). The first multi-source extension
beyond YouTube (slice 1.5 RSS + slice 3b yt-dlp search). Archive.org
is Tier 1 in speaktube's multi-source rollout - best ethos fit, no
auth, no rate-limit, public-domain long tail.

The cross-source schema additivity guard lives in
``tests/test_curator_slice3b_topic_search.py::test_emits_same_canonical_columns_as_slice_1_5``
(extended in this slice to also include slice 7). This file covers
archive-specific concerns: allowlist registration, Archive.org-shape
edge cases (creator-as-list, missing runtime, malformed publicdate),
and the canonical-row contract on real responses.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pandas as pd
import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "script_library" / "scripts" / "curator_archive_org_pull.json"
)


# ── Helpers ─────────────────────────────────────────────────────────


def _run_script(monkeypatch, tmp_path, mock_response):
    """Execute the curator_archive_org_pull script with HTTP mocked + cwd
    set so the script runs deterministically. ``mock_response`` is the
    dict that ``resp.json()`` returns."""
    import os
    from scheduled_input_engine.executor import CodeExecutor

    spec = json.loads(SCRIPT_PATH.read_text())
    code = spec["code"]

    def _factory(*args, **kwargs):
        resp = unittest.mock.Mock()
        resp.status_code = 200
        resp.json.return_value = mock_response
        resp.text = json.dumps(mock_response) if isinstance(mock_response, dict) else str(mock_response)
        resp.content = (resp.text or "").encode()
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


def _mock_doc(**overrides):
    base = {
        "identifier": "test_item_001",
        "title": "Test Item Title",
        "description": "Test description.",
        "creator": "Test Creator",
        "date": "1985",
        "publicdate": "2010-06-15T12:30:00Z",
        "downloads": 1234,
        "runtime": "1:23:45",
    }
    base.update(overrides)
    return base


def _mock_response(docs):
    return {
        "responseHeader": {"status": 0, "params": {}},
        "response": {
            "numFound": len(docs),
            "start": 0,
            "docs": docs,
        },
    }


# ── 1. Allowlist + script-metadata sanity ───────────────────────────


def test_archive_org_is_allowlisted():
    """Slice 7 (2026-05-17): without ``archive.org`` on the allowlist,
    the sandboxed script can't reach the advancedsearch.php endpoint."""
    from global_settings import DEFAULTS
    patterns = DEFAULTS["allowed_api_domains"]
    assert any("archive" in p for p in patterns), (
        "archive.org missing from allowed_api_domains"
    )


def test_defaults_yaml_mirrors_archive_org_allowlist():
    """The in-code DEFAULTS must match the YAML defaults so a fresh
    install reads the same allowlist."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "global_settings.defaults.yaml") as f:
        yaml_defaults = yaml.safe_load(f)
    patterns = yaml_defaults["allowed_api_domains"]
    assert any("archive" in p for p in patterns), (
        "archive.org missing from allowed_api_domains in defaults YAML"
    )


def test_archive_org_script_metadata_sanity():
    spec = json.loads(SCRIPT_PATH.read_text())
    assert spec["trust_level"] == "sandboxed"
    assert spec["suggested_subdirectory"] == "IMMUTABLE/curator_candidates"
    assert spec["api_url"].startswith("https://archive.org/advancedsearch.php")
    assert spec["requires_credentials"] == []
    assert "from functionality" not in spec["code"]
    assert "import functionality" not in spec["code"]


# ── 2. Happy path - well-formed response → canonical rows ───────────


def test_happy_path_two_docs_two_canonical_rows(monkeypatch, tmp_path):
    payload = _mock_response([
        _mock_doc(identifier="charade_1963", title="Charade (1963)",
                  creator="Stanley Donen", runtime="1:53:00"),
        _mock_doc(identifier="metropolis_1927", title="Metropolis (1927)",
                  creator="Fritz Lang", runtime="2:33:00"),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    assert len(df) == 2
    assert set(df["source"].unique()) == {"archive_org"}
    assert set(df["video_external_id"]) == {"charade_1963", "metropolis_1927"}
    # Every video_url is yt-dlp-resolvable
    assert all(
        url.startswith("https://archive.org/details/")
        for url in df["video_url"]
    )
    # Every thumbnail_url uses the Archive.org img service
    assert all(
        url.startswith("https://archive.org/services/img/")
        for url in df["thumbnail_url"]
    )
    # channel_id is namespaced
    assert all(cid.startswith("archive_org:") for cid in df["channel_id"])


def test_runtime_parsed_to_duration_seconds(monkeypatch, tmp_path):
    payload = _mock_response([
        _mock_doc(identifier="hms", runtime="1:23:45"),     # 5025 sec
        _mock_doc(identifier="ms",  runtime="45:00"),       # 2700 sec
        _mock_doc(identifier="s",   runtime="42"),           # 42 sec
        _mock_doc(identifier="bad", runtime="not a time"),  # null
        _mock_doc(identifier="empty", runtime=""),          # null
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    durations = dict(zip(df["video_external_id"], df["duration_seconds"]))
    assert int(durations["hms"]) == 5025
    assert int(durations["ms"]) == 2700
    assert int(durations["s"]) == 42
    # None lands as NaN/None/empty depending on the executor's serialiser;
    # accept any of those - the contract is "not a valid duration".
    def _is_null(v):
        if v is None or v == "":
            return True
        try:
            import math
            return math.isnan(float(v))
        except (TypeError, ValueError):
            return False
    assert _is_null(durations["bad"]), f"bad runtime: {durations['bad']!r}"
    assert _is_null(durations["empty"]), f"empty runtime: {durations['empty']!r}"


def test_publicdate_parsed_to_epoch(monkeypatch, tmp_path):
    """The script uses publicdate (when present) to populate _epoch
    so the composer's `discovered_at_epoch >= relative_time("-2d")`
    filter works against Archive.org's add-to-archive timestamp."""
    import datetime as _dt
    payload = _mock_response([
        _mock_doc(identifier="dated", publicdate="2019-04-12T18:30:00Z"),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    expected_epoch = int(
        _dt.datetime(2019, 4, 12, 18, 30, tzinfo=_dt.timezone.utc).timestamp()
    )
    assert int(df.iloc[0]["_epoch"]) == expected_epoch


# ── 3. Archive.org shape quirks - list vs scalar, missing fields ────


def test_creator_as_list_takes_first_value(monkeypatch, tmp_path):
    """Archive.org returns ``creator`` as either a string OR a list of
    strings depending on how the item was uploaded. The script picks
    the first when it's a list."""
    payload = _mock_response([
        _mock_doc(
            identifier="multi_creator",
            creator=["Primary Creator", "Secondary Creator"],
        ),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    row = df.iloc[0]
    assert row["channel_name"] == "Primary Creator"
    assert row["channel_id"] == "archive_org:primary_creator"


def test_title_and_description_as_lists(monkeypatch, tmp_path):
    payload = _mock_response([
        _mock_doc(
            identifier="multi_field",
            title=["First Title", "Alt Title"],
            description=["First description.", "Alt description."],
        ),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    row = df.iloc[0]
    assert row["title"] == "First Title"
    assert row["description"] == "First description."


def test_missing_creator_lands_unknown(monkeypatch, tmp_path):
    payload = _mock_response([
        _mock_doc(identifier="anon", creator=None),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    row = df.iloc[0]
    assert row["channel_name"] == ""
    assert row["channel_id"] == "archive_org:unknown"


def test_malformed_publicdate_falls_back_to_now(monkeypatch, tmp_path):
    """If publicdate can't be parsed, _epoch falls back to now -
    the row still lands."""
    payload = _mock_response([
        _mock_doc(identifier="bad_date", publicdate="not-a-real-date"),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    assert len(df) == 1
    # _epoch is around "now" - definitely > 2025 epoch
    assert int(df.iloc[0]["_epoch"]) > 1_700_000_000


def test_published_iso_carries_raw_value(monkeypatch, tmp_path):
    """The raw ISO string survives in `published_iso` even when _epoch
    parsing succeeds - speaktube's `published_at` field carries it
    through after composer dispatch."""
    payload = _mock_response([
        _mock_doc(identifier="iso_test", publicdate="2010-06-15T12:30:00Z"),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    assert df.iloc[0]["published_iso"] == "2010-06-15T12:30:00Z"


# ── 4. Failure tolerance ────────────────────────────────────────────


def test_empty_docs_emits_info_row(monkeypatch, tmp_path):
    """A search that returns 0 docs should produce a well-shaped
    info_row, NOT crash."""
    payload = _mock_response([])
    df = _run_script(monkeypatch, tmp_path, payload)
    assert len(df) == 1
    assert df.iloc[0]["source"] == "archive_org_info"
    assert "0 items" in df.iloc[0]["title"]


def test_missing_response_key_emits_info_row(monkeypatch, tmp_path):
    """If Archive.org returns JSON without the 'response' key (server
    error), the script must produce an info_row, not crash."""
    df = _run_script(monkeypatch, tmp_path, {"error": "Internal Server Error"})
    assert len(df) == 1
    assert df.iloc[0]["source"] == "archive_org_info"


def test_within_run_dedup_by_identifier(monkeypatch, tmp_path):
    """If Archive.org returns the same identifier twice in one response
    (rare but possible across search-sort instability), the script
    dedups within the run. Cross-run dedup happens at the composer."""
    payload = _mock_response([
        _mock_doc(identifier="duplicate_id", title="First instance"),
        _mock_doc(identifier="duplicate_id", title="Second instance"),
        _mock_doc(identifier="unique_id", title="Unique"),
    ])
    df = _run_script(monkeypatch, tmp_path, payload)
    assert len(df) == 2
    assert set(df["video_external_id"]) == {"duplicate_id", "unique_id"}


# ── 5. Schema additivity drift guard (script-level) ─────────────────


_CURATOR_CANDIDATE_FROZEN_COLS = {
    "_epoch", "discovered_at_epoch", "source",
    "video_external_id", "video_url", "title",
    "channel_id", "channel_name", "channel_url",
    "published_iso", "description",
    "duration_seconds", "thumbnail_url", "raw_blob",
}


def test_archive_org_script_emits_canonical_14_col_schema(monkeypatch, tmp_path):
    """Slice 7 follows the same additive-only canonical schema as
    slice 1.5 + 3b. If a column is missing here, the composer's
    union-via-SPQL stops working for archive.org rows."""
    payload = _mock_response([_mock_doc()])
    df = _run_script(monkeypatch, tmp_path, payload)
    missing = _CURATOR_CANDIDATE_FROZEN_COLS - set(df.columns)
    assert not missing, (
        f"curator_archive_org_pull dropped frozen column(s) {sorted(missing)}. "
        "Additive-only - see CLAUDE.md."
    )
