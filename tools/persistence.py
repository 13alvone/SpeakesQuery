#!/usr/bin/env python3
"""
SpeakesQuery user-data persistence tool.
======================================

Wraps four operations that protect against data loss across container
rebuilds (``./update.sh``), bind-mount misconfigurations, and accidental
deletes:

  snapshot   Emit a JSON manifest of every user-data target on disk
             (paths, sizes, sha256 for small files, dir summaries for
             large directories like ``indexes/``). Snapshots are the
             ground truth for diff detection.

  backup     Tar.gz every user-data target into a timestamped archive
             under ``~/speakesquery-backups/``. Defaults to small files
             only; pass ``--include-indexes`` to also bundle parquet
             data (potentially gigabytes).

  restore    Untar a backup archive back into the project tree. Refuses
             to overwrite existing files without ``--force`` so an
             accidental restore can't trash live data.

  diff       Compare two snapshot files and report adds, removes, size
             changes, and zero-byte regressions. Exits non-zero when
             user-data files have disappeared or shrunk to zero.

The tool is stdlib-only so it can run on the bare host Python without
the project's virtualenv (important: ``update.sh`` runs on operator
machines that may not have the project venv activated).

Typical wiring (already in ``update.sh``):

    pre=~/speakesquery-backups/snapshot-pre.json
    post=~/speakesquery-backups/snapshot-post.json
    tar=~/speakesquery-backups/userdata-$(date -u +%Y%m%dT%H%M%SZ).tar.gz

    python3 -m tools.persistence snapshot --output "$pre"
    python3 -m tools.persistence backup   --output "$tar"
    # ... docker stop/rm/build/up ...
    python3 -m tools.persistence snapshot --output "$post"
    python3 -m tools.persistence diff --before "$pre" --after "$post"

Manual recovery:

    python3 -m tools.persistence restore --tarball <path-to-tarball>

Exit codes (per subcommand):
  0   Operation succeeded; no problems detected
  1   Operation completed but found something the operator should see
      (diff: regressions; restore: would-overwrite without --force)
  2   Usage / argument error
  3   Hard failure (I/O, permissions, corrupt tarball)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ── Project layout ────────────────────────────────────────────────────────
# This script lives at <project_root>/tools/persistence.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.path.expanduser("~"))
DEFAULT_BACKUP_DIR = HOME / "speakesquery-backups"

# ── Targets ───────────────────────────────────────────────────────────────
# Directories whose individual files we hash + record. Small config dirs
# whose content is the user's hand-curated YAML state.
DIR_TARGETS_HASHED: tuple[str, ...] = (
    "saved_searches",
    "alert_groups",
    "macros",
    "boilerplate_prompts",
    "email_groups",
    "analyzer_prompts",
    "lookups",
    # Phase 2 / Bet 3 slice 1 (2026-05-08): LLM model registry. Tiny
    # YAML directory; user edits to model pricing / endpoint URLs MUST
    # round-trip through the persistence tool. Default-included so a
    # fresh restore from backup gets the registry alongside everything
    # else the user customised.
    "models",
    # Phase 3 / Bet 4 slice 1 (2026-05-08): notebook user data. Same
    # seed-from-defaults pattern as alert_groups/ + models/. Live cell
    # state + reactive cache hashes round-trip through the persistence
    # tool so a backup-restore preserves the user's analysis work.
    "notebooks",
    # default_saved_searches and default_alert_groups are shipped-with-the-code
    # (tracked in git) but bundling them lets the diff catch divergence
    # between the user's live tree and the templates the next build will
    # install. The runtime stores read from saved_searches/ and
    # alert_groups/ respectively; the default_* trees seed missing files
    # via the *Store._seed_defaults() pattern (no overwrite).
    "default_saved_searches",
    "default_alert_groups",
    "default_models",
    "default_notebooks",
    # indexes/IMMUTABLE/ is the OEB pick journal - the user's
    # decade-horizon trading record. CLAUDE.md flags it as the
    # "must survive forever" tree. Per-file hashed (small data - pick
    # records are ~100 bytes each, ~1 MiB total over 10 years) so a
    # restore can verify bit-identical equivalence. ALWAYS in default
    # backups even though its parent (indexes/) is summary-only -
    # losing the trading record between backups is the failure mode
    # this whole tool exists to prevent. Add 2026-05-06 after the
    # backup-tool audit found IMMUTABLE was excluded by default
    # because it nested under DIR_TARGETS_SUMMARIZED["indexes"].
    "indexes/IMMUTABLE",
)

# Directories where per-file hashing is too expensive (parquet at scale).
# We capture aggregate stats only.
DIR_TARGETS_SUMMARIZED: tuple[str, ...] = (
    "indexes",
    "jobs",
    "scheduled_input_scripts",
    "executed_scheduled_searches",
    # Phase 3 / Bet 4 slice 3 (2026-05-09): notebook reactive cache.
    # Regenerable (re-running the notebook rebuilds it); aggregate
    # stats only - per-file hashing of pickle payloads at scale would
    # be expensive. Bind-mounted in docker-compose so container
    # rebuilds preserve it; install.sh creates the dir up-front.
    "notebook_cache",
    # Phase 6 / Bet 5 slice 1 (2026-05-16): Google Takeout (YouTube)
    # export. User-supplied INPUT - read once by
    # `tools.curator_takeout_import` and emitted as structured parquets
    # under indexes/IMMUTABLE/curator_takeout/ (which is in
    # DIR_TARGETS_HASHED and IS bit-perfect backed up). The raw
    # Takeout HTML is recoverable from Google directly, so we record
    # only aggregate stats here for diagnostic purposes - bundling the
    # bytes would bloat default backups without adding recoverability
    # the user doesn't already have via google.com/takeout.
    "youtube_profile",
)

# Single-file targets at project root. Each is a YAML config or SQLite
# database whose disappearance equals data loss.
FILE_TARGETS: tuple[str, ...] = (
    "global_settings.yaml",
    ".env",
    "credentials.sqlite",
    "last_chance.sqlite",
    "scheduled_inputs.db",
    "scheduled_inputs_history.db",
    "saved_searches.db",
    "saved_search_history.db",
    "alert_group_runs.sqlite",
    "claude_api_history.sqlite",
    "analyzer_results.sqlite",
    # Phase 2 / Bet 3 slice 3 (2026-05-08): provider-agnostic LLM call
    # history + cache. Lives at project root (not in indexes/) so
    # cleanup-budget eviction never deletes paid-for cache hits.
    "llm_call_history.sqlite",
)

# Targets outside the project tree. Currently just the Fernet credential
# vault - a small directory under the operator's home dir.
EXTERNAL_TARGETS: tuple[Path, ...] = (
    HOME / ".speakes-query",
)

# Marker prefix used inside backup tarballs for absolute-path targets so
# they can round-trip back to the same location on restore.
HOME_MARKER = "__HOME__"


# ── Colors ────────────────────────────────────────────────────────────────
def _colors_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _color(code: str, text: str) -> str:
    if not _colors_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str) -> str:  return _color("32", t)
def _yellow(t: str) -> str: return _color("33", t)
def _red(t: str) -> str:    return _color("31", t)
def _cyan(t: str) -> str:   return _color("36", t)
def _bold(t: str) -> str:   return _color("1",  t)


# ── Snapshot ──────────────────────────────────────────────────────────────
def _hash_file(path: Path, chunk: int = 1 << 20) -> str:
    """Streaming sha256 - small chunks so a multi-MB SQLite file doesn't
    blow up memory on a low-RAM server."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _stat_entry(path: Path, *, hashed: bool) -> dict[str, Any]:
    st = path.stat()
    entry = {
        "size": st.st_size,
        "mtime": st.st_mtime,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mode": oct(st.st_mode & 0o777),
    }
    if hashed and st.st_size > 0:
        try:
            entry["sha256"] = _hash_file(path)
        except OSError as exc:
            entry["sha256_error"] = str(exc)
    return entry


