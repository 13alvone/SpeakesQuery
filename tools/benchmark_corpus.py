"""Reproducible synthetic-corpus benchmark harness (weakness audit W1, 2026-07-12).

Anyone must be able to reproduce SpeakesQuery's published performance
numbers with one command. This tool has three phases:

    # 1) Generate a synthetic corpus of gzip parquet app-log files
    python -m tools.benchmark_corpus --generate --size-gb 1

    # 2) Time a fixed set of representative SPQL pipelines end-to-end
    #    through the REAL engine (3 runs each, median reported)
    python -m tools.benchmark_corpus --run [--json bench_results.json]

    # 3) Remove the generated corpus
    python -m tools.benchmark_corpus --cleanup

Phases compose: ``--generate --size-gb 1 --run --cleanup`` does all three.

Design notes:

* The corpus is deterministic: a fixed 90-day epoch window plus a
  per-file seed (``base_seed + file_index``) means two machines running
  the same command produce row-identical data (parquet byte layout may
  differ across pyarrow versions; row content does not).
* Files are written in ~64 MB uncompressed chunks (estimated at
  ``APPROX_ROW_BYTES`` serialized bytes per row) to mirror real
  ingestion: many files per index directory, so the benchmark exercises
  DuckDB's multi-file glob path, not a single-file fast path.
* ``--run`` goes through ``process_query_with_diagnostics`` - the exact
  code path the app's /api/query endpoint and the alert-group feeder
  loop use. No shortcuts around the ANTLR parse or the handler chain.
* ``--cleanup`` refuses to delete a directory that does not carry the
  ``.benchmark_corpus_manifest.json`` marker written at generate time,
  so a typo'd ``--dest`` can never delete user data.

Stdlib + project deps only (pandas / numpy / pyarrow / duckdb). psutil
is NOT a project dependency, so machine context comes from /proc on
Linux and degrades to "unknown" elsewhere.
"""

import argparse
import json
import math
import os
import platform
import shutil
import statistics
import sys
import time

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from functionality.atomic_write import write_text_atomic
from tools.generate_sample_data import (
    ERROR_TEMPLATES,
    HOSTS,
    INFO_TEMPLATES,
    LEVELS,
    LEVEL_WEIGHTS,
    PATHS,
    SERVICES,
    SERVICE_WEIGHTS,
    STATUS_CODES,
    USERS,
    WARN_TEMPLATES,
)

MANIFEST_NAME = ".benchmark_corpus_manifest.json"
GENERATOR_ID = "speakesquery.tools.benchmark_corpus"
MANIFEST_VERSION = 1

DEFAULT_DEST = os.path.join(PROJECT_ROOT, "benchmark_corpus")
BASE_SEED = 20260712

# Fixed 90-day window (2026-06-01 00:00:00 UTC .. 2026-08-29 23:59:59 UTC)
# so the corpus is deterministic regardless of when it is generated and
# the published time-bound epochs stay stable.
START_EPOCH = 1780272000
WINDOW_SECONDS = 90 * 86400
END_EPOCH = START_EPOCH + WINDOW_SECONDS - 1

# ~64 MB uncompressed per file at an estimated ~200 serialized bytes/row.
TARGET_FILE_BYTES = 64 * 2 ** 20
APPROX_ROW_BYTES = 200
DEFAULT_ROWS_PER_FILE = TARGET_FILE_BYTES // APPROX_ROW_BYTES

EXPECTED_COLUMNS = [
    "_epoch", "timestamp", "level", "service", "host", "path",
    "status_code", "response_ms", "client_ip", "message", "bytes_sent",
]

ERROR_STATUS_POOL = [429, 500, 500, 502, 404]
WARN_STATUS_POOL = [200, 200, 301, 400, 429]


