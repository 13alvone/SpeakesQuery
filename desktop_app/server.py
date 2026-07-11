#!/usr/bin/env python3
"""
SpeakesQuery Desktop Server
─────────────────────────
Minimal Flask server that powers the desktop UI.
Query execution, file browsing, lookup management, scheduled input CRUD,
global settings, and credential management.

Run locally (PyCharm or terminal):
    python desktop_app/server.py

Run in Docker:
    ./install.sh

PyCharm CE debugging:
    1. Set project interpreter to your venv
    2. Run → Edit Configurations → + → Python
       Script: desktop_app/server.py
       Working directory: <project root>
    3. Set breakpoints anywhere, click Debug
"""

import sys
import os
import io
import re
import time
import logging
import tempfile
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path for speakesquery imports.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Load .env file if present (for local development)
try:
    from dotenv import load_dotenv
    _env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(_env_file):
        load_dotenv(_env_file)
except ImportError:
    pass  # python-dotenv not installed - env vars must be set externally

import pandas as pd
from flask import Flask, request, jsonify, send_file, send_from_directory, make_response, Response

logging.basicConfig(
    level=logging.INFO,
    # SpeakesQuery convention (see CLAUDE.md): call sites embed the level
    # marker themselves - `[i]` info, `[x]` error, `[!]` warning.  Adding
    # `[INFO]/[ERROR]` here would double-prefix every line.  Format keeps
    # just a timestamp + message so the in-code markers stand alone.
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
DESKTOP_DIR = os.path.dirname(os.path.abspath(__file__))

# Cached sensitive-path prefixes used by _safe_error_message() to redact
# absolute filesystem paths before they reach API responses.
_HOME_DIR = os.path.expanduser("~")
_REDACT_PATHS = [
    (PROJECT_ROOT, "<project>"),
    (_HOME_DIR, "~"),
]


def _safe_error_message(exc: BaseException, max_len: int = 500) -> str:
    """Render an exception for inclusion in a JSON error response.

    Strips absolute paths that would leak the install location, collapses
    multi-line tracebacks to the first line, and caps overall length so that
    pathological exception messages don't dominate the response. The full
    exception (with traceback) is still emitted via ``logger.exception()``
    at the call site for operator debugging.
    """
    msg = str(exc) if exc is not None else ""
    for needle, replacement in _REDACT_PATHS:
        if needle:
            msg = msg.replace(needle, replacement)
    # Collapse multi-line content to the first non-empty line so we don't
    # accidentally emit a traceback in the JSON body.
    for line in msg.splitlines():
        line = line.strip()
        if line:
            msg = line
            break
    if len(msg) > max_len:
        msg = msg[: max_len - 1].rstrip() + "\u2026"
    return msg or exc.__class__.__name__


def _curator_opt_str(v) -> str:
    """Pandas/parquet-aware coercion to a non-None string.

    pd.isna() handles None, np.nan, and pd.NA uniformly. Used by
    curator endpoints that read parquet rows back into JSON - never
    elide keys (the speaktube renderer reads by name), always return
    a non-None string so json.dumps emits "" rather than null.

    Slice 12 (2026-05-17): promoted from local helpers inside
    api_curator_playlist_today so /api/search can share them.
    """
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v) if v is not None else ""


def _curator_opt_float(v):
    """Pandas-aware float coercion. Returns None for NaN / None /
    non-numeric input; never raises."""
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _curator_opt_int(v):
    """Pandas-aware int coercion. Returns None for NaN / None /
    non-numeric input; never raises."""
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _curator_opt_bool(v) -> bool:
    """Pandas/numpy-aware bool coercion.

    numpy.bool_ values (returned by pandas from parquet bool columns)
    are NOT Python bool subclass - isinstance(v, bool) returns False.
    Use bool(v) after pd.isna() short-circuit for safe coercion.
    String inputs ("true"/"yes"/"1") coerce as expected. Slice 10
    caught this gotcha; slice 12 promotes the helper for reuse.
    """
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    try:
        return bool(v)
    except Exception:
        return False


app = Flask(__name__, static_folder=None)

# Mutable state - which directory the file browser is pointing at.
# Resolved lazily on first access via _get_browse_dir() so that
# global_settings (imported later) can provide the configured path.
_browse_dir: str | None = None


def _get_browse_dir() -> str:
    """Return the current browse directory, initialising from settings on first call."""
    global _browse_dir
    if _browse_dir is None:
        try:
            from global_settings import get_settings as _gs
            _browse_dir = str(_gs().indexes_dir())
        except Exception:
            _browse_dir = os.path.join(PROJECT_ROOT, "indexes")
    return _browse_dir


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOOKUPS_DIR = os.path.join(PROJECT_ROOT, "lookups")

LOOKUP_EXTENSIONS = {".csv", ".parquet", ".json", ".tsv", ".xml", ".yaml", ".yml"}

# Only these formats may be uploaded - narrower than the full browse set.
UPLOAD_EXTENSIONS = {".csv", ".json", ".tsv", ".parquet"}

# 200 MB upload ceiling.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Index import - accepted file types and size ceiling.
IMPORT_EXTENSIONS = {".csv", ".parquet", ".sqlite", ".sqlite3", ".db"}
MAX_IMPORT_BYTES = 200 * 1024 * 1024

# Filename sanitisation - allow only alphanumeric, dash, underscore, dot.
_SAFE_FILENAME_RE = re.compile(r'^[\w\-. ]+$')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_epoch_column(df: pd.DataFrame, date_field: str | None = None) -> pd.DataFrame:
    """Guarantee that *df* has an ``_epoch`` column (int64, Unix seconds).

    Priority:
      1. If ``_epoch`` already exists → return as-is.
      2. If *date_field* is provided and the column exists → convert to epoch.
      3. Otherwise → stamp every row with the current time.
    """
    if "_epoch" in df.columns:
        return df

    if date_field and date_field in df.columns:
        converted = pd.to_datetime(df[date_field], errors="coerce")
        df["_epoch"] = (
            converted.astype("int64") // 10**9  # nanoseconds → seconds
        )
    else:
        df["_epoch"] = int(time.time())

    return df


def _build_tree(directory: str) -> dict:
    """Recursively build a tree of .parquet files under *directory*."""
    node: dict = {"files": [], "dirs": {}}
    if not os.path.isdir(directory):
        logger.warning("[!] _build_tree: directory does not exist: %s", directory)
        return node
    if not os.access(directory, os.R_OK):
        logger.warning(
            "[!] _build_tree: permission denied reading directory: %s "
            "(running as uid=%s)",
            directory,
            os.getuid() if hasattr(os, "getuid") else "n/a",
        )
        return node
    try:
        entries = sorted(
            os.scandir(directory), key=lambda e: (e.is_file(), e.name.lower())
        )
        for entry in entries:
            if entry.is_file() and entry.name.endswith(".parquet"):
                rel = os.path.relpath(entry.path, PROJECT_ROOT)
                node["files"].append({"name": entry.name, "path": rel})
            elif entry.is_dir() and not entry.name.startswith("."):
                child = _build_tree(entry.path)
                if child["files"] or child["dirs"]:
                    node["dirs"][entry.name] = child
    except PermissionError:
        logger.warning(
            "[!] _build_tree: PermissionError scanning %s - files in this "
            "directory will not appear in the UI. Check that the process user "
            "has read access to the indexes volume mount.",
            directory,
        )
    return node


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the single-file UI."""
    return send_from_directory(DESKTOP_DIR, "ui.html")


@app.route("/api/query", methods=["POST"])
def run_query():
    """Execute a SpeakesQuery DSL string and return the result rows as JSON."""
    # H-CE-3 (2026-04-22): use the diagnostic variant so the UI can
    # distinguish "query crashed (SyntaxError at line 3)" from "query
    # returned zero rows (no data in index)". The old non-diagnostic
    # ``process_query`` collapsed both cases into ``(None, None)``.
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics

    data = request.get_json(force=True, silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Empty query."}), 400

    logger.info("[i] run_query: %s", repr(query[:120]))
    try:
        df, job_id, diagnostic = process_query_with_diagnostics(query)

        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            # Build a diagnostic hint for the operator / UI. If the
            # diagnostic string names a concrete exception class (e.g.
            # ``"InvalidInputException: …"``) prefer that verbatim;
            # otherwise fall back to the generic no-data hint + any
            # filesystem-permission context.
            if diagnostic and not diagnostic.startswith("empty:"):
                hint = f"Query failed: {diagnostic}"
            else:
                hint = "No data returned from query."
                idx_dir = _get_browse_dir()
                if not os.path.isdir(idx_dir):
                    hint += f" Indexes directory does not exist: {idx_dir}"
                elif not os.access(idx_dir, os.R_OK):
                    hint += (
                        f" Indexes directory is not readable (permission denied): "
                        f"{idx_dir} - check volume mount permissions."
                    )
            logger.warning(
                "[!] Query returned empty result: %s | hint: %s",
                repr(query[:120]), hint,
            )
            return jsonify({"status": "error", "message": hint})

        df = df.fillna("")
        # Scrub NaN/None inside list-typed cells (multi-value fields) that
        # fillna cannot reach - bare NaN in a list produces invalid JSON.
        for col in df.columns:
            if df[col].apply(lambda v: isinstance(v, list)).any():
                df[col] = df[col].apply(
                    lambda v: [x for x in v if not (isinstance(x, float) and x != x)]
                    if isinstance(v, list) else v
                )
        # Build time-range metadata from _epoch column if present
        time_range = None
        if "_epoch" in df.columns:
            try:
                epoch_col = pd.to_numeric(df["_epoch"], errors="coerce").dropna()
                if not epoch_col.empty:
                    time_range = {
                        "earliest": int(epoch_col.min()),
                        "latest": int(epoch_col.max()),
                    }
            except Exception:
                pass  # non-numeric _epoch - skip

        return jsonify({
            "status": "success",
            "results": df.to_dict(orient="records"),
            "column_names": df.columns.tolist(),
            "job_id": job_id,
            "time_range": time_range,
        })
    except Exception as exc:
        logger.exception("[x] Query error")
        return jsonify({"status": "error", "message": _safe_error_message(exc)})


@app.route("/api/tree", methods=["GET"])
def get_tree():
    """Return the directory tree of .parquet files."""
    global _browse_dir
    # Allow the UI to switch directories via query-param.
    new_dir = request.args.get("path", "").strip()
    if new_dir:
        if os.path.isdir(new_dir):
            _browse_dir = new_dir
        else:
            return jsonify({
                "status": "error",
                "message": f"Directory not found: {new_dir}",
                "tree": {},
                "current_dir": _get_browse_dir(),
            })

    browse = _get_browse_dir()
    tree = _build_tree(browse)
    resp: dict = {"status": "success", "tree": tree, "current_dir": browse}
    # Surface permission/existence issues to the UI so operators get
    # immediate feedback instead of a silently empty file browser.
    if not os.path.isdir(browse):
        resp["warning"] = f"Indexes directory does not exist: {browse}"
    elif not os.access(browse, os.R_OK):
        resp["warning"] = (
            f"Indexes directory is not readable (permission denied): {browse} "
            " - check volume mount permissions."
        )
    elif not tree["files"] and not tree["dirs"]:
        resp["warning"] = f"No .parquet files found under {browse}"
    return jsonify(resp)


@app.route("/api/save", methods=["POST"])
def save_results():
    """Return query results as a downloadable CSV or JSON file."""
    data = request.get_json(force=True, silent=True) or {}
    results = data.get("results", [])
    columns = data.get("columns", [])
    fmt = data.get("format", "csv").lower()

    if not results:
        return jsonify({"status": "error", "message": "No results to save."}), 400

    df = pd.DataFrame(results, columns=columns)
    buf = io.BytesIO()

    if fmt == "json":
        buf.write(df.to_json(orient="records", indent=2).encode("utf-8"))
        mimetype = "application/json"
        ext = "json"
    else:
        df.to_csv(buf, index=False)
        mimetype = "text/csv"
        ext = "csv"

    buf.seek(0)
    return send_file(
        buf,
        mimetype=mimetype,
        as_attachment=True,
        download_name=f"speakesquery_results.{ext}",
    )


@app.route("/api/lookups", methods=["GET"])
def list_lookups():
    """Return metadata for every lookup file in the lookups/ directory."""
    if not os.path.isdir(LOOKUPS_DIR):
        return jsonify({"status": "success", "files": []})

    files = []
    for entry in sorted(os.scandir(LOOKUPS_DIR), key=lambda e: e.name.lower()):
        if not entry.is_file():
            continue
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in LOOKUP_EXTENSIONS:
            continue
        stat = entry.stat()
        files.append({
            "name": entry.name,
            "type": ext.lstrip("."),
            "size_bytes": stat.st_size,
            "created": getattr(stat, 'st_birthtime', stat.st_ctime),
            "modified": stat.st_mtime,
            "accessed": stat.st_atime,
        })
    return jsonify({"status": "success", "files": files})


@app.route("/api/lookups/preview", methods=["GET"])
def preview_lookup():
    """Return the first N rows of a lookup file as JSON for previewing."""
    filename = request.args.get("file", "").strip()
    limit = min(int(request.args.get("limit", 200)), 5000)

    if not filename:
        return jsonify({"status": "error", "message": "Missing 'file' parameter."}), 400

    # Prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.join(LOOKUPS_DIR, safe_name)

    if not os.path.isfile(filepath):
        return jsonify({"status": "error", "message": f"File not found: {safe_name}"}), 404

    ext = os.path.splitext(safe_name)[1].lower()
    try:
        if ext == ".csv":
            df = pd.read_csv(filepath, nrows=limit)
        elif ext == ".tsv":
            df = pd.read_csv(filepath, sep="\t", nrows=limit)
        elif ext == ".parquet":
            df = pd.read_parquet(filepath)
            df = df.head(limit)
        elif ext == ".json":
            df = pd.read_json(filepath)
            df = df.head(limit)
        else:
            return jsonify({"status": "error", "message": f"Preview not supported for {ext} files."}), 400

        total_rows = None
        if ext in (".csv", ".tsv"):
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                total_rows = sum(1 for _ in f) - 1
        elif ext == ".parquet":
            import pyarrow.parquet as pq
            total_rows = pq.read_metadata(filepath).num_rows
        elif ext == ".json":
            total_rows = len(df)

        df = df.fillna("")
        return jsonify({
            "status": "success",
            "file": safe_name,
            "total_rows": total_rows,
            "preview_rows": len(df),
            "columns": df.columns.tolist(),
            "rows": df.to_dict(orient="records"),
        })
    except Exception as exc:
        logger.exception("[x] Lookup preview error")
        return jsonify({"status": "error", "message": _safe_error_message(exc)}), 500


@app.route("/api/lookups/upload", methods=["POST"])
def upload_lookup():
    """
    Accept a single file upload into the lookups/ directory.

    Security:
      - Extension whitelist (csv, json, tsv, parquet only).
      - Filename sanitisation - no path separators or special chars.
      - Size cap (200 MB).
      - Content validation - the file must actually parse as the
        declared format so that a renamed executable cannot sneak in.
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file provided."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"status": "error", "message": "Empty filename."}), 400

    # --- filename safety ---
    raw_name = os.path.basename(f.filename)  # strip any path components
    if not _SAFE_FILENAME_RE.match(raw_name):
        return jsonify({"status": "error", "message": "Filename contains disallowed characters."}), 400

    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(e.lstrip(".") for e in UPLOAD_EXTENSIONS))
        return jsonify({"status": "error", "message": f"Unsupported file type. Allowed: {allowed}"}), 400

    # --- size check (read into memory so we can validate content) ---
    data = f.read()
    if len(data) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify({"status": "error", "message": f"File exceeds {mb} MB limit."}), 400

    # --- content validation - the bytes must parse as the declared format ---
    try:
        if ext == ".csv":
            pd.read_csv(io.BytesIO(data), nrows=5)
        elif ext == ".tsv":
            pd.read_csv(io.BytesIO(data), sep="\t", nrows=5)
        elif ext == ".json":
            pd.read_json(io.BytesIO(data))
        elif ext == ".parquet":
            import pyarrow.parquet as pq
            pq.read_metadata(io.BytesIO(data))  # validates magic bytes + schema
    except Exception:
        return jsonify({"status": "error", "message": f"File content is not valid {ext.lstrip('.')} data."}), 400

    # --- write to disk ---
    os.makedirs(LOOKUPS_DIR, exist_ok=True)
    dest = os.path.join(LOOKUPS_DIR, raw_name)
    with open(dest, "wb") as out:
        out.write(data)

    logger.info("[i] Lookup uploaded: %s (%d bytes)", raw_name, len(data))
    return jsonify({"status": "success", "message": f"Uploaded {raw_name}"})


@app.route("/api/lookups/delete", methods=["POST"])
def delete_lookup():
    """Delete a lookup file by name."""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get("file", "").strip()
    if not filename:
        return jsonify({"status": "error", "message": "Missing 'file' parameter."}), 400

    safe_name = os.path.basename(filename)
    filepath = os.path.join(LOOKUPS_DIR, safe_name)

    if not os.path.isfile(filepath):
        return jsonify({"status": "error", "message": f"File not found: {safe_name}"}), 404

    os.remove(filepath)
    logger.info("[i] Lookup deleted: %s", safe_name)
    return jsonify({"status": "success", "message": f"Deleted {safe_name}"})


@app.route("/api/lookups/download", methods=["GET"])
def download_lookup():
    """Download a lookup file."""
    filename = request.args.get("file", "").strip()
    if not filename:
        return jsonify({"status": "error", "message": "Missing 'file' parameter."}), 400

    safe_name = os.path.basename(filename)
    filepath = os.path.join(LOOKUPS_DIR, safe_name)

    if not os.path.isfile(filepath):
        return jsonify({"status": "error", "message": f"File not found: {safe_name}"}), 404

    return send_file(filepath, as_attachment=True, download_name=safe_name)


# ---------------------------------------------------------------------------
# Index Import API
# ---------------------------------------------------------------------------

def _resolve_indexes_dir() -> Path:
    """Return the indexes directory as a Path, with fallback."""
    try:
        return _get_global_settings().indexes_dir()
    except Exception:
        return Path(os.path.join(PROJECT_ROOT, "indexes"))


@app.route("/api/indexes/import", methods=["POST"])
def import_to_index():
    """
    Import a CSV, Parquet, or SQLite3 file as a queryable index.

    Expects ``multipart/form-data`` with:
      - ``file``       (required) - the file to import.
      - ``index_name`` (required) - target subdirectory under ``indexes/``.
      - ``date_field`` (optional) - column name to derive ``_epoch`` from.
      - ``table``      (optional) - for SQLite only, import a single table.
    """
    # --- file presence ---
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file provided."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"status": "error", "message": "Empty filename."}), 400

    # --- index_name presence & validation ---
    index_name = request.form.get("index_name", "").strip()
    if not index_name:
        return jsonify({"status": "error", "message": "Missing 'index_name' parameter."}), 400

    from scheduled_input_engine.store import ScheduledInputStore
    try:
        ScheduledInputStore.validate_subdirectory(index_name)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    # --- filename safety ---
    raw_name = os.path.basename(f.filename)
    if not _SAFE_FILENAME_RE.match(raw_name):
        return jsonify({"status": "error", "message": "Filename contains disallowed characters."}), 400

    ext = os.path.splitext(raw_name)[1].lower()
    if ext not in IMPORT_EXTENSIONS:
        allowed = ", ".join(sorted(e.lstrip(".") for e in IMPORT_EXTENSIONS))
        return jsonify({"status": "error", "message": f"Unsupported file type. Allowed: {allowed}"}), 400

    # --- size check ---
    data = f.read()
    if len(data) > MAX_IMPORT_BYTES:
        mb = MAX_IMPORT_BYTES // (1024 * 1024)
        return jsonify({"status": "error", "message": f"File exceeds {mb} MB limit."}), 400

    # --- optional form fields ---
    date_field = request.form.get("date_field", "").strip() or None
    table_name = request.form.get("table", "").strip() or None

    # --- resolve indexes directory & writer ---
    indexes_dir = _resolve_indexes_dir()
    from scheduled_input_engine.parquet_writer import ParquetWriter
    writer = ParquetWriter(str(indexes_dir))

    files_written = 0
    total_rows = 0
    table_details = []

    try:
        if ext in (".sqlite", ".sqlite3", ".db"):
            # --- SQLite: write to temp file, read table(s), convert ---
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
            try:
                os.write(tmp_fd, data)
                os.close(tmp_fd)

                conn = sqlite3.connect(tmp_path)
                try:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                    all_tables = [row[0] for row in cursor.fetchall()]
                    if not all_tables:
                        return jsonify({"status": "error", "message": "SQLite file contains no tables."}), 400

                    if table_name:
                        if table_name not in all_tables:
                            return jsonify({
                                "status": "error",
                                "message": f"Table '{table_name}' not found. Available: {', '.join(all_tables)}"
                            }), 400
                        tables_to_import = [table_name]
                    else:
                        tables_to_import = all_tables

                    for tbl in tables_to_import:
                        # Quote table name to prevent SQL injection
                        df = pd.read_sql_query(
                            f'SELECT * FROM "{tbl}"', conn
                        )
                        if df.empty:
                            continue
                        df = _ensure_epoch_column(df, date_field)
                        writer.write_atomic(df, index_name)
                        files_written += 1
                        total_rows += len(df)
                        table_details.append({"name": tbl, "rows": len(df)})
                finally:
                    conn.close()
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        elif ext == ".csv":
            # --- content validation ---
            try:
                pd.read_csv(io.BytesIO(data), nrows=5)
            except Exception:
                return jsonify({"status": "error", "message": "File content is not valid CSV data."}), 400

            df = pd.read_csv(io.BytesIO(data))
            df = _ensure_epoch_column(df, date_field)
            writer.write_atomic(df, index_name)
            files_written = 1
            total_rows = len(df)

        elif ext == ".parquet":
            # --- content validation ---
            try:
                import pyarrow.parquet as pq
                pq.read_metadata(io.BytesIO(data))
            except Exception:
                return jsonify({"status": "error", "message": "File content is not valid Parquet data."}), 400

            df = pd.read_parquet(io.BytesIO(data))
            df = _ensure_epoch_column(df, date_field)
            writer.write_atomic(df, index_name)
            files_written = 1
            total_rows = len(df)

    except Exception as exc:
        logger.exception("[!] Index import failed: %s", exc)
        return jsonify({"status": "error", "message": f"Import failed: {_safe_error_message(exc)}"}), 500

    msg = f"Imported {total_rows:,} rows into index={index_name} ({files_written} file(s))."
    logger.info("[i] Index import: %s - %s", raw_name, msg)

    result = {
        "status": "success",
        "message": msg,
        "files_written": files_written,
        "total_rows": total_rows,
    }
    if table_details:
        result["tables"] = table_details
    return jsonify(result)


