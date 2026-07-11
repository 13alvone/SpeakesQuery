"""Run every default-feeder live end-to-end + SPQL roundtrip.

Usage::

    source .speakesQueryDevEnv/bin/activate
    python -m tests._live_runner

Reads ``secrets.txt`` at project root, runs each feeder's ingestion
script against real APIs, writes a temporary Parquet under
``indexes/<subdir>/``, then executes the feeder's saved-search SPQL
query against that Parquet and reports:

* row count produced by the script
* per-expected-column empty ratio
* row count returned by the saved search

Designed for iterative live debugging, not as a pytest (pytest wrapper
lives in ``test_live_integration.py``). Keeps its output structured so
the caller can grep / diff across runs.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._live_harness import (  # noqa: E402
    FEEDERS, Feeder, audit_columns, load_secrets, run_script_live,
)

DEFAULT_SS_DIR = PROJECT_ROOT / "default_saved_searches"
INDEXES_DIR = PROJECT_ROOT / "indexes"


def _creds_for(feeder: Feeder, secrets: dict[str, list[str]]) -> dict[str, str]:
    """Resolve a feeder's credential dict from the parsed secrets file.

    Fallbacks follow industry norms:
    * ``contact`` credentials (SEC EDGAR's User-Agent contact string)
      can be synthesised from the ``[gmail]`` section when no dedicated
      section is present - SEC only requires a way to reach you, not a
      key. This matches SEC's own guidance ("email address or name+email").
    """
    out: dict[str, str] = {}
    for cred_name, section in feeder.required_creds.items():
        values = secrets.get(section.lower(), [])
        if values:
            out[cred_name] = values[0]
            continue

        # SEC_EDGAR_CONTACT fallback: derive from [gmail] section.
        if cred_name == "SEC_EDGAR_CONTACT":
            gmail_lines = secrets.get("gmail", [])
            if gmail_lines:
                out[cred_name] = f"SpeakesQuery Testing <{gmail_lines[0]}>"
                continue

        raise RuntimeError(
            f"{feeder.name} needs {cred_name}; section [{section}] "
            f"missing or empty in secrets.txt"
        )
    return out


def _detect_sentinel_error(df) -> str | None:
    """Return a short reason string if *df* is a one-row ERROR sentinel."""
    if len(df) == 1 and "ticker" in df.columns:
        first = str(df["ticker"].iloc[0]).upper()
        if first == "ERROR":
            detail = ""
            if "error_detail" in df.columns:
                detail = str(df["error_detail"].iloc[0])[:140]
            return detail or "ingest script emitted ERROR sentinel row"
    return None


def _run_feeder_ingest(feeder: Feeder, secrets: dict[str, list[str]]) -> tuple[int, Path, dict, str | None]:
    """Return ``(row_count, parquet_path, audit_report, sentinel_reason)``."""
    from scheduled_input_engine.parquet_writer import ParquetWriter

    creds = _creds_for(feeder, secrets)
    df = run_script_live(feeder, creds=creds)
    writer = ParquetWriter(INDEXES_DIR, target_file_mb=128)
    filename = f"_live_test_{int(time.time())}.parquet"
    path = writer.write_atomic(
        df, subdirectory=feeder.subdirectory, filename=filename, overwrite=True,
    )
    report = audit_columns(df, feeder.expected_columns)
    sentinel = _detect_sentinel_error(df)
    return len(df), path, report, sentinel


def _run_saved_search(feeder: Feeder) -> int:
    """Execute the default saved search query for *feeder*; return row count."""
    from query_engine.CmdExecutionBackend import run_query_and_return_results_df

    ss_path = DEFAULT_SS_DIR / f"{feeder.name}.yaml"
    ss = yaml.safe_load(ss_path.read_text())
    query = ss["query"]
    df, _ = run_query_and_return_results_df(query)
    if df is None:
        return 0
    return len(df)


def _format_audit(report: dict) -> str:
    lines = []
    for col, r in report.items():
        if not r["present"]:
            lines.append(f"    [XX] {col}: NOT PRESENT in output")
            continue
        if r["empty_ratio"] is None:
            continue
        pct = r["empty_ratio"] * 100
        tag = "OK" if pct < 50 else ("WARN" if pct < 100 else "FAIL")
        lines.append(
            f"    [{tag}] {col}: empty {r['n_empty']}/{r['n_rows']} ({pct:.0f}%)"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    only = set(argv[1:]) if len(argv) > 1 else None
    secrets = load_secrets()

    results: list[dict] = []
    for feeder in FEEDERS:
        if only and feeder.name not in only:
            continue
        print(f"\n{'='*72}\n==  {feeder.name}  ({feeder.script})\n{'='*72}")
        t0 = time.monotonic()
        try:
            rows, path, report, sentinel = _run_feeder_ingest(feeder, secrets)
        except Exception as exc:
            print(f"  INGEST FAILED: {exc}")
            traceback.print_exc()
            results.append({
                "feeder": feeder.name,
                "ingest_ok": False,
                "error": str(exc),
            })
            continue
        ingest_s = time.monotonic() - t0
        note = ""
        if rows == 0:
            note = "  (empty result - valid, upstream had no matching data)"
        elif sentinel:
            note = f"  (SENTINEL - upstream error: {sentinel})"
        print(f"  ingest: {rows} rows in {ingest_s:.1f}s -> {path}{note}")
        if rows and not sentinel:
            print(_format_audit(report))

        ss_rows = None
        try:
            ss_rows = _run_saved_search(feeder)
            print(f"  saved_search: {ss_rows} rows returned")
        except Exception as exc:
            print(f"  SPQL FAILED: {exc}")
            traceback.print_exc()

        results.append({
            "feeder": feeder.name,
            "ingest_ok": True,
            "ingest_rows": rows,
            "saved_search_rows": ss_rows,
            "audit": report,
            "path": str(path),
            "sentinel": sentinel,
        })

    print(f"\n{'='*72}\n==  SUMMARY\n{'='*72}")
    for r in results:
        if not r.get("ingest_ok"):
            print(f"  FAIL {r['feeder']}: {r['error']}")
            continue
        aud = r["audit"]
        rows = r["ingest_rows"]
        spql = r["saved_search_rows"] or 0
        sentinel = r.get("sentinel")
        if rows == 0:
            tag = "EMPTY"  # legitimate no-data case
        elif sentinel:
            tag = "UPSTREAM_ERR"  # script ran, external API failed (429, 5xx, …)
        else:
            n_bad = sum(
                1
                for info in aud.values()
                if not info["present"]
                or (info["empty_ratio"] is not None and info["empty_ratio"] >= 1.0)
            )
            tag = "PASS" if n_bad == 0 and spql > 0 else "REVIEW"
        print(
            f"  {tag:<13s}  {r['feeder']:<24s}  ingest={rows:>5}  spql={spql:<4}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
