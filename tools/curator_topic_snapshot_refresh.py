"""Curator topic snapshot refresh CLI - Phase 6 / Bet 5 slice 3.

One-shot tool that reads the user's watch history, computes a fresh
topic snapshot, optionally labels clusters via an LLM, and writes the
snapshot to ``indexes/IMMUTABLE/curator_topic_snapshots/*.parquet``.

Same code path the engine-scheduled refresh job uses (see
:meth:`scheduled_input_engine.engine.ScheduledInputEngine._run_topic_snapshot_refresh`),
so behaviour is identical. Three reasons to invoke the CLI directly
rather than waiting for the schedule:

* **Bootstrap.** Right after a Takeout import there's no snapshot yet
  and the composer's topic-similarity scoring has nothing to score
  against. ``python -m tools.curator_topic_snapshot_refresh`` builds
  the first snapshot immediately.
* **Tuning.** Iterating on ``--n-clusters``, ``--decay-lambda-days``,
  or ``--no-labels`` to find the right grain for your history is
  faster from the CLI than from Settings + cron wait.
* **Dry-run preview.** ``--dry-run-labels`` skips the LLM call and
  stamps placeholder labels - same money-leak gate the engine path
  honours, useful for previewing centroids before spending.

Usage::

    python -m tools.curator_topic_snapshot_refresh
    python -m tools.curator_topic_snapshot_refresh --n-clusters 12
    python -m tools.curator_topic_snapshot_refresh --no-labels
    python -m tools.curator_topic_snapshot_refresh --dry-run-labels --json
    python -m tools.curator_topic_snapshot_refresh --label-model claude-haiku-4-5-20251001

Exit codes
----------
``0`` - snapshot computed and written successfully (or no history yet).
``1`` - fatal error (missing history, embedder load failure, etc.).

Output is a human-readable cluster summary by default; ``--json``
emits the full snapshot serialisation on stdout (useful for piping).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _resolve_history_root(arg_root: Optional[str]) -> Path:
    """Find the watch_history parquet root.

    Default: ``<indexes>/IMMUTABLE/curator_takeout/watch_history``.
    Override with ``--history-root``.
    """
    if arg_root:
        return Path(arg_root).resolve()
    try:
        from global_settings import get_settings
        indexes = get_settings().indexes_dir()
    except Exception:
        indexes = Path(__file__).resolve().parent.parent / "indexes"
    return (indexes / "IMMUTABLE" / "curator_takeout" / "watch_history").resolve()


def _load_history(root: Path):
    """Concat all watch_history parquets under ``root`` into one DataFrame."""
    import pandas as pd

    if not root.exists():
        return pd.DataFrame()
    parquets = sorted(root.glob("*.parquet"))
    if not parquets:
        return pd.DataFrame()
    frames = []
    for p in parquets:
        try:
            frames.append(pd.read_parquet(p))
        except Exception as exc:
            logging.warning("[!] Could not read %s: %s - skipping", p, exc)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _print_human(snapshot, *, model_used_for_labels: str) -> None:
    print()
    print(f"Curator topic snapshot - {snapshot.snapshot_id}")
    print(f"  embedder:         {snapshot.model_name}  (dim={snapshot.dim})")
    print(f"  history rows:     {snapshot.n_history_rows}")
    print(f"  clusters:         {snapshot.n_clusters}")
    print(f"  decay lambda:     {snapshot.decay_lambda_days:.1f} days")
    if model_used_for_labels:
        print(f"  label model:      {model_used_for_labels}")
    print()
    print("Clusters (by recency-weighted importance):")
    sorted_clusters = sorted(
        snapshot.clusters, key=lambda c: c.weight, reverse=True,
    )
    for c in sorted_clusters:
        label = c.label or "(unlabeled)"
        print(
            f"  [{c.cluster_id:2d}]  weight={c.weight:6.2f}  "
            f"n={c.n_members:4d}  {label}"
        )
        for t in c.exemplar_titles[:3]:
            shown = t[:78].replace("\n", " ")
            print(f"          · {shown}")


def _print_json(snapshot) -> None:
    from analyzers.topic_vectors import snapshot_to_records
    json.dump(snapshot_to_records(snapshot), sys.stdout, indent=2, default=str)
    print()


def _write_snapshot_to_log(snapshot) -> int:
    """Emit one log row per cluster via the IMMUTABLE writer, then flush.

    Caught 2026-05-17 on the user's first bootstrap: log_writer buffers
    writes in-process and relies on a background thread to flush
    periodically. When the CLI process exits, that thread doesn't get
    to flush before the kill, so the buffered rows are LOST. The
    engine's flush thread is in a DIFFERENT process (the Flask server)
    and never sees the CLI's buffer. ALL `tools/*.py` CLIs that emit
    via log_writer MUST call `flush_all()` before returning - same
    discipline as test fixtures.
    """
    from functionality.log_writer import log_curator_topic_cluster, flush_all
    from analyzers.topic_vectors import snapshot_to_records

    rows = snapshot_to_records(snapshot)
    for r in rows:
        log_curator_topic_cluster(
            snapshot_epoch=r["snapshot_epoch"],
            snapshot_id=r["snapshot_id"],
            model_name=r["model_name"],
            dim=r["dim"],
            n_clusters=r["n_clusters"],
            n_history_rows=r["n_history_rows"],
            decay_lambda_days=r["decay_lambda_days"],
            cluster_id=r["cluster_id"],
            centroid_json=r["centroid_json"],
            weight=r["weight"],
            n_members=r["n_members"],
            exemplar_titles_json=r["exemplar_titles_json"],
            label=r["label"],
        )
    # MUST flush before returning - the CLI process exits immediately
    # after main() returns, killing any buffered-but-unwritten rows
    # before the periodic flush thread gets a chance. Without this
    # call the snapshot rows are silently lost: log_curator_topic_cluster
    # reports success but the parquet never lands on disk. Pinned by
    # tests/test_curator_topic_vectors_slice3.py::TestCLIRealTakeoutSchema.
    flush_all()
    return len(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="curator_topic_snapshot_refresh",
        description=(
            "Compute a fresh curator topic snapshot from watch history and "
            "write it to indexes/IMMUTABLE/curator_topic_snapshots/."
        ),
    )
    parser.add_argument(
        "--history-root",
        help=(
            "Override watch_history parquet root. Default: "
            "<indexes>/IMMUTABLE/curator_takeout/watch_history."
        ),
    )
    parser.add_argument(
        "--n-clusters", type=int, default=None,
        help=(
            "Target number of clusters. Default: curator_topic_n_clusters "
            "setting (default 10). Capped at len(history)."
        ),
    )
    parser.add_argument(
        "--decay-lambda-days", type=float, default=None,
        help=(
            "Recency half-life in days. Default: "
            "curator_topic_decay_lambda_days setting (default 180)."
        ),
    )
    parser.add_argument(
        "--no-labels", action="store_true",
        help=(
            "Skip the LLM labeling pass entirely. Cluster ids only, "
            "labels stay empty in the snapshot."
        ),
    )
    parser.add_argument(
        "--dry-run-labels", action="store_true",
        help=(
            "Label-pass dry run: no LLM calls, placeholder labels written. "
            "Cost: $0. Money-leak canary."
        ),
    )
    parser.add_argument(
        "--label-model", default=None,
        help=(
            "Registry id for the labeling model. Default: "
            "curator_topic_label_model_id setting (default "
            "llamacpp-qwen35-122b-a10b)."
        ),
    )
    parser.add_argument(
        "--max-cost-usd", type=float, default=None,
        help=(
            "Hard ceiling on labeling cost (cumulative). Remaining "
            "clusters past the cap get a 'budget capped' placeholder."
        ),
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help=(
            "Compute the snapshot but DON'T emit log rows. Useful for "
            "previewing a snapshot before committing to forever-data."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the snapshot as JSON instead of the human table.",
    )
    parser.add_argument(
        "--title-col", default="video_title",
        help=(
            "Column in the watch_history parquet that carries the title "
            "to embed. Default 'video_title' matches the slice-1 Takeout "
            "importer's schema (tools/curator_takeout_import.py); set to "
            "'title' for hand-crafted history corpora that follow the "
            "module's generic default."
        ),
    )
    parser.add_argument(
        "--epoch-col", default="_epoch",
        help=(
            "Column carrying the Unix-second timestamp used for recency "
            "weighting. Default '_epoch' matches every SpeakesQuery parquet."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging (DEBUG level).",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    history_root = _resolve_history_root(args.history_root)
    history_df = _load_history(history_root)
    if history_df.empty:
        print(
            f"[!] No watch_history parquets found under {history_root}. "
            "Run tools/curator_takeout_import.py first to bootstrap.",
            file=sys.stderr,
        )
        return 1

    # Defer the analyzers import so a bad argv path doesn't pay the
    # sentence-transformers / sklearn load cost.
    from analyzers.topic_vectors import (
        compute_topic_snapshot,
        label_clusters_with_llm,
    )

    try:
        snapshot = compute_topic_snapshot(
            history_df,
            title_col=args.title_col,
            epoch_col=args.epoch_col,
            n_clusters=args.n_clusters,
            decay_lambda_days=args.decay_lambda_days,
        )
    except Exception as exc:
        logging.error("[x] compute_topic_snapshot failed: %s", exc)
        # Surface a hint when the schema mismatch is the likely culprit -
        # this is the single most common first-bootstrap error caught
        # 2026-05-16 (Takeout watch_history uses 'video_title', not
        # 'title', so the module default fails on real data).
        if "missing column" in str(exc) and history_df is not None:
            logging.error(
                "[x] Available columns in the loaded history: %s. "
                "If your parquet uses a different title column, pass "
                "--title-col=<name>.", sorted(history_df.columns),
            )
        return 1

    model_used_for_labels = ""
    if not args.no_labels:
        try:
            label_clusters_with_llm(
                snapshot,
                model_id=args.label_model,
                dry_run=args.dry_run_labels,
                max_cost_usd=args.max_cost_usd,
            )
            model_used_for_labels = args.label_model or "(setting default)"
            if args.dry_run_labels:
                model_used_for_labels += " [dry-run]"
        except Exception as exc:
            logging.warning(
                "[!] label_clusters_with_llm failed (continuing without "
                "labels): %s", exc,
            )

    rows_written = 0
    if not args.no_write:
        try:
            rows_written = _write_snapshot_to_log(snapshot)
        except Exception as exc:
            logging.error("[x] Failed to write snapshot rows: %s", exc)
            return 1

    if args.json:
        _print_json(snapshot)
    else:
        _print_human(snapshot, model_used_for_labels=model_used_for_labels)
        if rows_written:
            print()
            print(
                f"Wrote {rows_written} cluster row(s) to "
                "indexes/IMMUTABLE/curator_topic_snapshots/."
            )
        elif args.no_write:
            print()
            print("--no-write: snapshot computed but not persisted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
