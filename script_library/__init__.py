"""
Script Library
──────────────
Curated, premade ingestion scripts that are ready to deploy.
Each script is a JSON file in script_library/scripts/ with metadata
and tested Python3 code compatible with the ingestion sandbox.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


def list_scripts() -> list[dict]:
    """Return metadata for all library scripts (no code)."""
    scripts = []
    if not os.path.isdir(SCRIPTS_DIR):
        return scripts

    for fname in sorted(os.listdir(SCRIPTS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(SCRIPTS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scripts.append({
                "id": fname.removesuffix(".json"),
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "category": data.get("category", "General"),
                "api_url": data.get("api_url", ""),
                "requires_credentials": data.get("requires_credentials", []),
                "credential_kinds": data.get("credential_kinds", {}),
                "suggested_cron": data.get("suggested_cron", "0 */6 * * *"),
                "suggested_subdirectory": data.get("suggested_subdirectory", ""),
                "suggested_overwrite": data.get("suggested_overwrite", False),
                # Optional: per-script wall-clock cap hint (added 2026-04-23).
                # When present, the deploy flow populates the task's
                # ``timeout_seconds`` column from this; otherwise the
                # engine uses the global ``default_script_timeout_seconds``.
                # Slow workloads (e.g. options_unusual_activity_pro with
                # 10 tickers × Yahoo pacing + Black-Scholes greeks) set 300.
                "suggested_timeout_seconds": data.get("suggested_timeout_seconds"),
                "trust_level": data.get("trust_level", "sandboxed"),
                # Support tier (W13, 2026-07-12): "core" = maintained by
                # the project, CI-mocked, stable documented API;
                # "example" = author-provided reference on an unofficial
                # or fragile endpoint - use at your own risk. Default is
                # "example" (fail-safe: an unclassified script must
                # never silently claim maintenance).
                "support_tier": data.get("support_tier", "example"),
                "tags": data.get("tags", []),
            })
        except Exception as exc:
            logger.warning("Failed to load library script %s: %s", fname, exc)
    return scripts


def get_script(script_id: str) -> dict | None:
    """Return full script details including code."""
    path = os.path.join(SCRIPTS_DIR, f"{script_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["id"] = script_id
        return data
    except Exception as exc:
        logger.warning("Failed to load library script %s: %s", script_id, exc)
        return None
