"""Embedding backfill CLI - Phase 1 / Bet 2 slice 5.

One-shot tool to populate ``<source>.embeddings.parquet`` sidecars for
every parquet under ``indexes/``. Use it to bootstrap the semantic-search
layer on an existing corpus *without* waiting for the (default 15-minute)
sweeper cadence to chip away at it.

The CLI is a thin wrapper over :class:`functionality.embedding_sweeper.EmbeddingSweeper`
- same code path the engine-registered sweeper uses, so behavior is
identical. Two extra knobs that aren't exposed via Settings:

* ``--root <path>`` - backfill against an arbitrary directory rather than
  ``indexes/`` (useful for a one-off batch on a separate corpus).
* ``--cleanup`` - also run the embedding-budget cleanup pass after the
  sweep, evicting oldest sidecars if you went over ``max_embeddings_size_gb``.

Usage::

    python -m tools.embed_backfill                  # default indexes/ root
    python -m tools.embed_backfill --root /tmp/news # custom root
    python -m tools.embed_backfill --cleanup        # sweep + budget evict
    python -m tools.embed_backfill --json           # machine-readable output

Output is a per-source summary table, plus a totals line. With ``--json``
the entire :class:`SweepReport` lands as JSON on stdout - handy for
piping into ``jq`` or another script.

The CLI never raises on a per-source error: a corrupt parquet just lands
in ``failures`` and the sweep continues. Exit code is ``0`` on a clean
sweep, ``1`` if any source failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _resolve_root(arg_root: str | None) -> Path:
    if arg_root:
        return Path(arg_root).resolve()
    # Default: indexes/ from settings (or fall back to project root).
    try:
        from global_settings import get_settings
        return get_settings().indexes_dir().resolve()
    except Exception:
        return (Path(__file__).resolve().parent.parent / "indexes").resolve()


def _print_human(report, root: Path) -> None:
    print()
    print(f"Embedding backfill - root: {root}")
    print(f"  sources discovered: {report.sources_seen}")
    print(f"  sources embedded:   {report.sources_embedded} ({report.rows_embedded} rows)")
    print(f"  sources fresh:      {report.sources_skipped_fresh}")
    print(f"  sources empty:      {report.sources_skipped_empty}")
    print(f"  sources no-text:    {report.sources_skipped_no_text}")
    print(f"  sources failed:     {report.sources_failed}")
    print(f"  elapsed:            {report.elapsed_ms / 1000:.2f}s")

    if report.sources_failed:
        print()
        print("Failures:")
        for r in report.failures:
            print(
                f"  - {r.source}: {r.error_class} - {r.error_message[:80]}"
            )

    if report.sources_embedded:
        print()
        # Show the slowest 5 by elapsed_ms - useful for spotting outlier
        # sources that might want a per-source override later.
        embedded = [r for r in report.per_source if r.status == "embedded"]
        embedded.sort(key=lambda r: r.elapsed_ms, reverse=True)
        print("Slowest embedded sources:")
        for r in embedded[:5]:
            print(f"  - {r.source} - {r.rows} rows in {r.elapsed_ms} ms")


def _print_json(report) -> None:
    payload = asdict(report)
    payload["per_source"] = [
        {**asdict(r), "source": str(r.source)} for r in report.per_source
    ]
    json.dump(payload, sys.stdout, indent=2, default=str)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="embed_backfill",
        description=(
            "Populate embedding sidecars for every parquet under indexes/. "
            "Same code path as the engine-scheduled sweeper."
        ),
    )
    parser.add_argument(
        "--root",
        help="Override the indexes root. Default: settings.indexes_dir().",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Also run cleanup_embeddings after the sweep, evicting oldest "
            "sidecars if total size exceeds max_embeddings_size_gb."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human table.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging (DEBUG level).",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    root = _resolve_root(args.root)
    if not root.exists():
        print(f"[!] Root path does not exist: {root}", file=sys.stderr)
        return 2

    # Defer the import so a malformed argv path doesn't pay the
    # sentence-transformers load cost.
    from functionality.embedding_sweeper import EmbeddingSweeper

    sweeper = EmbeddingSweeper(root)
    report = sweeper.sweep_once()

    if args.cleanup:
        try:
            from scheduled_input_engine.cleanup import cleanup_embeddings
            deleted = cleanup_embeddings(indexes_dir=root)
            if not args.json:
                print()
                print(f"Cleanup pass: {len(deleted)} sidecars evicted "
                      "(over budget)")
        except Exception as exc:
            logging.error("[x] cleanup_embeddings failed: %s", exc)

    if args.json:
        _print_json(report)
    else:
        _print_human(report, root)

    # Exit non-zero if any source failed so a CI / cron caller can flag it.
    return 1 if report.sources_failed else 0


if __name__ == "__main__":
    sys.exit(main())
