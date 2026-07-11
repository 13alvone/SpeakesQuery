"""
Regression test - every default_saved_searches/*.yaml index path must map
to a library-script ``suggested_subdirectory``.

Caught 2026-04-25 on egib_oil_price_regime: the saved-search query read
``index="indexes/commodities/fred_commodity_prices/*.parquet"`` but the
matching script (``script_library/scripts/fred_commodity_prices.json``)
writes to ``commodities/fred_prices``. Result: the saved-search returned
zero rows even when the script ran successfully - silent failure mode,
indistinguishable from "no data yet" in the UI. The diagnose tool
flagged it as ``[MISSING]`` rather than the more common ``[deploy]``.

This test walks every YAML under default_saved_searches/, extracts the
``index="indexes/<subdir>/..."`` path from the query, and asserts that
some library script writes parquet to that subdirectory.

Library scripts that don't write to indexes (e.g., system scripts whose
output lives elsewhere) shouldn't be referenced by an alert-group feeder
saved-search - if a saved-search points at an index path no script can
produce, the feeder is broken by construction.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

DEFAULT_SAVED_SEARCHES = PROJECT_ROOT / "default_saved_searches"
SCRIPTS_DIR = PROJECT_ROOT / "script_library" / "scripts"

INDEX_PATH_RE = re.compile(
    r'index\s*=\s*"indexes/([^"*]+?)/\*\.parquet"', re.IGNORECASE
)

# Paths written by the system (dispatcher, log_writer) rather than by
# library scripts. Saved-searches that read these are legitimate even
# without a matching script ``suggested_subdirectory``.
#
# - ``logs/`` - the standard SPQL-queryable log tree
# - ``IMMUTABLE/`` - Wave 2 of OEB (2026-04-27): the protected trading
#   record. ag_picks and ag_picks_closures here are written by the
#   alert-group dispatcher and the oeb_pick_tracker_pro script (which
#   IS an ingestion script, but writes via log_ag_pick_closure, not via
#   the GENERATE_RESULTS / suggested_subdirectory contract).
SYSTEM_MANAGED_PATH_PREFIXES = ("logs/", "IMMUTABLE/")


def _collect_script_subdirectories() -> set[str]:
    out: set[str] = set()
    for p in SCRIPTS_DIR.glob("*.json"):
        spec = json.loads(p.read_text())
        sub = spec.get("suggested_subdirectory")
        if sub:
            out.add(sub.strip("/"))
    return out


def _collect_saved_search_index_paths() -> list[tuple[str, str]]:
    """Return [(saved_search_name, index_subdir)] for every default
    saved-search whose query starts with an ``index="..."`` clause."""
    out: list[tuple[str, str]] = []
    for p in sorted(DEFAULT_SAVED_SEARCHES.glob("*.yaml")):
        spec = yaml.safe_load(p.read_text()) or {}
        query = (spec.get("query") or "").strip()
        for match in INDEX_PATH_RE.finditer(query):
            out.append((p.stem, match.group(1).strip("/")))
    return out


SCRIPT_SUBDIRS = _collect_script_subdirectories()
SAVED_SEARCH_PATHS = _collect_saved_search_index_paths()


@pytest.mark.parametrize(
    "saved_search,index_subdir",
    SAVED_SEARCH_PATHS,
    ids=[f"{name}::{sub}" for name, sub in SAVED_SEARCH_PATHS],
)
def test_saved_search_index_path_has_matching_script(
    saved_search: str, index_subdir: str
) -> None:
    """Every saved-search ``index="indexes/<subdir>/*.parquet"`` must
    have a library script whose ``suggested_subdirectory`` equals
    ``<subdir>``. Otherwise the feeder will silently return zero rows
    once deployed, regardless of credentials or scheduler state."""
    if index_subdir in SCRIPT_SUBDIRS:
        return
    if any(index_subdir.startswith(prefix) for prefix in SYSTEM_MANAGED_PATH_PREFIXES):
        return
    nearest = sorted(
        SCRIPT_SUBDIRS,
        key=lambda s: (
            -sum(1 for a, b in zip(s, index_subdir) if a == b),
            len(s),
        ),
    )[:3]
    raise AssertionError(
        f"Saved-search {saved_search!r} reads index path {index_subdir!r}, "
        f"but no library script declares suggested_subdirectory={index_subdir!r}. "
        f"Either rename the saved-search index path to match an existing "
        f"script's suggested_subdirectory, or update the script's "
        f"suggested_subdirectory to match.\n"
        f"Nearest known subdirectories: {nearest}"
    )