def _snapshot_dir_hashed(root: Path, rel: str) -> dict[str, Any]:
    """Hash every file under root, keyed by path relative to root."""
    result: dict[str, Any] = {"type": "dir_hashed", "files": {}}
    if not root.is_dir():
        result["missing"] = True
        return result
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if not path.is_file():
            # Skip symlinks, sockets, etc. - user data is plain files.
            continue
        try:
            entry = _stat_entry(path, hashed=True)
        except OSError as exc:
            entry = {"stat_error": str(exc)}
        relpath = str(path.relative_to(root))
        result["files"][relpath] = entry
    result["file_count"] = len(result["files"])
    result["total_size"] = sum(
        f.get("size", 0) for f in result["files"].values()
    )
    return result


def _snapshot_dir_summary(root: Path) -> dict[str, Any]:
    """Aggregate stats only - file_count, total_size, mtime range. Cheap."""
    result: dict[str, Any] = {"type": "dir_summary"}
    if not root.is_dir():
        result["missing"] = True
        return result
    file_count = 0
    total_size = 0
    min_mtime: float | None = None
    max_mtime: float | None = None
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        file_count += 1
        total_size += st.st_size
        m = st.st_mtime
        if min_mtime is None or m < min_mtime:
            min_mtime = m
        if max_mtime is None or m > max_mtime:
            max_mtime = m
    result["file_count"] = file_count
    result["total_size"] = total_size
    result["min_mtime"] = min_mtime
    result["max_mtime"] = max_mtime
    return result


