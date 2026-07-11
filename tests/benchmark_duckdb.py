#!/usr/bin/env python3
"""
Benchmark: DuckDB index loading vs pure-Pandas baseline.

Compares the DuckDB-based process_index_calls() against the equivalent
pure-Pandas approach (pd.read_parquet + df.query + pd.concat) that the
old C++ extensions were actually calling under the hood.

Usage:
    python tests/benchmark_duckdb.py
"""

import os
import sys
import time
import random
import tempfile
import statistics
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from functionality.duckdb_index_call import process_index_calls


# ---------------------------------------------------------------------------
# Pure-Pandas baseline (what the C++ extensions actually did via pybind11)
# ---------------------------------------------------------------------------

def pandas_load_and_filter(parquet_path: str, filter_expr: str = None) -> pd.DataFrame:
    """Simulate the old C++ path: pd.read_parquet → df.query → return."""
    df = pd.read_parquet(parquet_path)
    if filter_expr:
        df = df.query(filter_expr)
    return df


def pandas_load_glob(pattern: str, filter_expr: str = None) -> pd.DataFrame:
    """Simulate the old C++ path for glob patterns: read each file, filter, concat."""
    import glob as _glob
    files = sorted(_glob.glob(pattern))
    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        if filter_expr:
            try:
                df = df.query(filter_expr)
            except Exception:
                pass
        dfs.append(df)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


# ---------------------------------------------------------------------------
# Synthetic data generator
# ---------------------------------------------------------------------------

def generate_large_parquet(path: str, n_rows: int, n_cols: int = 10):
    """Generate a Parquet file with n_rows and filterable columns."""
    rng = random.Random(42)
    data = {
        "timestamp": [int(time.time()) - rng.randint(0, 86400 * 30) for _ in range(n_rows)],
        "level": [rng.choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]) for _ in range(n_rows)],
        "status": [rng.choice(["ok", "fail", "timeout", "retry"]) for _ in range(n_rows)],
        "region": [rng.choice(["US", "EU", "ASIA", "LATAM"]) for _ in range(n_rows)],
        "userRole": [rng.choice(["admin", "user", "guest", "service"]) for _ in range(n_rows)],
        "errorCode": [rng.choice([200, 301, 400, 401, 403, 404, 500, 502, 503]) for _ in range(n_rows)],
        "amount": [round(rng.uniform(0, 10000), 2) for _ in range(n_rows)],
        "attempts": [rng.randint(1, 20) for _ in range(n_rows)],
        "message": [f"Event message {i} with details" for i in range(n_rows)],
        "source": [f"host-{rng.randint(1, 50):03d}" for _ in range(n_rows)],
    }
    # Add extra columns if needed
    for c in range(n_cols - 10):
        data[f"extra_{c}"] = [rng.random() for _ in range(n_rows)]

    df = pd.DataFrame(data)
    df.to_parquet(path, index=False)
    return path


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def timeit(fn, rounds=20, warmup=3):
    """Run fn() multiple times and return (median_ms, min_ms, max_ms, all_ms)."""
    times = []
    for _ in range(warmup):
        fn()
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    return statistics.median(times), min(times), max(times), times


def fmt(median, mn, mx):
    return f"{median:8.2f}ms  (min {mn:.2f}, max {mx:.2f})"


def run_benchmark(label, duckdb_fn, pandas_fn, rounds=20):
    """Run and compare DuckDB vs Pandas for a given scenario."""
    print(f"\n  {label}")
    print(f"  {'─' * 60}")

    d_med, d_min, d_max, _ = timeit(duckdb_fn, rounds=rounds)
    p_med, p_min, p_max, _ = timeit(pandas_fn, rounds=rounds)

    speedup = p_med / d_med if d_med > 0 else float("inf")
    winner = "DuckDB" if d_med < p_med else "Pandas"

    print(f"    DuckDB: {fmt(d_med, d_min, d_max)}")
    print(f"    Pandas: {fmt(p_med, p_min, p_max)}")
    print(f"    Winner: {winner} ({speedup:.1f}x)" if winner == "DuckDB"
          else f"    Winner: {winner} ({1/speedup:.1f}x)")


