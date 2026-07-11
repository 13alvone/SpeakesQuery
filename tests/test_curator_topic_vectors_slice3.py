"""Curator topic vectors - slice 3 tests.

Phase 6 / Bet 5 slice 3 (2026-05-16). Covers:

* **Topic vector unit tests** - :mod:`analyzers.topic_vectors`'s pure
  functions exercised against synthetic history + candidate frames.
  Recency weighting, KMeans clustering, cosine scoring, JSON
  serialisation round-trip.
* **Money-leak canary** - :func:`label_clusters_with_llm` with
  ``dry_run=True`` makes ZERO calls to :func:`analyzers.llm_router.call_llm`.
  Same defence-in-depth pattern as the slice-7 ``| llm`` budget gate
  + the slice-2 dispatcher dry-run canary.
* **IMMUTABLE schema additivity** - frozen column snapshot for
  ``curator_topic_snapshots`` so a future change can't silently drop
  a column from the forever-data tree.
* **Dispatcher hook drift guard** - :meth:`AlertGroupDispatcher._maybe_apply_topic_scoring`
  honours ``apply_topic_scoring`` AG flag, degrades gracefully on
  missing snapshots / scoring failures, returns df unchanged when
  ``df is None``.
* **Composer YAML + prompt drift guard** - the curator_playlist_composer
  AG ships with ``apply_topic_scoring: true`` AND the prompt mentions
  the topic-scoring semantics + 30% channel diversity cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest
import yaml


# ── 1. IMMUTABLE schema additivity (frozen-column drift guard) ───────

_CURATOR_TOPIC_SNAPSHOTS_FROZEN_COLS = {
    "_epoch",
    "snapshot_epoch",
    "snapshot_id",
    "model_name",
    "dim",
    "n_clusters",
    "n_history_rows",
    "decay_lambda_days",
    "cluster_id",
    "centroid_json",
    "weight",
    "n_members",
    "exemplar_titles_json",
    "label",
}


def test_curator_topic_snapshots_schema_is_additive_only():
    """Removing a column from curator_topic_snapshots breaks every
    historical SPQL query against the snapshot timeline. ADD columns
    additively; never remove them. Same rule as the OEB pick journal
    and the other curator_* schemas (memory:
    ``project_curator_vision_2026_05_16``).
    """
    from functionality.log_writer import SCHEMAS
    actual = set(SCHEMAS["curator_topic_snapshots"])
    missing = _CURATOR_TOPIC_SNAPSHOTS_FROZEN_COLS - actual
    assert not missing, (
        f"curator_topic_snapshots schema dropped frozen column(s): "
        f"{sorted(missing)}. IMMUTABLE schemas are additive-only "
        "(CLAUDE.md Do Not pin)."
    )


def test_curator_topic_snapshots_routes_to_immutable():
    """The protected-from-cleanup property is what makes the
    topic-evolution timeline ('what was the user into 6 months ago?')
    actually durable for the life-project horizon.
    """
    from functionality.log_writer import IMMUTABLE_CATEGORIES
    assert "curator_topic_snapshots" in IMMUTABLE_CATEGORIES


# ── 2. Topic vectors unit tests ─────────────────────────────────────


def _make_synthetic_history(n_rows: int = 60) -> pd.DataFrame:
    """Three obvious topical clusters (cooking / woodworking / coding)."""
    cooking = [
        "Pasta carbonara from scratch",
        "Sourdough starter day by day",
        "Knife skills basics",
        "Brown butter sage gnocchi",
        "Pressure cooker bone broth",
        "Cast iron seasoning tutorial",
        "Tempering chocolate at home",
        "Three-day osso buco",
        "Lacto-fermented hot sauce",
        "Korean kimchi from cabbage",
        "Espresso latte art beginners",
        "Sushi rice technique",
        "Beef wellington at home",
        "Pizza dough cold ferment",
        "Stock-making basics",
        "Mochi from scratch",
        "Wagyu searing tips",
        "Bread scoring practice",
        "Pasta water emulsion",
        "Knife sharpening on whetstones",
    ]
    woodworking = [
        "Japanese hand plane tuning",
        "Dovetail joinery by hand",
        "Mortise and tenon basics",
        "Resawing on a band saw",
        "Shellac French polish",
        "Workbench from rough lumber",
        "Sharpening chisels on water stones",
        "Bowsaw vs frame saw",
        "Hand-cut tenons technique",
        "Box joints on a router table",
        "Veneering with hide glue",
        "Cabinet face frame assembly",
        "Drawer slip joinery",
        "Lathe tool sharpening jig",
        "Steam bending wooden parts",
        "Hand-tooled leather workshop",
        "Tablesaw push stick design",
        "Marquetry inlay technique",
        "Hand chisel mortise method",
        "Roubo bench leg vise",
    ]
    coding = [
        "Rust async runtime explained",
        "Python decorator deep dive",
        "Building a database from scratch",
        "Functional programming in Haskell",
        "Distributed consensus algorithms",
        "WebAssembly in production",
        "Compiler error messages design",
        "Garbage collection internals",
        "Rust lifetimes from first principles",
        "Python asyncio internals",
        "TCP/IP packet inspection",
        "Building a Lisp interpreter",
        "GraphQL schema design patterns",
        "Operating systems virtual memory",
        "Concurrency in modern Go",
        "Optimising SQL query plans",
        "WebRTC peer-to-peer",
        "Linux kernel module dev",
        "Building a HTTP/3 server",
        "Type theory for working programmers",
    ]
    titles = (cooking + woodworking + coding)[:n_rows]
    now = 1_700_000_000  # fixed epoch for determinism
    # Spread epochs over the last year so recency weighting has signal.
    epochs = [now - (i * 86400 * 30 // max(1, n_rows)) for i in range(len(titles))]
    return pd.DataFrame({
        "_epoch": epochs,
        "title": titles,
    })


@pytest.fixture(scope="module")
def synthetic_history_df():
    return _make_synthetic_history()


class TestComputeTopicSnapshot:
    def test_returns_well_formed_snapshot(self, synthetic_history_df):
        from analyzers.topic_vectors import compute_topic_snapshot
        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=3, decay_lambda_days=180.0,
            now_epoch=1_700_000_000, random_state=0,
        )
        assert snap.n_clusters >= 1
        assert snap.n_clusters <= 3
        assert snap.n_history_rows == len(synthetic_history_df.index)
        assert snap.dim > 0
        assert snap.model_name  # non-empty
        # Every cluster has a unit-norm centroid (within float32 epsilon).
        for c in snap.clusters:
            norm = float(np.linalg.norm(c.centroid))
            assert abs(norm - 1.0) < 1e-3, (
                f"cluster {c.cluster_id} centroid not unit-norm: {norm}"
            )
            assert c.n_members >= 1
            assert c.weight >= 0.0
            assert isinstance(c.exemplar_titles, list)
            assert len(c.exemplar_titles) >= 1

    def test_raises_on_empty_history(self):
        from analyzers.topic_vectors import (
            compute_topic_snapshot, TopicVectorsError,
        )
        with pytest.raises(TopicVectorsError) as excinfo:
            compute_topic_snapshot(pd.DataFrame(), n_clusters=3)
        assert excinfo.value.error_class == "EmptyHistory"

    def test_raises_on_missing_column(self):
        from analyzers.topic_vectors import (
            compute_topic_snapshot, TopicVectorsError,
        )
        df = pd.DataFrame({"_epoch": [1, 2, 3]})  # no "title"
        with pytest.raises(TopicVectorsError) as excinfo:
            compute_topic_snapshot(df, n_clusters=2)
        assert excinfo.value.error_class == "MissingColumn"

    def test_caps_n_clusters_at_history_length(self):
        from analyzers.topic_vectors import compute_topic_snapshot
        # 4 rows requesting K=20 - KMeans would blow up without a cap.
        df = pd.DataFrame({
            "_epoch": [1_700_000_000] * 4,
            "title": ["a", "b", "c", "d"],
        })
        snap = compute_topic_snapshot(df, n_clusters=20)
        assert snap.n_clusters <= 4


class TestScoreCandidates:
    def test_appends_score_columns(self, synthetic_history_df):
        from analyzers.topic_vectors import (
            compute_topic_snapshot, score_candidates_against_snapshot,
        )
        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=3, now_epoch=1_700_000_000,
            random_state=0,
        )
        candidates = pd.DataFrame({
            "title": [
                "Pasta dough technique",
                "Rust ownership rules",
                "Hand plane sharpening jig",
            ],
            "channel_name": ["ChefA", "RustyDev", "WoodWorks"],
        })
        scored = score_candidates_against_snapshot(candidates, snap)
        for col in (
            "interest_score", "topic_cluster_id",
            "topic_label", "topic_similarity",
        ):
            assert col in scored.columns
        assert (scored["interest_score"] >= -1.0).all()
        assert (scored["interest_score"] <= 1.0).all()
        # Original columns preserved
        assert list(scored["channel_name"]) == ["ChefA", "RustyDev", "WoodWorks"]

    def test_empty_candidates_returns_empty_schema(self, synthetic_history_df):
        from analyzers.topic_vectors import (
            compute_topic_snapshot, score_candidates_against_snapshot,
        )
        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=2, now_epoch=1_700_000_000,
            random_state=0,
        )
        scored = score_candidates_against_snapshot(
            pd.DataFrame({"title": []}), snap,
        )
        for col in (
            "interest_score", "topic_cluster_id",
            "topic_label", "topic_similarity",
        ):
            assert col in scored.columns
        assert len(scored.index) == 0


class TestSerializationRoundTrip:
    def test_records_round_trip(self, synthetic_history_df):
        from analyzers.topic_vectors import (
            compute_topic_snapshot,
            snapshot_to_records,
            records_to_snapshot,
        )
        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=3, now_epoch=1_700_000_000,
            random_state=0,
        )
        # Add labels so we test the label field round-trip too.
        for c in snap.clusters:
            c.label = f"cluster-{c.cluster_id}-label"
        records = snapshot_to_records(snap)
        assert len(records) == len(snap.clusters)
        # JSON-encoded fields parse cleanly
        for r in records:
            assert isinstance(json.loads(r["centroid_json"]), list)
            assert isinstance(json.loads(r["exemplar_titles_json"]), list)
        reloaded = records_to_snapshot(records)
        assert reloaded.snapshot_id == snap.snapshot_id
        assert reloaded.snapshot_epoch == snap.snapshot_epoch
        assert reloaded.n_clusters == snap.n_clusters
        assert len(reloaded.clusters) == len(snap.clusters)
        for orig, reloaded_c in zip(snap.clusters, reloaded.clusters):
            assert orig.cluster_id == reloaded_c.cluster_id
            assert orig.label == reloaded_c.label
            np.testing.assert_allclose(
                orig.centroid, reloaded_c.centroid, rtol=1e-5,
            )


class TestCleanLabel:
    def test_strips_think_block(self):
        from analyzers.topic_vectors import _clean_label
        raw = "<think>let me think about this</think>\nJapanese Woodworking"
        assert _clean_label(raw) == "Japanese Woodworking"

    def test_handles_multiline_response(self):
        from analyzers.topic_vectors import _clean_label
        raw = "Python Async Internals\n\nExtra commentary that should be dropped."
        assert _clean_label(raw) == "Python Async Internals"

    def test_strips_quotes(self):
        from analyzers.topic_vectors import _clean_label
        assert _clean_label('"Cooking Fundamentals"') == "Cooking Fundamentals"
        assert _clean_label("`Rust Lifetimes`") == "Rust Lifetimes"

    def test_empty_input(self):
        from analyzers.topic_vectors import _clean_label
        assert _clean_label("") == ""
        assert _clean_label(None) == ""

    def test_caps_label_length(self):
        from analyzers.topic_vectors import _clean_label
        long = "Topic " * 50
        cleaned = _clean_label(long)
        assert len(cleaned) <= 80


# ── 3. MONEY-LEAK CANARY ───────────────────────────────────────────


class TestLabelClustersMoneyLeakCanary:
    """**Critical**: ``label_clusters_with_llm(dry_run=True)`` makes
    ZERO calls to :func:`analyzers.llm_router.call_llm`. Same defence-
    in-depth pattern as :class:`TestDryRunMoneyLeakCanary` in
    ``tests/test_curator_composer_slice2.py``.

    If a future refactor accidentally bypasses the ``dry_run`` short-
    circuit (a misplaced conditional, a try/except that swallows the
    early return, etc.), this test fails loudly BEFORE the user pays
    for a label-pass they explicitly asked to preview.
    """

    def test_dry_run_makes_zero_call_llm_invocations(self, synthetic_history_df):
        from analyzers.topic_vectors import (
            compute_topic_snapshot, label_clusters_with_llm,
        )
        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=3, now_epoch=1_700_000_000,
            random_state=0,
        )

        # Sentinel - raise loudly on any call_llm invocation. The
        # dry-run path must NEVER reach the router.
        sentinel = MagicMock(
            side_effect=AssertionError(
                "MONEY LEAK: label_clusters_with_llm(dry_run=True) called "
                "analyzers.llm_router.call_llm - the dry-run short-circuit "
                "was bypassed. See test_curator_topic_vectors_slice3."
            )
        )
        with patch("analyzers.llm_router.call_llm", sentinel):
            result = label_clusters_with_llm(
                snap, dry_run=True, model_id="llamacpp-qwen3-32b-q4km",
            )

        sentinel.assert_not_called()
        # Placeholder labels still get written so the snapshot stays
        # internally consistent.
        for c in result.clusters:
            assert "dry-run" in c.label.lower()

    def test_max_cost_capped_skips_remaining_clusters(self, synthetic_history_df):
        """``max_cost_usd`` is a cumulative-cost ceiling. Once it's
        crossed, remaining clusters get a placeholder, not another
        billable LLM call.

        This is the secondary money-leak gate - for callers who can't
        use ``dry_run`` (because they actually want labels for SOME
        clusters) but want a hard ceiling. Mirrors the slice-7 budget
        cap on the ``| llm`` pipe.
        """
        from analyzers.topic_vectors import (
            compute_topic_snapshot, label_clusters_with_llm,
        )
        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=4, now_epoch=1_700_000_000,
            random_state=0,
        )

        # Each fake call "costs" $0.10. Cap at $0.15 → first call
        # succeeds, second tips us over, third+ get placeholders.
        call_count = {"n": 0}

        def fake_call(model_id, **kwargs):
            call_count["n"] += 1
            return SimpleNamespace(
                text=f"Topic {call_count['n']}",
                model_id=model_id,
                provider="lmstudio",
                model_name="fake",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.10,
                latency_ms=42,
                request_id="rid",
                raw_response=None,
            )

        with patch(
            "analyzers.llm_router.call_llm", side_effect=fake_call,
        ) as call_mock:
            label_clusters_with_llm(
                snap,
                model_id="llamacpp-qwen3-32b-q4km",
                max_cost_usd=0.15,
            )

        # First call costs $0.10 → cumulative $0.10 < $0.15 (OK).
        # Second call costs $0.10 → cumulative $0.20 >= $0.15 → caps.
        # Third and beyond: budget-capped placeholders, no call.
        assert call_mock.call_count <= 2
        # At least one cluster ended up with a budget-capped placeholder.
        budget_capped = sum(
            1 for c in snap.clusters if "budget capped" in c.label.lower()
        )
        assert budget_capped >= 1


# ── 4. Dispatcher hook drift guards ─────────────────────────────────


class TestDispatcherTopicScoringHook:
    """The :meth:`AlertGroupDispatcher._maybe_apply_topic_scoring` hook
    is the surface that determines whether a given AG fire benefits
    from topic-similarity scoring. Pin its contract:

    * Flag off → df unchanged.
    * df is None → df unchanged (no crash).
    * No snapshot persisted → df unchanged + warn (graceful).
    * Snapshot present + flag on → df gains the score columns.
    """

    def test_flag_off_returns_df_unchanged(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        df = pd.DataFrame({"title": ["a"], "x": [1]})
        out = AlertGroupDispatcher._maybe_apply_topic_scoring(
            df, group={}, group_name="test",
        )
        assert out is df  # Same object, not just equal
        # Or at minimum, no new columns appeared.
        assert set(out.columns) == {"title", "x"}

    def test_df_none_returns_none(self):
        from alert_groups.dispatcher import AlertGroupDispatcher
        out = AlertGroupDispatcher._maybe_apply_topic_scoring(
            None, group={"apply_topic_scoring": True}, group_name="test",
        )
        assert out is None

    def test_no_snapshot_falls_through_gracefully(self):
        """When ``load_latest_snapshot()`` returns None, the hook
        leaves df unchanged and emits a warning. The composer can
        still produce output - the prompt is written to tolerate
        unscored rows.
        """
        from alert_groups.dispatcher import AlertGroupDispatcher
        df = pd.DataFrame({"title": ["a"], "x": [1]})
        with patch(
            "analyzers.topic_vectors.load_latest_snapshot",
            return_value=None,
        ):
            out = AlertGroupDispatcher._maybe_apply_topic_scoring(
                df, group={"apply_topic_scoring": True}, group_name="test",
            )
        assert set(out.columns) == {"title", "x"}

    def test_snapshot_present_appends_score_columns(self, synthetic_history_df):
        """End-to-end happy path: snapshot exists, flag is on, hook
        produces the augmented DataFrame.
        """
        from alert_groups.dispatcher import AlertGroupDispatcher
        from analyzers.topic_vectors import compute_topic_snapshot

        snap = compute_topic_snapshot(
            synthetic_history_df, n_clusters=3, now_epoch=1_700_000_000,
            random_state=0,
        )
        df = pd.DataFrame({
            "title": ["Pasta dough technique", "Rust async deep dive"],
            "channel_name": ["ChefA", "RustyDev"],
        })

        with patch(
            "analyzers.topic_vectors.load_latest_snapshot",
            return_value=snap,
        ):
            out = AlertGroupDispatcher._maybe_apply_topic_scoring(
                df, group={"apply_topic_scoring": True}, group_name="test",
            )

        assert "interest_score" in out.columns
        assert "topic_cluster_id" in out.columns
        assert "topic_label" in out.columns
        assert "topic_similarity" in out.columns
        # Original cols intact
        assert list(out["channel_name"]) == ["ChefA", "RustyDev"]


# ── 5. Composer YAML + prompt drift guard ───────────────────────────


def _composer_yaml_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "default_alert_groups"
        / "curator_playlist_composer.yaml"
    )


class TestComposerYamlDrift:
    """The curator_playlist_composer is the FIRST consumer of the
    slice-3 topic-scoring hook. Pin its config so a future edit can't
    silently revert to slice-2 behaviour.
    """

    def test_apply_topic_scoring_true(self):
        with open(_composer_yaml_path()) as f:
            ag = yaml.safe_load(f)
        assert ag["apply_topic_scoring"] is True, (
            "curator_playlist_composer must opt in to slice-3 topic "
            "scoring via apply_topic_scoring: true."
        )

    def test_output_kind_still_playlist(self):
        """Slice-2 routing contract - output_kind drives the dispatcher's
        per-AG parse/log/extract path."""
        with open(_composer_yaml_path()) as f:
            ag = yaml.safe_load(f)
        assert ag["output_kind"] == "playlist"

    def test_dry_run_still_true(self):
        """Slice-2 default: ship dry_run=true so a fresh deploy
        doesn't immediately spend on the first AG fire. The operator
        flips after eyeballing one dry run."""
        with open(_composer_yaml_path()) as f:
            ag = yaml.safe_load(f)
        assert ag["dry_run"] is True

    def test_prompt_explains_topic_scoring(self):
        """The new interest_score semantics must be documented in the
        prompt so the LLM understands cosine-similarity-to-centroid
        and doesn't fall back to legacy watch-count reasoning.
        """
        with open(_composer_yaml_path()) as f:
            ag = yaml.safe_load(f)
        prompt = ag["prompt_text"]
        for marker in (
            "topic_cluster_id",
            "topic_label",
            "topic-driven",
        ):
            assert marker in prompt, (
                f"composer prompt missing required marker {marker!r} - "
                "slice 3 topic-scoring documentation absent"
            )

    def test_prompt_includes_diversity_cap(self):
        """The 30% single-channel cap is the anti-bootstrap-bias
        instruction. Without it, the LLM defaults to the highest-
        weight channels regardless of cluster diversity.
        """
        with open(_composer_yaml_path()) as f:
            ag = yaml.safe_load(f)
        prompt = ag["prompt_text"]
        assert "30%" in prompt, (
            "composer prompt must include the 30% single-channel cap - "
            "the anti-bootstrap-bias mechanism for slice 3."
        )


# ── 6. AlertGroupStore allowlist drift guard ────────────────────────


class TestCLIRealTakeoutSchema:
    """Regression: the snapshot-refresh CLI must successfully read a
    watch_history parquet written by ``tools/curator_takeout_import.py``,
    whose canonical schema is
    ``['_epoch', 'video_id', 'video_url', 'video_title', 'channel_id',
       'channel_url', 'channel_name', 'watched_iso', 'tz_abbrev']``
    (note: ``video_title``, NOT ``title``).

    Caught 2026-05-16 on the user's first bootstrap attempt - the CLI
    called ``compute_topic_snapshot`` without specifying ``title_col``,
    so the module default ``'title'`` raised ``MissingColumn`` on the
    real schema. Fix: CLI now defaults ``--title-col=video_title``.

    These tests pin the integration so a future module-API drift or a
    well-meaning CLI refactor can't re-break the bootstrap path
    silently. Synthetic ``title``-column tests above stay too because
    the module's generic default is still load-bearing for callers
    that DO use ``title`` (test fixtures, future Phase 7 readers,
    etc.).
    """

    TAKEOUT_HISTORY_COLS = [
        "_epoch", "video_id", "video_url", "video_title",
        "channel_id", "channel_url", "channel_name",
        "watched_iso", "tz_abbrev",
    ]

    def _make_takeout_history_df(self, n_rows: int = 30):
        """Synthetic frame matching the real Takeout importer schema."""
        topics = [
            "Pasta carbonara from scratch",
            "Sourdough starter day by day",
            "Hand plane sharpening jig",
            "Dovetail joinery basics",
            "Python decorator deep dive",
            "Rust ownership rules",
        ]
        titles = (topics * ((n_rows // len(topics)) + 1))[:n_rows]
        now = 1_700_000_000
        rows = []
        for i, t in enumerate(titles):
            rows.append({
                "_epoch": now - i * 86400,
                "video_id": f"vid_{i:03d}",
                "video_url": f"https://www.youtube.com/watch?v=vid_{i:03d}",
                "video_title": t,
                "channel_id": f"UC{i % 5:02d}",
                "channel_url": f"https://www.youtube.com/channel/UC{i % 5:02d}",
                "channel_name": f"Channel {i % 5}",
                "watched_iso": "2026-05-16T09:00:00-07:00",
                "tz_abbrev": "PDT",
            })
        return pd.DataFrame(rows, columns=self.TAKEOUT_HISTORY_COLS)

    def test_compute_topic_snapshot_accepts_video_title_col(self):
        """The module function MUST work when called with the explicit
        ``title_col='video_title'`` argument the curator CLI passes.
        """
        from analyzers.topic_vectors import compute_topic_snapshot
        df = self._make_takeout_history_df()
        snap = compute_topic_snapshot(
            df,
            title_col="video_title",
            epoch_col="_epoch",
            n_clusters=3,
            now_epoch=1_700_000_000,
            random_state=0,
        )
        assert snap.n_clusters >= 1
        assert snap.n_history_rows == len(df.index)

    def test_cli_defaults_to_video_title(self, tmp_path):
        """End-to-end pin: invoke ``tools.curator_topic_snapshot_refresh``'s
        ``main()`` with NO ``--title-col`` flag, against a parquet that
        has the real Takeout schema. Must succeed.

        Without the slice-3a hotfix, this test fails with exit code 1
        and a ``MissingColumn`` error log. With the fix, exit code is 0.
        """
        from tools.curator_topic_snapshot_refresh import main

        history_dir = tmp_path / "watch_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        self._make_takeout_history_df().to_parquet(
            history_dir / "test_history.parquet", index=False,
        )

        # Skip labeling to avoid an LLM call in tests; skip writing
        # so we don't touch real IMMUTABLE state.
        exit_code = main([
            "--history-root", str(history_dir),
            "--no-labels",
            "--no-write",
        ])
        assert exit_code == 0, (
            "snapshot-refresh CLI defaulted to title_col='title' against "
            "a Takeout-schema parquet - slice-3a hotfix regressed."
        )

    def test_cli_title_col_override_still_works(self, tmp_path):
        """The ``--title-col`` flag is the documented escape hatch for
        non-default schemas. Pin that it works for a custom column name.
        """
        from tools.curator_topic_snapshot_refresh import main

        history_dir = tmp_path / "history"
        history_dir.mkdir(parents=True, exist_ok=True)

        # Synthetic frame using a CUSTOM column name 'headline'.
        base_titles = [
            "Pasta carbonara from scratch",
            "Sourdough starter day by day",
            "Hand plane sharpening jig",
            "Dovetail joinery basics",
            "Python decorator deep dive",
            "Rust ownership rules",
        ]
        n = 24  # 4× cycle of base_titles
        headlines = (base_titles * 4)[:n]
        df = pd.DataFrame({
            "_epoch": [1_700_000_000 - i * 3600 for i in range(n)],
            "headline": headlines,
        })
        df.to_parquet(history_dir / "custom_history.parquet", index=False)

        exit_code = main([
            "--history-root", str(history_dir),
            "--title-col", "headline",
            "--no-labels",
            "--no-write",
        ])
        assert exit_code == 0, (
            "--title-col=headline didn't override the default - CLI bug."
        )

    def test_cli_flushes_log_writer_before_exit(self):
        """The CLI MUST call ``functionality.log_writer.flush_all()`` after
        emitting cluster rows.

        Caught 2026-05-17 on production: the CLI's first real run wrote
        10 cluster rows successfully (per its own success log) but the
        parquets NEVER appeared on disk. Root cause: log_writer buffers
        in-process; the periodic flush thread runs in the SAME process
        as the buffer. When the CLI's fresh ``python -m tools.X``
        process exits, the flush thread is killed before it gets a
        chance to write the buffered rows. The engine's flush thread
        is in a DIFFERENT process (the Flask server) and never sees
        the CLI's buffer.

        Symptom: snapshot bootstrap reports success; SPQL queries
        against ``indexes/IMMUTABLE/curator_topic_snapshots/*`` return
        zero rows; slice 3b ingestion can't find the snapshot and
        falls through to its empty-snapshot info_row.

        Source-string pin (not behavioural test, but cheap and
        sufficient): the CLI module must import + call `flush_all`.
        """
        from pathlib import Path as _Path
        src = (
            _Path(__file__).resolve().parent.parent
            / "tools" / "curator_topic_snapshot_refresh.py"
        ).read_text()
        assert "flush_all" in src, (
            "tools/curator_topic_snapshot_refresh.py doesn't import "
            "or call flush_all() - buffered cluster rows will be "
            "lost when the CLI process exits. Add "
            "`from functionality.log_writer import flush_all` and "
            "call flush_all() after the cluster-row emission loop."
        )
        # Also confirm the call site is in the write path
        assert (
            "flush_all()" in src
        ), "flush_all is imported but never called - fix the CLI's write path."

    def test_failure_log_includes_available_columns_hint(self, tmp_path, caplog):
        """When the CLI's title_col is wrong, the error log surfaces the
        actual columns so the operator can copy-paste the right name.

        Diagnostic UX: caught 2026-05-16 - the user's first failure
        showed only ``missing column 'title'`` without listing what WAS
        in the parquet. The CLI now logs the available columns on
        MissingColumn errors so the operator never has to read source
        code to find the fix.
        """
        import logging
        from tools.curator_topic_snapshot_refresh import main

        history_dir = tmp_path / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame({
            "_epoch": [1_700_000_000] * 5,
            "video_title": ["t"] * 5,  # real Takeout column
        })
        df.to_parquet(history_dir / "h.parquet", index=False)

        with caplog.at_level(logging.ERROR):
            exit_code = main([
                "--history-root", str(history_dir),
                "--title-col", "this_column_does_not_exist",
                "--no-labels",
                "--no-write",
            ])
        assert exit_code == 1
        msg = "\n".join(rec.message for rec in caplog.records)
        assert "Available columns" in msg, (
            "MissingColumn error didn't surface available-columns hint - "
            "operator can't diagnose without reading source."
        )
        assert "video_title" in msg


class TestAlertGroupStoreAllowlist:
    """``apply_topic_scoring`` must be in the ``update_group`` updatable
    allowlist or a PUT via the API silently drops the field - same
    failure mode caught for slice-2's ``dry_run`` + ``output_kind``.
    """

    def test_apply_topic_scoring_is_updatable(self, tmp_path, monkeypatch):
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store._alert_groups_dir = tmp_path
        store._defaults_dir = tmp_path / "missing"  # no seed
        tmp_path.mkdir(exist_ok=True)
        # Create a minimal AG so update_group has something to update.
        store.save_group({
            "name": "ts_test",
            "description": "x",
            "search_names": ["a"],
            "prompt_text": "p",
            "schedule": "0 5 * * *",
            "max_rows": 10,
            "email_address": "",
            "output_kind": "playlist",
            "apply_topic_scoring": False,
        }, overwrite=True)
        updated = store.update_group("ts_test", {"apply_topic_scoring": True})
        assert updated["apply_topic_scoring"] is True, (
            "AlertGroupStore.update_group dropped apply_topic_scoring - "
            "add it to the updatable allowlist."
        )

    def test_topic_scoring_title_col_is_updatable(self, tmp_path):
        from alert_group_store import AlertGroupStore
        store = AlertGroupStore()
        store._alert_groups_dir = tmp_path
        store._defaults_dir = tmp_path / "missing2"
        tmp_path.mkdir(exist_ok=True)
        store.save_group({
            "name": "ts_test2",
            "description": "x",
            "search_names": ["a"],
            "prompt_text": "p",
            "schedule": "0 5 * * *",
            "max_rows": 10,
            "email_address": "",
            "apply_topic_scoring": True,
        }, overwrite=True)
        updated = store.update_group(
            "ts_test2", {"topic_scoring_title_col": "video_title"},
        )
        assert updated.get("topic_scoring_title_col") == "video_title", (
            "AlertGroupStore.update_group dropped topic_scoring_title_col - "
            "add it to the updatable allowlist."
        )