def _snapshot_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"type": "file", "missing": True}
    if not path.is_file():
        return {"type": "file", "error": "not_a_regular_file"}
    entry = _stat_entry(path, hashed=True)
    entry["type"] = "file"
    return entry


def build_snapshot(project_root: Path) -> dict[str, Any]:
    """Build a full manifest dict ready to dump as JSON."""
    snap: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "gid": os.getgid() if hasattr(os, "getgid") else None,
        "targets": {},
    }
    for rel in DIR_TARGETS_HASHED:
        snap["targets"][rel] = _snapshot_dir_hashed(project_root / rel, rel)
    for rel in DIR_TARGETS_SUMMARIZED:
        snap["targets"][rel] = _snapshot_dir_summary(project_root / rel)
    for rel in FILE_TARGETS:
        snap["targets"][rel] = _snapshot_file(project_root / rel)
    for ext in EXTERNAL_TARGETS:
        # External targets are recorded under their absolute path so
        # diff can compare across snapshots from different operators.
        key = str(ext)
        if ext.is_dir():
            snap["targets"][key] = _snapshot_dir_hashed(ext, key)
        else:
            snap["targets"][key] = _snapshot_file(ext)
    return snap


def cmd_snapshot(args: argparse.Namespace) -> int:
    snap = build_snapshot(PROJECT_ROOT)
    output_path = Path(args.output) if args.output else None
    blob = json.dumps(snap, indent=2, sort_keys=True)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(blob, encoding="utf-8")
        if not args.quiet:
            print(_green(f"[OK] snapshot written to {output_path}"))
            n_targets = len(snap["targets"])
            print(f"      {n_targets} target(s) recorded")
    else:
        sys.stdout.write(blob + "\n")
    return 0