@app.route("/api/indexes/import/sqlite-tables", methods=["POST"])
def import_sqlite_tables():
    """
    Return the list of table names in an uploaded SQLite file.

    Used by the UI to let the user pick which table(s) to import.
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file provided."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"status": "error", "message": "Empty filename."}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".sqlite", ".sqlite3", ".db"):
        return jsonify({"status": "error", "message": "File is not a SQLite database."}), 400

    data = f.read()
    if len(data) > MAX_IMPORT_BYTES:
        mb = MAX_IMPORT_BYTES // (1024 * 1024)
        return jsonify({"status": "error", "message": f"File exceeds {mb} MB limit."}), 400

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
    try:
        os.write(tmp_fd, data)
        os.close(tmp_fd)

        conn = sqlite3.connect(tmp_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()
    except Exception:
        return jsonify({"status": "error", "message": "File is not a valid SQLite database."}), 400
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return jsonify({"status": "success", "tables": tables})


# ---------------------------------------------------------------------------
# Scheduled Inputs API
# ---------------------------------------------------------------------------

def _get_engine():
    """Lazy accessor for the scheduled input engine."""
    from scheduled_input_engine import get_engine
    return get_engine()


@app.route("/api/si/list", methods=["GET"])
def si_list():
    """Return all scheduled inputs, each enriched with credential + last-run status.

    For every task, we attach:
      - ``requires_credentials``: list of credential keys the task's
        library script declares it needs (empty if no library match or
        the script is no-auth).
      - ``missing_credentials``: subset of ``requires_credentials`` that
        is NOT yet populated in the vault for this task_id.
      - ``last_run_at``: epoch seconds of the most recent execution
        (any status), or ``None`` if the task has never run.
      - ``last_run_status``: ``"success"`` | ``"failed"`` | ``None``.
      - ``last_run_error``: error message from the last run if it
        failed, else ``None``. UI renders as tooltip on the red pill.

    The UI renders pills from these fields (green "Ready" vs yellow
    "Needs X, Y" for credentials; timestamp or "Never" for last-run)
    so operators can see at a glance which deployments still need
    credential setup or which have never ingested.
    """
    engine = _get_engine()
    tasks = engine.store.list_scheduled_inputs()

    # Load library scripts (cheap, lazy-cached inside script_library module).
    try:
        from script_library import list_scripts as _list_lib
        lib_scripts = _list_lib()
    except Exception:  # pragma: no cover - defensive fallback
        lib_scripts = []

    vault = getattr(engine, "_vault", None)

    for task in tasks:
        subdir = (task.get("subdirectory") or "").strip("/")
        required: list[str] = []
        if subdir:
            for s in lib_scripts:
                if (s.get("suggested_subdirectory") or "").strip("/") == subdir:
                    required = list(s.get("requires_credentials") or [])
                    break
        task["requires_credentials"] = required

        missing: list[str] = []
        if required and vault is not None:
            try:
                present = set(vault.list_keys(task["id"]))
            except Exception:
                present = set()
            missing = [k for k in required if k not in present]
        task["missing_credentials"] = missing

        # Attach last-run metadata so the UI's "Last Run" column can
        # render "Never" / "5m ago" / red-pill-with-error without
        # making an N+1 round-trip per row.
        try:
            last_run = engine.store.get_last_run(task["id"])
        except Exception:  # pragma: no cover - defensive
            last_run = None
        if last_run:
            # Handle both legacy (start_time/end_time) and current
            # (execution_start_time/execution_end_time) schemas via
            # whichever key is populated.
            last_epoch = (
                last_run.get("execution_start_time")
                or last_run.get("start_time")
            )
            task["last_run_at"] = float(last_epoch) if last_epoch else None
            task["last_run_status"] = last_run.get("status")
            task["last_run_error"] = last_run.get("error_message")
        else:
            task["last_run_at"] = None
            task["last_run_status"] = None
            task["last_run_error"] = None

    return jsonify({"status": "success", "tasks": tasks})


@app.route("/api/si/add", methods=["POST"])
def si_add():
    """Create a new scheduled input.

    When ``run_on_create`` is ``True`` (the default), the task is
    executed immediately after save so the parquet + schema exist right
    away - no waiting for the first cron tick. Pass ``run_on_create:
    false`` in the POST body to opt out (e.g. during bulk imports).

    Response shape:
      {"status": "success", "task": {...},
       "first_run": {...} | null}    # present when run_on_create=true

    ``first_run`` is the ``execution_history`` row (status, runtime,
    error_message, etc.) so the UI can surface any first-run failure
    inline on the success toast rather than making the user scroll the
    History modal to find out why "Last Run" is red.
    """
    data = request.get_json(force=True, silent=True) or {}
    required = ("title", "code", "cron_schedule")
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"status": "error", "message": f"Missing fields: {', '.join(missing)}"}), 400

    engine = _get_engine()
    add_kwargs = dict(
        title=data["title"],
        code=data["code"],
        cron_schedule=data["cron_schedule"],
        description=data.get("description", ""),
        overwrite=data.get("overwrite", "false"),
        subdirectory=data.get("subdirectory", ""),
        api_url=data.get("api_url"),
    )
    # Forward trust_level only when explicitly provided so the store's
    # "sandboxed" default still applies for callers that omit it.
    if "trust_level" in data:
        add_kwargs["trust_level"] = data["trust_level"]

    # Per-task timeout: explicit payload value wins. If absent AND the
    # subdirectory matches a library script that declares a
    # ``suggested_timeout_seconds`` hint, auto-populate from the hint.
    # Otherwise the task stores NULL → engine falls back to the global
    # ``default_script_timeout_seconds`` at run time.
    timeout_seconds = data.get("timeout_seconds")
    if not timeout_seconds:
        sub = (data.get("subdirectory") or "").strip("/")
        if sub:
            try:
                from script_library import list_scripts as _list_lib
                for s in _list_lib():
                    if (s.get("suggested_subdirectory") or "").strip("/") == sub:
                        hint = s.get("suggested_timeout_seconds")
                        if hint:
                            timeout_seconds = int(hint)
                        break
            except Exception:
                pass
    if timeout_seconds:
        add_kwargs["timeout_seconds"] = timeout_seconds

    # Default: run the task immediately so first data + schema land
    # right away. Operator can opt out with ``run_on_create: false``
    # (set by the UI when the "Run immediately after save" checkbox
    # is unchecked).
    run_on_create = data.get("run_on_create", True)

    try:
        task = engine.add_task(**add_kwargs)
        # Migrate any staging credentials (script_id=0) to the new task
        engine.migrate_staging_credentials(task["id"])
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    first_run = None
    if run_on_create:
        try:
            first_run = engine.run_task_now(task["id"])
        except Exception as exc:
            # Task was saved successfully; first run failed. Surface the
            # error in the response but don't undo the save - the user
            # can fix the credential / API / code issue and hit Run Now
            # again from the list.
            first_run = {
                "status": "failed",
                "error_message": f"{type(exc).__name__}: {exc}",
            }

    return jsonify({
        "status": "success",
        "task": task,
        "first_run": first_run,
    })


@app.route("/api/si/<int:task_id>/run", methods=["POST"])
def si_run_now(task_id):
    """Trigger an immediate, synchronous ingestion of this task.

    Bypasses the cron schedule - same code path as APScheduler's
    periodic trigger. Blocks until the run completes (subject to the
    task's own execution timeout) and returns the resulting
    ``execution_history`` row so the UI can update the Last Run column
    immediately.

    Response shape:
      {"status": "success", "run": {task_id, status, runtime,
                                     error_message, ...}}
    """
    engine = _get_engine()
    try:
        run = engine.run_task_now(task_id)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
        }), 500
    return jsonify({"status": "success", "run": run})


@app.route("/api/si/<int:task_id>", methods=["GET"])
def si_get(task_id):
    """Return a single scheduled input."""
    engine = _get_engine()
    try:
        task = engine.store.get_scheduled_input(task_id)
        return jsonify({"status": "success", "task": task})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/si/<int:task_id>", methods=["PUT"])
def si_update(task_id):
    """Update a scheduled input."""
    data = request.get_json(force=True, silent=True) or {}
    engine = _get_engine()
    try:
        task = engine.update_task(task_id, **data)
        return jsonify({"status": "success", "task": task})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/si/<int:task_id>", methods=["DELETE"])
def si_delete(task_id):
    """Delete a scheduled input."""
    engine = _get_engine()
    engine.delete_task(task_id)
    return jsonify({"status": "success"})


@app.route("/api/si/<int:task_id>/test", methods=["POST"])
def si_test(task_id):
    """Mandatory test gate: execute code and return structured pass/fail result."""
    engine = _get_engine()
    try:
        task = engine.store.get_scheduled_input(task_id)
        # Preserve the task's stored trust_level - a task saved as
        # "unrestricted" must test under that mode too, otherwise
        # the test re-runs against RestrictedPython and fails for
        # any _pro pattern the sandbox rejects.
        result = engine.test_task(
            task["code"], task_id=task_id,
            trust_level=task.get("trust_level", "sandboxed"),
        )
        return jsonify({"status": "success", "summary": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logger.exception("[x] Scheduled input test error")
        return jsonify({"status": "error", "message": _safe_error_message(exc)}), 500


@app.route("/api/si/lint", methods=["POST"])
def si_lint():
    """Quick syntax check - runs compile() and returns structured errors."""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"status": "ok", "errors": []})
    errors = []
    try:
        compile(code, "<user_code>", "exec")
    except SyntaxError as exc:
        errors.append({
            "line": exc.lineno,
            "col": exc.offset,
            "message": exc.msg or str(exc),
            "text": exc.text.rstrip() if exc.text else "",
        })
    return jsonify({"status": "ok", "errors": errors})


@app.route("/api/si/test-code", methods=["POST"])
def si_test_code():
    """Test arbitrary ingestion code (before saving). Mandatory test gate.

    Accepts ``timeout_seconds`` in the payload so the UI's pre-save
    Test respects the operator's chosen wall-clock cap - critical for
    the chicken-and-egg case where a legitimately-slow library script
    (e.g. options_unusual_activity_pro with a 300s hint) can't be
    saved until Test passes, but Test hits the global 120s. The UI
    forwards the Timeout field's value so Test honors it too.
    """
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"status": "error", "message": "No code provided."}), 400
    engine = _get_engine()
    try:
        # Use staging credentials (script_id=0) when testing a new script
        task_id = data.get("task_id")
        if task_id is None:
            task_id = 0
        # Forward trust_level so _pro scripts test under the same mode
        # they'll save with; default to "sandboxed" when unspecified.
        kwargs = {}
        if "trust_level" in data:
            kwargs["trust_level"] = data["trust_level"]
        # Per-request timeout override. Clamped to the same [10, 3600]
        # range as persisted task timeouts so a rogue payload can't tie
        # up a worker indefinitely.
        timeout_raw = data.get("timeout_seconds")
        if timeout_raw not in (None, ""):
            try:
                t = int(timeout_raw)
                if 10 <= t <= 3600:
                    kwargs["timeout_seconds"] = t
            except (TypeError, ValueError):
                pass  # silently ignore garbage - fall back to global
        result = engine.test_task(code, task_id=task_id, **kwargs)
        return jsonify({"status": "success", "summary": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logger.exception("[x] Code test error")
        return jsonify({"status": "error", "message": _safe_error_message(exc)}), 500


@app.route("/api/si/<int:task_id>/toggle", methods=["POST"])
def si_toggle(task_id):
    """Enable or disable a scheduled input."""
    data = request.get_json(force=True, silent=True) or {}
    engine = _get_engine()
    try:
        disabled = not data.get("enabled", True)
        task = engine.update_task(task_id, disabled=disabled)
        return jsonify({"status": "success", "task": task})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/si/status", methods=["GET"])
def si_status():
    """Return scheduler status: all jobs with next_run_time."""
    engine = _get_engine()
    return jsonify({"status": "success", "jobs": engine.get_status()})


@app.route("/api/si/history", methods=["GET"])
def si_history():
    """Return execution history, optionally filtered by task_id."""
    engine = _get_engine()
    task_id = request.args.get("task_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    history = engine.store.get_execution_history(task_id=task_id, limit=limit)
    return jsonify({"status": "success", "history": history})


@app.route("/api/si/check-subdirectory", methods=["GET"])
def si_check_subdirectory():
    """Check if a subdirectory already exists under indexes/ and validate the path."""
    subdir = request.args.get("path", "").strip().replace("\\", "/").strip("/")
    if not subdir:
        return jsonify({"status": "success", "exists": False, "valid": True})

    # Validate via store logic
    from scheduled_input_engine.store import ScheduledInputStore
    try:
        ScheduledInputStore.validate_subdirectory(subdir)
    except ValueError as exc:
        return jsonify({"status": "success", "exists": False, "valid": False, "error": str(exc)})

    # Check filesystem existence
    try:
        settings = _get_global_settings()
        indexes_dir = settings.indexes_dir()
    except Exception:
        indexes_dir = Path(os.path.join(os.path.dirname(os.path.dirname(__file__)), "indexes"))

    target = (indexes_dir / subdir).resolve()
    if not target.is_relative_to(indexes_dir.resolve()):
        return jsonify({"status": "success", "exists": False, "valid": False,
                        "error": "Path traversal detected."})

    exists = target.is_dir()
    has_files = False
    if exists:
        has_files = any(target.glob("*.parquet"))

    return jsonify({
        "status": "success",
        "exists": exists,
        "has_files": has_files,
        "valid": True,
    })


# ---------------------------------------------------------------------------
# Saved Searches  (/api/ss/*)
# ---------------------------------------------------------------------------

from saved_search_store import SavedSearchStore

_ss_store = SavedSearchStore()
_ss_store.initialize()


@app.route("/api/ss/list", methods=["GET"])
def ss_list():
    """Return all saved searches with next_run_time."""
    searches = _ss_store.list_searches()
    return jsonify({"status": "success", "searches": searches})


@app.route("/api/ss/create", methods=["POST"])
def ss_create():
    """Create a new saved search YAML."""
    data = request.get_json(force=True, silent=True) or {}
    required = ("name", "query", "cron_schedule", "lookback", "email_address")
    missing = [f for f in required if not data.get(f, "").strip()]
    if missing:
        return jsonify({"status": "error", "message": f"Missing required fields: {', '.join(missing)}"}), 400

    overwrite = data.pop("overwrite", False)
    try:
        result = _ss_store.save_search(data, overwrite=bool(overwrite))
        return jsonify({"status": "success", "search": result})
    except FileExistsError:
        return jsonify({"status": "exists", "message": f'A saved search named "{data["name"]}" already exists.'})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/ss/<name>", methods=["GET"])
def ss_get(name):
    """Return a single saved search by name."""
    try:
        search = _ss_store.get_search(name)
        return jsonify({"status": "success", "search": search})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/ss/<name>", methods=["PUT"])
def ss_update(name):
    """Update an existing saved search."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _ss_store.update_search(name, data)
        return jsonify({"status": "success", "search": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/ss/<name>", methods=["DELETE"])
def ss_delete(name):
    """Soft-delete a saved search (archived in last_chance.sqlite for 30 days)."""
    try:
        _ss_store.delete_search(name)
        return jsonify({"status": "success", "message": f'Saved search "{name}" deleted (recoverable for 30 days).'})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/ss/<name>/yaml", methods=["GET"])
def ss_yaml(name):
    """Return the raw YAML text for a saved search."""
    try:
        raw = _ss_store.get_search_yaml(name)
        return jsonify({"status": "success", "yaml": raw})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/ss/validate-tokens", methods=["POST"])
def ss_validate_tokens():
    """
    Validate that $token$ variables in the email body are populated in
    historical query results.  Runs the query with a configurable lookback
    (1-90 days, integer seconds fidelity) and checks each token column for
    null / blank values.
    """
    # H-CE-3 (2026-04-22): diagnostic variant exposes the underlying
    # exception class in the ``diagnostic`` string so the UI error message
    # is precise instead of a generic "Query execution failed".
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics

    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    tokens = data.get("tokens", [])
    validation_days = max(1, min(90, int(data.get("validation_days", 30))))

    if not query:
        return jsonify({"status": "error", "message": "Query is required for token validation."}), 400
    if not tokens:
        return jsonify({"status": "success", "message": "No tokens to validate."})

    # Build a lookback suffix to inject a time window into the query.
    # Convert days to seconds for integer-second fidelity.
    lookback_seconds = validation_days * 86400

    df, _job_id, diagnostic = process_query_with_diagnostics(query)

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        if diagnostic and not diagnostic.startswith("empty:"):
            msg = f"Query execution failed: {diagnostic}"
            logger.warning("[!] Token validation query failed: %s", diagnostic)
            return jsonify({"status": "error", "message": msg}), 400
        return jsonify({
            "status": "error",
            "message": (
                f"Query returned no results over the validation window "
                f"({validation_days} days). Cannot verify tokens."
            ),
        })

    # Check each token against the result columns
    null_tokens = []
    available_cols = set(df.columns.tolist())
    total_rows = len(df)

    for token in tokens:
        if token not in available_cols:
            null_tokens.append({
                "token": token,
                "null_count": total_rows,
                "total_rows": total_rows,
                "reason": "column_missing",
            })
        else:
            col = df[token]
            # Count nulls, empty strings, and empty lists
            null_count = int(col.apply(
                lambda v: v is None or v == "" or (isinstance(v, float) and v != v)
                or (isinstance(v, list) and len(v) == 0)
            ).sum())
            if null_count > 0:
                null_tokens.append({
                    "token": token,
                    "null_count": null_count,
                    "total_rows": total_rows,
                    "reason": "null_values",
                })

    if null_tokens:
        return jsonify({
            "status": "warning",
            "null_tokens": null_tokens,
            "days_checked": validation_days,
        })

    return jsonify({"status": "success", "message": "All tokens validated.", "days_checked": validation_days})


# ---------------------------------------------------------------------------
# Macros  (/api/macros/*)
# ---------------------------------------------------------------------------

from macro_store import MacroStore
from handlers.MacroHandler import MacroHandler as _MacroHandlerCls

_macro_store = MacroStore()
_macro_store.initialize()
_macro_handler = _MacroHandlerCls(_macro_store)


@app.route("/api/macros/list", methods=["GET"])
def macros_list():
    """Return all macros."""
    macros = _macro_store.list_macros()
    return jsonify({"status": "success", "macros": macros})


@app.route("/api/macros/create", methods=["POST"])
def macros_create():
    """Create a new macro YAML."""
    data = request.get_json(force=True, silent=True) or {}
    required = ("name", "definition")
    missing = [f for f in required if not (data.get(f) or "").strip()]
    if missing:
        return jsonify({"status": "error", "message": f"Missing required fields: {', '.join(missing)}"}), 400

    overwrite = data.pop("overwrite", False)
    try:
        result = _macro_store.save_macro(data, overwrite=bool(overwrite))
        return jsonify({"status": "success", "macro": result})
    except FileExistsError:
        return jsonify({"status": "exists", "message": f'A macro named "{data["name"]}" already exists.'})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/macros/<name>", methods=["GET"])
def macros_get(name):
    """Return a single macro by name."""
    try:
        macro = _macro_store.get_macro(name)
        return jsonify({"status": "success", "macro": macro})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/macros/<name>", methods=["PUT"])
def macros_update(name):
    """Update an existing macro."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _macro_store.update_macro(name, data)
        return jsonify({"status": "success", "macro": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/macros/<name>", methods=["DELETE"])
def macros_delete(name):
    """Delete a macro."""
    try:
        _macro_store.delete_macro(name)
        return jsonify({"status": "success", "message": f'Macro "{name}" deleted.'})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/macros/expand", methods=["POST"])
def macros_expand():
    """Expand macro calls in a query string without executing."""
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Query is required."}), 400

    try:
        expanded = _macro_handler.expand(query)
        return jsonify({"status": "success", "original": query, "expanded": expanded})
    except (ValueError, RecursionError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/macros/expand-annotated", methods=["POST"])
def macros_expand_annotated():
    """Expand macro calls with inline annotation comments.

    Request JSON:
        query (str):     The raw query with backtick macro calls.
        depth (int):     Number of nesting levels to expand.
                         0 = expand all levels (default).
                         1 = first-level only, 2 = two levels, etc.
        max_depth (int): Hard ceiling on expansion depth (default 100).

    Response JSON:
        status (str):    "success" or "error"
        original (str):  The original query.
        expanded (str):  The annotated expanded query.
    """
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Query is required."}), 400

    try:
        depth = int(data.get("depth", 0))
        max_depth = int(data.get("max_depth", 100))
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Depth must be a number."}), 400

    try:
        expanded = _macro_handler.expand_annotated(query, target_depth=depth, max_depth=max_depth)
        return jsonify({"status": "success", "original": query, "expanded": expanded})
    except (ValueError, RecursionError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/macros/test", methods=["POST"])
def macros_test():
    """Expand macros in a query, then execute it and return results."""
    # H-CE-3 (2026-04-22): diagnostic variant surfaces real errors
    # (e.g. "InvalidInputException: column 'foo' not found") instead of
    # a generic "Query execution failed".
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics

    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Query is required."}), 400

    try:
        expanded = _macro_handler.expand(query)
    except (ValueError, RecursionError) as exc:
        return jsonify({"status": "error", "message": f"Macro expansion failed: {exc}"}), 400

    df, _job_id, diagnostic = process_query_with_diagnostics(expanded)

    # Non-empty diagnostic that isn't just an empty-rows signal → failure.
    if diagnostic and not diagnostic.startswith("empty:"):
        logger.warning("[!] Macro test query execution failed: %s", diagnostic)
        return jsonify({
            "status": "error",
            "message": f"Query execution failed: {diagnostic}",
        }), 400

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return jsonify({
            "status": "success",
            "expanded": expanded,
            "columns": [],
            "rows": [],
            "total": 0,
        })

    columns = df.columns.tolist()
    rows = df.head(500).fillna("").to_dict(orient="records")
    return jsonify({
        "status": "success",
        "expanded": expanded,
        "columns": columns,
        "rows": rows,
        "total": len(df),
    })


# ---------------------------------------------------------------------------
# Schedule Visualization  (/api/schedule/*)
# ---------------------------------------------------------------------------

@app.route("/api/schedule/heatmap", methods=["GET"])
def schedule_heatmap():
    """Return the unified schedule summary (jobs + per-hour distributions
    + recent-run history) used by the Schedule Visualization page.

    Query params:
      - ``lookahead_days``    (int, default 7, max 30) - cron expansion window
      - ``history_runs``      (int, default 5, max 50) - runs to average per job
      - ``history_days``      (int, default 30, max 180) - log lookback window
      - ``include_disabled``  (bool, default false) - count disabled jobs too
    """
    from schedule_visualization import build_schedule_summary

    def _intp(name, default, lo, hi):
        try:
            v = int(request.args.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    lookahead = _intp("lookahead_days", 7, 1, 30)
    hruns = _intp("history_runs", 5, 1, 50)
    hdays = _intp("history_days", 30, 1, 180)
    include_disabled_str = (request.args.get("include_disabled") or "").lower()
    include_disabled = include_disabled_str in ("1", "true", "yes", "on")

    try:
        summary = build_schedule_summary(
            lookahead_days=lookahead,
            history_lookback_runs=hruns,
            history_lookback_days=hdays,
            include_disabled=include_disabled,
        )
        return jsonify({"status": "success", **summary})
    except Exception as exc:
        logger.exception("[!] Schedule heatmap build failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/schedule/pdf", methods=["GET"])
def schedule_pdf():
    """Generate a polished PDF report of the entire scheduled-job
    landscape: cover page, executive summary, heatmaps, recent activity
    charts, per-AG health, anomalies, and an all-jobs appendix.

    Same query params as ``/api/schedule/heatmap`` plus ``activity_days``
    (default 14, max 365) for the bar/line chart window.

    Returns ``application/pdf`` with a filename suggesting the timestamp.
    Returns 503 if WeasyPrint isn't installed (with a hint in the body).
    """
    from datetime import datetime as _dt, timezone as _tz

    def _intp(name, default, lo, hi):
        try:
            v = int(request.args.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    lookahead = _intp("lookahead_days", 7, 1, 30)
    hruns = _intp("history_runs", 5, 1, 50)
    hdays = _intp("history_days", 30, 1, 180)
    activity_days = _intp("activity_days", 14, 1, 365)
    include_disabled_str = (request.args.get("include_disabled") or "").lower()
    include_disabled = include_disabled_str in ("1", "true", "yes", "on")

    try:
        from tools.schedule_pdf import build_pdf_bytes
    except ImportError as exc:
        logger.warning("[!] schedule_pdf module not importable: %s", exc)
        return jsonify({
            "status": "error",
            "message": "Schedule PDF module unavailable: " + str(exc),
        }), 503

    try:
        pdf_bytes = build_pdf_bytes(
            lookahead_days=lookahead,
            history_runs=hruns,
            history_days=hdays,
            include_disabled=include_disabled,
            activity_days=activity_days,
        )
    except RuntimeError as exc:
        # WeasyPrint not installed - actionable hint
        logger.warning("[!] schedule PDF generation failed: %s", exc)
        return jsonify({
            "status": "error",
            "message": str(exc),
            "hint": (
                "Install WeasyPrint with `pip install weasyprint`. "
                "On macOS, you may need `brew install pango cairo` first. "
                "On Linux, install libpango/libcairo via apt/yum."
            ),
        }), 503
    except Exception as exc:
        logger.exception("[!] schedule PDF render failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500

    ts = _dt.now(_tz.utc).strftime("%Y%m%d-%H%M")
    filename = f"speakesquery-schedule-report-{ts}.pdf"
    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/api/schedule/volume", methods=["GET"])
def schedule_volume():
    """Wave 6 (2026-04-26): per-day volume buckets for the bar + line
    charts on the Schedule page.

    Aggregates the last ``days`` days of activity from
    ``indexes/logs/{ingestion,search_runs,alert_groups}/*.parquet``.
    Returns one bucket per UTC day, oldest → newest, with empty days
    pre-zeroed so the chart x-axis is uniform.

    Query params:
      - ``days``  (int, default 14, max 365) - window size

    Response:
        {
          "status": "success",
          "days":   14,
          "buckets": [
            {"date": "2026-04-12", "ingestion_runs": 35,
             "search_runs": 200, "ag_dispatches": 11,
             "rows_ingested": 1542},
            ...
          ]
        }
    """
    from schedule_visualization import compute_daily_volume

    try:
        days = int(request.args.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 365))

    try:
        buckets = compute_daily_volume(days=days)
        return jsonify({
            "status": "success",
            "days": days,
            "buckets": buckets,
        })
    except Exception as exc:
        logger.exception("[!] Schedule volume aggregation failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Email Groups  (/api/email-groups/*)
# ---------------------------------------------------------------------------

from email_group_store import EmailGroupStore, resolve_recipients_for_send

_email_group_store = EmailGroupStore()
_email_group_store.initialize()


@app.route("/api/email-groups/list", methods=["GET"])
def email_groups_list():
    """Return all email groups."""
    groups = _email_group_store.list_groups()
    return jsonify({"status": "success", "groups": groups})


@app.route("/api/email-groups/create", methods=["POST"])
def email_groups_create():
    """Create a new email group YAML."""
    data = request.get_json(force=True, silent=True) or {}
    if not (data.get("name") or "").strip():
        return jsonify({"status": "error", "message": "Missing required field: name"}), 400
    if not data.get("email_addresses"):
        return jsonify(
            {"status": "error", "message": "Missing required field: email_addresses"}
        ), 400
    overwrite = data.pop("overwrite", False)
    try:
        result = _email_group_store.save_group(data, overwrite=bool(overwrite))
        return jsonify({"status": "success", "group": result})
    except FileExistsError:
        return jsonify(
            {
                "status": "exists",
                "message": f'An email group named "{data["name"]}" already exists.',
            }
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/email-groups/<name>", methods=["GET"])
def email_groups_get(name):
    """Return a single email group by name. Includes a resolved
    preview of the literal recipients (groups expanded) so the UI can
    show the user what an actual send will hit."""
    try:
        group = _email_group_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    try:
        resolved = _email_group_store.resolve_recipients(
            group.get("email_addresses", [])
        )
    except Exception:
        resolved = []
    return jsonify({"status": "success", "group": group, "resolved_recipients": resolved})


@app.route("/api/email-groups/<name>", methods=["PUT"])
def email_groups_update(name):
    """Update an existing email group."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _email_group_store.update_group(name, data)
        return jsonify({"status": "success", "group": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/email-groups/<name>", methods=["DELETE"])
def email_groups_delete(name):
    """Delete an email group."""
    try:
        _email_group_store.delete_group(name)
        return jsonify(
            {"status": "success", "message": f'Email group "{name}" deleted.'}
        )
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/email-groups/preview", methods=["POST"])
def email_groups_preview():
    """Resolve a raw recipients string into the literal list that would
    be sent. Useful for preview-before-save in the UI when a saved
    search or alert group references ``@group_name``.
    """
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("recipients", "")
    try:
        resolved = resolve_recipients_for_send(raw, store=_email_group_store)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "success", "resolved_recipients": resolved})


# ---------------------------------------------------------------------------
# Analyzer Prompts API  (/api/analyzer-prompts/*)
# ---------------------------------------------------------------------------

from analyzer_prompt_store import AnalyzerPromptStore

_ap_store = AnalyzerPromptStore()
_ap_store.initialize()


@app.route("/api/analyzer-prompts/list", methods=["GET"])
def ap_list():
    """Return all analyzer prompts."""
    prompts = _ap_store.list_prompts()
    return jsonify({"status": "success", "prompts": prompts})


@app.route("/api/analyzer-prompts/create", methods=["POST"])
def ap_create():
    """Create a new analyzer prompt YAML."""
    data = request.get_json(force=True, silent=True) or {}
    required = ("name", "prompt_text")
    missing = [f for f in required if not (data.get(f) or "").strip()]
    if missing:
        return jsonify({"status": "error", "message": f"Missing required fields: {', '.join(missing)}"}), 400

    overwrite = data.pop("overwrite", False)
    try:
        result = _ap_store.save_prompt(data, overwrite=bool(overwrite))
        return jsonify({"status": "success", "prompt": result})
    except FileExistsError:
        return jsonify({"status": "exists", "message": f'An analyzer prompt named "{data["name"]}" already exists.'})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/analyzer-prompts/<name>", methods=["GET"])
def ap_get(name):
    """Return a single analyzer prompt by name."""
    try:
        prompt = _ap_store.get_prompt(name)
        return jsonify({"status": "success", "prompt": prompt})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/analyzer-prompts/<name>", methods=["PUT"])
def ap_update(name):
    """Update an existing analyzer prompt."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _ap_store.update_prompt(name, data)
        return jsonify({"status": "success", "prompt": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/analyzer-prompts/<name>", methods=["DELETE"])
def ap_delete(name):
    """Soft-delete an analyzer prompt (archived in last_chance.sqlite for 30 days)."""
    try:
        _ap_store.delete_prompt(name)
        return jsonify({"status": "success", "message": f'Analyzer prompt "{name}" deleted (recoverable for 30 days).'})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/analyzer-prompts/<name>/yaml", methods=["GET"])
def ap_yaml(name):
    """Return the raw YAML text for an analyzer prompt."""
    try:
        raw = _ap_store.get_prompt_yaml(name)
        return jsonify({"status": "success", "yaml": raw})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/analyzer-prompts/validate-tokens", methods=["POST"])
def ap_validate_tokens():
    """Validate that $token$ placeholders in prompt_text are resolvable.

    Runs the saved search query and checks each token against the result
    columns and the set of known global tokens.
    """
    # H-CE-3 (2026-04-22): diagnostic variant gives a precise failure reason
    # for the warning log if the query crashes. UX stays the same (empty
    # columns → report unresolved tokens) but the log line now names the
    # exception class so operators can spot chronic failures.
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics
    from validation.AnalyzerPromptValidation import AnalyzerPromptValidation

    data = request.get_json(force=True, silent=True) or {}
    prompt_text = (data.get("prompt_text") or "").strip()
    query = (data.get("query") or "").strip()

    if not prompt_text:
        return jsonify({"status": "error", "message": "prompt_text is required."}), 400

    # Extract tokens from the prompt
    tokens = AnalyzerPromptValidation.extract_tokens(prompt_text)
    if not tokens:
        return jsonify({
            "status": "success",
            "tokens": [],
            "global_tokens": [],
            "column_tokens": [],
            "unresolved": [],
            "message": "No $token$ placeholders found in prompt text.",
        })

    # If a query is provided, execute it to discover available columns
    available_columns = []
    if query:
        df, _job_id, diagnostic = process_query_with_diagnostics(query)
        if df is not None and not df.empty:
            available_columns = df.columns.tolist()
        elif diagnostic and not diagnostic.startswith("empty:"):
            logger.warning(
                "[!] Token validation query failed: %s", diagnostic,
            )

    report = AnalyzerPromptValidation.validate_tokens_against_columns(
        prompt_text, available_columns
    )
    report["all_tokens"] = sorted(tokens)
    return jsonify({"status": "success", **report})


# ---------------------------------------------------------------------------
# Boilerplate Prompts  (/api/boilerplate-prompts/*)
# ---------------------------------------------------------------------------

from boilerplate_prompt_store import BoilerplatePromptStore

_bp_store = BoilerplatePromptStore()
_bp_store.initialize()


@app.route("/api/boilerplate-prompts/list", methods=["GET"])
def bp_list():
    """Return all boilerplate prompts."""
    prompts = _bp_store.list_prompts()
    return jsonify({"status": "success", "prompts": prompts})


@app.route("/api/boilerplate-prompts/create", methods=["POST"])
def bp_create():
    """Create a new boilerplate prompt YAML."""
    data = request.get_json(force=True, silent=True) or {}
    required = ("name", "template")
    missing = [f for f in required if not (data.get(f) or "").strip()]
    if missing:
        return jsonify({"status": "error", "message": f"Missing required fields: {', '.join(missing)}"}), 400

    overwrite = data.pop("overwrite", False)
    try:
        result = _bp_store.save_prompt(data, overwrite=bool(overwrite))
        return jsonify({"status": "success", "prompt": result})
    except FileExistsError:
        return jsonify({"status": "exists", "message": f'A boilerplate prompt named "{data["name"]}" already exists.'})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/boilerplate-prompts/<name>", methods=["GET"])
def bp_get(name):
    """Return a single boilerplate prompt by name."""
    try:
        prompt = _bp_store.get_prompt(name)
        return jsonify({"status": "success", "prompt": prompt})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/boilerplate-prompts/<name>", methods=["PUT"])
def bp_update(name):
    """Update an existing boilerplate prompt."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _bp_store.update_prompt(name, data)
        return jsonify({"status": "success", "prompt": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/boilerplate-prompts/<name>", methods=["DELETE"])
def bp_delete(name):
    """Soft-delete a boilerplate prompt (archived in last_chance.sqlite for 30 days)."""
    try:
        _bp_store.delete_prompt(name)
        return jsonify({"status": "success", "message": f'Boilerplate prompt "{name}" deleted (recoverable for 30 days).'})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/boilerplate-prompts/<name>/yaml", methods=["GET"])
def bp_yaml(name):
    """Return the raw YAML text for a boilerplate prompt."""
    try:
        raw = _bp_store.get_prompt_yaml(name)
        return jsonify({"status": "success", "yaml": raw})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


# ---------------------------------------------------------------------------
# Alert Groups  (/api/alert-groups/*)
# ---------------------------------------------------------------------------

from alert_group_store import AlertGroupStore

_ag_store = AlertGroupStore()
_ag_store.initialize()


@app.route("/api/alert-groups/list", methods=["GET"])
def ag_list():
    """Return all alert groups with next_run_time."""
    groups = _ag_store.list_groups()
    return jsonify({"status": "success", "groups": groups})


@app.route("/api/alert-groups/create", methods=["POST"])
def ag_create():
    """Create a new alert group YAML."""
    data = request.get_json(force=True, silent=True) or {}
    required = ("name", "search_names", "prompt_text")
    missing = []
    for f in required:
        val = data.get(f)
        if val is None:
            missing.append(f)
        elif isinstance(val, str) and not val.strip():
            missing.append(f)
        elif isinstance(val, list) and len(val) == 0:
            missing.append(f)
    if missing:
        return jsonify({"status": "error", "message": f"Missing required fields: {', '.join(missing)}"}), 400

    overwrite = data.pop("overwrite", False)
    try:
        result = _ag_store.save_group(data, overwrite=bool(overwrite))
    except FileExistsError:
        return jsonify({"status": "exists", "message": f'An alert group named "{data["name"]}" already exists.'})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    # Register the new AG with APScheduler so its cron fires without
    # waiting for a server restart. Same rationale as the PUT handler.
    try:
        engine = _get_engine()
        from alert_groups.scheduler import register_alert_group_jobs
        register_alert_group_jobs(engine._scheduler)
    except Exception as exc:
        app.logger.warning(
            "[!] AG CREATE: save succeeded but scheduler register "
            "failed for '%s': %s. Restart the server if the cron "
            "doesn't fire.", data.get("name"), exc,
        )

    return jsonify({"status": "success", "group": result})


@app.route("/api/alert-groups/<name>", methods=["GET"])
def ag_get(name):
    """Return a single alert group by name."""
    try:
        group = _ag_store.get_group(name)
        return jsonify({"status": "success", "group": group})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/alert-groups/<name>", methods=["PUT"])
def ag_update(name):
    """Update an existing alert group.

    Re-registers the APScheduler cron job after the YAML save so
    schedule/disabled edits take effect immediately rather than
    requiring a server restart (bug caught 2026-04-21 audit). A user
    changing ``cron_schedule`` via the UI previously continued to fire
    on the old schedule until the container was rebuilt.
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _ag_store.update_group(name, data)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    # Re-register the job with APScheduler so cron/disabled edits take
    # effect live. Failure here logs a warning but doesn't block the
    # save - operator can restart the container as a fallback.
    try:
        engine = _get_engine()
        from alert_groups.scheduler import register_alert_group_jobs
        register_alert_group_jobs(engine._scheduler)
    except Exception as exc:
        app.logger.warning(
            "[!] AG PUT: save succeeded but scheduler re-register failed "
            "for '%s': %s. Restart the server if cron/disabled changes "
            "don't take effect.", name, exc,
        )

    return jsonify({"status": "success", "group": result})


@app.route("/api/alert-groups/<name>", methods=["DELETE"])
def ag_delete(name):
    """Soft-delete an alert group (archived in last_chance.sqlite for 30 days).

    Removes the APScheduler cron job after the soft-delete so the job
    stops firing immediately.
    """
    try:
        _ag_store.delete_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    # Unregister the cron job - without this, the deleted AG would
    # keep firing until the container restarted (and then silently
    # fail because the YAML is gone).
    try:
        engine = _get_engine()
        job_id = f"alert_group:{name}"
        if engine._scheduler.get_job(job_id):
            engine._scheduler.remove_job(job_id)
    except Exception as exc:
        app.logger.warning(
            "[!] AG DELETE: soft-delete succeeded but scheduler "
            "remove_job failed for '%s': %s", name, exc,
        )

    return jsonify({"status": "success", "message": f'Alert group "{name}" deleted (recoverable for 30 days).'})


@app.route("/api/alert-groups/<name>/yaml", methods=["GET"])
def ag_yaml(name):
    """Return the raw YAML text for an alert group."""
    try:
        raw = _ag_store.get_group_yaml(name)
        return jsonify({"status": "success", "yaml": raw})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404


@app.route("/api/alert-groups/<name>/run", methods=["POST"])
def ag_run(name):
    """
    Manually trigger an alert group dispatch (bypasses schedule).

    Query params:
      * ``dry_run=true`` runs everything up to the messages build but
        skips the Claude API call and the email send - the response
        still carries the full payload that would have been sent.
      * ``force=true`` bypasses the per-AG rate limit
        (``max_dispatches_per_day`` / ``min_interval_between_runs_hours``)
        and the circuit breaker. Budget + freshness checks still run.
        Use when the operator has seen the rate limit fire and
        explicitly wants to override it for a single dispatch.
    """
    try:
        group = _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    dry_run = (request.args.get("dry_run", "").lower()
               in ("1", "true", "yes"))
    force = (request.args.get("force", "").lower()
             in ("1", "true", "yes"))

    from alert_groups.dispatcher import AlertGroupDispatcher
    dispatcher = AlertGroupDispatcher()
    run_result = dispatcher.run(group, dry_run=dry_run, force=force)

    # For dry runs, parse the JSON-encoded messages payload back out so
    # the client gets structured data instead of an opaque string.
    preview = None
    if run_result.status == "dry_run" and run_result.response_text:
        try:
            import json as _json
            preview = _json.loads(run_result.response_text)
        except Exception:
            preview = None

    return jsonify({
        "status": "success" if run_result.status in ("success", "dry_run", "prompt_only")
                  else "error",
        "dry_run": dry_run,
        "run": {
            "group_name": run_result.group_name,
            "status": run_result.status,
            "searches_used": run_result.searches_used,
            "estimated_tokens": run_result.estimated_tokens,
            "actual_tokens": run_result.actual_tokens,
            "cost_usd": run_result.cost_usd,
            "response_text": run_result.response_text,
            "error_message": run_result.error_message,
        },
        "preview": preview,
    })


@app.route("/api/alert-groups/<name>/dispatch-progress", methods=["GET"])
def ag_dispatch_progress(name):
    """Return live progress of an in-flight alert group dispatch.

    The UI polls this (1-2s cadence) during a manual Run click so the
    operator sees phase-by-phase progress instead of a static
    "Dispatching to Claude..." label for the whole 1-8 minute run.
    Entries are kept for 120s after completion so a late poll can
    still read the terminal status.

    Response when a dispatch is in-flight (or recently completed):

    ```
    {
      "status": "success",
      "in_flight": true,
      "progress": {
        "phase": "feeder_loop" | "calling_claude" | "claude_returned" |
                 "sending_email" | "done_success" | "done_error" | ...,
        "phase_label": "Feeder [4/10] 'ag_sec_catalysts' running…",
        "phase_elapsed_s": 12,
        "run_elapsed_s": 87,
        "feeder_idx": 4, "feeder_total": 10,
        "feeder_name": "ag_sec_catalysts",
        ...
      }
    }
    ```

    Response when no dispatch is tracked for this AG:

    ```
    { "status": "success", "in_flight": false, "progress": null }
    ```
    """
    from alert_groups.dispatcher import dispatch_progress_snapshot
    snap = dispatch_progress_snapshot(name)
    if snap is None:
        return jsonify({
            "status": "success", "in_flight": False, "progress": None,
        })
    phase = str(snap.get("phase") or "")
    in_flight = not phase.startswith("done_")
    return jsonify({
        "status": "success",
        "in_flight": in_flight,
        "progress": snap,
    })


def _ag_reregister_scheduler_jobs(name: str):
    """Shared helper: re-register AG cron jobs after a mutation so
    enable/disable/cron edits take effect without a server restart."""
    try:
        engine = _get_engine()
        from alert_groups.scheduler import register_alert_group_jobs
        register_alert_group_jobs(engine._scheduler)
    except Exception as exc:
        app.logger.warning(
            "[!] AG scheduler re-register failed for '%s': %s", name, exc,
        )


@app.route("/api/alert-groups/<name>/enable", methods=["POST"])
def ag_enable(name):
    """Enable an alert group."""
    try:
        result = _ag_store.update_group(name, {"disabled": False})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    _ag_reregister_scheduler_jobs(name)
    return jsonify({"status": "success", "group": result})


@app.route("/api/alert-groups/<name>/disable", methods=["POST"])
def ag_disable(name):
    """Disable an alert group."""
    try:
        result = _ag_store.update_group(name, {"disabled": True})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    _ag_reregister_scheduler_jobs(name)
    return jsonify({"status": "success", "group": result})


@app.route("/api/alert-groups/runs", methods=["GET"])
def ag_runs():
    """Return recent alert group run history."""
    group_name = request.args.get("group_name")
    limit = int(request.args.get("limit", 50))
    runs = _ag_store.list_runs(group_name=group_name, limit=limit)
    return jsonify({"status": "success", "runs": runs})


# ── Debug Report (2026-04-30) - iterative query-quality loop ────────────────
# Operator clicks "Debug" on an AG row → backend runs every saved search
# referenced by the AG, captures (name + SPQL + results), composes a single
# pasteable report with a Claude prompt prefix on top. Operator copies the
# whole thing, pastes to Claude, gets back per-search SPQL improvements.
#
# This is purely a diagnostic endpoint - does NOT call the dispatcher, does
# NOT call Claude, does NOT spend money. It just runs the saved searches
# the same way "Run Now" on the Saved Searches page does.

_DEBUG_REPORT_MAX_ROWS_PER_SEARCH = 50
_DEBUG_REPORT_MAX_VALUE_LEN = 200  # truncate long string cells


def _format_debug_value(v):
    """Render a single cell value for the report - truncate long strings."""
    if v is None:
        return ""
    s = str(v)
    if len(s) > _DEBUG_REPORT_MAX_VALUE_LEN:
        return s[:_DEBUG_REPORT_MAX_VALUE_LEN] + f"... [+{len(s) - _DEBUG_REPORT_MAX_VALUE_LEN} chars]"
    return s


def _format_duration_short(seconds: int) -> str:
    """Render a duration as a compact human string. Used by the ingestion probe."""
    if seconds < 0:
        return "future"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rem_m = minutes % 60
        return f"{hours}h {rem_m}m" if rem_m else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _probe_index_freshness(query: str) -> list:
    """Probe each ``index="..."`` clause in *query* for raw row count + epoch range.

    This bypasses the saved-search's where/sort/head/dedup chain and answers
    the question "is there any data here at all?" - distinguishing a genuinely
    empty result (filter too aggressive) from an ingestion gap (script not
    deployed, or never ran).

    Returns one dict per distinct index pattern found in the query:

        {
            "pattern": "indexes/equities/.../*.parquet",
            "file_count": int,
            "total_size_bytes": int,
            "row_count": int,
            "latest_epoch": int | None,
            "earliest_epoch": int | None,
            "error": str | None,
        }

    Always succeeds at the top level - per-pattern probe failures are
    captured in the dict's ``error`` field so a broken probe never breaks
    the wider debug report.
    """
    import re
    import duckdb
    from functionality.duckdb_index_call import _resolve_glob_pattern, _resolve_files

    if not isinstance(query, str) or not query.strip():
        return []

    patterns = re.findall(r'index\s*=\s*"([^"]+)"', query)
    if not patterns:
        return []

    seen = set()
    uniq = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    results = []
    for raw in uniq:
        probe = {
            "pattern": raw,
            "file_count": 0,
            "total_size_bytes": 0,
            "row_count": 0,
            "latest_epoch": None,
            "earliest_epoch": None,
            "error": None,
        }
        try:
            glob_pat = _resolve_glob_pattern(raw)
            files = _resolve_files(glob_pat)
            probe["file_count"] = len(files)
            try:
                probe["total_size_bytes"] = sum(
                    os.path.getsize(f) for f in files if os.path.isfile(f)
                )
            except OSError:
                pass

            if files:
                quoted = ", ".join(f"'{f}'" for f in files)
                # Per-call connection - the module-level
                # ``duckdb.sql()`` global is unsafe under concurrent
                # request threads. See /api/search for the production
                # incident (2026-05-18 InvalidInputException) that
                # forced this pattern across all curator/IMMUTABLE reads.
                con = duckdb.connect(database=":memory:")
                try:
                    con.execute("PRAGMA threads=1")
                    try:
                        # union_by_name=true protects against IMMUTABLE-tree
                        # schema additivity (ag_picks, curator_*, etc. all
                        # grow columns over time; empty-fire parquets infer
                        # Null-typed columns). Without it this probe could
                        # crash on the very indexes it's meant to diagnose.
                        row = con.execute(
                            f"SELECT COUNT(*) AS n, "
                            f"TRY_CAST(MIN(_epoch) AS BIGINT) AS min_e, "
                            f"TRY_CAST(MAX(_epoch) AS BIGINT) AS max_e "
                            f"FROM read_parquet([{quoted}], union_by_name=true)"
                        ).fetchone()
                        probe["row_count"] = int(row[0]) if row[0] is not None else 0
                        probe["earliest_epoch"] = int(row[1]) if row[1] is not None else None
                        probe["latest_epoch"] = int(row[2]) if row[2] is not None else None
                    except duckdb.Error as exc:
                        try:
                            row = con.execute(
                                f"SELECT COUNT(*) AS n "
                                f"FROM read_parquet([{quoted}], union_by_name=true)"
                            ).fetchone()
                            probe["row_count"] = int(row[0]) if row[0] is not None else 0
                            probe["error"] = (
                                f"_epoch unreadable (legacy schema or mixed): {exc}"
                            )
                        except duckdb.Error as exc2:
                            probe["error"] = f"duckdb read failed: {exc2}"
                finally:
                    con.close()
        except Exception as exc:
            probe["error"] = f"{type(exc).__name__}: {exc}"

        results.append(probe)

    return results


def _render_ingestion_probe(probes: list) -> list:
    """Render the per-search ingestion probe block as report text lines.

    Distinguishes:
    * 0 files matched   → script not deployed/scheduled, never ran
    * N files but stale → script ran historically but not recently
    * N files + fresh   → data is healthy; empty result is a filter issue
    """
    if not probes:
        return []
    from datetime import datetime as _dtcls, timezone as _tzcls
    now = int(_dtcls.now(_tzcls.utc).timestamp())

    lines = ["", "--- INGESTION PROBE ---"]
    for p in probes:
        lines.append(f"Index pattern: {p['pattern']}")
        if p["file_count"] == 0:
            err = p.get("error")
            if err:
                lines.append(f"  Files: 0  ({err})")
            else:
                lines.append(
                    "  Files: 0 - no parquet files matched glob "
                    "(script not deployed/scheduled, or never ran)"
                )
            continue
        size_mb = p["total_size_bytes"] / (1024 * 1024) if p["total_size_bytes"] else 0.0
        lines.append(f"  Files: {p['file_count']:,}  ({size_mb:.2f} MB total)")
        lines.append(f"  Rows: {p['row_count']:,}")
        latest = p.get("latest_epoch")
        if latest:
            try:
                latest_iso = _dtcls.fromtimestamp(latest, tz=_tzcls.utc).isoformat()
                ago = _format_duration_short(max(0, now - latest))
                lines.append(f"  Latest snapshot: {latest_iso}  ({ago} ago)")
            except (OSError, OverflowError, ValueError):
                lines.append(f"  Latest snapshot: epoch={latest} (unrenderable)")
        earliest = p.get("earliest_epoch")
        if earliest and earliest != latest:
            try:
                earliest_iso = _dtcls.fromtimestamp(earliest, tz=_tzcls.utc).isoformat()
                lines.append(f"  Earliest snapshot: {earliest_iso}")
            except (OSError, OverflowError, ValueError):
                lines.append(f"  Earliest snapshot: epoch={earliest} (unrenderable)")
        if p.get("error"):
            lines.append(f"  Note: {p['error']}")
    return lines


def _build_debug_report_prompt_prefix(ag_name: str, generated_at: str, summary: dict) -> str:
    """The Claude prompt prefix the operator copies along with the report.

    Designed for the SpeakesQuery options-query iteration loop (2026-04-30):
    operator pastes this whole block to Claude, asks for per-search SPQL
    improvements, then iterates.
    """
    return (
        f"# SpeakesQuery Alert Group Debug Report - {ag_name}\n"
        f"# Generated: {generated_at}\n"
        f"# Summary: {summary['ok']} ok, {summary['empty']} empty, "
        f"{summary['error']} error / {summary['total_rows']} total rows "
        f"across {summary['total_searches']} searches\n"
        f"\n"
        f"## Prompt for Claude\n"
        f"\n"
        f"I'm sharing the debug output of every saved search referenced by my\n"
        f'"{ag_name}" alert group. For each search please evaluate:\n'
        f"\n"
        f"1. Is it returning **meaningful, decision-relevant data** (vs raw row\n"
        f"   truncation that hides the broader pattern)?\n"
        f"2. Does the SPQL include appropriate **aggregation** (`stats`, `eventstats`),\n"
        f"   **time bounds** (`earliest=`/`latest=`), **sort**, and **head/limit**?\n"
        f"3. Does the row shape match what the AG's Claude prompt template\n"
        f"   expects to receive?\n"
        f"4. What **concrete SPQL improvements** would sharpen the output?\n"
        f"\n"
        f"For each saved search below, propose a sharpened version of its query\n"
        f"with brief rationale. Prioritise searches that:\n"
        f"\n"
        f"- Return raw rows without aggregation\n"
        f"- Have no time bound (could include stale/ancient data)\n"
        f"- Return zero rows (filter too aggressive, or the schedule is\n"
        f"  misaligned with the data's update cadence)\n"
        f"- Return errors\n"
        f"\n"
        f"Reference the SpeakesQuery time-bound syntax shipped 2026-04-29:\n"
        f"epoch int / Splunk relative (`-1d`, `-1h@h`, `now`) / ISO 8601 with\n"
        f"explicit offset / inline `/<IANA-tz>` suffix to override default UTC.\n"
        f"\n"
        f"After your proposals I'll either approve, redirect, or share more data\n"
        f"so you can iterate.\n"
        f"\n"
        f"## Debug Data\n"
        f"\n"
    )


def _build_debug_report_text(ag_name: str, generated_at: str, summary: dict, searches: list) -> str:
    """Compose the human-readable + Claude-pasteable debug report text."""
    lines = [_build_debug_report_prompt_prefix(ag_name, generated_at, summary)]

    for idx, item in enumerate(searches, start=1):
        sep = "=" * 70
        lines.append(sep)
        lines.append(f"SEARCH {idx}/{len(searches)} - {item['name']}")
        lines.append(f"Status: {item['status']}")
        if item.get("description"):
            lines.append(f"Description: {item['description']}")
        if item["status"] == "ok":
            lines.append(
                f"Rows: {item['row_count']} "
                f"(showing {len(item['sample_rows'])} below)"
            )
            cols = item.get("columns") or []
            lines.append(f"Columns ({len(cols)}): {', '.join(cols) if cols else '(none)'}")
        elif item["status"] == "error":
            lines.append(f"Error: {item.get('error', '(no detail)')}")
        elif item["status"] == "empty":
            lines.append(f"Rows: 0  (query parsed and executed but matched nothing)")
        elif item["status"] == "missing":
            lines.append(
                f"Note: this saved-search name is referenced by the AG but the "
                f"YAML was not found in the saved_searches/ store."
            )

        if item.get("query"):
            lines.append("")
            lines.append("--- SPQL ---")
            lines.append(item["query"].rstrip())

        # Ingestion probe - answers "is there any data here at all?" so
        # an empty result can be triaged as filter-too-aggressive vs
        # script-not-deployed. Non-empty searches still get the probe so
        # the operator sees freshness alongside row count.
        probe_lines = _render_ingestion_probe(item.get("ingestion_probe") or [])
        lines.extend(probe_lines)

        sample_rows = item.get("sample_rows") or []
        if sample_rows:
            lines.append("")
            lines.append(f"--- RESULTS ({len(sample_rows)} rows) ---")
            cols = item.get("columns") or []
            # Render as compact JSON-like blocks - preserves structure for
            # Claude while staying scannable.
            for row_idx, row in enumerate(sample_rows, start=1):
                pairs = ", ".join(
                    f"{k}={_format_debug_value(row.get(k))!r}" for k in cols
                ) if cols else str(row)
                lines.append(f"  [{row_idx}] {pairs}")
            if item.get("truncated"):
                lines.append(
                    f"  ... ({item['row_count'] - len(sample_rows)} more rows omitted; "
                    f"increase _DEBUG_REPORT_MAX_ROWS_PER_SEARCH or paste a follow-up)"
                )
        elif item["status"] == "ok":
            lines.append("")
            lines.append("--- RESULTS ---")
            lines.append("  (no rows)")

        lines.append("")

    return "\n".join(lines)


@app.route("/api/alert-groups/<name>/debug-report", methods=["POST"])
def ag_debug_report(name):
    """Run every saved search referenced by an AG and return a structured
    debug report for iterative query-quality refinement.

    Returns both:
      - ``searches`` (structured list, for any future programmatic consumer)
      - ``report_text`` (the formatted, pasteable text the operator copies)

    Does NOT call Claude, does NOT consume Claude budget, does NOT trigger
    email. Pure diagnostic. Same execution path as the Saved Searches "Run
    Now" button.
    """
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics
    from datetime import datetime, timezone as _tz
    import math

    try:
        group = _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    search_names = group.get("search_names") or []
    if not search_names:
        return jsonify({
            "status": "error",
            "message": f'Alert group "{name}" has no saved searches to debug.',
        }), 400

    # Try to use the saved-search store the same way the dispatcher does
    try:
        from saved_search_store import SavedSearchStore
        ss_store = SavedSearchStore()
        # Don't re-init - module-level singleton is fine; we just need read access
    except Exception as exc:
        app.logger.warning(
            "[!] AG debug-report: cannot import SavedSearchStore: %s", exc,
        )
        ss_store = None

    searches_out = []
    for ss_name in search_names:
        item = {
            "name": ss_name,
            "status": "missing",
            "query": "",
            "description": "",
            "row_count": 0,
            "columns": [],
            "sample_rows": [],
            "truncated": False,
            "error": None,
        }

        # Step 1: load the saved-search YAML
        ss = None
        if ss_store is not None:
            try:
                ss = ss_store.get_search(ss_name)
            except FileNotFoundError:
                ss = None
            except Exception as exc:
                item["status"] = "error"
                item["error"] = f"Failed to load saved search: {type(exc).__name__}: {exc}"
                searches_out.append(item)
                continue

        if ss is None:
            # Saved search referenced but not found - keep status="missing"
            searches_out.append(item)
            continue

        item["query"] = ss.get("query", "") or ""
        item["description"] = ss.get("description", "") or ""

        if not item["query"].strip():
            item["status"] = "error"
            item["error"] = "Saved search has no query field."
            searches_out.append(item)
            continue

        # Probe ingestion freshness BEFORE executing - runs even if the
        # query later fails or returns empty so the operator can always
        # see whether the upstream data actually exists.
        try:
            item["ingestion_probe"] = _probe_index_freshness(item["query"])
        except Exception as exc:
            app.logger.warning(
                "[!] AG debug-report: ingestion probe crashed for %s: %s",
                ss_name, exc,
            )
            item["ingestion_probe"] = []

        # Step 2: execute the query
        try:
            df, _job_id, diagnostic = process_query_with_diagnostics(item["query"])
        except Exception as exc:
            item["status"] = "error"
            item["error"] = f"{type(exc).__name__}: {exc}"
            searches_out.append(item)
            continue

        if diagnostic and (df is None or (hasattr(df, "empty") and df.empty)):
            # process_query_with_diagnostics returns the diagnostic for both
            # error and empty cases. Distinguish: anything tagged "empty:" is
            # a successful parse with zero rows; anything else is an error.
            if str(diagnostic).startswith("empty:"):
                item["status"] = "empty"
            else:
                item["status"] = "error"
                item["error"] = diagnostic
            searches_out.append(item)
            continue

        if df is None or (hasattr(df, "empty") and df.empty):
            item["status"] = "empty"
            searches_out.append(item)
            continue

        # Step 3: serialize results (capped + scrubbed)
        try:
            item["status"] = "ok"
            item["row_count"] = int(len(df))
            item["columns"] = [str(c) for c in df.columns]
            sample = df.head(_DEBUG_REPORT_MAX_ROWS_PER_SEARCH)
            # Convert via to_dict; replace NaN with None for JSON-safety
            records = sample.to_dict(orient="records")
            cleaned = []
            for rec in records:
                cleaned_row = {}
                for k, v in rec.items():
                    try:
                        if isinstance(v, float) and math.isnan(v):
                            cleaned_row[str(k)] = None
                        else:
                            cleaned_row[str(k)] = v
                    except Exception:
                        cleaned_row[str(k)] = str(v)
                cleaned.append(cleaned_row)
            item["sample_rows"] = cleaned
            item["truncated"] = item["row_count"] > _DEBUG_REPORT_MAX_ROWS_PER_SEARCH
        except Exception as exc:
            item["status"] = "error"
            item["error"] = f"Result serialisation failed: {type(exc).__name__}: {exc}"
            item["row_count"] = 0
            item["columns"] = []
            item["sample_rows"] = []

        searches_out.append(item)

    summary = {
        "total_searches": len(searches_out),
        "ok": sum(1 for s in searches_out if s["status"] == "ok"),
        "empty": sum(1 for s in searches_out if s["status"] == "empty"),
        "error": sum(1 for s in searches_out if s["status"] == "error"),
        "missing": sum(1 for s in searches_out if s["status"] == "missing"),
        "total_rows": sum(int(s.get("row_count") or 0) for s in searches_out),
    }

    generated_at = datetime.now(_tz.utc).isoformat()
    report_text = _build_debug_report_text(
        ag_name=name,
        generated_at=generated_at,
        summary=summary,
        searches=searches_out,
    )

    return jsonify({
        "status": "success",
        "ag_name": name,
        "generated_at": generated_at,
        "summary": summary,
        "searches": searches_out,
        "report_text": report_text,
    })


@app.route("/api/alert-groups/<name>/metrics", methods=["GET"])
def ag_metrics(name):
    """Aggregate success rate, cost, latency, and streaks for an AG.

    Reads ``alert_group_runs.sqlite`` for status history + `claude_api_history.sqlite`
    for per-call cost / latency detail. Window selectable via ``hours=`` query
    param (default 24).
    """
    try:
        _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 24))))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "hours must be int"}), 400

    import time as _time
    since_epoch = int(_time.time()) - hours * 3600
    all_runs = _ag_store.list_runs(group_name=name, limit=1000)
    window_runs = []
    for r in all_runs:
        t = r.get("triggered_at") or ""
        # triggered_at is 'YYYY-MM-DD HH:MM:SS' UTC from the sqlite DEFAULT
        try:
            import datetime as _dt
            run_epoch = int(
                _dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc
                ).timestamp()
            )
        except Exception:
            run_epoch = 0
        if run_epoch >= since_epoch:
            window_runs.append(r)

    total = len(window_runs)
    success = sum(1 for r in window_runs if r.get("status") == "success")
    error = sum(1 for r in window_runs if r.get("status") == "error")
    skipped = sum(1 for r in window_runs if r.get("status") == "skipped")
    success_rate = (success / total) if total else 0.0

    costs = [r.get("cost_usd") or 0 for r in window_runs if r.get("cost_usd")]
    tokens = [r.get("actual_tokens") or 0
              for r in window_runs if r.get("actual_tokens")]

    # Current consecutive error streak (all-time, not just window) - matches
    # the circuit-breaker counter.
    from alert_groups.dispatcher import AlertGroupDispatcher
    breaker_streak = AlertGroupDispatcher._consecutive_error_count(name)

    # Per-call latency / cost detail from claude_api_history
    try:
        from analyzers.claude_history_store import ClaudeHistoryStore
        claude_stats = ClaudeHistoryStore.get_instance().stats(
            since_epoch=since_epoch, group_name=name,
        )
    except Exception:
        claude_stats = {}

    return jsonify({
        "status": "success",
        "metrics": {
            "window_hours": hours,
            "total_runs": total,
            "success": success,
            "error": error,
            "skipped": skipped,
            "success_rate": round(success_rate, 3),
            "total_cost_usd": round(sum(costs), 6),
            "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else 0,
            "max_cost_usd": round(max(costs), 6) if costs else 0,
            "total_tokens": sum(tokens),
            "avg_tokens": int(sum(tokens) / len(tokens)) if tokens else 0,
            "consecutive_errors": breaker_streak,
            "claude_call_count": claude_stats.get("calls", 0),
            "claude_total_cost_usd": round(
                float(claude_stats.get("cost_usd") or 0), 6
            ),
        },
    })


@app.route("/api/alert-groups/<name>/reset-circuit-breaker", methods=["POST"])
def ag_reset_circuit_breaker(name):
    """Clear a tripped circuit breaker so the AG can dispatch again."""
    try:
        _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    try:
        updated = _ag_store.update_group(name, {"circuit_breaker_tripped": False})
        try:
            from functionality.log_writer import log_system_event
            log_system_event(
                component="alert_groups",
                event="circuit_breaker_reset",
                message=f"{name} manually reset via API",
            )
        except Exception:
            pass
        return jsonify({
            "status": "success",
            "message": f"Circuit breaker reset for '{name}'.",
            "group": updated,
        })
    except Exception as exc:
        logger.error("[x] reset-circuit-breaker failed for %s: %s", name, exc)
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
        }), 500


def _ag_build_resolver_context():
    """
    Build the loader-and-data bundle used by the feeder-status resolver.
    Extracted so both endpoints below share the same wiring.
    """
    # Reuse the module-level import (`_list_library_scripts`) rather than
    # re-importing locally, so tests can monkey-patch this server-level
    # name to inject stub scripts without touching the whole package.
    engine = _get_engine()
    return {
        "saved_search_loader": _ss_store.get_search,
        "library_scripts": _list_library_scripts(),
        "scheduled_tasks": engine.store.list_scheduled_inputs(enabled_only=False),
        "credentials_lister": engine._vault.list_keys,
        "indexes_root": _get_browse_dir(),
        "default_search_names": _ss_store.list_defaults(),
        # Added 2026-04-21: feeder-status now reports whether the
        # installed saved-search YAML has drifted from the shipped
        # default template. Surfaces a "Sync Template" affordance in the
        # Feeder Health UI for operators on Docker volumes whose
        # saved_searches/ dir was seeded pre-bugfix.
        "template_drift_checker": _ss_store.template_drift,
    }


@app.route("/api/alert-groups/<name>/install-default-feeder/<search_name>", methods=["POST"])
def ag_install_default_feeder(name, search_name):
    """
    Install a single missing default-template saved search into the user's
    `saved_searches/` directory.  The <name> path parameter identifies the
    calling alert group for audit/logging; the install itself just copies
    the YAML from `default_saved_searches/<search_name>.yaml` if present.

    Query params:
      * ``overwrite=true`` force-replaces an already-installed YAML with
        the current default template. Use this when Feeder Health has
        surfaced a ``template_drift`` warning (installed YAML is stale
        relative to git-tracked template - common on Docker volumes
        seeded pre-bugfix).
    """
    try:
        _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    overwrite = (request.args.get("overwrite", "").lower()
                 in ("1", "true", "yes"))

    try:
        # Capture the pre-install query (if any) so the audit row can
        # record WHAT was replaced during a sync operation.
        prior_query = None
        if overwrite:
            try:
                prior = _ss_store.get_search(search_name)
                prior_query = (prior or {}).get("query")
            except Exception:
                prior_query = None

        search = _ss_store.install_default(search_name, overwrite=overwrite)
        try:
            from functionality.log_writer import log_config_change
            log_config_change(
                subject=search_name,
                action=(
                    "resync_default_feeder" if overwrite
                    else "install_default_feeder"
                ),
                subject_type="saved_search",
                old_value={"query": prior_query} if prior_query else None,
                new_value={
                    "installed_for_ag": name,
                    "cron_schedule": search.get("cron_schedule"),
                    "purpose": search.get("purpose", "standalone"),
                    "query": search.get("query"),
                },
                actor="api",
                source=f"alert_group:{name}",
            )
        except Exception:
            pass
        return jsonify({
            "status": "success",
            "search": search,
            "resynced": bool(overwrite),
        })
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except FileExistsError as exc:
        return jsonify({"status": "exists", "message": str(exc)}), 409


@app.route("/api/alert-groups/<name>/pipeline-health", methods=["GET"])
def ag_pipeline_health(name):
    """
    Deep health check: in addition to the deployment/credential state,
    actually run each feeder's saved-search SPQL against the live
    indexes and report row count, any query error, and the columns
    the query returned.  This catches silent breakage that the basic
    feeder-status check misses - e.g. a Polymarket API format change
    where the parquet still lands but `strptime` in the SPQL raises.
    """
    from alert_groups.feeder_status import resolve_alert_group

    try:
        group = _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    ctx = _ag_build_resolver_context()
    status = resolve_alert_group(group, **ctx)

    from query_engine.CmdExecutionBackend import run_query_and_return_results_df
    import time as _time

    # Compute a freshness window based on the AG's schedule - a feeder
    # row is "fresh" if its `_epoch` is within the window leading up to
    # the next dispatch.  For scheduled AGs we use 2× the cron interval;
    # for manual-only we use a 24h default.
    window_seconds = 24 * 3600

    for feeder in status["feeders"]:
        feeder.setdefault("query_row_count", None)
        feeder.setdefault("query_error", None)
        feeder.setdefault("query_columns", [])
        feeder.setdefault("fresh_row_count", None)

        if feeder["state"] in (
            "missing_search", "unknown_index", "needs_deploy"
        ):
            continue

        search_name = feeder["search_name"]
        try:
            ss = _ss_store.get_search(search_name)
        except FileNotFoundError:
            continue
        query = ss.get("query") or ""
        if not query.strip():
            continue

        try:
            result_df, _job_id = run_query_and_return_results_df(query)
        except Exception as exc:  # pragma: no cover - logged for debugging
            logger.exception(
                "[x] pipeline-health: SPQL raised for %s", search_name
            )
            feeder["query_error"] = f"{type(exc).__name__}: {exc}"
            feeder["state"] = "query_broken"
            continue

        if result_df is None:
            # Engine returned None - usually empty results after filters,
            # but could also signal a parse error (logged by the engine).
            feeder["query_row_count"] = 0
            feeder["query_columns"] = []
            continue

        feeder["query_row_count"] = int(len(result_df))
        feeder["query_columns"] = list(result_df.columns)

        if "_epoch" in result_df.columns and len(result_df) > 0:
            try:
                cutoff = _time.time() - window_seconds
                fresh = result_df[result_df["_epoch"] >= cutoff]
                feeder["fresh_row_count"] = int(len(fresh))
            except Exception:
                feeder["fresh_row_count"] = None

    # Recompute the aggregate summary since query_broken may have flipped
    from alert_groups.feeder_status import FeederStatus, summarize, STATE_RANK
    counts: dict[str, int] = {k: 0 for k in STATE_RANK}
    counts["query_broken"] = 0  # new state not in STATE_RANK
    for f in status["feeders"]:
        counts[f["state"]] = counts.get(f["state"], 0) + 1
    rank = {**STATE_RANK, "query_broken": 4.5}  # between needs_deploy and no_lib
    if status["feeders"]:
        overall = max(
            (f["state"] for f in status["feeders"]),
            key=lambda s: rank.get(s, 99),
        )
    else:
        overall = status["summary"].get("overall", "unknown_index")
    status["summary"] = {
        "counts": counts,
        "overall": overall,
        "total": len(status["feeders"]),
    }
    status["window_seconds"] = window_seconds

    return jsonify({"status": "success", **status})


@app.route("/api/alert-groups/<name>/feeder-status", methods=["GET"])
def ag_feeder_status(name):
    """
    Report health of every saved search referenced by this alert group:
    whether each feeder maps to a library script, whether that script is
    deployed, whether required credentials are set, and whether data has
    landed in the expected index directory.
    """
    from alert_groups.feeder_status import resolve_alert_group

    try:
        group = _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    ctx = _ag_build_resolver_context()
    result = resolve_alert_group(group, **ctx)
    return jsonify({"status": "success", **result})


@app.route("/api/alert-groups/<name>/deploy-feeders", methods=["POST"])
def ag_deploy_feeders(name):
    """
    Bulk-deploy every library script referenced by this alert group's
    feeders that is not already scheduled.  Feeders already deployed,
    user-managed indexes, and missing saved searches are skipped.

    Query parameters:
      * ``run_after_deploy=true`` (default) - also trigger an immediate
        run for every newly-deployed task AND every existing-but-empty
        task (state=``pending``). Returns per-task run results in the
        ``runs`` array. Set to ``false`` to keep the historical
        deploy-only behaviour.
      * ``max_run_workers=4`` - bounded thread-pool concurrency for the
        run-now phase. Cap at 8.

    Returns a per-feeder action summary so the UI can show exactly what
    happened (installed / deployed / ran / skipped / failed) and which
    feeders still need manual intervention (e.g. missing credentials).

    Wave 2 fix (2026-04-25): the original "Fix Missing Feeders" only
    deployed scripts. The user then ran Pipeline Check, saw 0 rows for
    every feeder, and assumed the AG was broken - actually the cron
    just hadn't fired yet. Chaining run-now closes that loop so the
    operator gets immediate feedback on whether the deploy was
    successful AND whether the script returns data.
    """
    from alert_groups.feeder_status import resolve_alert_group

    try:
        group = _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    run_after_deploy = (
        request.args.get("run_after_deploy", "true").lower()
        in ("1", "true", "yes")
    )
    try:
        max_run_workers = int(request.args.get("max_run_workers", "4"))
    except ValueError:
        max_run_workers = 4
    max_run_workers = max(1, min(max_run_workers, 8))

    ctx = _ag_build_resolver_context()
    status = resolve_alert_group(group, **ctx)
    engine = _get_engine()

    deployed: list[dict] = []
    installed: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    # ── Pass 1: install any missing default saved searches ─────────────
    # A user who hasn't run the engine yet (fresh checkout) may be missing
    # the feeder YAMLs that back a project-shipped default AG.  Install
    # them first so the follow-up resolve sees the real feeder state.
    for feeder in status["feeders"]:
        if feeder["state"] == "missing_search" and feeder.get("installable"):
            name_ = feeder["search_name"]
            try:
                _ss_store.install_default(name_)
                installed.append({"search_name": name_, "source": "default_saved_searches"})
            except FileExistsError:
                # Race-condition: another caller installed it - treat as ok
                installed.append({
                    "search_name": name_,
                    "source": "default_saved_searches",
                    "note": "already present",
                })
            except Exception as exc:
                logger.exception(
                    "[x] ag_deploy_feeders: failed to install default %s", name_
                )
                failed.append({"search_name": name_, "reason": str(exc)})

    # Re-resolve if anything was installed so the deploy pass sees the
    # newly-installed feeders' actual states (needs_deploy, etc.).
    if installed:
        status = resolve_alert_group(group, **_ag_build_resolver_context())

    # ── Pass 2: deploy library scripts for feeders in needs_deploy ─────
    # Align each ingestion cron to fire BEFORE the AG's dispatch cron so
    # Parquet data is fresh when the alert group serializes results.
    # Fall back to the script's suggested_cron when the AG has no
    # schedule (manual-only) or the cron is too complex to shift safely.
    from alert_groups.feeder_status import derive_pre_cron
    _ag_cron = (group.get("schedule") or "").strip()
    _pre_cron = derive_pre_cron(_ag_cron, offset_minutes=60) if _ag_cron else None

    for feeder in status["feeders"]:
        state = feeder["state"]
        name_ = feeder["search_name"]
        if state != "needs_deploy":
            # Skip anything that isn't actionable in this pass.
            # `missing_search` with no `installable` flag stays skipped;
            # installable ones were handled in Pass 1.
            already_listed = any(
                i["search_name"] == name_ for i in installed
            )
            if not already_listed:
                skipped.append({
                    "search_name": name_,
                    "reason": state,
                    "library_script_id": feeder.get("library_script_id"),
                })
            continue

        script_id = feeder.get("library_script_id")
        if not script_id:
            failed.append({
                "search_name": name_,
                "reason": "no_library_script_id",
            })
            continue

        script = _get_library_script(script_id)
        if not script:
            failed.append({
                "search_name": name_,
                "reason": "library_script_not_found",
                "library_script_id": script_id,
            })
            continue

        # Pick the cron: AG-aligned "pre-cron" beats the script's default.
        suggested = script.get("suggested_cron") or "*/30 * * * *"
        chosen_cron = _pre_cron or suggested
        cron_source = (
            "ag_schedule_minus_60min" if _pre_cron else
            ("library_suggested" if script.get("suggested_cron") else "engine_default")
        )

        try:
            task = engine.add_task(
                title=script.get("title") or script_id,
                description=script.get("description", ""),
                code=script.get("code") or "",
                cron_schedule=chosen_cron,
                overwrite=(
                    "true" if script.get("suggested_overwrite") else "false"
                ),
                subdirectory=script.get("suggested_subdirectory") or "",
                api_url=script.get("api_url"),
                trust_level=script.get("trust_level", "sandboxed"),
            )
            # Pick up any credentials the user pre-staged against script_id=0
            engine.migrate_staging_credentials(task["id"])
            deployed.append({
                "search_name": name_,
                "library_script_id": script_id,
                "task_id": task["id"],
                "subdirectory": script.get("suggested_subdirectory") or "",
                "cron_schedule": chosen_cron,
                "cron_source": cron_source,
                "ag_schedule": _ag_cron or None,
                "requires_credentials": list(
                    script.get("requires_credentials") or []
                ),
            })
        except Exception as exc:  # pragma: no cover - logged for operators
            logger.exception(
                "[x] ag_deploy_feeders: failed to deploy %s for %s",
                script_id, name_,
            )
            failed.append({
                "search_name": name_,
                "library_script_id": script_id,
                "reason": str(exc),
            })

    # Re-resolve so the run-now phase below sees the post-deploy state
    # and the client gets the latest snapshot in the response.
    refreshed = resolve_alert_group(group, **_ag_build_resolver_context())

    # ── Pass 3: run-now any newly-deployed task + any pending task ─────
    # Closes the "Fix Missing Feeders → 0 rows" gap. Bounded thread-pool
    # concurrency so 10 tasks don't each wait for the previous one to
    # finish on the wire. Each task's own timeout still applies.
    runs: list[dict] = []
    if run_after_deploy:
        # Build a map: search_name → task_id for everything we want to
        # run. New deploys are first-class candidates; pre-existing
        # `pending` feeders close the day-1 empty case for users who
        # re-ran Fix Missing Feeders after a prior partial deploy.
        run_targets: list[tuple[str, int, str]] = []
        deployed_names = {d["search_name"] for d in deployed}

        for d in deployed:
            run_targets.append((
                d["search_name"], d["task_id"], "newly_deployed",
            ))

        for feeder in refreshed["feeders"]:
            sname = feeder["search_name"]
            if sname in deployed_names:
                continue
            # Pending = task exists, no parquet yet, ready to run.
            # needs_creds / disabled would just fail or be skipped - no
            # value in burning a worker on them.
            if feeder["state"] == "pending" and feeder.get("task_id"):
                run_targets.append((
                    sname, feeder["task_id"], "pending",
                ))

        if run_targets:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            logger.info(
                "[i] ag_deploy_feeders: running %d task(s) in parallel "
                "(max_workers=%d) for AG %s",
                len(run_targets), max_run_workers, name,
            )

            def _run_one(payload: tuple[str, int, str]) -> dict:
                sname, tid, reason = payload
                try:
                    run = engine.run_task_now(tid)
                    return {
                        "search_name": sname,
                        "task_id": tid,
                        "trigger_reason": reason,
                        "run": run,
                        "skipped": False,
                    }
                except Exception as exc:  # pragma: no cover - logged
                    logger.warning(
                        "[!] ag_deploy_feeders: run_task_now(%s) raised: %s",
                        tid, exc,
                    )
                    return {
                        "search_name": sname,
                        "task_id": tid,
                        "trigger_reason": reason,
                        "run": {
                            "status": "failed",
                            "error_message": (
                                f"{type(exc).__name__}: {exc}"
                            ),
                        },
                        "skipped": False,
                    }

            with ThreadPoolExecutor(max_workers=max_run_workers) as pool:
                futures = [pool.submit(_run_one, t) for t in run_targets]
                for fut in as_completed(futures):
                    runs.append(fut.result())

            # Re-resolve a final time so the response carries the
            # post-run feeder state (live vs still-empty), not just the
            # post-deploy state.
            refreshed = resolve_alert_group(
                group, **_ag_build_resolver_context()
            )

    return jsonify({
        "status": "success",
        "group_name": name,
        "deployed": deployed,
        "installed": installed,
        "skipped": skipped,
        "failed": failed,
        "runs": runs,
        "ran_after_deploy": run_after_deploy,
        "feeder_status": refreshed,
    })


@app.route("/api/alert-groups/<name>/manual-return", methods=["POST"])
def ag_manual_return(name):
    """
    Wave 3 (2026-04-25): accept an operator-pasted brief returned from
    an external LLM (typical use case: ``delivery_mode="prompt_only"``
    where the user runs the prompt in Claude.ai / ChatGPT / Gemini and
    wants the resulting picks captured into ``indexes/logs/ag_picks/``
    so historical-performance queries see manual + Claude-pipeline
    picks through the same surface).

    Request body:
        {
          "raw_text":          str (required) - the full LLM response,
                                preamble + fenced JSON block included
          "model_used":        str (required) - model id ("gpt-4o", etc.)
          "dispatch_run_id":   str (optional) - if provided, picks are
                                tagged with this run_request_id so they
                                join cleanly to the original Claude-pipeline
                                row in claude_api_history.sqlite. If
                                omitted, a synthetic id is generated:
                                ``manual:<group>:<utc_iso>``.
          "dry_run":           bool (default false) - when true, parses
                                + previews picks but does NOT write.
                                Used by the modal's Preview button.
        }

    Response:
        {
          "status": "success",
          "picks_parsed": int,        # picks the parser accepted
          "picks_written": int,       # rows actually committed (0 on dry_run)
          "run_request_id": str,      # what got written / would be written
          "source": "manual",
          "model_used": str,
          "preview": [normalized_pick_dict, ...] # always included
        }

    Dedup (commit path only): SHA-256 of (alert_group + raw_text). If
    the same hash was written within the last 7 days, return 409 with
    the prior run_request_id so the operator knows it's already there.
    """
    import hashlib
    import datetime as _dt
    from alert_groups.dispatcher import AlertGroupDispatcher

    try:
        _ag_store.get_group(name)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404

    payload = request.get_json(force=True, silent=True) or {}
    raw_text = (payload.get("raw_text") or "").strip()
    model_used = (payload.get("model_used") or "").strip()
    dispatch_run_id = (payload.get("dispatch_run_id") or "").strip()
    dry_run = bool(payload.get("dry_run", False))

    if not raw_text:
        return jsonify({
            "status": "error",
            "message": "raw_text is required",
        }), 400
    if not model_used:
        return jsonify({
            "status": "error",
            "message": (
                "model_used is required. Pick the model whose response "
                "you pasted (e.g. 'gpt-4o', 'claude-sonnet-4-6', "
                "'gemini-2.5-pro') so historical performance can "
                "group by model."
            ),
        }), 400

    # Parse first so the caller always gets a preview even if dedup
    # rejects the commit path.
    normalized = AlertGroupDispatcher._parse_picks_block(
        response_text=raw_text, group_name=name,
    )
    preview = list(normalized)

    if dry_run:
        return jsonify({
            "status": "success",
            "dry_run": True,
            "picks_parsed": len(preview),
            "picks_written": 0,
            "run_request_id": dispatch_run_id or "(would be auto-generated)",
            "source": "manual",
            "model_used": model_used,
            "preview": preview,
        })

    if not preview:
        return jsonify({
            "status": "error",
            "message": (
                "No picks recognized in the pasted text. The parser "
                "expects a fenced ```json [ {...}, ... ] ``` block at "
                "the end of the response with each pick carrying the "
                "required keys (idea_id, instrument_type, "
                "instrument_id, direction, conviction_pct, "
                "expected_return_pct, position_size_tier, entry_price, "
                "suggested_buy_epoch, suggested_sell_epoch, "
                "exit_catalyst, thesis). Use ?dry_run=true on the "
                "preview button to see exactly what got rejected."
            ),
            "preview": preview,
        }), 422

    # Dedup: same alert_group + same raw_text within last 7 days =
    # same paste, refuse to write twice. We hash just the body so a
    # whitespace-only difference doesn't slip a duplicate through.
    digest = hashlib.sha256(
        f"{name}\x00{raw_text}".encode("utf-8")
    ).hexdigest()
    try:
        from query_engine.CmdExecutionBackend import (
            run_query_and_return_results_df,
        )
        seven_days_ago = int(_dt.datetime.now(_dt.timezone.utc).timestamp()
                             - 7 * 86400)
        existing_q = (
            'index="indexes/logs/ag_picks/*.parquet" '
            f'| where alert_group="{name}" '
            f'| where source="manual" '
            f'| where _epoch >= {seven_days_ago} '
            f'| where source_signals="manual_return:{digest[:16]}" '
            '| stats count as n by run_request_id'
        )
        df, _job = run_query_and_return_results_df(existing_q)
        if df is not None and len(df) > 0:
            prior_id = str(df.iloc[0]["run_request_id"])
            return jsonify({
                "status": "duplicate",
                "message": (
                    "This exact brief was already submitted within "
                    "the last 7 days for this alert group."
                ),
                "prior_run_request_id": prior_id,
                "preview": preview,
            }), 409
    except Exception:
        # Dedup is best-effort - if the lookup fails, fall through to
        # the write rather than blocking the operator. Worst case is a
        # duplicate row in ag_picks which they can dedup downstream.
        pass

    if not dispatch_run_id:
        utc_iso = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        dispatch_run_id = f"manual:{name}:{utc_iso}"

    # Stash the dedup digest in source_signals so the dedup check above
    # has something to match on. Operators won't typically read this
    # field for manual returns; it doubles as the dedup marker.
    for pick in normalized:
        pick["source_signals"] = f"manual_return:{digest[:16]}"

    written = AlertGroupDispatcher._log_picks(
        normalized_picks=normalized,
        group_name=name,
        run_request_id=dispatch_run_id,
        source="manual",
        model_used=model_used,
    )

    try:
        from functionality.log_writer import log_alert_group_event
        log_alert_group_event(
            group_name=name,
            status="manual_return",
            error_message=None,
            duration_ms=None,
            dry_run=False,
        )
    except Exception:
        pass

    logger.info(
        "[i] AG '%s': manual return committed: %d pick(s) under %s "
        "(model=%s, digest=%s)",
        name, written, dispatch_run_id, model_used, digest[:16],
    )

    return jsonify({
        "status": "success",
        "dry_run": False,
        "picks_parsed": len(preview),
        "picks_written": written,
        "run_request_id": dispatch_run_id,
        "source": "manual",
        "model_used": model_used,
        "preview": preview,
    })


# ---------------------------------------------------------------------------
# Topology API  (/api/topology) - Wave 4 (2026-04-25)
# ---------------------------------------------------------------------------
# The cross-link badges in the Searches / Ingestion Scripts / Alert Groups
# tabs all need the same adjacency:
#   index   ←→ ingestion task     (matching subdirectory)
#   index   ←→ saved search       (extracted from query)
#   search  ←→ alert group        (alert_group.search_names list)
#   script  ←→ ingestion task     (deployed-as / library_script_id)
#
# Computed once per page-load and shipped as a single endpoint so the SPA
# can do all the joins client-side without N+1 fetches.


@app.route("/api/topology", methods=["GET"])
def api_topology():
    """Return the index↔script↔search↔alert-group adjacency graph.

    Drives the cross-link badges added in Wave 4 (2026-04-25) on every
    saved-search row, ingestion-task row, and alert-group row. One
    fetch per page-load; the SPA caches and joins client-side.

    Response shape:
        {
          "status": "success",
          "searches": [
            {"name": str, "indexes": [str], "subdirs": [str],
             "tasks": [{id, title, library_script_id, subdirectory}],
             "alert_groups": [str]}, ...
          ],
          "tasks": [
            {"id": int, "title": str, "subdirectory": str,
             "library_script_id": str|None, "disabled": bool,
             "feeds_searches": [str], "feeds_alert_groups": [str]}, ...
          ],
          "alert_groups": [
            {"name": str, "search_names": [str],
             "feeders": [{search_name, indexes, subdirs, tasks}, ...]},
            ...
          ],
          "scripts": [
            {"id": str, "suggested_subdirectory": str,
             "deployed_as_tasks": [int]}, ...
          ]
        }
    """
    from alert_groups.feeder_status import (
        extract_index_paths, _normalize_subdirectory,
    )

    engine = _get_engine()
    searches = _ss_store.list_searches()
    groups = _ag_store.list_groups()
    tasks = engine.store.list_scheduled_inputs(enabled_only=False)
    scripts = _list_library_scripts()

    # ── Build subdir indexes for fast lookup ──────────────────────────
    # subdir → [task_dict]
    tasks_by_subdir: dict[str, list[dict]] = {}
    for t in tasks:
        sd = (t.get("subdirectory") or "").strip("/")
        if not sd:
            continue
        tasks_by_subdir.setdefault(sd, []).append(t)

    # subdir → [script_id]
    scripts_by_subdir: dict[str, list[str]] = {}
    for s in scripts:
        sd = (s.get("suggested_subdirectory") or "").strip("/")
        if not sd:
            continue
        scripts_by_subdir.setdefault(sd, []).append(s.get("id") or "")

    # ── Resolve each saved search's index paths + matching tasks ──────
    search_meta: dict[str, dict] = {}
    for s in searches:
        name = s.get("name") or ""
        if not name:
            continue
        indexes = extract_index_paths(s.get("query") or "")
        subdirs: list[str] = []
        seen: set[str] = set()
        for idx in indexes:
            sd = _normalize_subdirectory(idx)
            if sd and sd not in seen:
                seen.add(sd)
                subdirs.append(sd)
        # Match: any task whose subdirectory equals one of these subdirs
        matched_tasks: list[dict] = []
        for sd in subdirs:
            for t in tasks_by_subdir.get(sd, []):
                matched_tasks.append({
                    "id": t["id"],
                    "title": t.get("title") or "",
                    "library_script_id": t.get("library_script_id"),
                    "subdirectory": sd,
                    "disabled": bool(t.get("disabled", False)),
                })
        search_meta[name] = {
            "name": name,
            "indexes": indexes,
            "subdirs": subdirs,
            "tasks": matched_tasks,
            "alert_groups": [],  # filled below
        }

    # ── Fold alert-group memberships into each saved search ───────────
    ag_payload: list[dict] = []
    for g in groups:
        name = g.get("name") or ""
        if not name:
            continue
        search_names = list(g.get("search_names") or [])
        feeders: list[dict] = []
        for sn in search_names:
            sm = search_meta.get(sn)
            if sm is not None:
                # Reverse-link: this AG feeds this saved search
                if name not in sm["alert_groups"]:
                    sm["alert_groups"].append(name)
                feeders.append({
                    "search_name": sn,
                    "indexes": sm["indexes"],
                    "subdirs": sm["subdirs"],
                    "tasks": sm["tasks"],
                })
            else:
                feeders.append({
                    "search_name": sn,
                    "indexes": [],
                    "subdirs": [],
                    "tasks": [],
                    "missing": True,
                })
        ag_payload.append({
            "name": name,
            "search_names": search_names,
            "feeders": feeders,
        })

    # ── Build per-task reverse links ──────────────────────────────────
    # For each task, find which searches reference its subdirectory and
    # which AGs include those searches.
    tasks_payload: list[dict] = []
    for t in tasks:
        tid = t["id"]
        sd = (t.get("subdirectory") or "").strip("/")
        feeds_searches: list[str] = []
        feeds_ags: set[str] = set()
        if sd:
            for sm in search_meta.values():
                if sd in sm["subdirs"]:
                    feeds_searches.append(sm["name"])
                    feeds_ags.update(sm["alert_groups"])
        tasks_payload.append({
            "id": tid,
            "title": t.get("title") or "",
            "subdirectory": sd,
            "library_script_id": t.get("library_script_id"),
            "disabled": bool(t.get("disabled", False)),
            "feeds_searches": sorted(feeds_searches),
            "feeds_alert_groups": sorted(feeds_ags),
        })

    # ── Per-script reverse link: which tasks deployed it ──────────────
    scripts_payload: list[dict] = []
    for s in scripts:
        script_id = s.get("id") or ""
        sd = (s.get("suggested_subdirectory") or "").strip("/")
        deployed_tasks = [t["id"] for t in tasks_by_subdir.get(sd, [])]
        scripts_payload.append({
            "id": script_id,
            "suggested_subdirectory": sd,
            "deployed_as_tasks": deployed_tasks,
        })

    return jsonify({
        "status": "success",
        "searches": list(search_meta.values()),
        "tasks": tasks_payload,
        "alert_groups": ag_payload,
        "scripts": scripts_payload,
    })


# ---------------------------------------------------------------------------
# Jobs API  (/api/jobs/*)
# ---------------------------------------------------------------------------

from job_store import get_default_job_store as _get_job_store


@app.route("/api/jobs", methods=["GET"])
def jobs_list():
    """Return metadata for all non-expired jobs, newest first."""
    try:
        jobs = _get_job_store().list_jobs()
        return jsonify({"status": "success", "jobs": jobs})
    except Exception as exc:
        logger.exception("[x] Failed to list jobs")
        return jsonify({"status": "error", "message": _safe_error_message(exc)}), 500


@app.route("/api/jobs/<path:job_id>", methods=["GET"])
def jobs_get(job_id):
    """Return metadata for a single job."""
    meta = _get_job_store().get_job_meta(job_id)
    if meta is None:
        return jsonify({"status": "error", "message": f"Job '{job_id}' not found."}), 404
    return jsonify({"status": "success", "job": meta})


@app.route("/api/jobs/<path:job_id>/save", methods=["POST"])
def jobs_save(job_id):
    """Promote an auto-job to a saved job with TTL and optional rename."""
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip() or None
    ttl_days = int(data.get("ttl_days", 10))
    save_to_lookups = bool(data.get("save_to_lookups", False))

    try:
        meta = _get_job_store().save_job(
            job_id,
            name=name,
            ttl_days=ttl_days,
            save_to_lookups=save_to_lookups,
        )
        return jsonify({"status": "success", "job": meta})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        logger.exception("[x] Failed to save job")
        return jsonify({"status": "error", "message": _safe_error_message(exc)}), 500


@app.route("/api/jobs/<path:job_id>", methods=["DELETE"])
def jobs_delete(job_id):
    """Delete a job."""
    try:
        _get_job_store().delete_job(job_id)
        return jsonify({"status": "success"})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        logger.exception("[x] Failed to delete job")
        return jsonify({"status": "error", "message": _safe_error_message(exc)}), 500


# ---------------------------------------------------------------------------
# Documentation API  (/api/docs/*)
# ---------------------------------------------------------------------------

_DOCS_DIR = os.path.join(PROJECT_ROOT, "docs", "lang")

@app.route("/api/docs/", methods=["GET"])
def list_doc_files():
    """Return an ordered list of documentation files."""
    if not os.path.isdir(_DOCS_DIR):
        return jsonify([])
    files = sorted(f for f in os.listdir(_DOCS_DIR) if f.endswith(".md"))
    result = []
    for f in files:
        path = os.path.join(_DOCS_DIR, f)
        with open(path, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip().lstrip("# ").strip()
        result.append({"filename": f, "title": first_line})
    return jsonify(result)


@app.route("/api/docs/<filename>", methods=["GET"])
def get_doc_file(filename):
    """Return the raw Markdown content of a documentation file."""
    if not re.match(r'^[\w\-]+\.md$', filename):
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(_DOCS_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    return jsonify({"filename": filename, "content": content})


# ---------------------------------------------------------------------------
# Credentials API  (/api/credentials/*)
# ---------------------------------------------------------------------------

@app.route("/api/credentials/<int:script_id>", methods=["GET"])
def cred_list(script_id):
    """List credential key names (never values) for a script.

    By default returns the back-compat ``keys`` field - the merged
    per-task + global list, which is what the script will actually
    resolve at runtime.

    Pass ``?split=true`` (Wave-7 followup, 2026-04-26) to also get
    ``per_script`` + ``global`` arrays separately so the UI can render
    each layer distinctly + offer the "Make global" promote action on
    per-task entries only.
    """
    engine = _get_engine()
    split = (request.args.get("split", "").lower()
             in ("1", "true", "yes"))
    if split:
        info = engine.list_credentials_split(script_id)
        return jsonify({
            "status": "success",
            "keys": info["merged"],   # back-compat
            "per_script": info["per_script"],
            "global": info["global"],
        })
    keys = engine.list_credentials(script_id)
    return jsonify({"status": "success", "keys": keys})


@app.route(
    "/api/credentials/<int:script_id>/<key_name>/promote-to-global",
    methods=["POST"],
)
def cred_promote_to_global(script_id, key_name):
    """Promote a per-script credential to the global vault.

    Decrypts the per-script value server-side, re-encrypts it as a
    global, and removes the per-script entry. Plaintext never leaves
    the server. After promote, every script declaring ``key_name``
    in ``requires_credentials`` resolves the value automatically - no
    re-typing per script. Added 2026-04-26 to fix the "I can't reuse a
    saved API key across scripts" complaint.

    Returns 404 if the per-script credential doesn't exist.
    """
    engine = _get_engine()
    try:
        engine.promote_credential_to_global(script_id, key_name)
    except KeyError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        logger.exception(
            "[!] promote_to_global(script=%s, key=%s) failed: %s",
            script_id, key_name, exc,
        )
        return jsonify({"status": "error", "message": str(exc)}), 500
    return jsonify({
        "status": "success",
        "message": (
            f"Credential '{key_name}' is now global - every script "
            f"declaring it will resolve the value automatically."
        ),
    })


@app.route("/api/credentials/<int:script_id>", methods=["POST"])
def cred_store(script_id):
    """Store an encrypted credential for a script."""
    data = request.get_json(force=True, silent=True) or {}
    key_name = data.get("key_name", "").strip()
    value = data.get("value", "")
    if not key_name or not value:
        return jsonify({"status": "error", "message": "key_name and value are required."}), 400
    engine = _get_engine()
    try:
        engine.store_credential(script_id, key_name, value)
        return jsonify({"status": "success", "message": f"Credential '{key_name}' stored."})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/credentials/<int:script_id>/<key_name>", methods=["DELETE"])
def cred_delete(script_id, key_name):
    """Delete a single credential for a script."""
    engine = _get_engine()
    count = engine.delete_credential(script_id, key_name)
    if count:
        return jsonify({"status": "success", "message": f"Credential '{key_name}' deleted."})
    return jsonify({"status": "error", "message": "Credential not found."}), 404


# ── Global (one-to-many) credential vault ──────────────────────────
#
# Scripts that declare ``requires_credentials: ["FRED_API_KEY"]`` pick
# up the value from the global vault automatically - enter a key once,
# every script that needs it resolves. Per-task entries override
# globals (edge cases, rotation, A/B testing).

@app.route("/api/credentials/global", methods=["GET"])
def cred_global_list():
    """List global credential key names (never values)."""
    engine = _get_engine()
    keys = engine._vault.list_global_keys()
    return jsonify({"status": "success", "keys": keys})


@app.route("/api/credentials/global", methods=["POST"])
def cred_global_store():
    """Store (or update) a global credential.

    Body: ``{"key_name": "FRED_API_KEY", "value": "..."}``
    """
    data = request.get_json(force=True, silent=True) or {}
    key_name = data.get("key_name", "").strip()
    value = data.get("value", "")
    if not key_name or not value:
        return jsonify({"status": "error", "message": "key_name and value are required."}), 400
    engine = _get_engine()
    try:
        engine._vault.store_global(key_name, value)
        return jsonify({
            "status": "success",
            "message": f"Global credential '{key_name}' stored. Any task declaring it will pick it up automatically.",
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/credentials/global/<key_name>", methods=["DELETE"])
def cred_global_delete(key_name):
    """Delete a single global credential. Per-task entries with the same
    name are preserved (they would continue to resolve via the per-task
    layer even after the global is removed)."""
    engine = _get_engine()
    count = engine._vault.delete_global(key_name)
    if count:
        return jsonify({"status": "success", "message": f"Global credential '{key_name}' deleted."})
    return jsonify({"status": "error", "message": "Global credential not found."}), 404


# ---------------------------------------------------------------------------
# Analyzer API Key  (/api/settings/analyzer-key)
# Uses the credential vault with script_id=-1 (system-level credentials).
# ---------------------------------------------------------------------------

_ANALYZER_SCRIPT_ID = -1
_ANALYZER_KEY_NAME = "ANTHROPIC_API_KEY"


@app.route("/api/settings/analyzer-key", methods=["GET"])
def analyzer_key_check():
    """Check whether the analyzer API key is stored (never returns the value)."""
    engine = _get_engine()
    has_key = engine._vault.has_credentials(_ANALYZER_SCRIPT_ID)
    return jsonify({"status": "success", "has_key": has_key})


@app.route("/api/settings/analyzer-key", methods=["POST"])
def analyzer_key_store():
    """Store the Claude analyzer API key in the credential vault."""
    data = request.get_json(force=True, silent=True) or {}
    value = data.get("value", "").strip()
    if not value:
        return jsonify({"status": "error", "message": "API key value is required."}), 400
    engine = _get_engine()
    try:
        engine._vault.store(_ANALYZER_SCRIPT_ID, _ANALYZER_KEY_NAME, value)
        return jsonify({"status": "success", "message": "Analyzer API key stored securely."})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/settings/analyzer-key", methods=["DELETE"])
def analyzer_key_delete():
    """Delete the stored analyzer API key."""
    engine = _get_engine()
    count = engine._vault.delete(_ANALYZER_SCRIPT_ID, _ANALYZER_KEY_NAME)
    if count:
        return jsonify({"status": "success", "message": "Analyzer API key deleted."})
    return jsonify({"status": "error", "message": "No analyzer API key found."}), 404


@app.route("/api/analyzer/test", methods=["POST"])
def analyzer_test_connectivity():
    """Fire a minimal Claude API call to verify credentials + connectivity.

    Accepts an optional ``value`` in the request body so the UI can test a
    key that the user has typed but not yet saved. When omitted, the stored
    key from the vault is used. Response is always JSON with an ``ok``
    boolean plus diagnostic fields (latency, tokens, cost, error class).

    Each call is recorded in ``claude_api_history.sqlite`` and the
    ``indexes/logs/claude_api/`` Parquet stream, so the user can audit
    test attempts alongside production traffic.
    """
    from analyzers.claude_client import test_connectivity

    data = request.get_json(force=True, silent=True) or {}
    candidate_key = (data.get("value") or "").strip() or None
    try:
        result = test_connectivity(api_key=candidate_key)
    except Exception as exc:
        logger.error("[x] Claude test endpoint crashed: %s", exc)
        return jsonify({
            "status": "error",
            "ok": False,
            "message": _safe_error_message(exc),
        }), 500

    if result.get("ok"):
        return jsonify({"status": "success", **result})
    return jsonify({"status": "error", **result}), 400


@app.route("/api/claude-history", methods=["GET"])
def claude_history_list():
    """Return a page of Claude API call records for the UI history view."""
    from analyzers.claude_history_store import ClaudeHistoryStore
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
        since = request.args.get("since")
        until = request.args.get("until")
        source = request.args.get("source") or None
        group_name = request.args.get("group_name") or None
        status = request.args.get("status") or None
        include_payloads = request.args.get("payloads", "0") in ("1", "true", "yes")
        rows = ClaudeHistoryStore.get_instance().list_calls(
            limit=limit, offset=offset,
            since_epoch=int(since) if since else None,
            until_epoch=int(until) if until else None,
            source=source, group_name=group_name, status=status,
            include_payloads=include_payloads,
        )
        return jsonify({"status": "success", "rows": rows, "count": len(rows)})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logger.error("[x] Failed to list Claude history: %s", exc)
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
        }), 500


@app.route("/api/claude-history/<request_id>", methods=["GET"])
def claude_history_detail(request_id: str):
    """Return a single Claude API call with full decoded request + response."""
    from analyzers.claude_history_store import ClaudeHistoryStore
    try:
        row = ClaudeHistoryStore.get_instance().get_call(request_id)
        if row is None:
            return jsonify({"status": "error", "message": "Not found."}), 404
        return jsonify({"status": "success", "row": row})
    except Exception as exc:
        logger.error("[x] Failed to fetch Claude history row %s: %s", request_id, exc)
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
        }), 500


@app.route("/api/claude-history/stats", methods=["GET"])
def claude_history_stats():
    """Aggregate token + cost stats across the Claude history DB.

    Supports ``since`` (epoch) and ``group_name`` query filters for
    slicing. Returns ``db_size_bytes`` so the UI can surface when it's
    time to vacuum or back up.
    """
    from analyzers.claude_history_store import ClaudeHistoryStore
    try:
        since = request.args.get("since")
        group_name = request.args.get("group_name") or None
        store = ClaudeHistoryStore.get_instance()
        stats = store.stats(
            since_epoch=int(since) if since else None,
            group_name=group_name,
        )
        stats["db_size_bytes"] = store.db_size_bytes()
        return jsonify({"status": "success", "stats": stats})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logger.error("[x] Failed to compute Claude stats: %s", exc)
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
        }), 500


@app.route("/api/claude-history/vacuum", methods=["POST"])
def claude_history_vacuum():
    """Run VACUUM on the Claude history SQLite DB.

    Optional ``older_than_epoch`` deletes rows first. The user is expected
    to back up the DB file before invoking - this endpoint does not take a
    backup on their behalf.
    """
    from analyzers.claude_history_store import ClaudeHistoryStore
    try:
        data = request.get_json(force=True, silent=True) or {}
        cutoff = data.get("older_than_epoch")
        store = ClaudeHistoryStore.get_instance()
        removed = 0
        if cutoff is not None:
            removed = store.delete_older_than(int(cutoff))
        store.vacuum()
        return jsonify({
            "status": "success",
            "removed": removed,
            "db_size_bytes": store.db_size_bytes(),
        })
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logger.error("[x] Claude history vacuum failed: %s", exc)
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
        }), 500