def main():
    print("=" * 66)
    print("  DuckDB vs Pure-Pandas Benchmark")
    print("=" * 66)

    # ── Scenario 1: Small existing data (100 rows) ──────────────
    small_path = "indexes/default_test/error_tracking/system_alerts.parquet"
    if os.path.exists(small_path):
        run_benchmark(
            "Small file - full scan (100 rows, 13 cols)",
            lambda: process_index_calls(["index", "=", small_path]),
            lambda: pandas_load_and_filter(small_path),
        )
        run_benchmark(
            "Small file - with filter (errorCode > 400)",
            lambda: process_index_calls(["index", "=", small_path, "errorCode", ">", "400"]),
            lambda: pandas_load_and_filter(small_path, "errorCode > 400"),
        )

    # ── Scenario 2: Wildcard glob across multiple files ─────────
    glob_pattern = "indexes/archive/system_logs/*.parquet"
    import glob as _glob
    glob_files = _glob.glob(glob_pattern)
    if glob_files:
        run_benchmark(
            f"Glob scan - {len(glob_files)} files in system_logs/",
            lambda: process_index_calls(["index", "=", f"archive/system_logs/*"]),
            lambda: pandas_load_glob(glob_pattern),
        )
        run_benchmark(
            f"Glob scan + filter (errorCode == 500)",
            lambda: process_index_calls(["index", "=", f"archive/system_logs/*", "errorCode", "=", "500"]),
            lambda: pandas_load_glob(glob_pattern, "errorCode == 500"),
        )

    # ── Scenario 3: Synthetic large dataset ─────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        for size_label, n_rows in [("10K", 10_000), ("100K", 100_000), ("500K", 500_000)]:
            parquet_path = os.path.join(tmpdir, f"bench_{n_rows}.parquet")
            # Place in indexes dir so DuckDB resolver finds it
            bench_dir = os.path.join("indexes", "_benchmark_tmp")
            os.makedirs(bench_dir, exist_ok=True)
            out_path = os.path.join(bench_dir, f"bench_{n_rows}.parquet")

            print(f"\n  Generating {size_label} row dataset...")
            generate_large_parquet(out_path, n_rows)
            file_size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(f"  File size: {file_size_mb:.1f} MB")

            run_benchmark(
                f"Synthetic {size_label} - full scan ({n_rows} rows, 10 cols)",
                lambda p=out_path: process_index_calls(["index", "=", p.replace("indexes/", "")]),
                lambda p=out_path: pandas_load_and_filter(p),
                rounds=10,
            )
            run_benchmark(
                f"Synthetic {size_label} - filter (region == 'US' AND errorCode > 400)",
                lambda p=out_path: process_index_calls([
                    "index", "=", p.replace("indexes/", ""),
                    "region", "=", '"US"', "AND", "errorCode", ">", "400"
                ]),
                lambda p=out_path: pandas_load_and_filter(p, 'region == "US" and errorCode > 400'),
                rounds=10,
            )
            run_benchmark(
                f"Synthetic {size_label} - selective filter (level == 'CRITICAL')",
                lambda p=out_path: process_index_calls([
                    "index", "=", p.replace("indexes/", ""),
                    "level", "=", '"CRITICAL"'
                ]),
                lambda p=out_path: pandas_load_and_filter(p, 'level == "CRITICAL"'),
                rounds=10,
            )

        # Cleanup
        import shutil
        shutil.rmtree(os.path.join("indexes", "_benchmark_tmp"), ignore_errors=True)

    print(f"\n{'=' * 66}")
    print("  Benchmark complete.")
    print(f"{'=' * 66}\n")


if __name__ == "__main__":
    main()