# ── Backup ────────────────────────────────────────────────────────────────
def _add_to_tar(
    tar: tarfile.TarFile,
    src: Path,
    arcname: str,
    *,
    excluded_dirs: Iterable[str] = (),
) -> int:
    """Add one path to the tar; recursive for dirs. Returns bytes added."""
    if not src.exists():
        return 0
    excluded = tuple(excluded_dirs)

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # Skip any path under an excluded subdirectory (e.g. indexes/
        # under the project root if --include-indexes was not passed).
        for ex in excluded:
            if info.name == ex or info.name.startswith(ex + "/"):
                return None
        return info

    tar.add(str(src), arcname=arcname, filter=_filter)
    if src.is_file():
        return src.stat().st_size
    total = 0
    for p in src.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def cmd_backup(args: argparse.Namespace) -> int:
    output = Path(args.output) if args.output else (
        DEFAULT_BACKUP_DIR
        / f"speakesquery-userdata-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    excluded_at_root: list[str] = []
    if not args.include_indexes:
        excluded_at_root.extend(DIR_TARGETS_SUMMARIZED)

    bytes_added = 0
    paths_added = 0
    with tarfile.open(str(output), "w:gz") as tar:
        for rel in DIR_TARGETS_HASHED:
            src = PROJECT_ROOT / rel
            if not src.exists():
                continue
            bytes_added += _add_to_tar(tar, src, rel)
            paths_added += 1
        if args.include_indexes:
            for rel in DIR_TARGETS_SUMMARIZED:
                src = PROJECT_ROOT / rel
                if not src.exists():
                    continue
                # Avoid re-adding indexes/IMMUTABLE/ - it was already
                # bundled via the DIR_TARGETS_HASHED loop above. Without
                # this exclusion, --include-indexes would write the
                # IMMUTABLE pick journal twice (once hashed, once as
                # part of the bulk indexes/ tree).
                sub_excludes: list[str] = []
                if rel == "indexes":
                    sub_excludes.append("indexes/IMMUTABLE")
                bytes_added += _add_to_tar(
                    tar, src, rel, excluded_dirs=sub_excludes,
                )
                paths_added += 1
        for rel in FILE_TARGETS:
            src = PROJECT_ROOT / rel
            if not src.exists():
                continue
            bytes_added += _add_to_tar(tar, src, rel)
            paths_added += 1
        for ext in EXTERNAL_TARGETS:
            if not ext.exists():
                continue
            arcname = f"{HOME_MARKER}/{ext.name}"
            bytes_added += _add_to_tar(tar, ext, arcname)
            paths_added += 1

    if not args.quiet:
        print(_green(f"[OK] backup written to {output}"))
        print(f"      {paths_added} target(s), "
              f"~{bytes_added / 1024 / 1024:.1f} MiB raw")
        if not args.include_indexes:
            print(f"      (indexes/ excluded; pass --include-indexes "
                  f"to bundle data)")
    if args.print_path:
        print(str(output))
    return 0


# ── Restore ───────────────────────────────────────────────────────────────
def _safe_member_name(name: str, project_root: Path) -> Path | None:
    """Resolve a tar member path against project_root, refusing any path
    that would escape the project tree (defence against path traversal
    in a hostile tarball).

    Returns the destination Path on success, or None when the member
    should be skipped (path traversal / unsafe).
    """
    if name.startswith(f"{HOME_MARKER}/"):
        rel = name[len(HOME_MARKER) + 1:]
        candidate = HOME / rel
        try:
            candidate.relative_to(HOME)
        except ValueError:
            return None
        return candidate

    candidate = (project_root / name).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        return None
    return candidate


def cmd_restore(args: argparse.Namespace) -> int:
    tarpath = Path(args.tarball)
    if not tarpath.is_file():
        print(_red(f"[XX] tarball not found: {tarpath}"), file=sys.stderr)
        return 2

    if not args.yes:
        print(_yellow(
            f"[!!] About to restore {tarpath} into {PROJECT_ROOT}"
        ))
        print(_yellow(
            "     This will OVERWRITE existing user-data files matching "
            "members of the archive."
        ))
        print(_yellow("     Press Enter to continue, Ctrl-C to abort."))
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print(_red("\n[XX] aborted by user"), file=sys.stderr)
            return 1

    skipped: list[str] = []
    overwrites: list[str] = []
    extracted = 0
    bytes_extracted = 0
    with tarfile.open(str(tarpath), "r:*") as tar:
        for member in tar.getmembers():
            dest = _safe_member_name(member.name, PROJECT_ROOT)
            if dest is None:
                skipped.append(member.name)
                continue
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if not (member.isfile() or member.islnk() or member.issym()):
                continue
            if dest.exists() and not args.force:
                # Without --force, refuse to overwrite live data so a
                # mistaken restore can't trash a populated server.
                overwrites.append(str(dest))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            extracted_obj = tar.extractfile(member)
            if extracted_obj is None:
                continue
            data = extracted_obj.read()
            dest.write_bytes(data)
            try:
                os.chmod(dest, member.mode)
            except OSError:
                pass
            extracted += 1
            bytes_extracted += len(data)

    if overwrites and not args.force:
        print(_yellow(
            f"[!!] {len(overwrites)} file(s) NOT restored because they "
            "already exist (pass --force to overwrite):"
        ))
        for path in overwrites[:20]:
            print(f"      {path}")
        if len(overwrites) > 20:
            print(f"      ... and {len(overwrites) - 20} more")
    if skipped:
        print(_red(
            f"[XX] {len(skipped)} member(s) skipped (path traversal or "
            "unsafe target):"
        ))
        for name in skipped[:10]:
            print(f"      {name}")
    print(_green(
        f"[OK] restored {extracted} file(s), "
        f"~{bytes_extracted / 1024 / 1024:.1f} MiB"
    ))
    return 1 if (overwrites and not args.force) or skipped else 0


# ── Diff ──────────────────────────────────────────────────────────────────
def _flatten(snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten a snapshot into a single {path: entry} map for diffing.

    For directory targets we project per-file entries onto the dir/relpath
    so a missing file inside saved_searches/ shows up cleanly as a row
    with key ``saved_searches/foo.yaml``.
    """
    flat: dict[str, dict[str, Any]] = {}
    for target, payload in snap.get("targets", {}).items():
        if not isinstance(payload, dict):
            continue
        ttype = payload.get("type", "unknown")
        if ttype == "dir_hashed":
            for relpath, entry in payload.get("files", {}).items():
                flat[f"{target}/{relpath}"] = {**entry, "kind": "file"}
            # Also track the dir itself (so a totally-missing dir surfaces)
            flat[f"{target}/"] = {
                "kind": "dir_hashed",
                "missing": payload.get("missing", False),
                "file_count": payload.get("file_count", 0),
                "total_size": payload.get("total_size", 0),
            }
        elif ttype == "dir_summary":
            flat[f"{target}/"] = {
                "kind": "dir_summary",
                "missing": payload.get("missing", False),
                "file_count": payload.get("file_count", 0),
                "total_size": payload.get("total_size", 0),
                "max_mtime": payload.get("max_mtime"),
            }
        elif ttype == "file":
            flat[target] = {**payload, "kind": "file"}
    return flat


def _diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    fb = _flatten(before)
    fa = _flatten(after)

    removed: list[dict[str, Any]] = []
    zeroed: list[dict[str, Any]] = []
    shrunk: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for path, b_entry in fb.items():
        a_entry = fa.get(path)
        b_size = int(b_entry.get("size", b_entry.get("total_size", 0)) or 0)
        b_count = int(b_entry.get("file_count", 0) or 0)
        b_missing = bool(b_entry.get("missing"))
        if a_entry is None:
            if not b_missing and (b_size > 0 or b_count > 0):
                removed.append({"path": path, "before": b_entry})
            continue
        a_size = int(a_entry.get("size", a_entry.get("total_size", 0)) or 0)
        a_count = int(a_entry.get("file_count", 0) or 0)
        a_missing = bool(a_entry.get("missing"))

        if a_missing and not b_missing:
            removed.append({"path": path, "before": b_entry, "after": a_entry})
            continue
        if b_size > 0 and a_size == 0:
            zeroed.append({"path": path, "before": b_entry, "after": a_entry})
            continue
        if b_size > a_size and (b_size - a_size) > max(1, b_size * 0.10):
            shrunk.append({
                "path": path, "before": b_entry, "after": a_entry,
                "delta": a_size - b_size,
            })
            continue
        if b_count > a_count > 0 or (b_count > 0 and a_count == 0):
            shrunk.append({
                "path": path, "before": b_entry, "after": a_entry,
                "delta_count": a_count - b_count,
            })
            continue
        b_hash = b_entry.get("sha256")
        a_hash = a_entry.get("sha256")
        if b_hash and a_hash and b_hash != a_hash:
            changed.append({"path": path, "before": b_entry, "after": a_entry})

    for path, a_entry in fa.items():
        if path not in fb:
            a_size = int(a_entry.get("size", a_entry.get("total_size", 0)) or 0)
            if a_size > 0 or int(a_entry.get("file_count", 0) or 0) > 0:
                added.append({"path": path, "after": a_entry})

    return {
        "removed": removed,
        "zeroed": zeroed,
        "shrunk": shrunk,
        "added": added,
        "changed": changed,
    }


def cmd_diff(args: argparse.Namespace) -> int:
    before_path = Path(args.before)
    after_path = Path(args.after)
    if not before_path.is_file() or not after_path.is_file():
        print(_red("[XX] both --before and --after must point to "
                   "existing snapshot files"), file=sys.stderr)
        return 2
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    diff = _diff_snapshots(before, after)

    if args.json:
        sys.stdout.write(json.dumps(diff, indent=2, sort_keys=True) + "\n")
    else:
        _print_diff_report(diff, before_path, after_path)

    regression = bool(diff["removed"] or diff["zeroed"] or diff["shrunk"])
    return 1 if regression else 0


def _print_diff_report(
    diff: dict[str, list[dict[str, Any]]],
    before_path: Path,
    after_path: Path,
) -> None:
    print(_bold(f"Persistence diff: {before_path.name} → {after_path.name}"))
    print()

    def _section(title: str, items: list[dict[str, Any]], color) -> None:
        if not items:
            return
        print(color(f"  {title} ({len(items)}):"))
        for item in items[:50]:
            path = item["path"]
            before = item.get("before", {})
            after = item.get("after", {})
            b_size = before.get("size", before.get("total_size", "?"))
            a_size = after.get("size", after.get("total_size", "?"))
            print(f"    {path}    {b_size}  →  {a_size}")
        if len(items) > 50:
            print(f"    ... and {len(items) - 50} more")
        print()

    _section("REMOVED (file disappeared)", diff["removed"], _red)
    _section("ZEROED (file shrank to 0 bytes)", diff["zeroed"], _red)
    _section("SHRUNK (file lost data)", diff["shrunk"], _yellow)
    _section("CHANGED (content modified - usually fine)", diff["changed"], _cyan)
    _section("ADDED (new file)", diff["added"], _green)

    if not any(diff[k] for k in ("removed", "zeroed", "shrunk", "added",
                                 "changed")):
        print(_green("  No differences detected - perfect persistence."))
    elif not (diff["removed"] or diff["zeroed"] or diff["shrunk"]):
        print(_green("  No regressions. Adds + content changes only."))
    else:
        print(_red(
            "  REGRESSION DETECTED. Restore from the most recent backup "
            "if data was lost:"
        ))
        print(_red(
            f"    python3 -m tools.persistence restore --tarball "
            f"{DEFAULT_BACKUP_DIR}/<latest>.tar.gz"
        ))


# ── CLI ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.persistence",
        description="SpeakesQuery user-data snapshot / backup / restore / diff.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser(
        "snapshot",
        help="Emit a JSON manifest of every user-data target on disk.",
    )
    p_snap.add_argument(
        "--output", "-o",
        help="Write JSON to this file (default: stdout).",
    )
    p_snap.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress the success line; useful for piping.",
    )
    p_snap.set_defaults(func=cmd_snapshot)

    p_back = sub.add_parser(
        "backup",
        help="Tar.gz user-data targets into a timestamped archive.",
    )
    p_back.add_argument(
        "--output", "-o",
        help=("Output tarball path (default: "
              "~/speakesquery-backups/speakesquery-userdata-<UTC>.tar.gz)."),
    )
    p_back.add_argument(
        "--include-indexes", action="store_true",
        help=("Also bundle indexes/, jobs/, etc. (potentially gigabytes; "
              "off by default)."),
    )
    p_back.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress the success line.",
    )
    p_back.add_argument(
        "--print-path", action="store_true",
        help=("Print the resolved output path on stdout (after the OK "
              "line if not --quiet) - useful for shell wiring."),
    )
    p_back.set_defaults(func=cmd_backup)

    p_rest = sub.add_parser(
        "restore",
        help="Restore user-data from a backup tarball.",
    )
    p_rest.add_argument(
        "--tarball", "-t", required=True,
        help="Path to a backup tarball produced by `backup`.",
    )
    p_rest.add_argument(
        "--force", action="store_true",
        help="Overwrite existing files. Required to clobber live data.",
    )
    p_rest.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    p_rest.set_defaults(func=cmd_restore)

    p_diff = sub.add_parser(
        "diff",
        help="Compare two snapshots and report regressions.",
    )
    p_diff.add_argument(
        "--before", "-b", required=True,
        help="Snapshot file from BEFORE the operation under test.",
    )
    p_diff.add_argument(
        "--after", "-a", required=True,
        help="Snapshot file from AFTER the operation under test.",
    )
    p_diff.add_argument(
        "--json", action="store_true",
        help="Emit diff as JSON instead of a colored terminal report.",
    )
    p_diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(_red("\n[XX] interrupted"), file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(_red(f"[XX] {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