# ---------------------------------------------------------------------------
# Global Settings API  (/api/settings/*)
# ---------------------------------------------------------------------------

from global_settings import get_settings as _get_global_settings
from lexers.grammar_vocab import get_vocab as _get_grammar_vocab


@app.route("/api/version")
def api_version():
    """Return the current SpeakesQuery version."""
    try:
        version_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(version_file) as f:
            version = f.read().strip()
    except Exception:
        version = "unknown"
    return jsonify({"version": version})


@app.route("/api/system/clock", methods=["GET"])
def api_system_clock():
    """Return the server's current time + scheduler timezone.

    The UI top bar polls this so the operator can sanity-check "what
    time does SpeakesQuery think it is?" when writing cron expressions.
    The scheduler TZ is the authoritative timezone for every cron
    field on every alert group + saved search - we force UTC explicitly
    on boot (see ``scheduled_input_engine/engine.py``) so results are
    predictable regardless of the Docker host's system TZ.
    """
    import datetime as _dt
    import time as _time
    now_utc = _dt.datetime.now(_dt.timezone.utc)

    # Scheduler TZ - read from the running engine if available, else
    # fall back to the hard-coded expectation. Kept as a string so the
    # UI can render it directly.
    scheduler_tz = "UTC"
    try:
        engine = _get_engine()
        sched_tz = getattr(engine._scheduler, "timezone", None)
        if sched_tz is not None:
            scheduler_tz = str(sched_tz)
    except Exception:
        pass

    # Also surface the system local TZ so the operator can see how it
    # differs from scheduler TZ - e.g. "server is in UTC but cron
    # interprets in UTC too, so no conversion needed".
    try:
        system_tz = _time.tzname[0] or "UTC"
    except Exception:
        system_tz = "?"

    return jsonify({
        "server_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "server_time_iso": now_utc.isoformat(),
        "scheduler_timezone": scheduler_tz,
        "system_timezone": system_tz,
        "epoch": int(now_utc.timestamp()),
        # Helper: render a cron expression's NEXT fire time using the
        # scheduler's TZ. Unused in v1 - UI uses server_time_utc only -
        # but exposed for future per-AG "next run at" indicators.
        "note": (
            "All cron expressions on alert groups + saved searches are "
            "interpreted in scheduler_timezone. SpeakesQuery forces UTC "
            "explicitly so behaviour is independent of the Docker host "
            "system TZ. Example: `30 11 * * *` fires at 11:30 UTC daily."
        ),
    })


