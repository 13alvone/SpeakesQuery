"""Curator topic-search via yt-dlp - slice 3b tests.

Phase 6 / Bet 5 slice 3b (2026-05-16). Covers the
``curator_topic_search_pull_pro`` _pro-tier ingestion script
end-to-end against a synthetic topic snapshot on disk + a mocked
``yt_dlp.YoutubeDL.extract_info``. The slice-3b breadth piece:
discovers candidates OUTSIDE the user's existing YouTube
subscriptions by searching for content matching the user's
TOPIC-CLUSTER labels (not their subscription list).

Slice 1.5's analogue (``curator_youtube_rss_pull``) is sandboxed and
mocks ``requests.get``; this script is UNRESTRICTED (yt-dlp uses
urllib, not requests) so the mock layer is the yt_dlp module itself.

The script_library harness pins the empty-snapshot fallback (well-
shaped info_row when no snapshot exists). This file pins the happy
path:

* Snapshot exists with real labels → script picks top K clusters by
  weight, queries yt-dlp for each, emits one row per result.
* Snapshot has only placeholder labels (dry-run / "Cluster N") →
  script falls back to the first exemplar title as the search query.
* Snapshot rows are present but yt_dlp.extract_info raises → script
  records an error, emits the canonical info_row, doesn't crash.
* JSON metadata sanity (trust_level=unrestricted, the canonical
  13-column schema, etc.).
"""

from __future__ import annotations

import json
import os
import unittest.mock
from pathlib import Path

import pandas as pd
import pytest


# ── Constants ────────────────────────────────────────────────────────


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "script_library" / "scripts" / "curator_topic_search_pull_pro.json"
)


_CANONICAL_CANDIDATE_COLS = {
    "_epoch", "discovered_at_epoch", "source",
    "video_external_id", "video_url", "title",
    "channel_id", "channel_name", "channel_url",
    "published_iso", "description",
    "duration_seconds", "thumbnail_url", "raw_blob",
}


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_snapshot(
    immutable_root: Path,
    clusters: list[dict],
    snapshot_epoch: int = 1_700_000_000,
    snapshot_id: str = "test-snapshot-uuid",
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    dim: int = 384,
    decay_lambda_days: float = 180.0,
) -> None:
    """Write a synthetic topic snapshot parquet under
    ``<root>/curator_topic_snapshots/``.

    Each cluster dict carries: cluster_id, label, weight, n_members,
    exemplar_titles (list[str]). Centroid is stuffed with zeros - the
    script doesn't use it.
    """
    target = immutable_root / "curator_topic_snapshots"
    target.mkdir(parents=True, exist_ok=True)
    n_clusters = len(clusters)
    rows = []
    for c in clusters:
        rows.append({
            "_epoch": snapshot_epoch,
            "snapshot_epoch": snapshot_epoch,
            "snapshot_id": snapshot_id,
            "model_name": model_name,
            "dim": dim,
            "n_clusters": n_clusters,
            "n_history_rows": 5000,
            "decay_lambda_days": decay_lambda_days,
            "cluster_id": c["cluster_id"],
            "centroid_json": json.dumps([0.0] * dim),
            "weight": float(c["weight"]),
            "n_members": int(c.get("n_members", 100)),
            "exemplar_titles_json": json.dumps(c.get("exemplar_titles", [])),
            "label": c.get("label", ""),
        })
    df = pd.DataFrame(rows)
    df.to_parquet(target / "test_snapshot.parquet", index=False)


def _make_yt_dlp_mock(per_query_results: dict[str, list[dict]]):
    """Build a context-manager fake replacing ``yt_dlp.YoutubeDL``.

    ``per_query_results`` maps the bare query string (after the
    ``ytsearchN:`` prefix is stripped) to the list of dicts that
    extract_info should return as ``entries``.
    """

    class _FakeYDL:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            # url shape: "ytsearch{N}:{query}"
            if url.startswith("ytsearch"):
                rest = url.split(":", 1)[1] if ":" in url else url
            else:
                rest = url
            entries = per_query_results.get(rest, [])
            return {"entries": entries, "_type": "playlist"}

    return _FakeYDL


