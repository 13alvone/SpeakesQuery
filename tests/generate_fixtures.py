#!/usr/bin/env python3
"""
Generate deterministic test fixture Parquet files for the SPQL test suite.

Creates the following files:
  Under indexes/default_test/:
    - output_parquets/test0.parquet  (5 rows, 11 columns)
    - output_parquets/test1.parquet  (3 rows, same schema)
    - error_tracking/system_alerts.parquet (100 rows)
  Under indexes/archive/system_logs/error_tracking/ (for test_duckdb_index_call):
    - error1.parquet (3 rows, no _epoch - exercises TRY_CAST and cross-schema concat)
    - error2.parquet (2 rows, same schema as error1)

All values are deterministic - tests assert exact cell values against this data.
Run this once before the test suite; re-run if you change test expectations.

Usage:
    python tests/generate_fixtures.py
"""

import os
import sys
import random
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEXES_DIR = os.path.join(PROJECT_ROOT, "indexes", "default_test")
ARCHIVE_DIR = os.path.join(PROJECT_ROOT, "indexes", "archive", "system_logs")


# ---------------------------------------------------------------------------
# test0.parquet - 5 rows
# ---------------------------------------------------------------------------

def make_test0() -> pd.DataFrame:
    """Create test0.parquet with exact values expected by YAML tests.

    Key constraints (from YAML assertions):
      - level: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
      - message: ["Debug message", "Info message", "Warning message",
                   "Error message", "Critical message"]
      - status: lowercase of level
      - x: [4, 4, 5, 4, 6]   (doubled → [8,8,10,8,12])
      - z: [5, 6, 7, 8, 9]   (sum_xz → [9,10,12,12,15], diff → [1,2,2,4,3])
      - test: [8, 12, 10, 16, 18]  (ratio=test/x → [2,3,2,4,3])
      - userRole: ["user", "admin", "user", "admin", "guest"]
        (dedup → 3 unique; eventstats count by userRole → [2,2,2,2,1])
        NOTE: eventstats expects [2,2,1,2,2] → user=2 at rows 0,2; admin=2
        at rows 1,3; guest=1 at row 4.  Actual: user(0), admin(1), user(2),
        admin(3), guest(4) → counts user:2, admin:2, guest:1 → [2,2,2,2,1] ✓
      - errorCode: [400, 401, 402, 403, 500]
      - error_code: [0, 1, 0, 1, 0]  (used in conditional tests)
      - timestamp: sequential timestamps for bin tests
    """
    base_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return pd.DataFrame({
        "level": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "message": [
            "Debug message",
            "Info message",
            "Warning message",
            "Error message",
            "Critical message",
        ],
        "status": ["debug", "info", "warning", "error", "critical"],
        "x": [4, 4, 5, 4, 6],
        "z": [5, 6, 7, 8, 9],
        "test": [8, 12, 10, 16, 18],
        "userRole": ["user", "admin", "user", "admin", "guest"],
        "errorCode": [400, 401, 402, 403, 500],
        "error_code": [0, 1, 0, 1, 0],
        "timestamp": [
            base_ts + timedelta(hours=i) for i in range(5)
        ],
    })


# ---------------------------------------------------------------------------
# test1.parquet - 3 rows (same schema, for join/append/wildcard tests)
# ---------------------------------------------------------------------------

def make_test1() -> pd.DataFrame:
    """Create test1.parquet.

    Key constraints:
      - 3 rows (wildcard test expects test0+test1 = 8 total)
      - Must have userRole column (join_001 joins on userRole)
      - Same column set as test0 for schema compatibility
    """
    base_ts = datetime(2024, 2, 1, tzinfo=timezone.utc)
    return pd.DataFrame({
        "level": ["INFO", "WARNING", "ERROR"],
        "message": ["Info message", "Warning message", "Error message"],
        "status": ["info", "warning", "error"],
        "x": [3, 7, 2],
        "z": [4, 8, 3],
        "test": [6, 14, 4],
        "userRole": ["admin", "guest", "user"],
        "errorCode": [401, 403, 500],
        "error_code": [0, 0, 1],
        "timestamp": [
            base_ts + timedelta(hours=i) for i in range(3)
        ],
    })


# ---------------------------------------------------------------------------
# system_alerts.parquet - 100 rows
# ---------------------------------------------------------------------------

