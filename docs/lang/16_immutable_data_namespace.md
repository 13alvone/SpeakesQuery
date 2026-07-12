# IMMUTABLE Data Namespace

A protected sub-tree under `indexes/IMMUTABLE/` for data that must survive forever - explicitly excluded from BOTH the main indexes cleanup and the logs cleanup. Introduced 2026-04-26 with Wave 2 of the Options Edge Brief.

## Why it exists

The standard `indexes/` and `indexes/logs/` trees are size-bounded - when they exceed their budgets, the engine deletes the oldest files by mtime. For ephemeral data (raw API pulls, scheduled-search outputs that get re-derived) this is correct: the data is replaced by every fresh run.

For some data, deletion-by-mtime is catastrophic:

- The pick journal (`ag_picks`) is the historical record of every recommendation the brief ever made. Losing it breaks long-term performance attribution.
- The closure journal (`ag_picks_closures`) is the deterministic ledger of every realized win/loss. Losing it loses the hit-rate metric.
- Future trading-record streams (executed-trade reports, broker-confirmed fills, monthly account snapshots) need the same protection.

The IMMUTABLE namespace gives those data streams a permanent home.

## Layout

```
indexes/IMMUTABLE/<subdir>/*.parquet
```

Wave 2 shipped the first three subdirectories:

| Subdirectory | Schema (in `log_writer.SCHEMAS`) | Writer |
|------|-----|-----|
| `ag_picks/` | `ag_picks` | `log_ag_pick(...)` (alert-group dispatcher) |
| `ag_picks_closures/` | `ag_picks_closures` | `log_ag_pick_closure(...)` (`oeb_pick_tracker_pro` script) |
| `ag_picks_review_observations/` | `ag_picks_review_observations` | `log_ag_review_observation(...)` (alert-group dispatcher, weekly review only) |

Ingestion-script outputs can also live here without a `log_writer`
schema - future ingestion scripts can claim a new subdir at any time by
writing to `indexes/IMMUTABLE/<their_name>/`.

## How the protection works

Two cleanup functions enforce size budgets:

1. **`scheduled_input_engine.cleanup.cleanup_indexes`** runs over `indexes/`, deleting old files when subdir or total budgets exceed.
2. **`scheduled_input_engine.cleanup.cleanup_logs`** runs over `indexes/logs/`, with its own independent budget.

The engine wires `cleanup_indexes` with `skip_subdirs=["logs", "IMMUTABLE"]` so neither gets touched by the main pass. The logs cleanup never reaches inside `IMMUTABLE/` because the IMMUTABLE root is parallel to `logs/`, not nested.

The `LogWriter` class routes the three IMMUTABLE-bound categories through a separate writer instance rooted at `settings.immutable_dir()` so emit-on-the-hot-path lands in the protected tree without any caller doing path math.

## Settings

| Key | Default | Purpose |
|-----|---------|---------|
| `immutable_root` | `"indexes/IMMUTABLE"` | Root path relative to project root. Override only if you need to point at network-attached storage. |

Never set `immutable_root` to a path equal to or under `indexes_root` _unless_ the relative first segment is unique (the cleanup skip mechanism uses the first path segment). The default `"indexes/IMMUTABLE"` is unique under `indexes/` and works correctly.

## Programmatic access

```python
from global_settings import get_settings
settings = get_settings()

# Resolved paths
print(settings.immutable_dir())           # /path/to/project/indexes/IMMUTABLE
print(settings.immutable_subdir("foo"))   # /path/to/project/indexes/IMMUTABLE/foo

# Reject traversal
settings.immutable_subdir("foo/bar")      # ValueError
settings.immutable_subdir("../escape")    # ValueError
settings.immutable_subdir(".hidden")      # ValueError
```

## Adding a new IMMUTABLE-bound log category

Three coordinated edits:

1. **`functionality/log_writer.py`** - add the schema to `SCHEMAS` (additive only - never remove a column once shipped). Add the category name to `IMMUTABLE_CATEGORIES`.
2. **Add a `log_<category>(...)` helper function** that calls `emit("<category>", row)` with the documented kwargs.
3. **Update `tests/test_oeb_wave2.py::test_immutable_categories_match_intended_set`** to include the new category, plus any schema-additivity guards you want.

Once those three are in, calling the helper from anywhere routes the row to `indexes/IMMUTABLE/<category>/*.parquet` automatically.

## SPQL queries

Standard glob path:

```spl
index="indexes/IMMUTABLE/ag_picks/*.parquet"
| where alert_group="options_edge_brief"
| sort -_epoch
| head 50
```

The performance dashboard cookbook in [05_cookbook.md](05_cookbook.md#options-edge-brief--performance-attribution-dashboard-wave-2) has 10 templated queries for the OEB-specific streams.

## Schema additivity rule (decade-horizon)

Every column added to an IMMUTABLE-bound schema must remain forever. The design horizon is a decade of compounding - that's 10 years of historical SPQL queries that would break if a column disappears. The log writer accommodates additions gracefully (missing columns land as NULL when reading older parquets), but column REMOVAL is never safe without a one-time data migration.

Tests in `tests/test_oeb_wave2.py::test_immutable_schema_is_additive_only` capture frozen column snapshots and fail loud if any column is removed.

## Backup recommendation

`indexes/IMMUTABLE/` is the single most valuable directory tree in the project for any user actively trading from the brief's picks. It must be in your backup rotation. The [13_backup_recovery.md](13_backup_recovery.md) doc covers the canonical backup approach via `tools/persistence.py`.