def _run_script(monkeypatch, tmp_path, snapshot_clusters, per_query_results):
    """Execute the script with snapshot seeded + yt_dlp mocked.

    The script reads ``indexes/IMMUTABLE/curator_topic_snapshots/*.parquet``
    via a RELATIVE path, so we cd into ``tmp_path`` (which has the
    seeded layout) for the duration of the execute_test() call.
    """
    from scheduled_input_engine.executor import CodeExecutor

    immutable_root = tmp_path / "indexes" / "IMMUTABLE"
    _seed_snapshot(immutable_root, snapshot_clusters)

    spec = json.loads(SCRIPT_PATH.read_text())
    code = spec["code"]

    FakeYDL = _make_yt_dlp_mock(per_query_results)

    prev_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with unittest.mock.patch("yt_dlp.YoutubeDL", FakeYDL):
            executor = CodeExecutor(code, test_mode=True, trust_level="unrestricted")
            result = executor.execute_test()
    finally:
        os.chdir(prev_cwd)

    return result


# ── 1. JSON / metadata sanity ───────────────────────────────────────


def test_script_metadata_sanity():
    spec = json.loads(SCRIPT_PATH.read_text())
    assert spec["trust_level"] == "unrestricted"
    assert spec["suggested_subdirectory"] == "IMMUTABLE/curator_candidates"
    assert spec["requires_credentials"] == []
    assert spec["credential_kinds"] == {}
    # Slice 3b is the FIRST _pro curator candidate source; pin the
    # canonical 13-column schema verbatim in the code so a refactor
    # can't silently drop a column from the multi-source pool.
    for col in _CANONICAL_CANDIDATE_COLS:
        assert col in spec["code"], (
            f"script code missing canonical column {col!r} - "
            "candidate schema must stay aligned across all sources"
        )


def test_script_code_compiles():
    spec = json.loads(SCRIPT_PATH.read_text())
    compile(spec["code"], "<curator_topic_search_pull_pro>", "exec")


# ── 2. Happy path: real labels surface real candidates ──────────────


