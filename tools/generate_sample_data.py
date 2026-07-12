"""Regenerate the bundled sample dataset (weakness audit W12, 2026-07-12).

A fresh install must have something real to query in the first minute -
the app previously installed empty (test fixtures only), so value
arrived only after deploying connectors and waiting for cron. This
script produces ``indexes/sample/app_logs/1748736000_sample.parquet``:
30 days of realistic application-log events (June 2026, fixed epochs so
the file is deterministic and diff-stable in git).

The parquet is TRACKED IN GIT and baked into the Docker image
(_default_indexes seeding, same pattern as default_test). The Query
page's first-run card points its "try these 5 queries" at it.

Deterministic by design: fixed seed, fixed time range - rerunning this
script produces byte-identical rows (parquet metadata may differ).
Rerun only when deliberately changing the dataset shape, then update
the first-run queries in ui.html + docs/lang/01_fundamentals.md and the
row-count assertions in tests/test_sample_dataset.py.

Usage:
    python -m tools.generate_sample_data
"""

import os
import random
import sys

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2026-06-01 00:00:00 UTC .. 2026-06-30 23:59:59 UTC
START_EPOCH = 1780272000
END_EPOCH = 1782863999
ROW_COUNT = 5000
SEED = 20260712

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "indexes", "sample", "app_logs")
# Fixed filename (epoch prefix matches the ingestion naming convention)
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "1780272000_sample.parquet")

SERVICES = ["web", "api", "auth", "ingest", "scheduler", "db"]
SERVICE_WEIGHTS = [30, 25, 12, 15, 8, 10]
HOSTS = ["app-01", "app-02", "app-03"]
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVEL_WEIGHTS = [18, 62, 12, 7, 1]
USERS = [
    "amara", "bjorn", "chen", "dara", "elif", "farid", "gita", "hugo",
    "ines", "jun", "kai", "lena", "milo", "nadia", "omar", "priya",
]
PATHS = [
    "/api/query", "/api/tree", "/api/save", "/api/si/list",
    "/api/lookups", "/api/settings", "/", "/api/alert-groups",
    "/api/notebooks", "/api/schedule/heatmap",
]
STATUS_CODES = [200, 200, 200, 200, 201, 301, 400, 404, 429, 500]

INFO_TEMPLATES = [
    "user {user} logged in from {ip}",
    "user {user} ran query job {job}",
    "request {path} completed in {ms}ms",
    "scheduled task {job} finished: {rows} rows written",
    "cache hit for job {job}",
]
WARN_TEMPLATES = [
    "slow query from user {user}: {ms}ms on {path}",
    "retrying request to {path} (attempt 2)",
    "disk usage at 81% on {host}",
]
ERROR_TEMPLATES = [
    "request {path} failed with status {status} for user {user}",
    "timeout after {ms}ms connecting to upstream from {host}",
    "failed login for user {user} from {ip}",
    "parquet write failed on {host}: disk quota exceeded",
]


def build_dataframe() -> pd.DataFrame:
    rng = random.Random(SEED)
    rows = []
    for _ in range(ROW_COUNT):
        epoch = rng.randint(START_EPOCH, END_EPOCH)
        level = rng.choices(LEVELS, weights=LEVEL_WEIGHTS, k=1)[0]
        service = rng.choices(SERVICES, weights=SERVICE_WEIGHTS, k=1)[0]
        host = rng.choice(HOSTS)
        user = rng.choice(USERS)
        path = rng.choice(PATHS)
        ip = f"192.168.{rng.randint(0, 4)}.{rng.randint(2, 254)}"
        job = f"job-{rng.randint(1000, 9999)}"
        response_ms = int(rng.lognormvariate(4.6, 0.9)) + 5
        if level in ("ERROR", "CRITICAL"):
            status = rng.choice([429, 500, 500, 502, 404])
            template = rng.choice(ERROR_TEMPLATES)
        elif level == "WARNING":
            status = rng.choice([200, 200, 301, 400, 429])
            template = rng.choice(WARN_TEMPLATES)
        else:
            status = rng.choice(STATUS_CODES)
            template = rng.choice(INFO_TEMPLATES)
        message = template.format(
            user=user, ip=ip, path=path, ms=response_ms,
            job=job, rows=rng.randint(1, 5000), status=status, host=host,
        )
        rows.append({
            "_epoch": epoch,
            # ISO timestamp string alongside _epoch: realistic for log
            # data AND lets the first-run timechart query use the
            # documented `rename timestamp as _time` pattern.
            "timestamp": pd.Timestamp(epoch, unit="s", tz="UTC").strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "level": level,
            "service": service,
            "host": host,
            "path": path,
            "status_code": status,
            "response_ms": response_ms,
            "client_ip": ip,
            "message": message,
            "bytes_sent": rng.randint(180, 250_000),
        })
    df = pd.DataFrame(rows).sort_values("_epoch").reset_index(drop=True)
    df["_epoch"] = df["_epoch"].astype("int64")
    return df


def main() -> int:
    df = build_dataframe()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp_path = OUTPUT_FILE + ".tmp"
    df.to_parquet(tmp_path, compression="gzip", index=False)
    os.replace(tmp_path, OUTPUT_FILE)
    print(f"[i] Wrote {len(df)} rows x {len(df.columns)} cols to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