@app.route("/api/visual-builder/parse", methods=["POST"])
def visual_builder_parse():
    """Parse an SPQL string into ``{index_clause, stages}`` for the
    Visual Builder canvas.

    Phase 4 / Bet 4 slice 6: round-trip endpoint. The SPA's "Load"
    button POSTs operator-pasted SPQL here and uses the returned
    structure to populate stage cards.

    Per the slice-5 architectural principle (see
    ``reference_reuse_existing_endpoint_for_ui_surface``), a new
    endpoint is justified by NEW BEHAVIOUR, not new UI. Parsing
    SPQL → stage list IS new behaviour (no existing endpoint does
    this). The parser is grammar-version-stable: it doesn't go
    through ANTLR; it's a flat split-on-pipe-outside-quotes.

    Body:
        ``{ "spql": "<SPQL string>" }``

    Returns:
        ``{ "status": "success",
            "index_clause": str,
            "stages": [ {"command": str, "kwargs": str}, ... ] }``

    400 on missing / non-string ``spql`` field.
    """
    from lexers.spql_pipeline_split import split_spql_pipeline

    data = request.get_json(force=True, silent=True) or {}
    spql = data.get("spql")
    if not isinstance(spql, str):
        return jsonify({
            "status": "error",
            "message": "Missing or non-string 'spql' field in request body.",
            "error_class": "InvalidInput",
        }), 400
    parsed = split_spql_pipeline(spql)
    return jsonify({
        "status": "success",
        "index_clause": parsed["index_clause"],
        "stages": parsed["stages"],
    })


