"""Curator IMMUTABLE-tree read robustness - schema-heterogeneity guard.

Class of bug discovered on production 2026-05-18: every read against a
curator IMMUTABLE glob (``/api/dignity/today``, ``/api/search``,
``/api/playlist/today``, the dispatcher's thin-history detection, the
keyword-pool reader in log_writer) is vulnerable to two failure modes
DuckDB does not handle gracefully without ``union_by_name=true``:

1. **Empty-fire Null-typed columns.** The hourly
   ``curator_telemetry_pull`` ingestion script writes a parquet PER
   FIRE even when zero events were fetched in that 6h window. pyarrow
   infers all-None string columns as the ``Null`` logical type. When
   that parquet sorts FIRST in the glob, DuckDB uses its schema for the
   whole multi-file read, and any downstream WHERE/IN clause comparing
   to a VARCHAR literal/parameter fails with
   ``Conversion Error: Unimplemented type for cast (VARCHAR -> "NULL")``.

2. **Additive schema drift.** Pre-slice-4 ``curator_candidates``
   parquets lack ``thumbnail_url``. The ``/api/search`` SELECT clause
   does ``COALESCE(thumbnail_url, '') AS thumbnail_url`` - when the
   first-scanned parquet's schema lacks the column, DuckDB's binder
   falls back to the SELECT alias and errors with
   ``Binder Error: Column "thumbnail_url" referenced that exists in the
   SELECT clause - but this column cannot be referenced before it is
   defined``.

The fix is ``union_by_name=true`` on every multi-file glob read against
an IMMUTABLE/curator_* path. This file pins:

* Two reproducer tests (one per error class above) that confirm the
  bug is gone and would have caught the production failure.
* A drift-guard test that walks every known curator-IMMUTABLE-touching
  ``read_parquet`` call site and asserts ``union_by_name=true`` is
  present - so a future edit can't silently regress.

See CLAUDE.md "Do Not" pin: "Never read a multi-file IMMUTABLE-tree
parquet glob without union_by_name=true."
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest


# ── Fixtures (mirror the shape of tests/test_curator_speaktube_slice1.py) ──


@pytest.fixture
def isolated_immutable(tmp_path, monkeypatch):
    """Redirect ``settings.immutable_dir()`` to a tmp path so writes and
    REST reads land in an isolated tree.
    """
    from global_settings import get_settings
    from functionality.log_writer import LogWriter

    settings = get_settings()
    settings.set("immutable_root", str(tmp_path / "IMM"))
    LogWriter.reset_for_tests()
    yield tmp_path / "IMM"
    settings.reset("immutable_root")
    LogWriter.reset_for_tests()


# Mirror the ingestion-script column list exactly. The bug reproduces
# because pyarrow infers all-None columns as Null logical type when the
# DataFrame is empty (zero events fetched).
_TELEMETRY_COLUMNS = [
    "_epoch", "event_ts_iso", "event_date",
    "event_type", "video_external_id",
    "chosen_by", "run_date", "position", "slot_kind",
    "watched_seconds", "total_seconds",
    "rating", "reason", "kind", "content", "query",
    "raw_json",
]

# The pre-slice-4 ``curator_candidates`` shape (no ``thumbnail_url``).
_CANDIDATE_PRE_SLICE_4_COLUMNS = [
    "_epoch", "discovered_at_epoch", "source", "video_external_id", "video_url",
    "title", "channel_id", "channel_name", "channel_url",
    "published_iso", "description", "duration_seconds", "raw_blob",
]

# The post-slice-4 ``curator_candidates`` shape (with ``thumbnail_url``).
_CANDIDATE_POST_SLICE_4_COLUMNS = (
    _CANDIDATE_PRE_SLICE_4_COLUMNS + ["thumbnail_url"]
)


def _write_telemetry_empty_parquet(immutable_root: Path, filename: str) -> Path:
    """Write a zero-row curator_telemetry parquet - pyarrow infers
    all-None string columns as Null logical type. Sorts FIRST in the
    glob via the explicit filename prefix."""
    target = immutable_root / "curator_telemetry"
    target.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([], columns=_TELEMETRY_COLUMNS)
    path = target / filename
    df.to_parquet(path, index=False, compression="gzip")
    return path


def _write_telemetry_real_parquet(
    immutable_root: Path,
    filename: str,
    event_date: str,
    events: list[tuple[str, str]],
) -> Path:
    """Write a real curator_telemetry parquet with VARCHAR-typed
    columns (because there ARE string values to infer from)."""
    target = immutable_root / "curator_telemetry"
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for et, chosen in events:
        rows.append({
            "_epoch": 1_700_000_000,
            "event_ts_iso": f"{event_date}T09:00:00-07:00",
            "event_date": event_date,
            "event_type": et,
            "video_external_id": "vid_" + chosen,
            "chosen_by": chosen,
            "run_date": event_date,
            "position": 1,
            "slot_kind": "main",
            "watched_seconds": None,
            "total_seconds": None,
            "rating": None,
            "reason": "",
            "kind": "",
            "content": "",
            "query": "",
            "raw_json": "{}",
        })
    df = pd.DataFrame(rows, columns=_TELEMETRY_COLUMNS)
    path = target / filename
    df.to_parquet(path, index=False, compression="gzip")
    return path


def _write_candidate_parquet(
    immutable_root: Path,
    filename: str,
    rows: list[dict],
    columns: list[str],
) -> Path:
    """Write a curator_candidates parquet with the exact column set
    requested (lets us emulate pre-slice-4 vs post-slice-4 schemas)."""
    target = immutable_root / "curator_candidates"
    target.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=columns)
    path = target / filename
    df.to_parquet(path, index=False, compression="gzip")
    return path


# ── 1. Reproducer: empty-fire Null-typed parquet breaks /api/dignity/today ──


def test_dignity_today_survives_empty_first_parquet(
    isolated_immutable, client,
):
    """Production failure mode: hourly ingestion writes an empty
    parquet during a quiet window. Sorted alphabetically first in the
    glob. Without ``union_by_name=true``, DuckDB picks Null-typed
    columns from that file's schema and the WHERE/IN clauses fail with
    ``VARCHAR -> NULL`` cast errors.
    """
    # Empty parquet sorts BEFORE the real one alphabetically.
    _write_telemetry_empty_parquet(
        isolated_immutable, "0000_quiet_window.parquet",
    )
    _write_telemetry_real_parquet(
        isolated_immutable, "9999_active_window.parquet",
        event_date="2026-05-17",
        events=[
            ("play_start", "curator"),
            ("play_start", "user_manual"),
            ("play_end", "curator"),
            ("play_start", "recommendation"),
        ],
    )

    resp = client.get("/api/dignity/today?date=2026-05-17")
    assert resp.status_code == 200, (
        f"endpoint regressed to 500 on empty-first parquet: "
        f"{resp.get_json()}"
    )
    payload = resp.get_json()
    assert payload["total_plays"] == 4
    assert payload["chosen_plays"] == 3
    assert payload["dignity_pct"] == pytest.approx(75.0)


def test_dignity_today_survives_two_empty_parquets(
    isolated_immutable, client,
):
    """Edge case: every parquet in the tree is an empty fire (e.g. the
    user just deployed the ingestion task - no events have happened
    yet). The endpoint must still return 200 with the empty-state
    response, not 500."""
    _write_telemetry_empty_parquet(isolated_immutable, "0000_a.parquet")
    _write_telemetry_empty_parquet(isolated_immutable, "0001_b.parquet")

    resp = client.get("/api/dignity/today?date=2026-05-17")
    assert resp.status_code == 200
    payload = resp.get_json()
    # No plays anywhere → empty-state contract.
    assert payload["total_plays"] == 0
    assert payload["chosen_plays"] == 0
    assert payload["dignity_pct"] is None


# ── 2. Reproducer: missing-column parquet breaks /api/search ─────────────


def test_search_survives_pre_slice4_parquet_first(
    isolated_immutable, client,
):
    """Production failure mode: a curator_candidates parquet predating
    slice 4 (no ``thumbnail_url`` column) sorts first in the glob.
    Without ``union_by_name=true``, DuckDB resolves the SELECT's
    ``COALESCE(thumbnail_url, '') AS thumbnail_url`` against a schema
    that lacks the column, falls back to the SELECT alias, and emits
    the misleading ``cannot be referenced before it is defined`` binder
    error.
    """
    # Old-schema parquet first (alphabetical sort).
    _write_candidate_parquet(
        isolated_immutable,
        "0000_pre_slice4.parquet",
        [{
            "_epoch": 100,
            "discovered_at_epoch": 100,
            "source": "youtube_rss",
            "video_external_id": "old_vid",
            "video_url": "https://www.youtube.com/watch?v=old_vid",
            "title": "rare earth magnets explained",
            "channel_id": "c1",
            "channel_name": "OldChannel",
            "channel_url": "",
            "published_iso": "",
            "description": "",
            "duration_seconds": 100,
            "raw_blob": "{}",
        }],
        columns=_CANDIDATE_PRE_SLICE_4_COLUMNS,
    )
    # New-schema parquet with the thumbnail_url column.
    _write_candidate_parquet(
        isolated_immutable,
        "9999_post_slice4.parquet",
        [{
            "_epoch": 200,
            "discovered_at_epoch": 200,
            "source": "youtube_rss",
            "video_external_id": "new_vid",
            "video_url": "https://www.youtube.com/watch?v=new_vid",
            "title": "rare earth metals deep dive",
            "channel_id": "c2",
            "channel_name": "NewChannel",
            "channel_url": "",
            "published_iso": "",
            "description": "",
            "duration_seconds": 200,
            "raw_blob": "{}",
            "thumbnail_url": "https://img.example/new_vid.jpg",
        }],
        columns=_CANDIDATE_POST_SLICE_4_COLUMNS,
    )

    resp = client.get("/api/search?q=rare%20earth")
    assert resp.status_code == 200, (
        f"endpoint regressed to 500 on pre-slice4-first parquet: "
        f"{resp.get_json()}"
    )
    payload = resp.get_json()
    items = payload["items"]
    # Both rows should match the substring search.
    assert len(items) == 2
    # Most-recent first (ORDER BY _epoch DESC).
    assert items[0]["video"]["external_id"] == "new_vid"
    assert items[0]["video"]["thumbnail_url"] == "https://img.example/new_vid.jpg"
    # Old-schema row carries an empty thumbnail (COALESCE-substituted).
    assert items[1]["video"]["external_id"] == "old_vid"
    assert items[1]["video"]["thumbnail_url"] == ""


def test_search_survives_all_files_missing_thumbnail_url(
    isolated_immutable, client,
):
    """Second-order production failure mode (caught 2026-05-18 after
    the first fix landed): ``union_by_name=true`` only helps when at
    least one file in the glob declares the column. When EVERY file
    pre-dates the column addition (e.g. an operator deploys the
    ingestion script before the slice that added the column, OR the
    operator hasn't run the post-slice-4 ingestion yet), ALL files
    agree on a schema that lacks ``thumbnail_url`` and the
    ``COALESCE(thumbnail_url, '')`` in the SELECT still trips the
    binder error.

    The fix needs more than ``union_by_name=true``: a synthetic
    zero-row stub via ``UNION ALL BY NAME ... WHERE 1=0`` that
    declares ``thumbnail_url AS VARCHAR`` so the result schema always
    includes the column, even when every file lacks it.
    """
    # Single pre-slice-4 parquet, no other files.
    _write_candidate_parquet(
        isolated_immutable,
        "lone_pre_slice4.parquet",
        [{
            "_epoch": 100,
            "discovered_at_epoch": 100,
            "source": "youtube_rss",
            "video_external_id": "lone_vid",
            "video_url": "https://www.youtube.com/watch?v=lone_vid",
            "title": "rare earth elements primer",
            "channel_id": "c1",
            "channel_name": "LoneChannel",
            "channel_url": "",
            "published_iso": "",
            "description": "",
            "duration_seconds": 100,
            "raw_blob": "{}",
        }],
        columns=_CANDIDATE_PRE_SLICE_4_COLUMNS,
    )

    resp = client.get("/api/search?q=rare%20earth")
    assert resp.status_code == 200, (
        f"search endpoint regressed to 500 when all files lack "
        f"thumbnail_url: {resp.get_json()}"
    )
    payload = resp.get_json()
    items = payload["items"]
    assert len(items) == 1
    assert items[0]["video"]["external_id"] == "lone_vid"
    # Missing column → empty string via the synthetic stub + COALESCE.
    assert items[0]["video"]["thumbnail_url"] == ""


# ── 3. Reproducer: /api/playlist/today also affected by the class of bug ──


def test_playlist_today_survives_empty_first_parquet(
    isolated_immutable, client,
):
    """Same class of bug as the dignity endpoint, applied to the
    playlist read path. The composer always emits items so an
    empty-fire parquet is less likely here than in telemetry, but the
    class of failure is identical and the endpoint must be hardened
    against it - multiple composer fires + a partially-written
    rollback could plausibly leave an empty parquet behind."""
    from functionality.log_writer import (
        log_curator_playlist_item, flush_all,
    )
    import datetime as _dt

    # Empty parquet sorts first.
    target = isolated_immutable / "curator_playlist"
    target.mkdir(parents=True, exist_ok=True)
    empty_cols = [
        "_epoch", "run_date", "composed_at_iso", "growth_dial", "theme",
        "position", "slot_kind", "rationale",
        "external_id", "url", "title", "channel_name",
        "thumbnail_url", "published_at",
        "duration_seconds", "interest_score", "growth_score",
        "slop_score", "score_reasoning", "thin_history_active",
    ]
    pd.DataFrame([], columns=empty_cols).to_parquet(
        target / "0000_empty.parquet", index=False, compression="gzip",
    )

    # Now write a real playlist via the canonical helper so format
    # matches production.
    today_iso = _dt.date.today().isoformat()
    composed_iso = (
        _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    )
    log_curator_playlist_item(
        run_date=today_iso,
        composed_at_iso=composed_iso,
        growth_dial=-0.7,
        theme="",
        position=1,
        slot_kind="main",
        rationale="why this is here",
        external_id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title="Test video",
        channel_name="TestChannel",
        thumbnail_url="https://img.example/vid1.jpg",
        published_at="2026-05-17T12:00:00Z",
        duration_seconds=600,
        interest_score=0.8,
        growth_score=0.5,
        slop_score=0.1,
        score_reasoning="Good fit",
        thin_history_active=False,
    )
    flush_all()

    resp = client.get("/api/playlist/today")
    assert resp.status_code == 200, (
        f"playlist endpoint regressed to 500 on empty-first parquet: "
        f"{resp.get_json()}"
    )
    payload = resp.get_json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["video"]["external_id"] == "vid1"


# ── 4. Drift guard: every curator-IMMUTABLE read_parquet must use union_by_name=true ──


# Maps file path → list of (line-number-ish-pattern, description) for
# the read_parquet call sites that read curator/IMMUTABLE-tree
# parquets. New call sites get added here so the drift guard catches
# them.
_PINNED_READ_SITES = [
    # (file, anchor-pattern, description)
    (
        "desktop_app/server.py",
        r"SELECT MAX\(composed_at_iso\)\s+\"?\s*\"?\s*FROM read_parquet",
        "/api/playlist/today MAX(composed_at_iso) read",
    ),
    (
        "desktop_app/server.py",
        r"SELECT MAX\(run_date\) AS run_date\s+\"?\s*\"?\s*FROM read_parquet",
        "/api/playlist/today MAX(run_date) read",
    ),
    (
        "desktop_app/server.py",
        r"chosen_by IN \('curator', 'user_manual', 'playlist'\)",
        "/api/dignity/today telemetry read (anchor on the IN list)",
    ),
    (
        "desktop_app/server.py",
        r"regexp_matches\(LOWER\(title\)",
        "/api/search candidate-pool read (anchor on the title regex)",
    ),
    (
        "alert_groups/dispatcher.py",
        r"SELECT SUM\(watched_seconds\) AS total",
        "thin-history detection telemetry read",
    ),
    (
        # Anchor on a unique string fragment within the read_parquet
        # call so a future SQL reformat doesn't invalidate the guard.
        "functionality/log_writer.py",
        r"SELECT MAX\(_epoch\)",
        "keyword-pool: most-recent playlist composition cutoff read",
    ),
    (
        "functionality/log_writer.py",
        r"FIRST\(keyword ORDER BY _epoch ASC\)",
        "keyword-pool: active-keyword read",
    ),
]


@pytest.mark.parametrize("file,anchor,desc", _PINNED_READ_SITES)
def test_curator_read_parquet_uses_union_by_name(file, anchor, desc):
    """Drift guard: every pinned curator-IMMUTABLE read_parquet call
    site must include ``union_by_name=true``. Renaming a query, adding
    a new SELECT, or copy-pasting an unprotected read_parquet pattern
    will trip this test.

    The pattern: locate the anchor line, walk back/forward looking for
    the nearest ``read_parquet(`` token, then assert ``union_by_name=true``
    appears between the opening paren and the matching close.

    Add a tuple to ``_PINNED_READ_SITES`` when introducing a new
    curator/IMMUTABLE read.
    """
    repo_root = Path(__file__).parent.parent
    src = (repo_root / file).read_text(encoding="utf-8")

    # Find the anchor.
    m = re.search(anchor, src)
    assert m is not None, (
        f"{desc}: anchor pattern {anchor!r} not found in {file} - "
        f"either the production code moved or this drift guard is "
        f"stale. Update _PINNED_READ_SITES."
    )

    # Walk forward from the anchor a few lines looking for the
    # ``read_parquet(`` token - most of our queries have the anchor
    # IN the same SQL string just before/around the read_parquet call.
    window_start = max(0, m.start() - 600)
    window_end = min(len(src), m.end() + 600)
    window = src[window_start:window_end]

    assert "read_parquet" in window, (
        f"{desc}: no read_parquet call within 600 chars of anchor in {file}"
    )
    assert "union_by_name=true" in window, (
        f"{desc}: read_parquet call near the anchor in {file} is "
        f"missing ``union_by_name=true``. Add it to prevent the "
        f"VARCHAR->NULL / binder-error class of bug - see "
        f"tests/test_curator_immutable_read_robustness.py for the "
        f"reproducer."
    )


def test_no_unprotected_curator_immutable_read_parquet_in_server():
    """Stronger guard: scan ``desktop_app/server.py`` for every
    ``read_parquet(`` whose nearest 200-character neighborhood
    references ``curator_`` / ``IMMUTABLE``, and assert
    ``union_by_name`` is in the same neighborhood.

    Catches a future "I added a new curator endpoint and copy-pasted
    a read_parquet pattern" mistake the parametrized drift guard would
    miss because it only checks pinned anchors.
    """
    repo_root = Path(__file__).parent.parent
    src = (repo_root / "desktop_app/server.py").read_text(encoding="utf-8")

    failures: list[str] = []
    for m in re.finditer(r"read_parquet\(", src):
        start = max(0, m.start() - 250)
        end = min(len(src), m.end() + 600)
        window = src[start:end]
        if "curator_" not in window and "IMMUTABLE" not in window:
            continue  # not a curator/IMMUTABLE read - out of scope
        if "union_by_name" in window:
            continue
        # Locate line number for the diagnostic.
        line_no = src[:m.start()].count("\n") + 1
        failures.append(
            f"desktop_app/server.py:{line_no}: read_parquet near "
            f"curator/IMMUTABLE token but missing union_by_name=true"
        )

    assert not failures, (
        "Unprotected curator/IMMUTABLE read_parquet sites found:\n"
        + "\n".join(failures)
        + "\n\nAdd union_by_name=true to each - see "
        "tests/test_curator_immutable_read_robustness.py for the "
        "class-of-bug rationale."
    )


# ── 5. Thread-safety drift guard: no module-level duckdb.sql() for IMMUTABLE reads ──


@pytest.mark.parametrize("file", [
    "desktop_app/server.py",
    "alert_groups/dispatcher.py",
    "functionality/log_writer.py",
])
def test_no_module_level_duckdb_sql_for_immutable_reads(file):
    """Drift guard: the module-level ``duckdb.sql(...)`` helper uses a
    SHARED default connection that is NOT thread-safe under concurrent
    request threads. Two endpoints firing ``duckdb.sql()`` near-
    simultaneously can leave the global connection in a bad state,
    producing the misleading
    ``InvalidInputException: Attempting to execute an unsuccessful or
    closed pending query result`` on the second caller's
    ``.fetchall()`` / ``.df()``.

    Caught 2026-05-18 when prod's keyword-prefs GET (which hits
    ``read_active_curator_keyword_pool``) and /api/search fired back-
    to-back. The fix is per-call ``duckdb.connect(database=":memory:")``
    + ``con.close()`` in a try/finally - same pattern /api/dignity/today
    uses (the only one that wasn't broken).

    Scope: scan all three files for ``duckdb.sql(`` near a
    ``read_parquet(`` token. If any are found, that call site must
    switch to a per-call connection.
    """
    repo_root = Path(__file__).parent.parent
    src = (repo_root / file).read_text(encoding="utf-8")

    failures: list[str] = []
    for m in re.finditer(r"duckdb\.sql\(", src):
        # Skip matches inside a comment line - these are docstring /
        # inline-comment references explaining WHY we don't use the
        # module-level helper, not actual calls.
        line_start = src.rfind("\n", 0, m.start()) + 1
        line_content = src[line_start:m.start()].lstrip()
        if line_content.startswith("#"):
            continue
        # Also skip when the match appears inside backticks (RST/MD
        # inline code in a docstring) - adjacent backtick is the
        # canonical signal.
        if (
            m.start() >= 1 and src[m.start() - 1] == "`"
        ) or (
            m.start() >= 2 and src[m.start() - 2:m.start()] == "``"
        ):
            continue

        # Skim ±400 chars: is this near a read_parquet (IMMUTABLE-ish
        # context) or near a ``curator_``/``IMMUTABLE`` token?
        window = src[max(0, m.start() - 400): m.end() + 400]
        if (
            "read_parquet" in window
            or "curator_" in window
            or "IMMUTABLE" in window
        ):
            line_no = src[:m.start()].count("\n") + 1
            failures.append(
                f"{file}:{line_no}: module-level duckdb.sql(...) for an "
                f"IMMUTABLE-tree read - switch to per-call "
                f"duckdb.connect(database=\":memory:\") + con.close()"
            )

    assert not failures, (
        "Module-level duckdb.sql() found near IMMUTABLE reads - these "
        "are NOT thread-safe under Flask's concurrent request model:\n"
        + "\n".join(failures)
        + "\n\nUse the per-call connection pattern (see "
        "/api/dignity/today for the template)."
    )


def test_search_endpoint_uses_per_call_connection(isolated_immutable, client):
    """Concurrency-safety regression: fire several search requests
    rapid-fire to exercise the per-call connection pattern. With the
    module-level ``duckdb.sql()`` global, repeated calls under load
    eventually produce ``InvalidInputException``. With per-call
    connections, every call succeeds independently.

    This isn't a strict reproducer of the production race (Flask test
    client serializes requests) - it's a smoke test that the per-call
    connection pattern works correctly across many invocations and
    doesn't leak connections.
    """
    _write_candidate_parquet(
        isolated_immutable,
        "lone.parquet",
        [{
            "_epoch": 100,
            "discovered_at_epoch": 100,
            "source": "youtube_rss",
            "video_external_id": "vid",
            "video_url": "http://x",
            "title": "rare earth metals primer",
            "channel_id": "c",
            "channel_name": "C",
            "channel_url": "",
            "published_iso": "",
            "description": "",
            "duration_seconds": 100,
            "raw_blob": "{}",
        }],
        columns=_CANDIDATE_PRE_SLICE_4_COLUMNS,
    )

    for i in range(8):
        resp = client.get("/api/search?q=rare%20earth")
        assert resp.status_code == 200, (
            f"iteration {i}: search endpoint returned 500 "
            f"({resp.get_json()}) - per-call connection pattern may "
            f"be leaking or shared"
        )
        assert len(resp.get_json()["items"]) == 1
