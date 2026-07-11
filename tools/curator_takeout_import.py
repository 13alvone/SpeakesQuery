"""Google Takeout (YouTube) → curator IMMUTABLE parquet import.

Phase 6 / Bet 5 slice 1 (2026-05-16). One-shot CLI that converts the
user's Google Takeout YouTube export into structured Parquet rows
under ``indexes/IMMUTABLE/curator_takeout/<kind>/``. Read once at
bootstrap; the ongoing data refresh comes from the speaktube telemetry
pull ingestion script. Re-runnable safely: each run writes a new
``<epoch>_<uuid>.system4.system4.parquet`` so historical imports are
preserved alongside fresh ones (cleanup never touches IMMUTABLE).

Usage::

    python -m tools.curator_takeout_import                   # default paths
    python -m tools.curator_takeout_import --root ~/yt/      # custom Takeout root
    python -m tools.curator_takeout_import --json            # machine-readable summary

Expected Takeout layout (subset - missing files are skipped with a warning)::

    <root>/
      subscriptions/subscriptions.csv
      playlists/playlists.csv
      playlists/<NAME>-videos.csv     # one per playlist
      history/watch-history.html

Outputs (each in its own subdirectory)::

    indexes/IMMUTABLE/curator_takeout/subscriptions/<epoch>_<uuid>.system4.system4.parquet
    indexes/IMMUTABLE/curator_takeout/playlists_metadata/<epoch>_<uuid>.system4.system4.parquet
    indexes/IMMUTABLE/curator_takeout/playlist_videos/<epoch>_<uuid>.system4.system4.parquet
    indexes/IMMUTABLE/curator_takeout/watch_history/<epoch>_<uuid>.system4.system4.parquet

Exit code: 0 on success (one or more sources imported), 1 if no Takeout
files at all were found at <root>.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


logger = logging.getLogger(__name__)


# ── Timezone abbrev → UTC offset (hours) ─────────────────────────────
# Used by the watch-history parser. Google Takeout writes "May 13,
# 2026, 8:28:07 PM PDT" - Python's strptime can't parse the abbrev so
# we strip it off and apply the offset manually. Unknown abbrevs land
# as None (the row still gets a parsed event time but no TZ correction).
_TZ_OFFSET_HOURS: dict[str, int] = {
    "UTC": 0, "GMT": 0,
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8,
    "HST": -10,
    "BST": +1, "CET": +1, "CEST": +2,
}


# ── Schemas (kept in lockstep with this file's writer) ───────────────
# Empty-day output still carries the schema - pass these to the
# DataFrame constructor with `columns=` so a Takeout export missing a
# given artifact still produces a queryable (empty) parquet.

_SUBSCRIPTIONS_COLS = ["_epoch", "channel_id", "channel_url", "channel_title"]

_PLAYLISTS_METADATA_COLS = [
    "_epoch", "playlist_id", "playlist_title", "visibility",
    "video_order_mode", "created_iso", "updated_iso",
]

_PLAYLIST_VIDEOS_COLS = [
    "_epoch", "playlist_name", "video_id", "video_url", "added_iso",
]

_WATCH_HISTORY_COLS = [
    "_epoch", "video_id", "video_url", "video_title",
    "channel_id", "channel_url", "channel_name",
    "watched_iso", "tz_abbrev",
]


@dataclass
class _ImportReport:
    root: str
    output_dir: str
    subscriptions_rows: int = 0
    playlists_metadata_rows: int = 0
    playlist_videos_rows: int = 0
    watch_history_rows: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


# ── Path resolution ──────────────────────────────────────────────────


def _resolve_root(arg_root: str | None) -> Path:
    """Pick the Takeout source directory.

    Priority: --root override, then ``<project_root>/youtube_profile/``,
    then CWD/youtube_profile/ as a last resort.
    """
    if arg_root:
        return Path(arg_root).expanduser().resolve()
    here = Path(__file__).resolve().parent.parent
    candidate = here / "youtube_profile"
    if candidate.exists():
        return candidate.resolve()
    return (Path.cwd() / "youtube_profile").resolve()


def _resolve_output_dir(arg_out: str | None) -> Path:
    """Pick the IMMUTABLE/curator_takeout/ destination.

    Honours global_settings.immutable_subdir() when available so test
    overrides flow through; falls back to the canonical default path.
    """
    if arg_out:
        return Path(arg_out).expanduser().resolve()
    try:
        from global_settings import get_settings
        return get_settings().immutable_subdir("curator_takeout").resolve()
    except Exception:
        return (
            Path(__file__).resolve().parent.parent
            / "indexes" / "IMMUTABLE" / "curator_takeout"
        ).resolve()


# ── Subscriptions ────────────────────────────────────────────────────


def _parse_subscriptions(path: Path) -> list[dict[str, Any]]:
    """Parse subscriptions.csv. Columns: Channel Id, Channel Url, Channel Title."""
    rows: list[dict[str, Any]] = []
    epoch = int(time.time())
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            cid = (r.get("Channel Id") or "").strip()
            if not cid:
                continue
            rows.append({
                "_epoch": epoch,
                "channel_id": cid,
                "channel_url": (r.get("Channel Url") or "").strip(),
                "channel_title": (r.get("Channel Title") or "").strip(),
            })
    return rows


# ── Playlists ────────────────────────────────────────────────────────


def _parse_playlists_metadata(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    epoch = int(time.time())
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            pid = (r.get("Playlist ID") or "").strip()
            if not pid:
                continue
            rows.append({
                "_epoch": epoch,
                "playlist_id": pid,
                "playlist_title": (r.get("Playlist Title (Original)") or "").strip(),
                "visibility": (r.get("Playlist Visibility") or "").strip(),
                "video_order_mode": (r.get("Playlist Video Order") or "").strip(),
                "created_iso": (r.get("Playlist Create Timestamp") or "").strip(),
                "updated_iso": (r.get("Playlist Update Timestamp") or "").strip(),
            })
    return rows


def _parse_playlist_videos(playlists_dir: Path) -> list[dict[str, Any]]:
    """Parse every ``<NAME>-videos.csv`` under playlists/. Returns one row
    per video with the originating playlist's display name prefixed.
    """
    rows: list[dict[str, Any]] = []
    epoch = int(time.time())
    for csv_path in sorted(playlists_dir.glob("*-videos.csv")):
        # "Watch later-videos.csv" → "Watch later"
        playlist_name = csv_path.stem.rsplit("-videos", 1)[0]
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    vid = (r.get("Video ID") or "").strip()
                    if not vid:
                        continue
                    rows.append({
                        "_epoch": epoch,
                        "playlist_name": playlist_name,
                        "video_id": vid,
                        "video_url": f"https://www.youtube.com/watch?v={vid}",
                        "added_iso": (
                            r.get("Playlist Video Creation Timestamp") or ""
                        ).strip(),
                    })
        except Exception as exc:
            logger.warning(
                "[!] curator_takeout_import: skipped %s - %s", csv_path, exc,
            )
    return rows


# ── Watch history ────────────────────────────────────────────────────


_HISTORY_DATE_RE = re.compile(
    r"^(?P<month>[A-Z][a-z]+)\s+(?P<day>\d+),\s+(?P<year>\d{4}),\s+"
    r"(?P<hour>\d+):(?P<min>\d{2}):(?P<sec>\d{2})\s*"
    r"(?P<ampm>AM|PM)\s+(?P<tz>[A-Z]{2,5})$"
)


def _parse_history_timestamp(text: str) -> tuple[int | None, str, str]:
    """Parse "May 13, 2026, 8:28:07 PM PDT" → (epoch, iso, tz_abbrev).

    Returns ``(None, "", "")`` if the format doesn't match. ``epoch``
    is the UTC Unix-seconds derived from the abbrev's offset; ``iso``
    is a wall-clock ISO without offset (the abbrev is preserved in
    ``tz_abbrev`` so downstream can re-derive offset if the lookup
    table grows). ``epoch`` is ``None`` for an unknown abbrev - the
    iso still parses so the row keeps usable wall-clock data.
    """
    m = _HISTORY_DATE_RE.match(text.strip().replace(" ", " "))
    if not m:
        return None, "", ""

    months = {
        "January": 1, "February": 2, "March": 3, "April": 4,
        "May": 5, "June": 6, "July": 7, "August": 8, "September": 9,
        "October": 10, "November": 11, "December": 12,
    }
    month = months.get(m.group("month"))
    if month is None:
        return None, "", m.group("tz")

    hour = int(m.group("hour"))
    if m.group("ampm") == "PM" and hour != 12:
        hour += 12
    elif m.group("ampm") == "AM" and hour == 12:
        hour = 0

    import datetime as _dt
    try:
        local = _dt.datetime(
            int(m.group("year")), month, int(m.group("day")),
            hour, int(m.group("min")), int(m.group("sec")),
        )
    except ValueError:
        return None, "", m.group("tz")

    iso = local.isoformat()
    tz = m.group("tz")
    offset_h = _TZ_OFFSET_HOURS.get(tz)
    if offset_h is None:
        return None, iso, tz

    utc = local - _dt.timedelta(hours=offset_h)
    epoch = int(utc.replace(tzinfo=_dt.timezone.utc).timestamp())
    return epoch, iso, tz


_WATCH_VIDEO_HREF_RE = re.compile(
    r'<a\s+href="(?P<url>https?://www\.youtube\.com/watch\?v=(?P<vid>[A-Za-z0-9_\-]{6,32}))[^"]*"[^>]*>(?P<title>[^<]+)</a>'
)
_WATCH_CHANNEL_HREF_RE = re.compile(
    r'<a\s+href="(?P<url>https?://www\.youtube\.com/channel/(?P<cid>UC[A-Za-z0-9_\-]{20,30}))"[^>]*>(?P<name>[^<]+)</a>'
)
_WATCH_OUTER_CELL_RE = re.compile(
    r'<div class="outer-cell[^"]*">(.+?)</div></div></div>',
    re.DOTALL,
)


def _parse_watch_history(path: Path) -> list[dict[str, Any]]:
    """Parse Takeout's watch-history.html.

    Each entry is a single ``<div class="outer-cell ...">`` block with
    a "Watched <a>title</a><br><a>channel</a><br>DATE" structure.
    Robust to:

    * Entries with missing channel (private / deleted videos)
    * Entries that aren't "Watched" (skipped - file is single-product
      so this should be rare but we don't trust the export blindly)
    * Date parse failures (row written with epoch=None)
    """
    rows: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for cell_match in _WATCH_OUTER_CELL_RE.finditer(text):
        cell = cell_match.group(1)
        if "Watched" not in cell:
            continue

        vm = _WATCH_VIDEO_HREF_RE.search(cell)
        if not vm:
            continue

        cm = _WATCH_CHANNEL_HREF_RE.search(cell)
        date_blob = ""
        # Grab the date string after the last <br>
        last_br = cell.rfind("<br>")
        if last_br != -1:
            tail = cell[:last_br].rsplit("<br>", 1)
            if len(tail) == 2:
                date_blob = re.sub(r"<[^>]+>", "", tail[1]).strip()

        epoch, watched_iso, tz = _parse_history_timestamp(date_blob)

        rows.append({
            "_epoch": epoch if epoch is not None else int(time.time()),
            "video_id": vm.group("vid"),
            "video_url": vm.group("url"),
            "video_title": vm.group("title").strip(),
            "channel_id": cm.group("cid") if cm else "",
            "channel_url": cm.group("url") if cm else "",
            "channel_name": cm.group("name").strip() if cm else "",
            "watched_iso": watched_iso,
            "tz_abbrev": tz,
        })
    return rows


# ── Driver ───────────────────────────────────────────────────────────


def _write_dataframe(
    rows: list[dict[str, Any]],
    columns: list[str],
    output_dir: Path,
    subdirectory: str,
) -> int:
    """Atomically write *rows* to ``<output_dir>/<subdirectory>/``.

    Always emits a parquet with the declared columns - an empty input
    still gets a well-shaped (zero-row) parquet so downstream SPQL
    queries don't trip over a missing column. Returns row count.
    """
    import pandas as pd
    from scheduled_input_engine.parquet_writer import ParquetWriter

    df = pd.DataFrame(rows, columns=columns)
    writer = ParquetWriter(output_dir, target_file_mb=64)
    writer.write_atomic(df, subdirectory=subdirectory)
    return len(df)


def import_takeout(root: Path, output_dir: Path) -> _ImportReport:
    """Run all four parsers and emit one parquet per artifact.

    Each artifact is independent: a missing Takeout file logs to
    ``report.skipped`` and continues. A parser exception lands in
    ``report.failed`` (so a single bad entry doesn't halt the import).
    """
    started_at = time.time()
    report = _ImportReport(root=str(root), output_dir=str(output_dir))

    if not root.exists():
        report.failed.append(f"root not found: {root}")
        report.elapsed_ms = int((time.time() - started_at) * 1000)
        return report

    output_dir.mkdir(parents=True, exist_ok=True)

    # Subscriptions
    subs_path = root / "subscriptions" / "subscriptions.csv"
    if subs_path.exists():
        try:
            rows = _parse_subscriptions(subs_path)
            report.subscriptions_rows = _write_dataframe(
                rows, _SUBSCRIPTIONS_COLS, output_dir, "subscriptions",
            )
            logger.info("[i] curator_takeout_import: subscriptions → %d rows", len(rows))
        except Exception as exc:
            report.failed.append(f"subscriptions: {exc}")
            logger.exception("[x] curator_takeout_import: subscriptions failed")
    else:
        report.skipped.append("subscriptions/subscriptions.csv")

    # Playlists
    playlists_dir = root / "playlists"
    playlists_meta = playlists_dir / "playlists.csv"
    if playlists_meta.exists():
        try:
            rows = _parse_playlists_metadata(playlists_meta)
            report.playlists_metadata_rows = _write_dataframe(
                rows, _PLAYLISTS_METADATA_COLS, output_dir, "playlists_metadata",
            )
            logger.info("[i] curator_takeout_import: playlists_metadata → %d rows", len(rows))
        except Exception as exc:
            report.failed.append(f"playlists_metadata: {exc}")
            logger.exception("[x] curator_takeout_import: playlists_metadata failed")
    else:
        report.skipped.append("playlists/playlists.csv")

    if playlists_dir.exists():
        try:
            rows = _parse_playlist_videos(playlists_dir)
            report.playlist_videos_rows = _write_dataframe(
                rows, _PLAYLIST_VIDEOS_COLS, output_dir, "playlist_videos",
            )
            logger.info("[i] curator_takeout_import: playlist_videos → %d rows", len(rows))
        except Exception as exc:
            report.failed.append(f"playlist_videos: {exc}")
            logger.exception("[x] curator_takeout_import: playlist_videos failed")
    else:
        report.skipped.append("playlists/ (directory)")

    # Watch history
    history_path = root / "history" / "watch-history.html"
    if history_path.exists():
        try:
            rows = _parse_watch_history(history_path)
            report.watch_history_rows = _write_dataframe(
                rows, _WATCH_HISTORY_COLS, output_dir, "watch_history",
            )
            logger.info("[i] curator_takeout_import: watch_history → %d rows", len(rows))
        except Exception as exc:
            report.failed.append(f"watch_history: {exc}")
            logger.exception("[x] curator_takeout_import: watch_history failed")
    else:
        report.skipped.append("history/watch-history.html")

    report.elapsed_ms = int((time.time() - started_at) * 1000)
    return report


def _print_human(report: _ImportReport) -> None:
    print()
    print(f"Google Takeout (YouTube) → curator import")
    print(f"  root:   {report.root}")
    print(f"  output: {report.output_dir}")
    print(f"  subscriptions:        {report.subscriptions_rows} rows")
    print(f"  playlists_metadata:   {report.playlists_metadata_rows} rows")
    print(f"  playlist_videos:      {report.playlist_videos_rows} rows")
    print(f"  watch_history:        {report.watch_history_rows} rows")
    print(f"  elapsed:              {report.elapsed_ms / 1000:.2f}s")
    if report.skipped:
        print()
        print("Skipped (not present in export):")
        for s in report.skipped:
            print(f"  - {s}")
    if report.failed:
        print()
        print("Failed:")
        for f in report.failed:
            print(f"  - {f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="curator_takeout_import",
        description=__doc__.split("\n\n", 1)[0],
    )
    parser.add_argument("--root", help="Path to Takeout YouTube/ root (default: <project_root>/youtube_profile/)")
    parser.add_argument("--out", help="Output dir (default: indexes/IMMUTABLE/curator_takeout/)")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary instead of human-readable")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    root = _resolve_root(args.root)
    output_dir = _resolve_output_dir(args.out)
    report = import_takeout(root, output_dir)

    if args.json:
        json.dump(asdict(report), sys.stdout, indent=2)
        print()
    else:
        _print_human(report)

    total_rows = (
        report.subscriptions_rows
        + report.playlists_metadata_rows
        + report.playlist_videos_rows
        + report.watch_history_rows
    )
    if total_rows == 0 and not report.failed:
        # Nothing imported AND no exceptions - the user pointed at a
        # directory that didn't have Takeout files in it. Exit 1 so a
        # CI-style invocation surfaces the mistake.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