@app.route("/api/grammar/vocab", methods=["GET"])
def grammar_vocab():
    """Return the grammar-derived vocabulary for console autocomplete.

    The vocab is extracted from ``lexers/speakesQuery.g4`` at first call and
    cached. See :mod:`lexers.grammar_vocab` for the returned shape.
    """
    try:
        return jsonify({"status": "success", "vocab": _get_grammar_vocab()})
    except Exception as exc:
        logger.error("[x] Failed to load grammar vocab: %s", exc)
        return jsonify({
            "status": "error",
            "message": f"Failed to load grammar vocab: {_safe_error_message(exc)}",
        }), 500


@app.route("/api/settings", methods=["GET"])
def settings_get():
    """Return all global settings."""
    try:
        settings = _get_global_settings()
        return jsonify({"status": "success", "settings": settings.get_all()})
    except Exception as exc:
        logger.error("[x] Failed to load settings: %s", exc)
        return jsonify({"status": "error", "message": f"Failed to load settings: {_safe_error_message(exc)}"}), 500


@app.route("/api/settings", methods=["POST"])
def settings_update():
    """Update one or more global settings."""
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        return jsonify({"status": "error", "message": "No settings provided."}), 400
    try:
        settings = _get_global_settings()
        errors = settings.update(data)
    except PermissionError as exc:
        logger.error("[x] Permission denied writing settings: %s", exc)
        return jsonify({
            "status": "error",
            "message": "Permission denied writing global_settings.yaml. "
                       "Check file/directory permissions.",
        }), 500
    except Exception as exc:
        logger.error("[x] Failed to save settings: %s", exc)
        return jsonify({"status": "error", "message": f"Failed to save settings: {_safe_error_message(exc)}"}), 500
    if errors:
        return jsonify({"status": "partial", "errors": errors, "settings": settings.get_all()})
    return jsonify({"status": "success", "settings": settings.get_all()})


