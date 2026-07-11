# Logging Index

SpeakesQuery records significant events to `indexes/logs/` as Parquet files
that you can query directly with SPQL. The tree has its own size budget
that does **not** count against the main `indexes/` budget - so noisy
logging can never evict your actual ingested data.

## What is logged

Every event lands in one of fifteen category subdirectories. Categories
whose subdir lives under `indexes/IMMUTABLE/` are forever-data: outside
the logs budget, never garbage-collected.

| Category | Subdir | What lands here |
|----------|--------|------------------|
| `config` | `indexes/logs/config/` | Settings mutations (`set`, `reset`, `reset_all`) from the UI or API. Secret values such as `smtp_password` are redacted. |
| `search_runs` | `indexes/logs/search_runs/` | Each scheduled saved search execution: status, row count, duration, error message. |
| `alert_groups` | `indexes/logs/alert_groups/` | Every alert group dispatch attempt - success, error, skipped, dry-run - with tokens, cost, searches used, duration. |
| `claude_api` | `indexes/logs/claude_api/` | Metadata for every Claude API call: model, input/output/cache tokens, cost USD, latency, status, attempt number, retried flag, Headroom route (`headroom_path`). (Full request + response payloads go to a separate SQLite DB - see [Claude Analyzer](11_claude_analyzer.md#history-store).) |
| `ingestion` | `indexes/logs/ingestion/` | Scheduled input pipeline runs: task id, duration, row count, trust level, error. |
| `patch_suggestions` | `indexes/logs/patch_suggestions/` | One row per Claude-drafted fix suggestion for a failed ingestion script (the patch drafter - engine failure path + `POST /api/patch-drafter/suggest`): status, model, cost, unified diff, explanation. |
| `system` | `indexes/logs/system/` | Startup, shutdown, scheduler state, and operational notices. |
| `ag_picks` | `indexes/IMMUTABLE/ag_picks/` | One row per opportunity surfaced by an alert group dispatch (e.g. each of the Daily Opportunity Brief's 5 picks). Captures `idea_id`, instrument type/id, direction, conviction, entry/take-profit/stop prices, suggested buy+sell epochs, hold hours, thesis, and source-signal feeders. **Wave 3 (2026-04-25)** added `source` (`"claude"` for live-dispatch picks, `"manual"` for operator pastes via the Upload Brief modal) and `model_used` (model id string) for cross-LLM performance comparison. Populated by the dispatcher from the mandatory fenced JSON tail in Claude's response, OR by `POST /api/alert-groups/<name>/manual-return` when the operator pastes an external-LLM brief. Used both for historical backtesting and for the next dispatch's "reserved picks" dedup feeder. See [Alert Groups - Pick Capture](12_alert_groups.md#pick-capture-backtesting). |
| `ag_picks_closures` | `indexes/IMMUTABLE/ag_picks_closures/` | One row per pick closure graded deterministically by `oeb_pick_tracker_pro` (marker/examiner separation): outcome, trigger rule, entry/exit prices, P&L, days held, closure quality, account-fit flags. |
| `ag_picks_review_observations` | `indexes/IMMUTABLE/ag_picks_review_observations/` | Aggregated observations from the OEB performance-review AG: review period, hit rates (overall vs account-fit), best/worst signal class, rule-tweak recommendations, calibration status. |
| `curator_telemetry` | `indexes/IMMUTABLE/curator_telemetry/` | Speaktube player events (plays, watch progress, ratings, searches) pulled by the `curator_telemetry_pull` ingestion - the user's viewing telemetry. |
| `curator_reflections` | `indexes/IMMUTABLE/curator_reflections/` | The user's written reflections (end-of-day or per-video), posted via `POST /api/reflections`. |
| `curator_playlist` | `indexes/IMMUTABLE/curator_playlist/` | One row per item in each composed daily playlist - the historical record of what the curator suggested, with per-item scores and rationale. `GET /api/playlist/today` reconstructs the latest run from these rows. |
| `curator_keyword_prefs` | `indexes/IMMUTABLE/curator_keyword_prefs/` | One row per keyword posted via `POST /api/preferences/keywords`; the active pool boosts title-matching candidates' `interest_score` at compose time. |
| `curator_topic_snapshots` | `indexes/IMMUTABLE/curator_topic_snapshots/` | One row per cluster per topic snapshot (centroid vector, weight, exemplar titles, label) - the topic-evolution timeline of the user's interests. |

## Budget and retention

Two settings govern the logs tree:

- `max_logs_size_gb` (default **5**) - total cap across all log subdirs.
- `max_logs_subdirectory_size_gb` (default **2**) - cap per individual
  category subdir.

Oldest-first eviction runs on the same APScheduler interval as the main
cleanup (`cleanup_interval_hours`, default 6h). Files are sorted by mtime
so whole Parquet chunks evict together.

Disable logging entirely by setting `logs_enabled: false` in
`global_settings.yaml` - the emit calls become no-ops with no I/O.

## Querying logs with SPQL

Treat the log subdirs like any other index:

```spql
# Last 24h of Claude spend, grouped by model
index="indexes/logs/claude_api/*.parquet"
  | where _epoch > relative_time("-24h")
  | stats sum(cost_usd) as cost, sum(input_tokens) as in_tok,
          sum(output_tokens) as out_tok, count as calls
          by model

# Alert group failures this week with error detail
index="indexes/logs/alert_groups/*.parquet"
  | where status="error" AND _epoch > relative_time("-7d")
  | table group_name, error_message, duration_ms, _epoch
  | sort -_epoch

# Settings changes audit (who, when, what)
index="indexes/logs/config/*.parquet"
  | where action="set"
  | table _epoch, subject, old_value, new_value, actor
  | sort -_epoch
  | head 50

# Detect cost spikes - avg per-call cost jumping 3x recently
index="indexes/logs/claude_api/*.parquet"
  | where status="success"
  | bin _epoch span=1h
  | stats avg(cost_usd) as avg_cost by _epoch
  | sort -_epoch
```

## Schema per category

Every row starts with `_epoch` (Unix seconds). Additional columns:

- **config**: `action`, `subject`, `subject_type`, `old_value`, `new_value`, `actor`, `source`
- **search_runs**: `search_name`, `status`, `row_count`, `duration_ms`, `error_message`, `query_hash`, `triggered_by`

  `status` semantics: `success` (rows > 0), `empty` (the query ran
  cleanly but matched zero rows - a valid quiet day, `row_count=0`),
  `error` (the query failed; `error_message` carries the diagnostic).
  Before 2026-07-01 the scheduler collapsed empty results into
  `status="error", error_message="process_query returned None"` - if
  you are aggregating over historical rows that cross that date, treat
  that exact error_message as ambiguous (it was usually just a quiet
  day). Dispatcher-invoked feeder runs carry
  `triggered_by="alert_group:<name>"`; cron runs leave it null.
- **alert_groups**: `group_name`, `status`, `searches_used` (CSV of names), `estimated_tokens`, `actual_tokens`, `cost_usd`, `error_message`, `duration_ms`, `dry_run`
- **claude_api**: `request_id`, `group_name`, `source`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `latency_ms`, `status`, `error_class`, `error_message`, `stop_reason`, `attempt_num`, `retried`, `headroom_path` (`headroom` / `direct` / `direct-fallback` - the Headroom proxy route for the attempt)
- **ingestion**: `task_id`, `title`, `status`, `duration_ms`, `error_message`, `row_count`, `attempt`, `trust_level`
- **patch_suggestions**: `task_id`, `title`, `error_hash`, `status`, `model`, `cost_usd`, `latency_ms`, `patch`, `explanation`, `request_id`, `error_message`, `input_tokens`, `output_tokens`, `drafter_error_class`, `drafter_error_message`
- **system**: `level`, `component`, `event`, `message`
- **ag_picks_closures**: `event_timestamp`, `alert_group`, `idea_id`, `instrument_type`, `instrument_id`, `outcome`, `trigger_rule`, `entry_price`, `exit_price`, `exit_epoch`, `pnl_per_contract_usd`, `pnl_pct_vs_max_loss`, `days_held`, `leg_prices_at_close_json`, `closure_quality`, `account_size_floor_usd`, `fits_account_at_entry`, `current_account_size_usd_at_close`, `fits_account_at_close`
- **ag_picks_review_observations**: `event_timestamp`, `alert_group`, `run_request_id`, `review_period_start`, `review_period_end`, `review_period_days`, `n_picks_overall`, `n_picks_account_fit`, `hit_rate_overall`, `hit_rate_account_fit`, `best_signal_class`, `worst_signal_class`, `observation_text`, `observation_evidence`, `observation_actionable`, `rule_tweak_recommendation_text`, `rule_tweak_rationale`, `rule_tweak_expected_impact`, `row_kind`, `calibration_status`, `calibration_n_closures`
- **curator_telemetry**: `event_ts_iso`, `event_date`, `event_type`, `video_external_id`, `chosen_by`, `run_date`, `position`, `slot_kind`, `watched_seconds`, `total_seconds`, `rating`, `reason`, `kind`, `content`, `query`, `raw_json`
- **curator_reflections**: `event_ts_iso`, `date`, `kind`, `content`, `video_external_id`, `source`
- **curator_keyword_prefs**: `event_ts_iso`, `keyword`, `source`, `raw_request`
- **curator_playlist**: `run_date`, `composed_at_iso`, `growth_dial`, `theme`, `position`, `slot_kind`, `rationale`, `external_id`, `url`, `title`, `channel_name`, `thumbnail_url`, `published_at`, `duration_seconds`, `interest_score`, `growth_score`, `slop_score`, `score_reasoning`, `thin_history_active`
- **curator_topic_snapshots**: `snapshot_epoch`, `snapshot_id`, `model_name`, `dim`, `n_clusters`, `n_history_rows`, `decay_lambda_days`, `cluster_id`, `centroid_json`, `weight`, `n_members`, `exemplar_titles_json`, `label`

(The `ag_picks` column set is large and options-aware - see the frozen
snapshot in `functionality/log_writer.py::SCHEMAS` and
[Alert Groups - Pick Capture](12_alert_groups.md#pick-capture-backtesting).)

Unknown columns in emitted rows are dropped; missing columns land as
`null`. The writer is append-only - there is no `UPDATE` path.

## Writing custom logs from Python code

If you're extending SpeakesQuery with a new event source:

```python
from functionality.log_writer import (
    log_config_change, log_alert_group_event, log_claude_api_call,
    log_search_run, log_ingestion_run, log_system_event,
)

log_system_event(component="my_feature", event="bootstrap",
                 message="initialized with 3 defaults")
```

Rows are buffered in-memory and flushed every `logs_flush_interval_seconds`
(default 30s) or sooner if the per-category buffer hits 500 rows.
`flush_all()` forces an immediate flush - typically only needed in
shutdown hooks and tests.

For a brand-new log category, add an entry to `SCHEMAS` in
`functionality/log_writer.py` - the writer validates columns against
that dict.

## Cost alerting recipe

Pair the logs index with a scheduled saved search. Example: alert when
daily Claude spend exceeds $1.

```spql
# Daily Claude cost - returns 1 row per day, most recent first
index="indexes/logs/claude_api/*.parquet"
  | where status="success"
  | eval day=strftime(_epoch, "%Y-%m-%d")
  | stats sum(cost_usd) as daily_cost by day
  | where daily_cost > 1.0
  | sort -day
```

Schedule it hourly and attach an email alert - you'll catch runaway
spend long before your billing portal does.