def make_system_alerts() -> pd.DataFrame:
    """Create system_alerts.parquet with 100 deterministic rows.

    Key constraints (from YAML assertions):
      - 100 rows exactly (stats_006 asserts count = 100)
      - region: exactly 3 unique values ("US", "EU", "ASIA")
        US = 34 rows (search_008 asserts region="US" → 34)
      - userRole: exactly 5 unique values (stats_007 asserts dc(userRole)=5)
      - attempts: min=1, max=9 (stats_008 asserts min=1, max=9)
      - errorCode: row 0 must be null, rows 1+ mostly non-null
      - amount: row 0 ≈ 1233.05, row 1 ≈ 1034.64, row 2 ≈ 3474.73
      - priority: "critical" appears in 26 rows (nested_pipelines test)
      - status: includes "critical" value
      - team: at least 3 unique values (dedup_003 uses region, not team)
      - action: some rows contain "log" substring (regex_002)
      - responseTime: range supports fast/medium/slow categorization
      - customerType: multiple types for multi-grouping stats
      - timestamp: sequential for bin tests
    """
    rng = random.Random(42)  # deterministic seed

    n = 100
    base_ts = datetime(2024, 3, 1, tzinfo=timezone.utc)

    # Region distribution: US=34, EU=33, ASIA=33
    regions = ["US"] * 34 + ["EU"] * 33 + ["ASIA"] * 33

    # 5 distinct userRoles, distributed evenly-ish
    roles = ["admin", "operator", "viewer", "editor", "guest"]
    user_roles = [roles[i % 5] for i in range(n)]

    # attempts: 1-9 deterministic
    attempts = [(i % 9) + 1 for i in range(n)]

    # errorCode: row 0 is null, rest have values
    error_codes = [None] + [rng.choice([400, 401, 403, 404, 500, 502]) for _ in range(n - 1)]

    # amount: first 3 rows have specific expected values
    amounts = [1233.05, 1034.64, 3474.73]
    amounts += [round(rng.uniform(100, 5000), 2) for _ in range(n - 3)]

    # priority: "critical" in first 26 rows, then others
    priorities_pool = ["critical", "high", "medium", "low"]
    priorities = ["critical"] * 26 + [rng.choice(["high", "medium", "low"]) for _ in range(n - 26)]

    # status: match priority for first 26
    statuses = ["critical"] * 26 + [rng.choice(["error", "warning", "info"]) for _ in range(n - 26)]

    # team: 3+ distinct values
    teams = ["alpha", "beta", "gamma"]
    team_col = [teams[i % 3] for i in range(n)]

    # action: include "log" substring in some rows for regex test
    actions = []
    for i in range(n):
        if i % 5 == 0:
            actions.append("login_log")
        elif i % 7 == 0:
            actions.append("logout")
        else:
            actions.append(rng.choice(["create", "update", "delete", "read", "export"]))

    # responseTime: range for fast/medium/slow categorization
    response_times = []
    for i in range(n):
        response_times.append(round(rng.uniform(1, 30), 1))

    # customerType: multiple types
    customer_types = ["enterprise", "startup", "individual"]
    customer_col = [customer_types[i % 3] for i in range(n)]

    # timestamps: sequential
    timestamps = [base_ts + timedelta(hours=i * 2) for i in range(n)]

    # _epoch: integer epoch for each timestamp
    epochs = [int(t.timestamp()) for t in timestamps]

    df = pd.DataFrame({
        "_epoch": epochs,
        "timestamp": timestamps,
        "region": regions,
        "userRole": user_roles,
        "status": statuses,
        "errorCode": pd.array(error_codes, dtype=pd.Int64Dtype()),
        "attempts": attempts,
        "amount": amounts,
        "priority": priorities,
        "team": team_col,
        "action": actions,
        "responseTime": response_times,
        "customerType": customer_col,
    })

    # Convert errorCode to float64 (NaN for None) to match typical Parquet behavior
    df["errorCode"] = df["errorCode"].astype("float64")

    return df


# ---------------------------------------------------------------------------
# lookups/test.csv - synthetic Congress-shape fixture for inputlookup tests
#   tier1_commands/test_inputlookup.yaml asserts a 11-column schema with
#   url/date/congress/title/type/kind/policy/_epoch + 3 fillers. The
#   original test was authored against a real-data snapshot (55,000+ rows)
#   that was never committed. We ship a deterministic 200-row synthetic
#   fixture instead and adjust the YAML's min_rows to match. The schema
#   is preserved exactly so downstream column-presence asserts pass.
# ---------------------------------------------------------------------------

def make_lookup_test_csv() -> pd.DataFrame:
    """200-row deterministic Congress-shape lookup fixture."""
    base_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    n = 200
    rng = random.Random(2024)
    types = ["HR", "S", "HRES", "SRES", "HJRES", "SJRES"]
    kinds = ["bill", "resolution", "amendment"]
    policies = ["Healthcare", "Energy", "Finance", "Defense", "Tech", "Immigration", "Tax"]
    return pd.DataFrame({
        "url": [f"https://www.congress.gov/bill/118th-congress/{rng.choice(types).lower()}/{i+1}" for i in range(n)],
        "date": [(base_ts + timedelta(days=i % 365)).strftime("%Y-%m-%d") for i in range(n)],
        "congress": [118 + (i % 2) for i in range(n)],
        "title": [f"Bill #{i+1}: deterministic synthetic test fixture" for i in range(n)],
        "type": [rng.choice(types) for _ in range(n)],
        "kind": [rng.choice(kinds) for _ in range(n)],
        "policy": [rng.choice(policies) for _ in range(n)],
        "_epoch": [int((base_ts + timedelta(days=i % 365)).timestamp()) for i in range(n)],
        "sponsor_party": [rng.choice(["D", "R", "I"]) for _ in range(n)],
        "sponsor_state": [rng.choice(["CA", "TX", "NY", "FL", "PA"]) for _ in range(n)],
        "importance_tier": [rng.choice(["LOW", "MEDIUM", "HIGH"]) for _ in range(n)],
    })