@app.route("/api/settings/reset", methods=["POST"])
def settings_reset():
    """Reset all settings to defaults."""
    try:
        settings = _get_global_settings()
        settings.reset_all()
        return jsonify({"status": "success", "settings": settings.get_all()})
    except Exception as exc:
        logger.error("[x] Failed to reset settings: %s", exc)
        return jsonify({"status": "error", "message": f"Failed to reset settings: {_safe_error_message(exc)}"}), 500


# ---------------------------------------------------------------------------
# Email Test API  (/api/email/*)
# ---------------------------------------------------------------------------

@app.route("/api/email/diagnose", methods=["POST"])
def email_diagnose():
    """Run a stepwise SMTP diagnostic against the currently saved settings.

    Unlike ``/api/email/test`` (which only says pass/fail), this walks TCP
    reach → STARTTLS → AUTH → optional delivery separately and returns a
    structured report. Use it when Send Test Email surfaces a generic
    "Send failed" message and you need to tell whether AUTH, TLS, or
    egress is the actual blocker.

    Request body (all optional)::

        {
          "send_to": "you@example.com",   // if present, also attempt delivery
          "strip_password": true          // retry AUTH with whitespace stripped
        }
    """
    from tools.smtp_diagnose import run_diagnostic
    data = request.get_json(force=True, silent=True) or {}
    send_to = (data.get("send_to") or "").strip() or None
    strip_password = bool(data.get("strip_password", False))
    try:
        report = run_diagnostic(send_to=send_to, strip_password=strip_password)
        return jsonify({"status": "success", "report": report.as_dict()})
    except Exception as exc:
        logger.error("[x] email_diagnose failed: %s", exc)
        return jsonify({
            "status": "error",
            "message": f"Diagnostic crashed: {_safe_error_message(exc)}",
        }), 500


@app.route("/api/email/test", methods=["POST"])
def email_test():
    """Send a test email using the currently saved SMTP settings."""
    data = request.get_json(force=True, silent=True) or {}
    to_addr = (data.get("to") or "").strip()
    if not to_addr:
        return jsonify({"status": "error", "message": "Recipient address required."}), 400

    try:
        from query_engine.Alert import send_email
        send_email(
            subject="SpeakesQuery - Test Email",
            body=(
                "This is a test email from SpeakesQuery.\n\n"
                "If you are reading this, your SMTP configuration is working correctly.\n"
                "Saved-search alerts will be delivered to the address configured on each search."
            ),
            to_addrs=to_addr,
            timeout_seconds=15,
        )
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)})
    except Exception as exc:
        logger.error("[x] Test email failed: %s", exc)
        return jsonify({"status": "error", "message": f"Send failed: {_safe_error_message(exc)}"})

    return jsonify({"status": "success", "message": f"Test email sent to {to_addr}."})


# ---------------------------------------------------------------------------
# Script Library API  (/api/library/*)
# ---------------------------------------------------------------------------

from script_library import list_scripts as _list_library_scripts
from script_library import get_script as _get_library_script


@app.route("/api/library/list", methods=["GET"])
def library_list():
    """Return metadata for all library scripts."""
    scripts = _list_library_scripts()
    return jsonify({"status": "success", "scripts": scripts})


@app.route("/api/library/<script_id>", methods=["GET"])
def library_get(script_id):
    """Return full details (including code) for a library script."""
    # Sanitise: only allow alphanumeric, underscore, hyphen
    if not re.match(r'^[\w\-]+$', script_id):
        return jsonify({"status": "error", "message": "Invalid script ID."}), 400
    script = _get_library_script(script_id)
    if script is None:
        return jsonify({"status": "error", "message": "Script not found."}), 404
    return jsonify({"status": "success", "script": script})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _log_startup_diagnostics() -> None:
    """Log diagnostic information about paths, permissions, and indexes."""
    idx_dir = _get_browse_dir()
    uid = os.getuid() if hasattr(os, "getuid") else "n/a"
    try:
        import getpass
        user = getpass.getuser()
    except (ImportError, KeyError):
        user = f"uid={uid}"
    logger.info("── Startup Diagnostics ──────────────────────────────────")
    logger.info("[i] PROJECT_ROOT : %s", PROJECT_ROOT)
    logger.info("[i] Indexes dir  : %s", idx_dir)
    logger.info("[i] Lookups dir  : %s", LOOKUPS_DIR)
    logger.info("[i] Running as   : uid=%s user=%s", uid, user)
    logger.info("[i] CWD          : %s", os.getcwd())

    for label, dirpath in [("Indexes", idx_dir), ("Lookups", LOOKUPS_DIR)]:
        if not os.path.exists(dirpath):
            logger.warning("[!] %s directory does NOT exist: %s", label, dirpath)
        elif not os.path.isdir(dirpath):
            logger.warning("[!] %s path is not a directory: %s", label, dirpath)
        elif not os.access(dirpath, os.R_OK):
            logger.warning("[!] %s directory is NOT readable (permission denied): %s", label, dirpath)
        else:
            # Count parquet files
            count = 0
            for root, _dirs, files in os.walk(dirpath):
                count += sum(1 for f in files if f.endswith(".parquet"))
            logger.info("[i] %s directory OK - %d .parquet file(s) found", label, count)
    logger.info("─────────────────────────────────────────────────────────")


def _persistence_target_inventory() -> list[dict]:
    """Return one row per user-data persistence target.

    Reuses the canonical list from ``tools.persistence`` so the in-app
    banner, the ``/api/persistence/audit`` endpoint, and the
    ``./update.sh`` snapshot tool all agree on what counts as user data.

    Lazy import so a missing ``tools/`` dir cannot crash server boot.
    """
    from pathlib import Path as _Path
    try:
        from tools.persistence import (
            DIR_TARGETS_HASHED, FILE_TARGETS,
            EXTERNAL_TARGETS, DIR_TARGETS_SUMMARIZED,
        )
    except ImportError as exc:
        logger.warning("[!] persistence audit: %s", exc)
        return []

    proj = _Path(PROJECT_ROOT)
    rows: list[dict] = []
    for rel in DIR_TARGETS_HASHED + DIR_TARGETS_SUMMARIZED:
        p = proj / rel
        rows.append({
            "path": rel,
            "kind": "dir",
            "exists": p.exists(),
            "is_dir": p.is_dir() if p.exists() else False,
        })
    for rel in FILE_TARGETS:
        p = proj / rel
        size = None
        if p.exists() and p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = None
        rows.append({
            "path": rel,
            "kind": "file",
            "exists": p.exists(),
            "size": size,
        })
    for ext in EXTERNAL_TARGETS:
        rows.append({
            "path": str(ext),
            "kind": "external",
            "exists": ext.exists(),
        })
    return rows


def _log_persistence_audit() -> None:
    """Loud-warn on missing persistence targets so a misconfigured Docker
    bind-mount or a destructive update is visible in ``docker logs``
    immediately. Companion to :func:`_log_startup_diagnostics`."""
    rows = _persistence_target_inventory()
    if not rows:
        return
    logger.info("── Persistence Audit ────────────────────────────────────")
    issues = [r for r in rows if not r["exists"]]
    if issues:
        for r in issues:
            logger.warning(
                "[!] persistence: missing %s: %s", r["kind"], r["path"]
            )
        logger.warning(
            "[!] %d/%d persistence target(s) missing - user data may not "
            "survive container rebuild. Check docker-compose.yml "
            "bind-mounts. See docs/lang/13_backup_recovery.md.",
            len(issues), len(rows),
        )
    else:
        logger.info("[i] persistence: all %d target(s) present", len(rows))
    logger.info("─────────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# Notebooks  (Phase 3 / Bet 4 slice 4 - first user-visible deliverable)
# ---------------------------------------------------------------------------
# Slices 1-3 shipped backend (notebook_store / notebook_engine /
# notebook_cache_store) without /api/* wiring; slice 4 exposes them
# through the SPA. Endpoints mirror the macros / alert-groups patterns
# (jsonify status/error/exists; same HTTP-code conventions).

from notebook_store import get_store as _get_notebook_store_singleton
from notebook_engine import NotebookEngine as _NotebookEngineCls
from notebook_cache_store import get_store as _get_notebook_cache_singleton

_notebook_engine = _NotebookEngineCls()


@app.route("/api/notebooks", methods=["GET"])
def notebooks_list():
    """List all notebooks (lightweight summary for the SPA list view).

    Returns id + name + description + cell_count + timestamps. The
    full record (including cell sources) is fetched via
    GET /api/notebooks/<id>.
    """
    store = _get_notebook_store_singleton()
    items = []
    for nb in store.list_notebooks():
        items.append({
            "id": nb["id"],
            "name": nb.get("name", ""),
            "description": nb.get("description", ""),
            "cell_count": len(nb.get("cells", [])),
            "default_max_cost_usd": nb.get("default_max_cost_usd", 0.0),
            "created_at": nb.get("created_at", ""),
            "updated_at": nb.get("updated_at", ""),
        })
    return jsonify({"status": "success", "notebooks": items})


@app.route("/api/notebooks/<notebook_id>", methods=["GET"])
def notebooks_get(notebook_id):
    """Return the full notebook record (including all cell sources)."""
    store = _get_notebook_store_singleton()
    nb = store.get_notebook(notebook_id)
    if nb is None:
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
        }), 404
    return jsonify({"status": "success", "notebook": nb})