class TestHappyPath:
    def test_real_labels_drive_searches_and_emit_rows(self, tmp_path):
        clusters = [
            {
                "cluster_id": 0,
                "label": "Japanese Woodworking",
                "weight": 12.5,
                "n_members": 80,
                "exemplar_titles": ["Hand plane tuning", "Dovetail joinery"],
            },
            {
                "cluster_id": 1,
                "label": "Python Async Internals",
                "weight": 9.1,
                "n_members": 60,
                "exemplar_titles": ["asyncio internals", "event loops"],
            },
        ]
        per_query_results = {
            "Japanese Woodworking": [
                {
                    "id": "vid_jw_1",
                    "title": "Mastering the Japanese plane",
                    "channel_id": "UCwoodworks",
                    "channel": "WoodWorks Channel",
                    "channel_url": "https://www.youtube.com/channel/UCwoodworks",
                    "duration": 1234,
                    "description": "Watch a master tune a plane.",
                    "view_count": 50_000,
                    "live_status": None,
                    "ie_key": "Youtube",
                },
                {
                    "id": "vid_jw_2",
                    "title": "Hand-cut dovetails for beginners",
                    "channel_id": "UCwoodworks",
                    "channel": "WoodWorks Channel",
                    "channel_url": "https://www.youtube.com/channel/UCwoodworks",
                    "duration": 980,
                    "description": "Step-by-step dovetail tutorial.",
                    "view_count": 22_000,
                    "live_status": None,
                    "ie_key": "Youtube",
                },
            ],
            "Python Async Internals": [
                {
                    "id": "vid_py_1",
                    "title": "How asyncio actually works",
                    "channel_id": "UCpython",
                    "channel": "Python Talks",
                    "channel_url": "https://www.youtube.com/channel/UCpython",
                    "duration": 2400,
                    "description": "Deep dive into asyncio.",
                    "view_count": 100_000,
                    "live_status": None,
                    "ie_key": "Youtube",
                },
            ],
        }

        result = _run_script(None, tmp_path, clusters, per_query_results)
        assert result["status"] == "pass", result.get("errors")
        head = result["head"]
        assert len(head) == 3, (
            f"expected 3 rows (2 + 1), got {len(head)}: {[r.get('title') for r in head]}"
        )

        sources = {row["source"] for row in head}
        assert sources == {
            "topic_search:youtube:0",
            "topic_search:youtube:1",
        }, f"unexpected sources: {sources}"

        # 13-column canonical schema preserved
        cols_seen = set(result["columns"])
        missing = _CANONICAL_CANDIDATE_COLS - cols_seen
        assert not missing, f"missing canonical columns: {missing}"

        # Spot-check shape on a row
        jw_row = next(r for r in head if r["video_external_id"] == "vid_jw_1")
        assert jw_row["channel_name"] == "WoodWorks Channel"
        assert jw_row["duration_seconds"] == 1234
        assert jw_row["video_url"] == "https://www.youtube.com/watch?v=vid_jw_1"
        # raw_blob carries cluster attribution for downstream debugging
        blob = json.loads(jw_row["raw_blob"])
        assert blob["cluster_id"] == 0
        assert blob["cluster_label"] == "Japanese Woodworking"
        assert blob["search_query"] == "Japanese Woodworking"
        assert blob["view_count"] == 50_000

    def test_clusters_ordered_by_weight_descending(self, tmp_path):
        """Top-weight cluster is queried first; lower-weight comes later.

        The script LIMIT MAX_CLUSTERS_PER_RUN clusters by weight DESC.
        Set up an over-budget cluster set and pin which one's results
        appear: only the higher-weight cluster's results should land.
        """
        clusters = []
        for i in range(12):  # MAX_CLUSTERS_PER_RUN default is 8
            clusters.append({
                "cluster_id": i,
                "label": f"Topic {i}",
                "weight": float(100 - i),  # cluster 0 heaviest
                "n_members": 50,
                "exemplar_titles": [f"exemplar for {i}"],
            })

        per_query_results = {}
        for i in range(12):
            per_query_results[f"Topic {i}"] = [
                {
                    "id": f"vid_{i}_a",
                    "title": f"Result for topic {i}",
                    "channel_id": f"UC{i}",
                    "channel": f"Channel {i}",
                    "channel_url": f"https://www.youtube.com/channel/UC{i}",
                    "duration": 600,
                    "description": "",
                    "view_count": 1000,
                    "live_status": None,
                    "ie_key": "Youtube",
                }
            ]

        result = _run_script(None, tmp_path, clusters, per_query_results)
        assert result["status"] == "pass"
        # Total rows = 8 clusters × 1 result each. row_count is the
        # full count; ``head`` is df.head(5) per executor.execute_test.
        assert result["row_count"] == 8, (
            f"expected 8 rows (top-8-by-weight × 1 result), got {result['row_count']}"
        )
        sources_in_head = [r["source"] for r in result["head"]]
        # Whatever rows DID surface in head must come from top-8-by-weight (ids 0..7).
        top_8_expected = {f"topic_search:youtube:{i}" for i in range(8)}
        actual = set(sources_in_head)
        assert actual.issubset(top_8_expected), (
            f"head row from outside top-8: {actual - top_8_expected}"
        )
        # Bottom clusters (8-11) MUST NOT appear in the head sample
        for i in (8, 9, 10, 11):
            assert f"topic_search:youtube:{i}" not in actual, (
                f"low-weight cluster {i} leaked into the head sample"
            )

    def test_placeholder_label_falls_back_to_exemplar(self, tmp_path):
        """When the cluster label is a dry-run placeholder, the script
        searches by the first exemplar title instead. Without this
        fallback, a snapshot built with ``--dry-run-labels`` would
        produce zero candidates (every cluster query is empty).
        """
        clusters = [
            {
                "cluster_id": 5,
                "label": "Cluster 5 (dry-run)",
                "weight": 8.0,
                "n_members": 40,
                "exemplar_titles": ["specific exemplar title"],
            },
            {
                "cluster_id": 6,
                "label": "Cluster 6 (budget capped)",
                "weight": 7.0,
                "n_members": 30,
                "exemplar_titles": ["another specific exemplar"],
            },
            {
                "cluster_id": 7,
                "label": "Cluster 7",  # generic id-only placeholder
                "weight": 6.0,
                "n_members": 20,
                "exemplar_titles": ["third exemplar"],
            },
        ]
        per_query_results = {
            "specific exemplar title": [
                {
                    "id": "vid_exemplar_1",
                    "title": "Result from exemplar fallback",
                    "channel_id": "UCfallback",
                    "channel": "Fallback Channel",
                    "channel_url": "https://www.youtube.com/channel/UCfallback",
                    "duration": 500,
                    "description": "",
                    "view_count": 100,
                    "live_status": None,
                    "ie_key": "Youtube",
                },
            ],
            "another specific exemplar": [
                {
                    "id": "vid_exemplar_2",
                    "title": "Result from second exemplar",
                    "channel_id": "UCfallback2",
                    "channel": "Fallback Channel 2",
                    "channel_url": "https://www.youtube.com/channel/UCfallback2",
                    "duration": 300,
                    "description": "",
                    "view_count": 50,
                    "live_status": None,
                    "ie_key": "Youtube",
                },
            ],
            "third exemplar": [
                {
                    "id": "vid_exemplar_3",
                    "title": "Result from third exemplar",
                    "channel_id": "UCfallback3",
                    "channel": "Fallback Channel 3",
                    "channel_url": "https://www.youtube.com/channel/UCfallback3",
                    "duration": 200,
                    "description": "",
                    "view_count": 25,
                    "live_status": None,
                    "ie_key": "Youtube",
                },
            ],
        }

        result = _run_script(None, tmp_path, clusters, per_query_results)
        assert result["status"] == "pass"
        ids = {r["video_external_id"] for r in result["head"]}
        assert ids == {"vid_exemplar_1", "vid_exemplar_2", "vid_exemplar_3"}

    def test_yt_dlp_raises_falls_through_to_info_row(self, tmp_path):
        """When ``YoutubeDL.extract_info`` raises (rate limit, network,
        captcha), the script logs an error count and continues. Zero
        emitted rows produces the canonical info_row, NOT a crash and
        NOT an empty DataFrame.
        """
        clusters = [
            {
                "cluster_id": 0,
                "label": "Real Topic Label",
                "weight": 5.0,
                "n_members": 30,
                "exemplar_titles": ["t1"],
            },
        ]

        class _RaisingYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def extract_info(self, url, download=False):
                raise RuntimeError("rate limit (mock)")

        from scheduled_input_engine.executor import CodeExecutor
        spec = json.loads(SCRIPT_PATH.read_text())
        immutable_root = tmp_path / "indexes" / "IMMUTABLE"
        _seed_snapshot(immutable_root, clusters)

        prev_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            with unittest.mock.patch("yt_dlp.YoutubeDL", _RaisingYDL):
                executor = CodeExecutor(spec["code"], test_mode=True, trust_level="unrestricted")
                result = executor.execute_test()
        finally:
            os.chdir(prev_cwd)

        assert result["status"] == "pass", result.get("errors")
        head = result["head"]
        # Exactly one info_row, no candidate rows
        assert len(head) == 1
        assert head[0]["source"] == "topic_search_info"
        assert "1 yt-dlp errors" in head[0]["title"]