class BenchmarkCorpusError(Exception):
    """Raised for refuse-to-proceed conditions (guardrails, bad args)."""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_file_dataframe(rows: int, seed: int) -> pd.DataFrame:
    """Build one deterministic file's worth of synthetic app-log rows.

    Same column shape and vocabulary as tools/generate_sample_data.py,
    vectorized with numpy so multi-GB corpora generate in minutes.
    """
    rng = np.random.default_rng(seed)

    epochs = np.sort(
        rng.integers(START_EPOCH, END_EPOCH + 1, size=rows, dtype=np.int64)
    )
    level_p = np.asarray(LEVEL_WEIGHTS, dtype=float)
    level_p /= level_p.sum()
    levels = rng.choice(LEVELS, size=rows, p=level_p)
    service_p = np.asarray(SERVICE_WEIGHTS, dtype=float)
    service_p /= service_p.sum()
    services = rng.choice(SERVICES, size=rows, p=service_p)
    hosts = rng.choice(HOSTS, size=rows)
    users = rng.choice(USERS, size=rows)
    paths = rng.choice(PATHS, size=rows)
    response_ms = (np.exp(rng.normal(4.6, 0.9, size=rows)) + 5).astype(np.int64)
    bytes_sent = rng.integers(180, 250_001, size=rows, dtype=np.int64)
    ip_c = rng.integers(0, 5, size=rows)
    ip_d = rng.integers(2, 255, size=rows)
    job_num = rng.integers(1000, 10000, size=rows)
    rows_written = rng.integers(1, 5001, size=rows)
    template_pick = rng.integers(0, 2 ** 31, size=rows)
    status_pick = rng.integers(0, 2 ** 31, size=rows)

    statuses = np.empty(rows, dtype=np.int64)
    messages = []
    for i in range(rows):
        level = levels[i]
        if level in ("ERROR", "CRITICAL"):
            status_pool, template_pool = ERROR_STATUS_POOL, ERROR_TEMPLATES
        elif level == "WARNING":
            status_pool, template_pool = WARN_STATUS_POOL, WARN_TEMPLATES
        else:
            status_pool, template_pool = STATUS_CODES, INFO_TEMPLATES
        status = status_pool[status_pick[i] % len(status_pool)]
        statuses[i] = status
        template = template_pool[template_pick[i] % len(template_pool)]
        messages.append(template.format(
            user=users[i],
            ip=f"192.168.{ip_c[i]}.{ip_d[i]}",
            path=paths[i],
            ms=response_ms[i],
            job=f"job-{job_num[i]}",
            rows=rows_written[i],
            status=status,
            host=hosts[i],
        ))

    timestamps = pd.to_datetime(epochs, unit="s", utc=True).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    client_ips = [f"192.168.{c}.{d}" for c, d in zip(ip_c, ip_d)]

    df = pd.DataFrame({
        "_epoch": epochs,
        "timestamp": timestamps,
        "level": levels,
        "service": services,
        "host": hosts,
        "path": paths,
        "status_code": statuses,
        "response_ms": response_ms,
        "client_ip": client_ips,
        "message": messages,
        "bytes_sent": bytes_sent,
    })
    df["_epoch"] = df["_epoch"].astype("int64")
    return df[EXPECTED_COLUMNS]