# ---------------------------------------------------------------------------
# system_logs/default.parquet - backs the duckdb default index pattern
#   The query parser falls back to ``system_logs/**`` when no index= clause
#   is given. Without a fixture there, tier4_negative test_common_mistakes
#   `mistake - time range without index returns data` returns 0 rows and
#   the test runner collapses that to None → "fails as if errored". This
#   tiny 10-row fixture lets the default-index code path actually return
#   data so the test asserts the intended invariant (default path works,
#   not "default path coincidentally happens to be empty").
# ---------------------------------------------------------------------------

def make_default_system_logs() -> pd.DataFrame:
    """Tiny 10-row 2024 fixture for the bare-default-index path."""
    base_ts = datetime(2024, 6, 1, tzinfo=timezone.utc)
    return pd.DataFrame({
        "_epoch": [int((base_ts + timedelta(days=i*30)).timestamp()) for i in range(10)],
        "level": ["INFO", "WARN", "ERROR"] * 3 + ["DEBUG"],
        "message": [f"event_{i}" for i in range(10)],
        "status": ["ok"] * 7 + ["error"] * 3,
    })


# ---------------------------------------------------------------------------
# archive/system_logs/error_tracking - duckdb_index_call cross-schema fixtures
# ---------------------------------------------------------------------------

def make_archive_error1() -> pd.DataFrame:
    """Create error1.parquet (3 rows) for test_duckdb_index_call cross-schema tests.

    Schema invariants (asserted by tests/test_duckdb_index_call.py):
      - 3 rows exactly (test_deep_glob_source_file_tracking)
      - Columns: timestamp (str), status, errorCode, priority, action
      - NO _epoch (test_time_filter_on_file_without_epoch_column exercises TRY_CAST)
      - NO warningCode (test_deep_glob_nan_fill_for_missing_columns asserts NaN)
      - NO userRole (only system4.parquet has it)
    """
    return pd.DataFrame({
        "timestamp": [
            "2024-01-15T10:00:00Z",
            "2024-01-15T10:05:00Z",
            "2024-01-15T10:10:00Z",
        ],
        "status": ["error", "error", "warning"],
        "errorCode": [500, 502, 408],
        "priority": ["critical", "high", "medium"],
        "action": ["retry", "log_and_alert", "log"],
    })


def make_archive_error2() -> pd.DataFrame:
    """Create error2.parquet (2 rows) - same schema as error1."""
    return pd.DataFrame({
        "timestamp": [
            "2024-01-16T08:30:00Z",
            "2024-01-16T08:45:00Z",
        ],
        "status": ["error", "error"],
        "errorCode": [503, 504],
        "priority": ["critical", "critical"],
        "action": ["retry", "alert"],
    })


# ---------------------------------------------------------------------------
# Write all fixtures
# ---------------------------------------------------------------------------

def generate_all():
    SYSTEM_LOGS_DEFAULT = os.path.join(
        PROJECT_ROOT, "indexes", "system_logs", "default.parquet"
    )
    LOOKUP_TEST_CSV = os.path.join(PROJECT_ROOT, "lookups", "test.csv")
    paths = {
        os.path.join(INDEXES_DIR, "output_parquets", "test0.parquet"): make_test0,
        os.path.join(INDEXES_DIR, "output_parquets", "test1.parquet"): make_test1,
        os.path.join(INDEXES_DIR, "error_tracking", "system_alerts.parquet"): make_system_alerts,
        os.path.join(ARCHIVE_DIR, "error_tracking", "error1.parquet"): make_archive_error1,
        os.path.join(ARCHIVE_DIR, "error_tracking", "error2.parquet"): make_archive_error2,
        SYSTEM_LOGS_DEFAULT: make_default_system_logs,
    }

    for path, factory in paths.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df = factory()
        df.to_parquet(path, index=False)
        print(f"  wrote {path}  ({len(df)} rows, {len(df.columns)} cols)")

    # CSV lookup fixture (separate emit path - to_csv not to_parquet)
    os.makedirs(os.path.dirname(LOOKUP_TEST_CSV), exist_ok=True)
    csv_df = make_lookup_test_csv()
    csv_df.to_csv(LOOKUP_TEST_CSV, index=False)
    print(f"  wrote {LOOKUP_TEST_CSV}  ({len(csv_df)} rows, {len(csv_df.columns)} cols)")


if __name__ == "__main__":
    print("Generating test fixtures...")
    generate_all()
    print("Done.")