# ── 3. Snapshot schema contract: cross-source canonical-schema drift guard ─


class TestComposerFeederSlice3bCompat:
    """The composer's saved-search feeder must use a LEFT join (not
    inner) so slice 3b's topic-search candidates - which have no
    matching channel_id in watch_history - aren't silently dropped.
    Plus the append branch that guarantees slice 3b rows are
    represented even when slice 1.5 produces >100 high-interest rows.

    Caught 2026-05-17 during the slice 3a production-readiness audit
    BEFORE the bug manifested in a live composer fire. The original
    slice-2 feeder (committed in 9b5f8f1) used an inner join, which
    worked fine when slice 1.5 was the only source but would have
    silently filtered out every slice-3b row.

    Generalises to the rule: any feeder that joins against a
    user-history index MUST use type=left when there are multiple
    candidate sources, OR explicitly filter the candidate pool to
    only sources guaranteed to have history matches.
    """

    FEEDER_YAML_PATH = (
        Path(__file__).resolve().parent.parent
        / "default_saved_searches"
        / "curator_scored_candidates_today.yaml"
    )

    def _load_feeder_query(self):
        import yaml as _yaml
        with open(self.FEEDER_YAML_PATH) as f:
            return _yaml.safe_load(f)["query"]

    def test_feeder_uses_left_join_not_inner(self):
        """Inner join would silently drop slice 3b candidates whose
        channel_id has no watch_history match."""
        q = self._load_feeder_query()
        assert "join type=left" in q, (
            "Composer feeder uses an inner join - slice 3b candidates "
            "from never-watched channels will be silently dropped. "
            "Change to `| join type=left channel_id [...]`."
        )

    def test_feeder_has_append_branch_for_topic_search(self):
        """The append branch guarantees slice 3b rows reach the LLM
        even when slice 1.5 produces enough rows to fill the head N."""
        q = self._load_feeder_query()
        assert "topic_search:" in q, (
            "Composer feeder missing the slice-3b append branch. "
            "Without it, head N after sort by interest_score will drop "
            "all slice 3b rows (their preliminary interest_score=0)."
        )
        assert "append" in q, (
            "Composer feeder missing the `| append [...]` syntax for "
            "the slice 3b parallel branch."
        )

    def test_feeder_coalesces_null_watch_count(self):
        """After the LEFT join, slice 3b rows have null watch_count.
        Without coalesce, eval interest_score / growth_score produce
        null/NaN that confuses the composer LLM."""
        q = self._load_feeder_query()
        assert "isnull(watch_count)" in q, (
            "Composer feeder doesn't coalesce null watch_count for "
            "slice 3b rows. Add `if_(isnull(watch_count), ...)` to "
            "both interest_score and growth_score evals."
        )

    def test_feeder_dedups_video_external_id(self):
        """If the same video surfaces in both sources (subscribed
        channel + topic search), the append + dedup ensures one row.
        Without dedup, the composer would see duplicates."""
        q = self._load_feeder_query()
        assert "dedup video_external_id" in q, (
            "Composer feeder must `| dedup video_external_id` after "
            "append to handle the case where slice 1.5 and slice 3b "
            "both surface the same video."
        )

    def test_feeder_projects_source_column(self):
        """The composer prompt benefits from seeing the row's source
        (slice 1.5 RSS vs slice 3b topic-search) for attribution."""
        q = self._load_feeder_query()
        # Look for `source` in the final table projection (not just
        # in the where clauses)
        table_segment_start = q.rfind("| table ")
        assert table_segment_start != -1, "Feeder missing final | table projection"
        table_segment = q[table_segment_start:]
        assert "source" in table_segment, (
            "Composer feeder's table projection doesn't include "
            "the `source` column. The LLM needs it to attribute "
            "candidates back to slice 1.5 vs slice 3b."
        )