def _load_manifest(dest: str) -> dict:
    """Return the parsed manifest dict, or raise BenchmarkCorpusError."""
    manifest_path = os.path.join(dest, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise BenchmarkCorpusError(
            f"No {MANIFEST_NAME} marker in {dest} - not a generated corpus."
        )
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise BenchmarkCorpusError(
            f"Unreadable manifest at {manifest_path}: {exc}"
        )
    if manifest.get("generator") != GENERATOR_ID:
        raise BenchmarkCorpusError(
            f"Manifest at {manifest_path} was not written by this tool "
            f"(generator={manifest.get('generator')!r})."
        )
    return manifest


def generate_corpus(dest: str, size_gb: float = None, files: int = None,
                    rows_per_file: int = None, seed: int = BASE_SEED) -> dict:
    """Generate the synthetic corpus and write the manifest. Returns manifest."""
    dest = os.path.abspath(dest)
    if rows_per_file is None:
        rows_per_file = DEFAULT_ROWS_PER_FILE
    if rows_per_file < 1:
        raise BenchmarkCorpusError("--rows-per-file must be >= 1")
    if files is None:
        if size_gb is None or size_gb <= 0:
            raise BenchmarkCorpusError(
                "--generate needs --size-gb N (or explicit --files/--rows-per-file)."
            )
        files = max(1, math.ceil(
            (size_gb * 2 ** 30) / (rows_per_file * APPROX_ROW_BYTES)
        ))
    if files < 1:
        raise BenchmarkCorpusError("--files must be >= 1")

    # Refuse to clobber a non-empty directory we did not generate.
    if os.path.isdir(dest) and os.listdir(dest):
        _load_manifest(dest)  # raises if it is not ours
        print(f"[!] Regenerating over existing corpus at {dest}")
        for name in os.listdir(dest):
            if name.endswith(".parquet") or name == MANIFEST_NAME:
                os.remove(os.path.join(dest, name))

    os.makedirs(dest, exist_ok=True)
    print(
        f"[i] Generating {files} file(s) x {rows_per_file} rows "
        f"(~{files * rows_per_file * APPROX_ROW_BYTES / 2 ** 30:.2f} GB "
        f"uncompressed estimate) into {dest}"
    )

    file_entries = []
    t0 = time.perf_counter()
    for i in range(files):
        file_seed = seed + i
        df = build_file_dataframe(rows_per_file, file_seed)
        name = f"{START_EPOCH}_bench_{i:05d}.parquet"
        final_path = os.path.join(dest, name)
        tmp_path = final_path + ".tmp"
        df.to_parquet(tmp_path, compression="gzip", index=False)
        os.replace(tmp_path, final_path)
        file_entries.append({
            "name": name,
            "rows": int(len(df)),
            "seed": file_seed,
            "bytes_on_disk": os.path.getsize(final_path),
        })
        print(f"[i] [{i + 1}/{files}] wrote {name} ({len(df)} rows)")

    manifest = {
        "generator": GENERATOR_ID,
        "manifest_version": MANIFEST_VERSION,
        "created_epoch": int(time.time()),
        "base_seed": seed,
        "size_gb_requested": size_gb,
        "rows_per_file": rows_per_file,
        "file_count": files,
        "rows_total": files * rows_per_file,
        "start_epoch": START_EPOCH,
        "end_epoch": END_EPOCH,
        "columns": EXPECTED_COLUMNS,
        "files": file_entries,
    }
    write_text_atomic(
        os.path.join(dest, MANIFEST_NAME),
        json.dumps(manifest, indent=2) + "\n",
    )
    elapsed = time.perf_counter() - t0
    print(
        f"[i] Corpus complete: {files} file(s), {files * rows_per_file} rows, "
        f"{elapsed:.1f}s"
    )
    return manifest


# ---------------------------------------------------------------------------
# Machine / corpus context
# ---------------------------------------------------------------------------

def _read_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def _read_mem_total_gb():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:  # psutil is NOT a project dep; use it only if the host has it
        import psutil
        return round(psutil.virtual_memory().total / 2 ** 30, 1)
    except Exception:
        return None


def collect_machine_context() -> dict:
    mem_gb = _read_mem_total_gb()
    return {
        "cpu_model": _read_cpu_model(),
        "cpu_count": os.cpu_count(),
        "ram_total_gb": mem_gb if mem_gb is not None else "unknown",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


def collect_corpus_context(dest: str) -> dict:
    """du-style size on disk, file count, row count, epoch range."""
    import duckdb

    parquet_files = sorted(
        os.path.join(dest, f) for f in os.listdir(dest) if f.endswith(".parquet")
    )
    if not parquet_files:
        raise BenchmarkCorpusError(f"No .parquet files found in {dest}")
    size_bytes = sum(os.path.getsize(f) for f in parquet_files)
    size_bytes += sum(
        os.path.getsize(os.path.join(dest, f))
        for f in os.listdir(dest)
        if not f.endswith(".parquet")
        and os.path.isfile(os.path.join(dest, f))
    )

    # Per-call connection (never the module-level duckdb.sql helper - see
    # CLAUDE.md thread-safety rule) with union_by_name for glob reads.
    con = duckdb.connect(database=":memory:")
    try:
        row_count, epoch_min, epoch_max = con.execute(
            "SELECT count(*), min(_epoch), max(_epoch) "
            "FROM read_parquet(?, union_by_name=true)",
            [parquet_files],
        ).fetchone()
    finally:
        con.close()

    return {
        "dest": dest,
        "file_count": len(parquet_files),
        "row_count": int(row_count),
        "size_on_disk_bytes": size_bytes,
        "size_on_disk_mb": round(size_bytes / 2 ** 20, 1),
        "epoch_min": int(epoch_min),
        "epoch_max": int(epoch_max),
    }


# ---------------------------------------------------------------------------
# Benchmark run
# ---------------------------------------------------------------------------

def build_pipelines(dest: str, epoch_min: int, epoch_max: int) -> list:
    """The fixed set of representative SPQL pipelines, as (name, spql)."""
    index_token = f'index="{dest}/*"'
    span = max(1, epoch_max - epoch_min)
    tb_earliest = epoch_min + (span * 4) // 9
    tb_latest = tb_earliest + span // 9  # ~1/9th of the corpus window
    return [
        {
            "name": "full_scan_head",
            "spql": f'{index_token} | head 100',
        },
        {
            "name": "filtered_search_agg",
            "spql": (
                f'{index_token} | search level="ERROR" '
                f'| stats count by service | sort -count'
            ),
        },
        {
            "name": "time_bounded_scan",
            "spql": (
                f'{index_token} earliest="{tb_earliest}" '
                f'latest="{tb_latest}" | stats count'
            ),
        },
        {
            "name": "timechart_daily",
            "spql": (
                f'{index_token} | rename timestamp as _time '
                f'| timechart span=1day count by level'
            ),
        },
        {
            "name": "rex_extract_agg",
            "spql": (
                f'{index_token} | rex field=message "user (?<user>\\w+)" '
                f'| stats count by user | sort -count | head 10'
            ),
        },
        {
            "name": "dedup_client_ip",
            "spql": f'{index_token} | dedup client_ip',
        },
    ]


def run_benchmarks(dest: str, runs: int = 3) -> dict:
    """Time every pipeline end-to-end through the real engine."""
    dest = os.path.abspath(dest)
    corpus = collect_corpus_context(dest)
    machine = collect_machine_context()

    # Lazy import: the engine pulls in ANTLR + macro/job stores; only the
    # --run phase needs any of it.
    from query_engine.CmdExecutionBackend import process_query_with_diagnostics

    pipelines = build_pipelines(dest, corpus["epoch_min"], corpus["epoch_max"])
    results = []
    for idx, pipe in enumerate(pipelines, start=1):
        print(f"[i] [{idx}/{len(pipelines)}] {pipe['name']}: {pipe['spql']}")
        run_seconds = []
        rows = 0
        status = "success"
        diagnostic = None
        for run_no in range(1, runs + 1):
            t0 = time.perf_counter()
            df, _job_id, diag = process_query_with_diagnostics(pipe["spql"])
            elapsed = time.perf_counter() - t0
            run_seconds.append(elapsed)
            if diag is None:
                rows = int(len(df.index))
            elif diag.startswith("empty:"):
                status, rows, diagnostic = "empty", 0, diag
            else:
                status, rows, diagnostic = "error", 0, diag
            print(
                f"[i]   run {run_no}/{runs}: {elapsed:.3f}s, "
                f"{rows} row(s), status={status}"
            )
            if status == "error":
                break
        results.append({
            "name": pipe["name"],
            "spql": pipe["spql"],
            "runs_seconds": [round(s, 4) for s in run_seconds],
            "median_seconds": round(statistics.median(run_seconds), 4),
            "rows": rows,
            "status": status,
            "diagnostic": diagnostic,
        })

    return {
        "generated_at_epoch": int(time.time()),
        "runs_per_pipeline": runs,
        "machine": machine,
        "corpus": corpus,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    """GitHub-markdown report table plus machine/corpus context."""
    m = report["machine"]
    c = report["corpus"]
    lines = [
        "## SpeakesQuery benchmark results",
        "",
        f"- CPU: {m['cpu_model']} ({m['cpu_count']} logical cores)",
        f"- RAM: {m['ram_total_gb']} GB",
        f"- Python: {m['python_version']} on {m['platform']}",
        (
            f"- Corpus: {c['row_count']:,} rows across {c['file_count']} "
            f"gzip parquet file(s), {c['size_on_disk_mb']} MB on disk"
        ),
        f"- Runs per pipeline: {report['runs_per_pipeline']} (median reported)",
        "",
        "| Pipeline | SPQL | Median (s) | Rows | Status |",
        "|---|---|---:|---:|---|",
    ]
    for r in report["results"]:
        # Shorten the absolute corpus path so the published table stays
        # readable; the JSON report keeps the verbatim SPQL.
        spql = r["spql"].replace(c["dest"], "<corpus>").replace("|", "\\|")
        lines.append(
            f"| {r['name']} | `{spql}` | {r['median_seconds']:.3f} "
            f"| {r['rows']:,} | {r['status']} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_corpus(dest: str) -> None:
    """Remove the corpus dir - ONLY if the manifest marker proves it is ours."""
    dest = os.path.abspath(dest)
    if not os.path.isdir(dest):
        raise BenchmarkCorpusError(f"Corpus directory does not exist: {dest}")
    if dest in ("/", os.path.abspath(PROJECT_ROOT)):
        raise BenchmarkCorpusError(f"Refusing to remove {dest}")
    _load_manifest(dest)  # raises BenchmarkCorpusError if not a corpus
    shutil.rmtree(dest)
    print(f"[i] Removed corpus directory {dest}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.benchmark_corpus",
        description=(
            "Reproducible synthetic-corpus benchmark for SpeakesQuery. "
            "Generate a deterministic gzip-parquet app-log corpus, time "
            "representative SPQL pipelines through the real engine, report "
            "medians as a GitHub markdown table."
        ),
    )
    p.add_argument("--generate", action="store_true",
                   help="Generate the synthetic corpus")
    p.add_argument("--run", action="store_true",
                   help="Run the benchmark pipelines against the corpus")
    p.add_argument("--cleanup", action="store_true",
                   help="Remove the generated corpus (manifest-guarded)")
    p.add_argument("--size-gb", type=float, default=None,
                   help="Approximate uncompressed corpus size to generate")
    p.add_argument("--files", type=int, default=None,
                   help="Explicit file count (overrides --size-gb)")
    p.add_argument("--rows-per-file", type=int, default=None,
                   help=f"Rows per file (default {DEFAULT_ROWS_PER_FILE})")
    p.add_argument("--seed", type=int, default=BASE_SEED,
                   help=f"Base seed (default {BASE_SEED}); file i uses seed+i")
    p.add_argument("--runs", type=int, default=3,
                   help="Timed runs per pipeline (default 3)")
    p.add_argument("--dest", default=DEFAULT_DEST,
                   help=f"Corpus directory (default {DEFAULT_DEST})")
    p.add_argument("--json", dest="json_path", default=None,
                   help="Also write the full report as JSON to this path")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not (args.generate or args.run or args.cleanup):
        print("[x] Nothing to do: pass --generate, --run, and/or --cleanup")
        return 1
    try:
        if args.generate:
            generate_corpus(
                args.dest,
                size_gb=args.size_gb,
                files=args.files,
                rows_per_file=args.rows_per_file,
                seed=args.seed,
            )
        if args.run:
            report = run_benchmarks(args.dest, runs=max(1, args.runs))
            print()
            print(render_markdown(report))
            if args.json_path:
                write_text_atomic(
                    args.json_path, json.dumps(report, indent=2) + "\n"
                )
                print(f"[i] JSON report written to {args.json_path}")
            errored = [r["name"] for r in report["results"]
                       if r["status"] == "error"]
            if errored:
                print(f"[x] Pipeline(s) errored: {', '.join(errored)}")
                return 1
        if args.cleanup:
            cleanup_corpus(args.dest)
    except BenchmarkCorpusError as exc:
        print(f"[x] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    exit_code = main()
    # Hard-exit instead of sys.exit: --run imports the query engine,
    # which starts non-daemon background threads (scheduler wiring)
    # that keep the interpreter alive after main() returns - the first
    # 1 GB run on 2026-07-12 printed its full report and then hung for
    # an hour. This is a one-shot CLI; flush and exit bluntly.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