@app.route("/api/notebooks", methods=["POST"])
def notebooks_create():
    """Create a new notebook (or overwrite if ``overwrite=true``)."""
    data = request.get_json(force=True, silent=True) or {}
    overwrite = data.pop("overwrite", False)
    if not data.get("id"):
        return jsonify({
            "status": "error",
            "message": "Notebook id is required.",
        }), 400
    try:
        result = _get_notebook_store_singleton().save_notebook(
            data, overwrite=bool(overwrite),
        )
        return jsonify({"status": "success", "notebook": result})
    except FileExistsError as exc:
        return jsonify({"status": "exists", "message": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/notebooks/<notebook_id>", methods=["PUT"])
def notebooks_update(notebook_id):
    """Update an existing notebook (cells replaced wholesale per the
    slice-1 store contract)."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = _get_notebook_store_singleton().update_notebook(
            notebook_id, data,
        )
        return jsonify({"status": "success", "notebook": result})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/notebooks/<notebook_id>", methods=["DELETE"])
def notebooks_delete(notebook_id):
    """Delete a notebook + cascade-invalidate its cache entries."""
    if not _get_notebook_store_singleton().delete_notebook(notebook_id):
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
        }), 404
    # Cascade: drop cache entries for this notebook so the UI never
    # shows stale results on a re-created notebook with the same id.
    try:
        evicted = _get_notebook_cache_singleton().invalidate_notebook(
            notebook_id,
        )
    except Exception as exc:
        logger.warning(
            "[!] Could not invalidate cache for deleted notebook %s: %s",
            notebook_id, exc,
        )
        evicted = 0
    return jsonify({
        "status": "success",
        "message": f"Notebook {notebook_id!r} deleted.",
        "cache_entries_invalidated": evicted,
    })


@app.route("/api/notebooks/<notebook_id>/execute", methods=["POST"])
def notebooks_execute(notebook_id):
    """Execute a notebook top-to-bottom and return the run result.

    Body fields (all optional):
      * ``use_cache`` (bool, default True) - slice-3 reactive cache
        controls. Pass False to force full re-execution.
      * ``namespace_overrides`` (dict, default {}) - slice-5
        param-form value overrides. Keys are cell ids of param cells
        that should use the supplied value instead of the YAML spec's
        ``default``. Param cells bypass the cache so different
        override values always produce the right downstream behaviour.
      * ``stop_at_cell_id`` (str, default null) - slice-6 per-cell
        Run support. When provided, runs cells [0..N] only (where N
        is the index of the cell with this id). Upstream cells
        normally hit cache; the target cell is the iteration focus.
        Returns 400 with ``error_class="UnknownCellId"`` if the id
        doesn't match any cell in the notebook.
    """
    data = request.get_json(force=True, silent=True) or {}
    use_cache = data.get("use_cache", True)
    namespace_overrides = data.get("namespace_overrides") or {}
    stop_at_cell_id = data.get("stop_at_cell_id")
    if not isinstance(namespace_overrides, dict):
        return jsonify({
            "status": "error",
            "message": "namespace_overrides must be a JSON object (dict).",
            "error_class": "InvalidInput",
            "expected": "dict",
            "actual": type(namespace_overrides).__name__,
        }), 400
    if stop_at_cell_id is not None and not isinstance(stop_at_cell_id, str):
        return jsonify({
            "status": "error",
            "message": "stop_at_cell_id must be a string when provided.",
            "error_class": "InvalidInput",
            "expected": "str",
            "actual": type(stop_at_cell_id).__name__,
        }), 400

    store = _get_notebook_store_singleton()
    nb = store.get_notebook(notebook_id)
    if nb is None:
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
            "error_class": "NotFound",
            "notebook_id": notebook_id,
        }), 404

    cache_store = _get_notebook_cache_singleton()
    try:
        run_result = _notebook_engine.execute_notebook(
            nb,
            namespace=dict(namespace_overrides) if namespace_overrides else None,
            use_cache=bool(use_cache),
            cache_store=cache_store,
            stop_at_cell_id=stop_at_cell_id,
        )
    except LookupError as exc:
        # stop_at_cell_id didn't match any cell. Structured 400 so an
        # AI agent (or the SPA) can branch on the error_class.
        return jsonify({
            "status": "error",
            "message": str(exc),
            "error_class": "UnknownCellId",
            "stop_at_cell_id": stop_at_cell_id,
            "valid_cell_ids": [c.get("id") for c in (nb.get("cells") or [])],
        }), 400
    return jsonify({"status": "success", "result": run_result.to_dict()})


@app.route("/api/notebooks/_cache/stats", methods=["GET"])
def notebooks_cache_stats():
    """Return cache statistics (entries, size, hits)."""
    return jsonify({
        "status": "success",
        "stats": _get_notebook_cache_singleton().stats(),
    })


@app.route("/api/notebooks/_cache/clear", methods=["POST"])
def notebooks_cache_clear():
    """Drop EVERY cache entry. Admin-grade action; survives no
    confirmation prompt at the API level - UI should confirm before
    posting.
    """
    freed = _get_notebook_cache_singleton().clear()
    return jsonify({"status": "success", "bytes_freed": freed})


# ---------------------------------------------------------------------------
# Notebook export  (Phase 3 / Bet 4 slice 8)
# ---------------------------------------------------------------------------

def _build_notebook_export_html(
    nb: dict, run_result_dict: "dict | None" = None,
) -> str:
    """Build a self-contained HTML rendering of a notebook.

    Dual-audience contract: the human-facing rendering (cell-type
    specific HTML) and a structured ``<script type="application/json"
    id="notebook-data">`` sidecar live in the SAME export. AI agents
    that ingest the .html file read the JSON sidecar without HTML
    scraping; humans see the rendered notebook in any browser.

    Vega-Lite chart cells embed their JSON spec + a CDN script tag -
    charts render in any browser with internet access. PDF export
    (which can't run JS) sees the spec as static text.
    """
    import html
    import json
    title = html.escape(nb.get("name") or nb.get("id") or "Notebook")
    description = html.escape(nb.get("description") or "")
    updated = html.escape(nb.get("updated_at") or "")
    cells = nb.get("cells") or []

    # Build the per-cell results map for quick lookup
    results_by_id: dict = {}
    if run_result_dict:
        for cr in run_result_dict.get("cells") or []:
            results_by_id[cr.get("cell_id")] = cr

    cell_html_blocks: list[str] = []
    for cell in cells:
        cid = html.escape(str(cell.get("id") or ""))
        ctype = html.escape(str(cell.get("type") or ""))
        source = cell.get("source") or ""
        result = results_by_id.get(cell.get("id"))

        # Header
        header = (
            f'<div class="nbx-cell-header">'
            f'<span class="nbx-cell-type nbx-cell-type-{ctype}">{ctype}</span>'
            f'<span class="nbx-cell-id">#{cid}</span>'
            f'</div>'
        )

        # Source block (always shown - viewers can read the cell's code)
        source_block = (
            f'<pre class="nbx-cell-source">{html.escape(source)}</pre>'
        )

        # Output: dispatch by type, falling back to result.output_repr
        body = ""
        if result and result.get("status") == "error":
            body = (
                f'<div class="nbx-cell-output nbx-error">'
                f'<strong>{html.escape(result.get("error_class") or "")}</strong>: '
                f'{html.escape(result.get("error_message") or "")}'
                f'</div>'
            )
        elif ctype == "markdown" and result and result.get("output_html"):
            # Trust server-side-rendered markdown (slice 5)
            body = (
                f'<div class="nbx-cell-output nbx-markdown">'
                f'{result.get("output_html")}'
                f'</div>'
            )
        elif ctype == "chart" and source.strip():
            # Embed the spec as JSON + a Vega-Lite mount point. The
            # included <script> at the top of the export bootstraps
            # vega-embed; this block just declares the spec.
            spec_json = json.dumps(_safe_json_load(source), default=str)
            body = (
                f'<div class="nbx-cell-output nbx-chart" '
                f'id="nbx-chart-{cid}" data-spec="{html.escape(spec_json)}">'
                f'<noscript>JavaScript required to render charts. Spec:</noscript>'
                f'</div>'
            )
        elif result and result.get("output_preview"):
            preview = result["output_preview"]
            body = _build_dataframe_html_table(preview)
        elif result and result.get("output_repr"):
            body = (
                f'<pre class="nbx-cell-output">'
                f'{html.escape(result.get("output_repr") or "")}'
                f'</pre>'
            )
        elif ctype == "param":
            # No "run" needed; show the spec verbatim.
            body = (
                f'<pre class="nbx-cell-output">{html.escape(source)}</pre>'
            )

        # Cache + runtime metadata
        meta = ""
        if result:
            cache = "⚡ cached" if result.get("cache_hit") else ""
            meta = (
                f'<div class="nbx-cell-meta">'
                f'<span>status: {html.escape(result.get("status") or "")}</span> '
                f'<span>runtime: {result.get("runtime_ms", 0)} ms</span> '
                f'<span class="nbx-cache-badge">{cache}</span>'
                f'</div>'
            )

        cell_html_blocks.append(
            f'<section class="nbx-cell" data-cell-id="{cid}" data-cell-type="{ctype}">'
            f'{header}{source_block}{body}{meta}'
            f'</section>'
        )

    # JSON sidecar - full notebook record + run result for AI consumers.
    sidecar = json.dumps({
        "schema_version": 1,
        "kind": "notebook_export",
        "notebook": nb,
        "run_result": run_result_dict,
    }, default=str)

    # Vega-Lite mount script. Loads from CDN once; iterates over every
    # ``[id^="nbx-chart-"]`` element and renders its data-spec.
    vega_mount = """
<script>
(function() {
  var chartHosts = document.querySelectorAll('[id^="nbx-chart-"]');
  if (!chartHosts.length) return;
  function loadScript(src) {
    return new Promise(function(resolve, reject) {
      var s = document.createElement('script');
      s.src = src; s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }
  Promise.resolve()
    .then(function() { return loadScript('https://cdn.jsdelivr.net/npm/vega@5'); })
    .then(function() { return loadScript('https://cdn.jsdelivr.net/npm/vega-lite@5'); })
    .then(function() { return loadScript('https://cdn.jsdelivr.net/npm/vega-embed@6'); })
    .then(function() {
      chartHosts.forEach(function(host) {
        try {
          var spec = JSON.parse(host.getAttribute('data-spec'));
          window.vegaEmbed(host, spec, {actions: false});
        } catch (e) {
          host.innerHTML = '<pre>' + (host.getAttribute('data-spec') || '') + '</pre>';
        }
      });
    })
    .catch(function() {
      chartHosts.forEach(function(host) {
        host.innerHTML = '<pre>(chart renderer unavailable; spec: ' +
          (host.getAttribute('data-spec') || '') + ')</pre>';
      });
    });
})();
</script>
""".strip()

    css = """
body { font-family: -apple-system, system-ui, sans-serif; max-width: 980px; margin: 2em auto; padding: 0 1em; color: #1e2226; }
h1 { margin: 0 0 .25em; }
.nbx-meta { color: #666; font-size: .9em; margin-bottom: 1.5em; }
.nbx-description { color: #444; margin-bottom: 2em; white-space: pre-wrap; }
.nbx-cell { border: 1px solid #d0d7de; border-radius: 6px; margin: 1em 0; overflow: hidden; }
.nbx-cell-header { display: flex; align-items: center; gap: .5em; padding: .35em .75em; background: #f6f8fa; border-bottom: 1px solid #d0d7de; font-size: .85em; }
.nbx-cell-type { display: inline-block; padding: .1em .45em; border-radius: 3px; font-family: monospace; font-weight: 600; text-transform: uppercase; font-size: .8em; color: white; background: #2a4d5b; }
.nbx-cell-type-pipe { background: #6a3d8a; }
.nbx-cell-type-python { background: #3a5e2a; }
.nbx-cell-type-markdown { background: #5a4a2a; }
.nbx-cell-type-chart { background: #2a4a6a; }
.nbx-cell-type-param { background: #6a4a3a; }
.nbx-cell-id { font-family: monospace; color: #666; }
.nbx-cell-source { padding: .75em 1em; margin: 0; background: #fff; font-family: ui-monospace, monospace; font-size: .85em; white-space: pre-wrap; word-break: break-word; border-bottom: 1px solid #eee; }
.nbx-cell-output { padding: .75em 1em; background: #fafbfc; border-top: 1px solid #eee; font-family: ui-monospace, monospace; font-size: .85em; white-space: pre-wrap; word-break: break-word; }
.nbx-cell-output.nbx-error { background: #ffeaea; color: #8a1f1f; }
.nbx-cell-output.nbx-markdown { font-family: inherit; white-space: normal; }
.nbx-cell-output.nbx-markdown table { border-collapse: collapse; }
.nbx-cell-output.nbx-markdown table th, .nbx-cell-output.nbx-markdown table td { border: 1px solid #d0d7de; padding: .25em .5em; }
.nbx-cell-output.nbx-markdown code { background: #f6f8fa; padding: 1px 4px; border-radius: 2px; }
.nbx-cell-output.nbx-markdown pre { background: #f6f8fa; padding: .5em; border-radius: 3px; overflow-x: auto; }
.nbx-cell-output.nbx-chart { padding: .5em; min-height: 60px; background: #fff; }
.nbx-cell-meta { padding: .3em .75em; background: #f6f8fa; border-top: 1px solid #eee; font-size: .8em; color: #666; }
.nbx-cache-badge { color: #1f6f3f; font-weight: 600; }
.nbx-df-table { width: 100%; border-collapse: collapse; font-family: ui-monospace, monospace; font-size: .85em; }
.nbx-df-table th, .nbx-df-table td { padding: .25em .5em; border: 1px solid #d0d7de; text-align: left; }
.nbx-df-table th { background: #f6f8fa; font-weight: 600; }
.nbx-df-summary { color: #666; font-size: .85em; padding: .25em .75em; }
"""

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        f"<meta charset=\"utf-8\" />\n"
        f"<title>{title}</title>\n"
        f"<style>{css}</style>\n"
        f"</head>\n"
        "<body>\n"
        f"<h1>{title}</h1>\n"
        f'<div class="nbx-meta">Last updated: {updated}</div>\n'
        f'<div class="nbx-description">{description}</div>\n'
        + "\n".join(cell_html_blocks) + "\n"
        f'<script type="application/json" id="notebook-data">{html.escape(sidecar)}</script>\n'
        f"{vega_mount}\n"
        "</body>\n"
        "</html>\n"
    )


def _safe_json_load(s: str):
    """Try to JSON-parse a string; on failure return ``None`` (for the
    chart-export fallback path). Never raises.
    """
    import json
    try:
        return json.loads(s or "")
    except Exception:
        return None


def _build_dataframe_html_table(preview: dict) -> str:
    """Render a slice-5 ``output_preview`` dict as an HTML table for
    the export. Mirrors the SPA's ``renderDataFramePreview`` shape but
    in plain Python (no JS needed for the export viewer).
    """
    import html
    cols = preview.get("columns") or []
    rows = preview.get("head_rows") or []
    if not cols:
        return '<div class="nbx-cell-output">(empty result set)</div>'
    th = "".join(
        f'<th>{html.escape(c.get("name") or "")}'
        f'<br><span style="color:#666;font-weight:400">{html.escape(c.get("dtype") or "")}</span></th>'
        for c in cols
    )
    tr = ""
    for row in rows:
        cells = "".join(
            f'<td>{"<em>null</em>" if row.get(c["name"]) is None else html.escape(str(row.get(c["name"])))}</td>'
            for c in cols
        )
        tr += f"<tr>{cells}</tr>"
    summary = (
        f'{preview.get("total_rows", 0)} rows × {preview.get("total_cols", 0)} cols'
    )
    if preview.get("head_truncated"):
        summary += f' (showing first {len(rows)})'
    return (
        '<div class="nbx-cell-output" style="padding:0;">'
        '<div style="overflow-x:auto;">'
        f'<table class="nbx-df-table"><thead><tr>{th}</tr></thead>'
        f'<tbody>{tr}</tbody></table>'
        '</div>'
        f'<div class="nbx-df-summary">{summary}</div>'
        '</div>'
    )


@app.route("/api/notebooks/<notebook_id>/export/html", methods=["POST"])
def notebooks_export_html(notebook_id):
    """Export a notebook as a self-contained HTML page.

    Body fields (all optional):
      * ``run_first`` (bool, default False) - execute the notebook
        before exporting so cell outputs appear in the rendered HTML.
        Off by default so the export is fast + idempotent; turn on
        when the operator wants the freshest results in their export.

    Returns the HTML directly (Content-Type: text/html). Includes a
    ``<script type="application/json" id="notebook-data">`` sidecar
    with the full notebook + run-result JSON so AI agents can ingest
    the export programmatically without HTML scraping.
    """
    data = request.get_json(force=True, silent=True) or {}
    run_first = bool(data.get("run_first", False))

    nb = _get_notebook_store_singleton().get_notebook(notebook_id)
    if nb is None:
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
            "error_class": "NotFound",
            "notebook_id": notebook_id,
        }), 404

    run_result_dict = None
    if run_first:
        try:
            run_result = _notebook_engine.execute_notebook(
                nb, use_cache=True,
                cache_store=_get_notebook_cache_singleton(),
            )
            run_result_dict = run_result.to_dict()
        except Exception as exc:
            logger.warning(
                "[!] notebook export: run failed for %s: %s",
                notebook_id, exc,
            )

    html_body = _build_notebook_export_html(nb, run_result_dict)
    return Response(html_body, mimetype="text/html; charset=utf-8")


@app.route("/api/notebooks/<notebook_id>/export/pdf", methods=["POST"])
def notebooks_export_pdf(notebook_id):
    """Export a notebook as a PDF via WeasyPrint.

    Same body shape as ``/export/html``. Notable limitation:
    WeasyPrint is a STATIC HTML renderer - it doesn't run JavaScript.
    Vega-Lite chart cells appear as their JSON spec text rather than
    rendered visualizations. The HTML export is the right tool when
    you want charts.
    """
    data = request.get_json(force=True, silent=True) or {}
    run_first = bool(data.get("run_first", False))

    nb = _get_notebook_store_singleton().get_notebook(notebook_id)
    if nb is None:
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
            "error_class": "NotFound",
            "notebook_id": notebook_id,
        }), 404

    run_result_dict = None
    if run_first:
        try:
            run_result = _notebook_engine.execute_notebook(
                nb, use_cache=True,
                cache_store=_get_notebook_cache_singleton(),
            )
            run_result_dict = run_result.to_dict()
        except Exception as exc:
            logger.warning(
                "[!] notebook PDF export: run failed for %s: %s",
                notebook_id, exc,
            )

    html_body = _build_notebook_export_html(nb, run_result_dict)

    try:
        from weasyprint import HTML as _WP_HTML
    except (ImportError, OSError) as exc:
        # OSError covers pip-installed WeasyPrint missing its system
        # libraries (pango/cairo) - same user-facing remedy either way.
        return jsonify({
            "status": "error",
            "message": f"WeasyPrint unavailable; PDF export disabled: {exc}",
            "error_class": "MissingDependency",
            "expected": "weasyprint",
        }), 503

    try:
        pdf_bytes = _WP_HTML(string=html_body).write_pdf()
    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": f"PDF render failed: {exc}",
            "error_class": "PdfRenderError",
        }), 500

    response = Response(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{notebook_id}.pdf"'
    )
    return response


@app.route("/api/models", methods=["GET"])
def models_list():
    """Return the LLM model registry (Phase-2 model_store).

    Slice-7 use case: the notebook SPA's pipe-cell affordance lets
    operators pick a registered model and insert ``model="<id>"`` into
    the cell source. The endpoint is generic enough to reuse for any
    future surface that needs to enumerate available LLM models
    (Phase 4 visual builder, agent dispatchers, etc.).

    Dual-audience response: ``models`` array carries the full
    structured record (id + provider + model_name + costs +
    description) so AI agents can reason about which model fits a
    task without parsing UI HTML.
    """
    from model_store import get_store as _get_model_store
    items = []
    for m in _get_model_store().list_models():
        items.append({
            "id": m.get("id"),
            "provider": m.get("provider"),
            "model_name": m.get("model_name"),
            "description": m.get("description", ""),
            "endpoint": m.get("endpoint", ""),
            "cost_per_input_million_usd": float(
                m.get("cost_per_input_million_usd") or 0.0
            ),
            "cost_per_output_million_usd": float(
                m.get("cost_per_output_million_usd") or 0.0
            ),
            "max_output_tokens": int(m.get("max_output_tokens") or 4096),
            "default_timeout_seconds": int(
                m.get("default_timeout_seconds") or 120
            ),
        })
    return jsonify({"status": "success", "models": items})


@app.route("/api/notebooks/_install_default/<notebook_id>", methods=["POST"])
def notebooks_install_default(notebook_id):
    """Re-install a default notebook (no-op if already present
    unless ``overwrite=true``)."""
    overwrite = (
        request.get_json(force=True, silent=True) or {}
    ).get("overwrite", False)
    installed = _get_notebook_store_singleton().install_default(
        notebook_id, overwrite=bool(overwrite),
    )
    if installed:
        return jsonify({
            "status": "success",
            "message": f"Notebook {notebook_id!r} installed.",
        })
    return jsonify({
        "status": "skipped",
        "message": (
            "Either no default exists for that id, or the notebook "
            "already exists (pass overwrite=true to replace it)."
        ),
    })


# ---------------------------------------------------------------------------
# Notebook → Alert Group promotion (Phase 3 / Bet 4 slice 9 - the headliner)
# ---------------------------------------------------------------------------
#
# Three endpoints split by intent:
#
# * GET  /api/notebooks/<id>/promote/<cell_id>/preview - dry-run only.
#   Returns the same structured preview the cell-engine renders. Cheap,
#   read-only; can be called without running the notebook first.
#
# * POST /api/notebooks/<id>/promote/<cell_id> - actually deploy.
#   The ONLY notebook-side path that mutates AG state. Body fields:
#     - overwrite_existing (bool, default True) - false fails when name
#       is taken; true updates in place (the headliner re-deploy flow).
#
# * GET  /api/alert-groups/<name>/as-notebook - round-trip.
#   Synthesises an editable notebook from an existing AG. Read-only;
#   returns the notebook record (caller decides whether to persist via
#   POST /api/notebooks).

@app.route(
    "/api/notebooks/<notebook_id>/promote/<cell_id>/preview",
    methods=["GET"],
)
def notebooks_promote_preview(notebook_id, cell_id):
    """Return the dry-run preview for a ``promote_to_alert_group`` cell.

    Same structured payload the cell-engine emits; serves the SPA's
    "show me what would happen" pane and is consumable by AI agents
    that introspect the notebook before deploy.

    404 if the notebook or cell doesn't exist; 200 with
    ``decision="blocked"`` and an ``errors`` list when the metadata
    is malformed (the SPA surfaces this in the same panel).
    """
    nb = _get_notebook_store_singleton().get_notebook(notebook_id)
    if nb is None:
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
            "error_class": "NotFound",
            "notebook_id": notebook_id,
        }), 404

    cell = next(
        (c for c in (nb.get("cells") or []) if c.get("id") == cell_id),
        None,
    )
    if cell is None:
        return jsonify({
            "status": "error",
            "message": f"Cell {cell_id!r} not found in notebook {notebook_id!r}.",
            "error_class": "UnknownCellId",
            "valid_cell_ids": [c.get("id") for c in (nb.get("cells") or [])],
        }), 404
    if cell.get("type") != "promote_to_alert_group":
        return jsonify({
            "status": "error",
            "message": (
                f"Cell {cell_id!r} is type {cell.get('type')!r}; "
                f"promote_to_alert_group expected."
            ),
            "error_class": "WrongCellType",
            "actual_type": cell.get("type"),
        }), 400

    from notebook_to_alert_group import build_promote_preview
    preview = build_promote_preview(nb, cell_id)
    return jsonify({"status": "success", "preview": preview})


@app.route(
    "/api/notebooks/<notebook_id>/promote/<cell_id>",
    methods=["POST"],
)
def notebooks_promote_deploy(notebook_id, cell_id):
    """Deploy a ``promote_to_alert_group`` cell - actually creates /
    updates the alert group YAML.

    Body fields (all optional):
      * ``overwrite_existing`` (bool, default True) - if False, fail
        when the AG name is already taken. The headliner re-deploy
        loop wants True (overwrite); pass False from the SPA when the
        operator explicitly wants a "fresh AG" intent and an existing
        name should be a hard error.

    Returns ``{status, ag, deploy_record}`` on success; ``400`` with a
    structured error_class for validation failures; ``404`` if the
    notebook or cell doesn't exist.
    """
    data = request.get_json(force=True, silent=True) or {}
    overwrite_existing = bool(data.get("overwrite_existing", True))

    nb = _get_notebook_store_singleton().get_notebook(notebook_id)
    if nb is None:
        return jsonify({
            "status": "error",
            "message": f"Notebook {notebook_id!r} not found.",
            "error_class": "NotFound",
            "notebook_id": notebook_id,
        }), 404

    cell = next(
        (c for c in (nb.get("cells") or []) if c.get("id") == cell_id),
        None,
    )
    if cell is None:
        return jsonify({
            "status": "error",
            "message": f"Cell {cell_id!r} not found in notebook {notebook_id!r}.",
            "error_class": "UnknownCellId",
            "valid_cell_ids": [c.get("id") for c in (nb.get("cells") or [])],
        }), 404
    if cell.get("type") != "promote_to_alert_group":
        return jsonify({
            "status": "error",
            "message": (
                f"Cell {cell_id!r} is type {cell.get('type')!r}; "
                f"promote_to_alert_group expected."
            ),
            "error_class": "WrongCellType",
            "actual_type": cell.get("type"),
        }), 400

    from notebook_to_alert_group import promote_cell_to_ag
    try:
        ag_record = promote_cell_to_ag(
            nb, cell_id, overwrite_existing=overwrite_existing,
        )
    except FileExistsError as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
            "error_class": "AlertGroupExists",
            "remediation": (
                "Pass overwrite_existing=true in the request body to "
                "update the existing AG."
            ),
        }), 409
    except ValueError as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
            "error_class": "ValidationError",
        }), 400
    except Exception as exc:
        logger.exception(
            "[x] /api/notebooks/.../promote: unexpected failure"
        )
        return jsonify({
            "status": "error",
            "message": f"Deploy failed: {exc}",
            "error_class": type(exc).__name__,
        }), 500

    # Re-register the new/updated AG with the live scheduler so the
    # next cron tick picks it up without a server restart. Best-effort:
    # if the scheduler isn't running (e.g. in test harnesses) we still
    # report the deploy as succeeded - the YAML is on disk and any
    # future scheduler start will pick it up via the seed path.
    try:
        from alert_groups.scheduler import register_alert_group_jobs
        engine = _get_engine()
        register_alert_group_jobs(engine._scheduler)
    except Exception as exc:
        logger.warning(
            "[!] /api/notebooks/.../promote: scheduler refresh failed: %s",
            exc,
        )

    return jsonify({
        "status": "success",
        "ag": ag_record,
        "deploy_record": {
            "notebook_id": notebook_id,
            "cell_id": cell_id,
            "ag_name": ag_record.get("name"),
            "overwrite_existing": overwrite_existing,
        },
    })


@app.route(
    "/api/alert-groups/<ag_name>/as-notebook",
    methods=["GET"],
)
def alert_group_as_notebook(ag_name):
    """Return a synthetic notebook record built from an existing AG.

    Round-trip path: AG → notebook (caller can save via POST
    /api/notebooks to spawn an editable copy). Pure read; no side
    effects on the source AG.

    404 if the AG doesn't exist.
    """
    from alert_group_store import AlertGroupStore
    store = AlertGroupStore()
    store.initialize()
    try:
        ag = store.get_group(ag_name)
    except FileNotFoundError:
        return jsonify({
            "status": "error",
            "message": f"Alert group {ag_name!r} not found.",
            "error_class": "NotFound",
            "ag_name": ag_name,
        }), 404
    from notebook_to_alert_group import alert_group_to_notebook
    notebook = alert_group_to_notebook(ag)
    return jsonify({"status": "success", "notebook": notebook})


# ============================================================================
# Curator / speaktube contract endpoints (Phase 6 / Bet 5 slice 1, 2026-05-16)
# ============================================================================
#
# Implements the four-endpoint contract the speaktube player polls against:
#
#   GET  /api/playlist/today - latest composed playlist (404 if empty)
#   GET  /api/dignity/today - % of today's plays that were chosen vs passive
#   POST /api/reflections - write a free-text user reflection
#   POST /api/growth_dial - set the bipolar exploration knob (-1.0..+1.0; slice 8 2026-05-17)
#
# Storage:
#   * curator_playlist / curator_telemetry / curator_reflections all land in
#     ``indexes/IMMUTABLE/<category>/*.parquet`` via the log_writer helpers.
#   * curator_growth_dial is a single scalar in global_settings.yaml.
#
# See docs/lang/21_curator_speaktube.md for the wire contract and rationale.

_CURATOR_VALID_REFLECTION_KINDS = frozenset({"eod", "per_video"})
_CURATOR_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _curator_immutable_glob(subdir: str) -> str | None:
    """Return a DuckDB-friendly glob for ``indexes/IMMUTABLE/<subdir>/`` or
    ``None`` if the directory is missing / empty.

    Used by both GET endpoints to short-circuit the "no composition has
    happened yet" case so we can return 404 / null per spec instead of
    raising on an empty glob.
    """
    try:
        from global_settings import get_settings
        settings = get_settings()
        root = settings.immutable_subdir(subdir)
    except Exception:
        return None
    if not root.exists():
        return None
    # Look for at least one .parquet - DuckDB glob errors on empty matches.
    if not any(root.rglob("*.parquet")):
        return None
    return str(root / "**" / "*.parquet")


def _curator_today_date(date_override: str | None = None) -> str:
    """Return the "today" the server should use for curator endpoints.

    Defaults to server-local date. ``?date=YYYY-MM-DD`` query param wins
    when present; speaktube can override if it ever needs a different
    notion of "today". Single-tenant LAN assumption: player + curator
    share a timezone, so server-local works in practice.
    """
    if date_override and _CURATOR_DATE_RE.match(date_override):
        return date_override
    import datetime as _dt
    return _dt.date.today().isoformat()


@app.route("/api/playlist/today", methods=["GET"])
def api_curator_playlist_today():
    """Return the most-recent composed playlist as the speaktube JSON shape.

    Reads ``indexes/IMMUTABLE/curator_playlist/*.parquet``, finds the
    most-recent ``run_date``, returns all rows for that date sorted by
    ``position``. Per the contract:

    * 404 - no playlist composition has happened yet (or the latest
      run_date is older than today).
    * 200 + non-empty ``items[]`` - happy path.

    Optional query params:
      * ``?date=YYYY-MM-DD`` - return the playlist for a specific date
        instead of "latest". Used by tests and operator inspection.
    """
    try:
        import duckdb
    except ImportError:
        return jsonify({
            "status": "error",
            "message": "duckdb not installed",
        }), 500

    # Validate ``date`` BEFORE checking the IMMUTABLE glob - a bad
    # date param should always be 400, never get masked by a 404 from
    # an empty pre-composition dir.
    date_filter = (request.args.get("date") or "").strip()
    if date_filter and not _CURATOR_DATE_RE.match(date_filter):
        return jsonify({
            "status": "error",
            "message": "date must be YYYY-MM-DD",
        }), 400

    glob = _curator_immutable_glob("curator_playlist")
    if glob is None:
        return jsonify({
            "status": "error",
            "message": "No playlist composition has run yet - check that the curator alert group has fired.",
            "error_class": "NoPlaylistComposed",
        }), 404

    try:
        con = duckdb.connect(database=":memory:")
        try:
            con.execute("PRAGMA threads=1")
            # Filter to the LATEST composition within the target run_date.
            # If the composer fires twice in a day (manual + cron, or two
            # manual fires), each one writes its own rows to the parquet
            # log. Without the composed_at_iso filter, the endpoint
            # returns the union of every composition for the day - which
            # speaktube would render as N×items_per_run picks. Caught
            # 2026-05-17 during the slice 3 production-readiness audit:
            # three back-to-back composer fires produced 13+13+14=40
            # items in the API response when speaktube expected ~14.
            #
            # The per-row ``composed_at_iso`` is the SAME across all rows
            # from one composer fire (set once at dispatch start), so the
            # MAX(composed_at_iso) WHERE run_date = ? filter cleanly
            # picks the latest fire's full row-set.
            # NB: ``union_by_name=true`` is load-bearing across every
            # curator IMMUTABLE read - see
            # _curator_read_parquet_clause() docstring for the two
            # failure modes it prevents (Null-typed empty parquets +
            # additive schema drift). Drift-guarded by
            # tests/test_curator_immutable_read_robustness.py.
            if date_filter:
                latest_iso = con.execute(
                    "SELECT MAX(composed_at_iso) "
                    "FROM read_parquet(?, union_by_name=true, hive_partitioning=0) "
                    "WHERE run_date = ?",
                    [glob, date_filter],
                ).fetchone()
                if not latest_iso or not latest_iso[0]:
                    rows = con.execute(
                        "SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=0) "
                        "WHERE run_date = ? "
                        "ORDER BY position ASC",
                        [glob, date_filter],
                    ).fetchdf()
                else:
                    rows = con.execute(
                        "SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=0) "
                        "WHERE run_date = ? AND composed_at_iso = ? "
                        "ORDER BY position ASC",
                        [glob, date_filter, latest_iso[0]],
                    ).fetchdf()
            else:
                latest_row = con.execute(
                    "SELECT MAX(run_date) AS run_date "
                    "FROM read_parquet(?, union_by_name=true, hive_partitioning=0)",
                    [glob],
                ).fetchone()
                if not latest_row or not latest_row[0]:
                    return jsonify({
                        "status": "error",
                        "message": "No playlist rows found.",
                        "error_class": "NoPlaylistComposed",
                    }), 404
                latest_iso = con.execute(
                    "SELECT MAX(composed_at_iso) "
                    "FROM read_parquet(?, union_by_name=true, hive_partitioning=0) "
                    "WHERE run_date = ?",
                    [glob, latest_row[0]],
                ).fetchone()
                if not latest_iso or not latest_iso[0]:
                    rows = con.execute(
                        "SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=0) "
                        "WHERE run_date = ? "
                        "ORDER BY position ASC",
                        [glob, latest_row[0]],
                    ).fetchdf()
                else:
                    rows = con.execute(
                        "SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=0) "
                        "WHERE run_date = ? AND composed_at_iso = ? "
                        "ORDER BY position ASC",
                        [glob, latest_row[0], latest_iso[0]],
                    ).fetchdf()
        finally:
            con.close()
    except Exception as exc:
        logger.exception("[x] /api/playlist/today: read failed")
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
            "error_class": type(exc).__name__,
        }), 500

    if rows.empty:
        return jsonify({
            "status": "error",
            "message": "No playlist rows found.",
            "error_class": "NoPlaylistComposed",
        }), 404

    def _opt_str(v) -> str:
        # pd.isna handles None, np.nan, and pd.NA uniformly. Scalar
        # check first so we don't pass an array (e.g. when r.get
        # returns a column-typed value) into a scalar context.
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        return str(v) if v is not None else ""

    def _opt_float(v):
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _opt_int(v):
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    first = rows.iloc[0]
    items = []
    for _, r in rows.iterrows():
        items.append({
            "position": _opt_int(r.get("position")) or 0,
            "slot_kind": _opt_str(r.get("slot_kind")) or "main",
            "rationale": _opt_str(r.get("rationale")),
            "video": {
                "external_id": _opt_str(r.get("external_id")),
                "url": _opt_str(r.get("url")),
                "title": _opt_str(r.get("title")),
                "channel_name": _opt_str(r.get("channel_name")),
                # Slice 4 (2026-05-17): empty string is the load-bearing
                # signal to the speaktube player that we don't have one -
                # the player falls back to YouTube synthesis for
                # thumbnails and to curator-order sort for missing
                # dates. Never elide the keys entirely; the renderer
                # checks ``video.thumbnail_url`` truthiness, not
                # presence.
                "thumbnail_url": _opt_str(r.get("thumbnail_url")),
                "published_at": _opt_str(r.get("published_at")),
                "duration_seconds": _opt_int(r.get("duration_seconds")),
                "interest_score": _opt_float(r.get("interest_score")),
                "growth_score": _opt_float(r.get("growth_score")),
                "slop_score": _opt_float(r.get("slop_score")),
                "score_reasoning": _opt_str(r.get("score_reasoning")),
            },
        })

    growth_at_compose = _opt_float(first.get("growth_dial"))
    if growth_at_compose is None:
        growth_at_compose = -0.7

    # Slice 10 (2026-05-17, speaktube req #12): surface the bipolar
    # operator-set dial AND the thin-history state alongside the
    # effective dial that was actually used for this composition.
    # Speaktube can render a "thin-history mode" badge when active -
    # the dial the user sees vs the dial that composed for them
    # naturally diverge when the boost fires.
    growth_dial_stored = None
    try:
        from global_settings import get_settings
        stored_raw = get_settings().get("curator_growth_dial")
        if stored_raw is not None:
            growth_dial_stored = float(stored_raw)
    except Exception:
        growth_dial_stored = None

    def _opt_bool(v) -> bool:
        # pandas/numpy can return numpy.bool_ which is NOT a Python
        # bool subclass - must coerce via try/except path that works
        # for both. pd.isna() short-circuits NaN/None/pd.NA.
        try:
            if pd.isna(v):
                return False
        except (TypeError, ValueError):
            pass
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        # bool / numpy.bool_ / int / float / numpy.int*: bool() works
        # uniformly. Anything else falls through to False.
        try:
            return bool(v)
        except Exception:
            return False

    return jsonify({
        "run_date": _opt_str(first.get("run_date")),
        "growth_dial": growth_at_compose,
        "growth_dial_stored": growth_dial_stored,
        "thin_history_active": _opt_bool(first.get("thin_history_active")),
        "theme": _opt_str(first.get("theme")),
        "items": items,
    })


@app.route("/api/dignity/today", methods=["GET"])
def api_curator_dignity_today():
    """Return today's algorithmic-dignity score.

    Computed as the share of today's playback events whose ``chosen_by``
    indicates an intentional pick (``curator`` / ``user_manual`` /
    ``playlist``) vs passive recommendation (``recommendation`` or empty).

    Always returns 200 per the contract - when no plays have happened
    yet, ``dignity_pct`` is ``null`` and counts are zero (speaktube
    renders both as "offline" in the footer).

    Optional query params:
      * ``?date=YYYY-MM-DD`` - override the date (default: server-local today).
    """
    try:
        import duckdb
    except ImportError:
        return jsonify({
            "status": "error",
            "message": "duckdb not installed",
        }), 500

    date_param = _curator_today_date(request.args.get("date"))
    glob = _curator_immutable_glob("curator_telemetry")
    if glob is None:
        return jsonify({
            "dignity_pct": None,
            "total_plays": 0,
            "chosen_plays": 0,
        })

    try:
        con = duckdb.connect(database=":memory:")
        try:
            con.execute("PRAGMA threads=1")
            # union_by_name=true tolerates curator_telemetry schema
            # heterogeneity (the ingestion script writes a parquet
            # PER FIRE even when zero events were fetched in that 6h
            # window - pyarrow infers all-None string columns as the
            # Null logical type, and without union_by_name the
            # WHERE/IN clauses below fail with
            # "Conversion Error: VARCHAR -> NULL". Drift-guarded by
            # tests/test_curator_immutable_read_robustness.py).
            row = con.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE event_type IN ('play_start', 'play_end')
                    ) AS total_plays,
                    COUNT(*) FILTER (
                        WHERE event_type IN ('play_start', 'play_end')
                          AND chosen_by IN ('curator', 'user_manual', 'playlist')
                    ) AS chosen_plays
                FROM read_parquet(?, union_by_name=true, hive_partitioning=0)
                WHERE event_date = ?
                """,
                [glob, date_param],
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:
        logger.exception("[x] /api/dignity/today: read failed")
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
            "error_class": type(exc).__name__,
        }), 500

    total = int(row[0]) if row and row[0] is not None else 0
    chosen = int(row[1]) if row and row[1] is not None else 0
    if total <= 0:
        return jsonify({
            "dignity_pct": None,
            "total_plays": 0,
            "chosen_plays": 0,
        })
    return jsonify({
        "dignity_pct": round(chosen / total * 100.0, 1),
        "total_plays": total,
        "chosen_plays": chosen,
    })


@app.route("/api/reflections", methods=["POST"])
def api_curator_reflections_create():
    """Record a free-text user reflection.

    Request body (JSON):
      ``{"date": "YYYY-MM-DD", "kind": "eod" | "per_video", "content": "..."}``

    Optional ``video_external_id`` when ``kind="per_video"``. Writes one
    row to ``indexes/IMMUTABLE/curator_reflections/*.parquet`` and
    returns 201 with the synthesized id (write epoch) per the contract.

    The reflection_submit telemetry event also flows through the
    log_curator_reflection helper (with ``source="telemetry_event"``),
    so the dataset captures both pathways uniformly.
    """
    body = request.get_json(silent=True) or {}
    date_val = (body.get("date") or "").strip()
    kind_val = (body.get("kind") or "").strip()
    content = body.get("content")
    video_external_id = (body.get("video_external_id") or "").strip()

    if not date_val or not _CURATOR_DATE_RE.match(date_val):
        return jsonify({
            "status": "error",
            "message": "date must be YYYY-MM-DD",
        }), 400
    if kind_val not in _CURATOR_VALID_REFLECTION_KINDS:
        return jsonify({
            "status": "error",
            "message": f"kind must be one of {sorted(_CURATOR_VALID_REFLECTION_KINDS)}, got {kind_val!r}",
        }), 400
    if not isinstance(content, str) or not content.strip():
        return jsonify({
            "status": "error",
            "message": "content must be a non-empty string",
        }), 400
    if kind_val == "per_video" and not video_external_id:
        return jsonify({
            "status": "error",
            "message": "video_external_id is required when kind=per_video",
        }), 400

    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    write_epoch = int(time.time())

    try:
        from functionality.log_writer import log_curator_reflection, flush_all
        log_curator_reflection(
            event_ts_iso=now_iso,
            date=date_val,
            kind=kind_val,
            content=content,
            video_external_id=video_external_id,
            source="api_post",
        )
        flush_all()
    except Exception as exc:
        logger.exception("[x] /api/reflections: write failed")
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
            "error_class": type(exc).__name__,
        }), 500

    return jsonify({
        "status": "success",
        "id": write_epoch,
        "date": date_val,
        "kind": kind_val,
    }), 201


@app.route("/api/growth_dial", methods=["POST"])
def api_curator_growth_dial_set():
    """Persist a new value for the exploration knob.

    Request body (JSON):
      ``{"value": -0.4, "set_at": "2026-05-16T09:14:22-07:00"}``

    Stores ``value`` in ``global_settings.yaml`` under
    ``curator_growth_dial``. The next playlist composition reads the
    updated value at run time. ``set_at`` is accepted but only logged -
    the persisted shape is "current value", not a history.

    Validates -1.0 <= value <= 1.0 (BIPOLAR per slice 8, 2026-05-17 -
    was 0.0..1.0 through slice 7). -1.0 = max familiarity; 0.0 =
    balanced; +1.0 = max exploration. Returns 200 on success, 400 on
    bad input.
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("value")

    if isinstance(raw, bool):
        return jsonify({
            "status": "error",
            "message": "value must be a number",
        }), 400
    if not isinstance(raw, (int, float)):
        return jsonify({
            "status": "error",
            "message": "value must be a number",
        }), 400
    if raw < -1.0 or raw > 1.0:
        return jsonify({
            "status": "error",
            "message": f"value must be in [-1.0, +1.0], got {raw}",
        }), 400

    new_value = float(raw)
    try:
        from global_settings import get_settings
        settings = get_settings()
        old_value = float(settings.get("curator_growth_dial") or 0.0)
        settings.set("curator_growth_dial", new_value)
        try:
            from functionality.log_writer import log_config_change
            log_config_change(
                action="update",
                subject="curator_growth_dial",
                subject_type="setting",
                old_value=str(old_value),
                new_value=str(new_value),
                actor="api",
                source="/api/growth_dial",
            )
        except Exception as exc:
            # Never let an audit-log failure swallow a successful write.
            # CLAUDE.md rule: surface the failure but don't bury it.
            logger.warning(
                "[!] /api/growth_dial: failed to log config change: %s", exc,
            )
    except ValueError as exc:
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
        }), 400
    except Exception as exc:
        logger.exception("[x] /api/growth_dial: persistence failed")
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
            "error_class": type(exc).__name__,
        }), 500

    return jsonify({
        "status": "success",
        "curator_growth_dial": new_value,
        "set_at": body.get("set_at") or "",
    })


# ── Phase 6 / Bet 5 slice 11 (2026-05-17) - speaktube req #10:
# /api/preferences/keywords. Speaktube's Discover view POSTs operator-
# supplied keywords to seed the next composer fire. Storage is
# IMMUTABLE (forever); the dispatcher's keyword-boost hook reads the
# "active pool" at AG-fire time and boosts interest_score on
# title-matching candidates. See docs/lang/21_curator_speaktube.md.

@app.route("/api/preferences/keywords", methods=["POST"])
def api_curator_keyword_prefs_post():
    """Accept a batch of keywords to seed the next composer fire.

    Request body::

        { "keywords": ["rare earth magnets", "public-domain noir"] }

    Each keyword writes one row to
    ``indexes/IMMUTABLE/curator_keyword_prefs/*.parquet`` with
    ``source="api_post"``. Case-insensitive dedup against the active
    pool: a keyword that's already in the pool (case-insensitively) is
    silently skipped (not an error). Empty / non-string entries are
    skipped. Whitespace is trimmed.

    Returns 200 with ``{status, added, skipped, pool_size}`` so
    speaktube can render a "tomorrow's pool: N keywords" badge.
    Validates the body shape; returns 400 on bad input.
    """
    body = request.get_json(silent=True) or {}
    keywords_raw = body.get("keywords")

    if not isinstance(keywords_raw, list):
        return jsonify({
            "status": "error",
            "message": "keywords must be a list of strings",
        }), 400

    # Clean + dedup the input batch (within-request CI dedup) so a
    # caller can POST [\"A\", \"a\"] without writing two rows.
    cleaned_pairs: list[tuple[str, str]] = []  # (display_form, lowered)
    seen_lower: set[str] = set()
    for k in keywords_raw:
        if not isinstance(k, str):
            continue
        kw = k.strip()
        if not kw:
            continue
        lo = kw.lower()
        if lo in seen_lower:
            continue
        seen_lower.add(lo)
        cleaned_pairs.append((kw, lo))

    if not cleaned_pairs:
        return jsonify({
            "status": "error",
            "message": "keywords must contain at least one non-empty string",
        }), 400

    # Read the active pool to skip CI-duplicates of EXISTING entries.
    try:
        from global_settings import get_settings
        fallback = int(get_settings().get("curator_keyword_pool_fallback_seconds") or 86400)
    except Exception:
        fallback = 86400
    from functionality.log_writer import read_active_curator_keyword_pool
    existing = {k.lower() for k in read_active_curator_keyword_pool(fallback_seconds=fallback)}

    import datetime as _dt
    import json as _json
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    raw_request_str = _json.dumps(body, default=str)

    added = 0
    skipped = 0
    try:
        from functionality.log_writer import log_curator_keyword_pref, flush_all
        for kw, lo in cleaned_pairs:
            if lo in existing:
                skipped += 1
                continue
            log_curator_keyword_pref(
                event_ts_iso=now_iso,
                keyword=kw,
                source="api_post",
                raw_request=raw_request_str,
            )
            existing.add(lo)
            added += 1
        flush_all()
    except Exception as exc:
        logger.exception("[x] /api/preferences/keywords: write failed")
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
            "error_class": type(exc).__name__,
        }), 500

    pool = read_active_curator_keyword_pool(fallback_seconds=fallback)
    return jsonify({
        "status": "success",
        "added": added,
        "skipped": skipped,
        "pool_size": len(pool),
    })


@app.route("/api/preferences/keywords", methods=["GET"])
def api_curator_keyword_prefs_get():
    """Return the currently-active keyword pool.

    Active = keywords POSTed since the most-recent ``curator_playlist``
    composition, OR (fallback) the trailing ``curator_keyword_pool_fallback_seconds``
    window when no composition exists yet. Always 200; returns
    ``{keywords: []}`` when the pool is empty.

    Speaktube can render this as a "tomorrow's pool" preview list on
    the Discover view.
    """
    from functionality.log_writer import read_active_curator_keyword_pool
    try:
        from global_settings import get_settings
        fallback = int(get_settings().get("curator_keyword_pool_fallback_seconds") or 86400)
    except Exception:
        fallback = 86400
    pool = read_active_curator_keyword_pool(fallback_seconds=fallback)
    return jsonify({"keywords": pool})


# ── Phase 6 / Bet 5 slice 12 (2026-05-17) - speaktube req #11:
# /api/search?q=<query>&sources=<...>&limit=<N>. Speaktube's Discover
# view fetches this for ad-hoc cross-source search. Returns the same
# JSON shape as /api/playlist/today so the renderer reuses one code
# path. Searches the ALREADY-INGESTED candidate pool (no real-time
# yt-dlp shell-out per request). See docs/lang/21_curator_speaktube.md.

_CURATOR_SEARCH_DEFAULT_LIMIT = 100
_CURATOR_SEARCH_MAX_LIMIT = 1000
# Same slop regex as the composer's scored-candidates feeder
_CURATOR_SLOP_RE = (
    r"(?i)won.?t believe|shocking|insane|gone wrong|you.?ll never"
)


@app.route("/api/search", methods=["GET"])
def api_curator_search():
    """Ad-hoc keyword search across the already-ingested candidate pool.

    Query params:
      * ``q`` (required, urlencoded) - whitespace-separated tokens.
        At least ONE token must match the candidate's title
        (case-insensitive substring; tokens OR'd). Empty or missing
        ``q`` → 400.
      * ``sources`` (optional, comma-separated) - restrict to specific
        ``source`` enum values (e.g. ``youtube_rss,archive_org``).
        Default: all sources.
      * ``limit`` (optional) - soft cap on returned items. Default 100;
        max 1000.

    Returns the same JSON shape as ``/api/playlist/today`` so the
    speaktube renderer reuses the playlist code path:

        {
          "run_date": "<today>",
          "growth_dial": <current operator setting>,
          "growth_dial_stored": <same>,
          "thin_history_active": false,    // not meaningful for search
          "theme": "",
          "items": [
            {
              "position": 1,
              "slot_kind": "main",
              "rationale": "",
              "video": {
                "external_id": "...",
                "url": "...",
                ...
              }
            }, ...
          ]
        }

    Apply the same slop-score heuristic as the composer feeder so
    speaktube can show a slop badge consistently across sources.
    """
    import re as _re
    import datetime as _dt

    q_raw = (request.args.get("q") or "").strip()
    if not q_raw:
        return jsonify({
            "status": "error",
            "message": "q is required (non-empty)",
            "error_class": "MissingQuery",
        }), 400

    sources_param = (request.args.get("sources") or "").strip()
    requested_sources: list[str] = []
    if sources_param:
        requested_sources = [
            s.strip() for s in sources_param.split(",") if s.strip()
        ]

    try:
        limit_raw = int(request.args.get("limit") or _CURATOR_SEARCH_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit_raw = _CURATOR_SEARCH_DEFAULT_LIMIT
    limit = max(1, min(_CURATOR_SEARCH_MAX_LIMIT, limit_raw))

    # Tokenise q on whitespace. Each token must be regex-escaped before
    # joining into an alternation so user input like "C++" or "1+1"
    # doesn't crash the regex compile.
    tokens = [t for t in q_raw.split() if t.strip()]
    if not tokens:
        return jsonify({
            "status": "error",
            "message": "q must contain at least one non-whitespace token",
            "error_class": "MissingQuery",
        }), 400
    try:
        title_pattern = "|".join(_re.escape(t) for t in tokens)
    except Exception:
        return jsonify({
            "status": "error",
            "message": "q produced an invalid search pattern",
            "error_class": "BadQuery",
        }), 400

    # Query the candidate pool via DuckDB.
    glob = _curator_immutable_glob("curator_candidates")
    if not glob:
        # No ingestion has happened yet - return an empty result with
        # the same shape so the renderer doesn't need a separate
        # "no data" path.
        return jsonify(_curator_search_empty_response())

    try:
        import duckdb
        import re as _re_for_sql
        # Escape single quotes for the SQL string literal.
        sql_pattern = title_pattern.replace("'", "''")

        sources_clause = ""
        if requested_sources:
            quoted_sources = ", ".join(
                "'" + s.replace("'", "''") + "'" for s in requested_sources
            )
            sources_clause = f" AND source IN ({quoted_sources})"

        # NB: we LOWER both sides for the substring match - DuckDB's
        # regexp_matches accepts (?i) but using LOWER() keeps the
        # behavior identical to the composer feeder's pattern.
        #
        # The ``UNION ALL BY NAME ... WHERE 1=0`` pattern synthesizes a
        # zero-row stub row that declares ``thumbnail_url AS VARCHAR``,
        # which forces DuckDB's schema unifier to include the column in
        # the result schema even when NO file in the glob has it
        # (single-file scenario or pre-slice-4 deploys). Without the
        # stub, ``union_by_name=true`` is a no-op when every file
        # agrees on a schema that lacks ``thumbnail_url`` - the
        # ``COALESCE(thumbnail_url, ...)`` then fails to bind on a
        # column that genuinely doesn't exist, falls back to looking at
        # the SELECT alias, and emits the misleading
        # ``cannot be referenced before it is defined`` binder error.
        # 2026-05-18: caught when prod had only pre-slice-4
        # curator_candidates parquets and ``union_by_name=true`` alone
        # wasn't enough. Drift-guarded by
        # tests/test_curator_immutable_read_robustness.py.
        sql = (
            f"SELECT "
            f"  video_external_id, video_url, title, channel_name, "
            f"  channel_id, channel_url, published_iso, "
            f"  COALESCE(thumbnail_url, '') AS thumbnail_url, "
            f"  duration_seconds, source, _epoch "
            f"FROM ("
            f"  SELECT * FROM read_parquet('{glob}', union_by_name=true) "
            f"  UNION ALL BY NAME "
            f"  SELECT CAST(NULL AS VARCHAR) AS thumbnail_url WHERE 1=0"
            f") "
            f"WHERE title IS NOT NULL "
            f"  AND title != '' "
            f"  AND regexp_matches(LOWER(title), LOWER('{sql_pattern}'))"
            f"  AND source != 'youtube_rss_info' "
            f"  AND source != 'archive_org_info' "
            f"  AND source != 'topic_search_info' "
            f"{sources_clause} "
            f"ORDER BY _epoch DESC "
            f"LIMIT {limit}"
        )
        # Use a per-request connection (matches /api/dignity/today's
        # pattern). The module-level ``duckdb.sql()`` shares a global
        # default connection that is NOT thread-safe - Flask serves
        # multiple endpoints concurrently, and another in-flight
        # endpoint that also calls ``duckdb.sql()`` (e.g. the keyword-
        # pool reader hit by GET /api/preferences/keywords) can put
        # the global connection in a bad state, producing
        # ``InvalidInputException: Attempting to execute an
        # unsuccessful or closed pending query result`` on this
        # endpoint's next .df() call. Caught 2026-05-18 when the user's
        # Discover view fired keyword-prefs save + search in quick
        # succession. Drift-guarded by
        # tests/test_curator_immutable_read_robustness.py.
        con = duckdb.connect(database=":memory:")
        try:
            con.execute("PRAGMA threads=1")
            rows = con.execute(sql).fetchdf()
        finally:
            con.close()
    except Exception as exc:
        logger.exception("[x] /api/search: DuckDB query failed")
        return jsonify({
            "status": "error",
            "message": _safe_error_message(exc),
            "error_class": type(exc).__name__,
        }), 500

    if rows is None or len(rows.index) == 0:
        return jsonify(_curator_search_empty_response(query=q_raw))

    # Compute slop_score in Python (same regex as feeder) so the value
    # matches what the composer would use.
    slop_re = _re.compile(_CURATOR_SLOP_RE)

    def _slop(title: str) -> float:
        return 0.8 if title and slop_re.search(title) else 0.1

    items: list[dict] = []
    for idx, (_, r) in enumerate(rows.iterrows(), start=1):
        title = _curator_opt_str(r.get("title"))
        eid = _curator_opt_str(r.get("video_external_id"))
        video_url = _curator_opt_str(r.get("video_url"))
        # Fall back to constructing a yt-dlp-resolvable URL from
        # the external_id when video_url is empty (older parquets).
        if not video_url and eid:
            # Source-aware fallback: YouTube uses watch?v=, archive.org
            # uses details/.
            src = _curator_opt_str(r.get("source"))
            if src == "archive_org":
                video_url = f"https://archive.org/details/{eid}"
            else:
                video_url = f"https://www.youtube.com/watch?v={eid}"

        items.append({
            "position": idx,
            "slot_kind": "main",
            "rationale": "",
            "video": {
                "external_id": eid,
                "url": video_url,
                "title": title,
                "channel_name": _curator_opt_str(r.get("channel_name")),
                "thumbnail_url": _curator_opt_str(r.get("thumbnail_url")),
                "published_at": _curator_opt_str(r.get("published_iso")),
                "duration_seconds": _curator_opt_int(r.get("duration_seconds")),
                # Interest = 1.0 (the user explicitly asked for this
                # via search), growth = null (not meaningful here),
                # slop = computed.
                "interest_score": 1.0,
                "growth_score": None,
                "slop_score": _slop(title),
                "score_reasoning": f"Matched search: {q_raw[:200]}",
            },
        })

    # Read current dial + thin-history state from settings so the
    # response shape matches /api/playlist/today.
    growth_stored = -0.7
    try:
        stored_raw = get_settings().get("curator_growth_dial")
        if stored_raw is not None:
            growth_stored = float(stored_raw)
    except Exception:
        growth_stored = -0.7

    today = _curator_today_date()
    return jsonify({
        "run_date": today,
        "growth_dial": growth_stored,
        "growth_dial_stored": growth_stored,
        "thin_history_active": False,  # not meaningful for ad-hoc search
        "theme": "",
        "items": items,
    })


def _curator_search_empty_response(*, query: str = "") -> dict:
    """Empty search result with the same shape as a populated one -
    saves the renderer from special-casing 'no data'."""
    growth_stored = -0.7
    try:
        from global_settings import get_settings
        stored_raw = get_settings().get("curator_growth_dial")
        if stored_raw is not None:
            growth_stored = float(stored_raw)
    except Exception:
        growth_stored = -0.7
    return {
        "run_date": _curator_today_date(),
        "growth_dial": growth_stored,
        "growth_dial_stored": growth_stored,
        "thin_history_active": False,
        "theme": "",
        "items": [],
    }


@app.route("/api/persistence/audit", methods=["GET"])
def api_persistence_audit():
    """Surface the persistence inventory for the UI banner.

    Returns ``status:success`` plus per-target dicts (path, kind, exists,
    size) so the SPA can render a "missing user-data target" warning if
    a Docker bind-mount is mis-configured. Cheap (no hashing, just stat).
    """
    rows = _persistence_target_inventory()
    issues = [r for r in rows if not r["exists"]]
    return jsonify({
        "status": "success",
        "total": len(rows),
        "healthy": len(rows) - len(issues),
        "issues": issues,
        "targets": rows,
    })


if __name__ == "__main__":
    # Start the scheduled input engine (ingestion tasks - scheduled_inputs.db)
    from scheduled_input_engine import start_engine
    engine = start_engine()

    # Start the saved-search scheduler (execute_query AsyncIOScheduler on a
    # daemon thread) AND register alert-group cron jobs on the ingestion
    # engine's BackgroundScheduler.
    #
    # Previously this wiring only happened in bare-metal ``run_all.sh``
    # (which starts ``query_engine/QueryEngine.py`` as its own process).
    # Docker only runs ``desktop_app/server.py``, so alert groups + saved
    # searches silently never auto-fired on any Docker deployment. Users
    # who clicked **Run** manually bypassed this, but anyone relying on
    # the configured cron schedule was seeing no emails, no log rows, and
    # no clue why. Now wired into the Flask entrypoint so Docker
    # behaviour matches bare-metal.
    try:
        from query_engine.QueryEngine import start_background_scheduling
        start_background_scheduling(engine._scheduler)
    except Exception as exc:
        logger.warning("[!] Could not start background scheduling: %s", exc)

    _log_startup_diagnostics()
    _log_persistence_audit()

    # Default to localhost so a freshly-cloned dev install never accidentally
    # exposes the API on the network.  Docker (and intentional remote-access
    # use) can opt in via HOST=0.0.0.0 in the environment.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5111))
    logger.info("[i] SpeakesQuery server starting on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