def test_emits_same_canonical_columns_as_slice_1_5():
    """Slice 1.5's curator_youtube_rss_pull and slice 3b's
    curator_topic_search_pull_pro MUST emit the exact same column set
    so the composer can read the entire candidate pool with one SPQL
    query. Pinned by reading both scripts and asserting their
    EXPECTED_COLUMNS lists match.

    The memory `reference_canonical_schema_for_multi_source_ingestion`
    is the design principle; this test is the enforcement.
    """
    scripts_dir = Path(__file__).resolve().parent.parent / "script_library" / "scripts"
    slice_1_5 = scripts_dir / "curator_youtube_rss_pull.json"
    slice_3b = SCRIPT_PATH
    # Slice 7 (2026-05-17): Archive.org joined the curator candidate
    # sources. Same canonical schema, different source string.
    slice_7 = scripts_dir / "curator_archive_org_pull.json"

    code_1_5 = json.loads(slice_1_5.read_text())["code"]
    code_3b = json.loads(slice_3b.read_text())["code"]
    code_7 = json.loads(slice_7.read_text())["code"]

    # All scripts declare EXPECTED_COLUMNS as a list literal. Extract
    # the unique column names (each is quoted in the literal).
    def _cols(code):
        # Find the EXPECTED_COLUMNS = [...] block (greedy across newlines).
        import re
        m = re.search(
            r"EXPECTED_COLUMNS\s*=\s*\[(.*?)\]", code, re.DOTALL,
        )
        assert m, "EXPECTED_COLUMNS list not found in script"
        body = m.group(1)
        return set(re.findall(r"'([^']+)'", body))

    cols_1_5 = _cols(code_1_5)
    cols_3b = _cols(code_3b)
    cols_7 = _cols(code_7)

    assert cols_1_5 == cols_3b == cols_7 == _CANONICAL_CANDIDATE_COLS, (
        "Curator candidate-row schemas drifted across sources!\n"
        f"  slice 1.5 (YouTube RSS):   {sorted(cols_1_5)}\n"
        f"  slice 3b  (yt-dlp search): {sorted(cols_3b)}\n"
        f"  slice 7   (Archive.org):   {sorted(cols_7)}\n"
        "Update ALL scripts + this test's _CANONICAL_CANDIDATE_COLS "
        "in lockstep. The composer reads the entire candidate pool "
        "via one SPQL `index=` query; one stale source breaks it."
    )
